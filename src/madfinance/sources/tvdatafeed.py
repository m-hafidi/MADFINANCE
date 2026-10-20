"""TradingView *unauthorized* datafeed — full OHLCV history for CSEMA symbols.

TradingView lists every Casablanca Stock Exchange instrument under the
``CSEMA`` exchange. Its chart datafeed (``prodata.tradingview.com``) serves
historical bars over a websocket without login; data is delayed ~15 minutes
(``delayed_streaming_900``), which matches the exchange's own public feed.

Constraints discovered empirically:

* one chart session may create **only one series** when unauthorized
  (a second ``create_series`` aborts the session with
  ``exceed limit of series in the session``) — so multi-symbol fetches open
  one short-lived connection per symbol, in a small thread pool;
* bars arrive as ``timescale_update`` frames keyed by the *series* id, with
  ``v = [timestamp, open, high, low, close, volume]``.

This module speaks the raw framing protocol directly (``~m~<len>~m~<json>``)
so there is no dependency on abandoned third-party wrappers.
"""

from __future__ import annotations

import json
import random
import re
import string
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import pandas as pd
from websocket import (
    WebSocketConnectionClosedException,
    WebSocketTimeoutException,
    create_connection,
)

from .._exceptions import SourceError
from ..cache import Cache

WS_URL = "wss://prodata.tradingview.com/socket.io/websocket"
ORIGIN = "https://www.tradingview.com"
EXCHANGE = "CSEMA"
DEFAULT_COUNT = 500
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 25.0
MAX_WORKERS = 4

#: madfinance interval -> TradingView resolution
RESOLUTIONS = {
    "1m": "1", "5m": "5", "15m": "15", "30m": "30",
    "60m": "60", "90m": "90", "120m": "120", "1h": "60",
    "1d": "1D", "1D": "1D",
    "1wk": "1W", "1W": "1W", "1w": "1W",
    "1mo": "1M", "1M": "1M",
}

#: intervals whose bars are cached on disk (daily and coarser only)
_CACHED_INTERVALS = {"1d", "1D", "1wk", "1W", "1w", "1mo", "1M"}
_TTL_BARS = 3600.0

_HEADER_RE = re.compile(r"~m~(\d+)~m~")
_cache = Cache("tradingview")


def tv_symbol(ticker: str) -> str:
    """``ATW`` -> ``CSEMA:ATW`` (already-prefixed symbols pass through)."""
    ticker = ticker.strip().upper()
    return ticker if ":" in ticker else f"{EXCHANGE}:{ticker}"


def fetch_bars(
    symbols: List[str],
    interval: str = "1d",
    count: int = DEFAULT_COUNT,
    timeout: float = READ_TIMEOUT,
) -> Dict[str, pd.DataFrame]:
    """Fetch interval bars for several symbols (one websocket session each).

    Returns ``{ticker: DataFrame}`` with columns Open/High/Low/Close/Volume and
    a ``DatetimeIndex`` of session dates (ascending). Symbols that fail
    individually are silently absent — the caller decides on fallbacks; if
    *no* symbol can be fetched, raises :class:`SourceError`.
    """
    symbols = list(dict.fromkeys(s.strip().upper() for s in symbols))
    if not symbols:
        return {}
    resolution = RESOLUTIONS.get(interval)
    if resolution is None:
        raise ValueError(f"Unsupported interval {interval!r}; "
                         f"valid: {', '.join(sorted(set(RESOLUTIONS)))}")

    use_cache = interval in _CACHED_INTERVALS
    results: Dict[str, pd.DataFrame] = {}
    errors: Dict[str, str] = {}
    pending: List[str] = []

    for sym in symbols:
        if use_cache:
            cached = _cache.get_df(f"bars:{sym}:{resolution}:{count}", _TTL_BARS)
            if cached is not None and not cached.empty:
                results[sym] = cached
                continue
        pending.append(sym)

    if pending:
        def _one(sym: str) -> pd.DataFrame:
            last: Optional[Exception] = None
            for attempt in range(2):  # one transparent reconnect
                try:
                    return _session_fetch(sym, resolution, count, timeout)
                except SourceError as exc:
                    last = exc
                    if attempt == 0:
                        time.sleep(0.5)
            raise SourceError(str(last))

        workers = min(MAX_WORKERS, len(pending))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_one, sym): sym for sym in pending}
            for future in as_completed(futures):
                sym = futures[future]
                try:
                    df = future.result()
                except SourceError as exc:
                    errors[sym] = str(exc)
                    continue
                if not df.empty:
                    results[sym] = df
                    if use_cache:
                        _cache.set_df(f"bars:{sym}:{resolution}:{count}", df)

    if not results and errors:
        detail = "; ".join(f"{k}: {v[:120]}" for k, v in list(errors.items())[:3])
        raise SourceError(f"TradingView returned no data: {detail}")
    return results


def _session_fetch(
    symbol: str, resolution: str, count: int, timeout: float,
) -> pd.DataFrame:
    try:
        ws = create_connection(WS_URL, timeout=CONNECT_TIMEOUT, origin=ORIGIN)
    except Exception as exc:
        raise SourceError(f"websocket connect failed: {exc}") from exc

    session_id = "cs_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    series_id = "sd_0"
    bars: List[list] = []
    state = {"completed": False, "error": None}

    def send_raw(frame: str) -> None:
        ws.send(frame)

    def send(name: str, args: list) -> None:
        payload = json.dumps({"m": name, "p": args}, separators=(",", ":"))
        send_raw(f"~m~{len(payload)}~m~{payload}")

    try:
        send("set_auth_token", ["unauthorized_user_token"])
        send("chart_create_session", [session_id, ""])
        symbol_spec = json.dumps(
            {"symbol": tv_symbol(symbol), "adjustment": "splits", "session": "regular"},
            separators=(",", ":"),
        )
        send("resolve_symbol", [session_id, "sds_0", "=" + symbol_spec])
        send("create_series", [session_id, series_id, "s0", "sds_0", resolution, count, ""])

        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline and not state["completed"] and not state["error"]:
            try:
                chunk = ws.recv()
            except (WebSocketTimeoutException, TimeoutError,
                    WebSocketConnectionClosedException, OSError):
                break
            if not chunk:
                continue
            buf = _drain(buf + chunk, {series_id: bars}, state, send_raw)
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if state["error"]:
        raise SourceError(f"{symbol}: {state['error']}")
    if not bars:
        raise SourceError(f"{symbol}: no bars received (timeout or empty series)")
    return _bars_frame(bars)


def _drain(buf: str, series_bars: Dict[str, list], state: dict, send_raw) -> str:
    """Consume complete frames from ``buf``; return the unconsumed remainder."""
    while True:
        header = _HEADER_RE.search(buf)
        if header is None:
            return buf
        if header.start() != 0:
            buf = buf[header.start():]  # discard leading garbage (heartbeats etc.)
            continue
        length = int(header.group(1))
        start = header.end()
        if len(buf) < start + length:
            return buf  # incomplete frame: wait for more bytes
        payload = buf[start:start + length]
        buf = buf[start + length:]
        if payload == "h":
            send_raw("~m~1~m~h")  # liveness ping: echo it back
            continue
        _handle_payload(payload, series_bars, state)


def _handle_payload(payload: str, series_bars: Dict[str, list], state: dict) -> None:
    try:
        msg = json.loads(payload)
    except ValueError:
        return
    name = msg.get("m")
    args = msg.get("p") or []
    if name == "timescale_update" and len(args) >= 2 and isinstance(args[1], dict):
        for sid, block in args[1].items():
            if sid not in series_bars or not isinstance(block, dict):
                continue
            for row in block.get("s") or []:
                # {"i": bar_index, "v": [timestamp, open, high, low, close(, volume)]}
                # Indices are published without a volume field — pad with NaN.
                if isinstance(row, dict) and isinstance(row.get("v"), (list, tuple)) and len(row["v"]) >= 5:
                    values = list(row["v"])[:6]
                    series_bars[sid].append(values + [float("nan")] * (6 - len(values)))
                elif isinstance(row, (list, tuple)) and len(row) >= 6:
                    values = list(row[1:])[:6]
                    series_bars[sid].append(values + [float("nan")] * (6 - len(values)))
    elif name == "series_completed":
        state["completed"] = True
    elif name in ("series_error", "symbol_error"):
        state["error"] = " | ".join(str(a) for a in args[1:])[:200] or name
    elif name == "critical_error":
        state["error"] = " | ".join(str(a) for a in args[1:])[:200] or "critical_error"


def _bars_frame(rows: List[list]) -> pd.DataFrame:
    """Build a sorted OHLCV frame from ``[timestamp, o, h, l, c, v]`` rows."""
    df = pd.DataFrame(rows, columns=["_ts", "Open", "High", "Low", "Close", "Volume"])
    df = df.drop_duplicates(subset="_ts").sort_values("_ts")
    index = pd.to_datetime(df.pop("_ts"), unit="s").dt.normalize()
    df = df.set_index(index)
    df.index.name = "Date"
    df["Volume"] = df["Volume"].fillna(0).astype("int64")
    for col in ("Open", "High", "Low", "Close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

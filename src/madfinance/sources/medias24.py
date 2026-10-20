"""Medias24 (``medias24.com/content/api``) — daily history and index series.

Free Moroccan market API used by the ``casased`` library. Sits behind a
Cloudflare JS-challenge that requires a browser TLS fingerprint (see
``madfinance._http``). Provides:

* ``getPriceHistory`` — daily close/high/low/volume for one ISIN
* ``getMasiHistory`` / ``getIndexHistory`` — MASI and MSI20 daily series
* ``getStockIntraday`` / ``getMarketIntraday`` / ``getIndexIntraday``

Limitation: no daily *open* price — callers should prefer the TradingView
source for candlestick-grade OHLCV and fall back to this one.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import pandas as pd

from .. import _http
from .._exceptions import SourceError, SymbolNotFound
from ..cache import Cache
from ..utils import parse_fr_number

API = "https://medias24.com/content/api"

_TTL_HISTORY = 6 * 3600.0     # daily bars freeze after the session
_TTL_INTRADAY = 120.0

_cache = Cache("medias24")


def price_history(
    isin: str,
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
) -> pd.DataFrame:
    """Daily bars for one ISIN: columns Close/High/Low/Volume (no Open).

    Dates are ``dd/mm/yyyy`` in the payload; the returned index is sorted
    ascending and covers ``[start, end]`` (inclusive).
    """
    start = start or dt.date(2000, 1, 1)
    end = end or dt.date.today()
    key = f"price:{isin}:{start}:{end}"
    cached = _cache.get_df(key, _TTL_HISTORY)
    if cached is not None:
        return cached

    payload = _http.get_json(API, params={
        "method": "getPriceHistory", "ISIN": isin, "format": "json",
        "from": start.isoformat(), "to": end.isoformat(),
    }, retries=2)
    rows = payload.get("result")
    if not isinstance(rows, list) or not rows:
        raise SourceError(f"Medias24 returned no history for {isin}")

    records = []
    for row in rows:
        date = _parse_date(row.get("date"))
        if date is None:
            continue
        records.append({
            "Close": parse_fr_number(row.get("value")),
            "High": parse_fr_number(row.get("max")),
            "Low": parse_fr_number(row.get("min")),
            "Volume": parse_fr_number(row.get("volume")),
        } | {"_d": date})
    df = pd.DataFrame.from_records(records)
    if df.empty:
        raise SourceError(f"Unparseable Medias24 history for {isin}")
    df = df.sort_values("_d").set_index("_d")
    df.index.name = "Date"
    df = df.loc[(df.index >= start) & (df.index <= end)]
    _cache.set_df(key, df)
    return df


def index_history(kind: str = "masi", period: str = "10y") -> pd.DataFrame:
    """Daily series of a Moroccan index: ``kind`` is ``masi`` or ``msi20``.

    Returns a single-column ``Close`` DataFrame indexed by date.
    """
    kind = kind.lower()
    if kind not in {"masi", "msi20"}:
        raise ValueError("kind must be 'masi' or 'msi20'")
    key = f"index:{kind}:{period}"
    cached = _cache.get_df(key, _TTL_HISTORY)
    if cached is not None:
        return cached

    if kind == "masi":
        params = {"method": "getMasiHistory", "periode": period, "format": "json"}
    else:
        params = {"method": "getIndexHistory", "ISIN": "msi20", "periode": period, "format": "json"}
    payload = _http.get_json(API, params=params, retries=2)
    result = payload.get("result") or {}
    labels, prices = result.get("labels"), result.get("prices")
    if not labels or not prices:
        raise SourceError(f"Medias24 returned no {kind.upper()} series")

    dates = pd.to_datetime([int(x) for x in labels], unit="s").normalize()
    values = [parse_fr_number(v) for v in prices]
    df = pd.DataFrame({"Close": values}, index=pd.DatetimeIndex(dates, name="Date"))
    df = df[~df.index.duplicated(keep="last")].sort_index()
    _cache.set_df(key, df)
    return df


def stock_intraday(isin: str) -> pd.DataFrame:
    """Today's intraday ticks for one ISIN (time-of-day index, ``Close``)."""
    key = f"intraday:{isin}"
    cached = _cache.get_json(key, _TTL_INTRADAY)
    if cached is not None:
        df = pd.DataFrame(cached)
        df.index = pd.Index(df.pop("_t"), name="Time")
        return df
    payload = _http.get_json(API, params={
        "method": "getStockIntraday", "ISIN": isin, "format": "json",
    }, retries=2)
    rows = payload.get("result")
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        raise SourceError(f"No intraday data for {isin}")
    row = rows[0]
    labels = row.get("labels") or []
    values = row.get("data") or row.get("values") or []
    df = pd.DataFrame({"Close": [parse_fr_number(v) for v in values]},
                      index=pd.Index(labels, name="Time"))
    _cache.set_json(key, [{"_t": str(t), "Close": v}
                          for t, v in zip(df.index, df["Close"])])
    return df


def _parse_date(text: Optional[str]) -> Optional[dt.date]:
    if not text:
        return None
    day, month, year = str(text).split("/")
    return dt.date(int(year), int(month), int(day))

"""Package-level helpers: ``download``, ``tickers``, market summary, screener.

``download`` batches many symbols through a **single** TradingView websocket
session (one connection instead of N) and backfills gaps per symbol — the
fast path for screeners, notebooks and portfolio trackers.
"""

from __future__ import annotations

import datetime as dt
import warnings
from typing import Dict, Iterable, List, Optional, Sequence, Union

import pandas as pd

from ._exceptions import MadFinanceError, SourceError, SymbolNotFound
from .cache import Cache
from .sources import bourse, medias24, tradingview, tvdatafeed
from .ticker import _finalize, _resample, INDEX_SYMBOLS
from .utils import period_to_range

_OHLCV = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


def tickers() -> List[str]:
    """All tickers currently listed on the Casablanca Stock Exchange."""
    return [u["symbol"] for u in bourse.get_universe()]


def market_summary() -> pd.DataFrame:
    """Market-wide quote table (price, change %, volume) — official source.

    Columns: ``symbol, name, price, change_pct, volume_value, currency``.
    """
    universe = bourse.get_universe()
    df = pd.DataFrame(universe)
    return df.set_index("symbol") if not df.empty else df


def screener(
    by: str = "market_cap",
    ascending: bool = False,
    limit: Optional[int] = None,
    columns: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    """Fundamentals screener over all Moroccan stocks (TradingView source).

    ``by`` accepts any screener field, e.g. ``market_cap``, ``pe_ttm``,
    ``revenue``, ``net_income``, ``eps_ttm``, ``free_cash_flow``…
    Default columns: ticker, name, last_price, market_cap, pe_ttm, eps_ttm,
    sector, revenue, net_income.
    """
    rows = tradingview.scan_market()
    df = pd.DataFrame(rows).set_index("ticker")
    default = ["long_name", "last_price", "market_cap", "pe_ttm", "eps_ttm",
               "sector_en", "industry_en", "revenue", "net_income",
               "free_cash_flow", "change_pct"]
    wanted = [c for c in (columns or default) if c in df.columns]
    df = df[wanted]
    if by not in df.columns:
        raise ValueError(f"Unknown screener column {by!r}. "
                         f"Available: {', '.join(sorted(df.columns))}")
    df = df.sort_values(by, ascending=ascending, na_position="last")
    return df.head(limit) if limit else df


def download(
    tickers: Union[str, Sequence[str]],
    period: Optional[str] = "1y",
    interval: str = "1d",
    start: Optional[Union[str, dt.date]] = None,
    end: Optional[Union[str, dt.date]] = None,
    raise_errors: bool = False,
) -> pd.DataFrame:
    """Download history for one or many tickers (yfinance-compatible shape).

    Multi-ticker results have ``MultiIndex`` columns ``(ticker, field)``;
    a single ticker returns flat OHLCV columns. Unknown tickers are skipped
    with a warning unless ``raise_errors=True``.

    >>> madfinance.download(["ATW", "IAM", "MASI"], period="6mo").head()
    """
    if isinstance(tickers, str):
        query_list: List[str] = [tickers]
    else:
        query_list = [str(t) for t in tickers]

    symbols: List[str] = []
    for query in query_list:
        try:
            symbols.append(query.strip().upper()
                            if query.strip().upper() in INDEX_SYMBOLS
                            else bourse.resolve_symbol(query))
        except SymbolNotFound as exc:
            if raise_errors:
                raise
            warnings.warn(f"madfinance: skipping {query!r} ({exc})", stacklevel=2)
    symbols = list(dict.fromkeys(symbols))  # dedupe, keep order
    if not symbols:
        return _empty_download()

    start_d, end_d = period_to_range(
        period=period if (start is None and end is None) else None,
        start=start, end=end,
    )

    frames = _fetch_many(symbols, interval, start_d, end_d, raise_errors)
    if not frames:
        return _empty_download()

    if len(symbols) == 1:
        return _finalize(frames[symbols[0]])

    combined = pd.concat(frames, axis=1, keys=list(frames.keys()))
    combined.columns.names = ["Ticker", "Field"]
    # stable field order inside each ticker group
    ordered: List[tuple] = []
    for sym in frames:
        ordered.extend((sym, f) for f in _OHLCV if (sym, f) in combined.columns)
    combined = combined[ordered]
    return combined.sort_index()


def clear_cache() -> int:
    """Delete every cached response; returns the number of files removed."""
    total = 0
    for subdir in ("bourse", "medias24", "tradingview"):
        total += Cache(subdir).clear()
    return total


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

def _fetch_many(
    symbols: List[str], interval: str, start_d: dt.date, end_d: dt.date,
    raise_errors: bool,
) -> Dict[str, pd.DataFrame]:
    """Fetch OHLCV for many symbols: one websocket, then per-symbol fallbacks."""
    frames: Dict[str, pd.DataFrame] = {}

    intraday = interval in ("1m", "5m", "15m", "30m", "60m")
    if intraday:
        for sym in symbols:
            try:
                df = bourse.get_intraday_bars(sym)
                minutes = int(interval.replace("m", ""))
                if minutes > 5 and not df.empty:
                    df = df.resample(f"{minutes}min").agg(
                        {"Open": "first", "High": "max", "Low": "min",
                         "Close": "last", "Volume": "sum"},
                    ).dropna(subset=["Close"])
                frames[sym] = _finalize(df)
            except MadFinanceError as exc:
                _warn_or_raise(sym, exc, raise_errors)
        return {s: _slice(df, start_d, end_d) for s, df in frames.items()}

    days = max((end_d - start_d).days, 1)
    count = min(5000, max(60, int(days * 0.75) + 20))
    try:
        fetched = tvdatafeed.fetch_bars(symbols, interval=interval, count=count)
        frames.update({s: _finalize(_slice(df, start_d, end_d))
                       for s, df in fetched.items()})
    except SourceError as exc:
        if raise_errors:
            raise
        warnings.warn(f"madfinance: batch TradingView fetch failed ({exc}); "
                      "trying per-symbol fallbacks", stacklevel=3)

    for sym in symbols:
        if sym in frames and not frames[sym].empty:
            continue
        try:
            frames[sym] = _fallback_daily(sym, interval, start_d, end_d)
        except MadFinanceError as exc:
            if raise_errors:
                raise
            _warn_or_raise(sym, exc, raise_errors=False)
    return {s: df for s, df in frames.items() if not df.empty}


def _fallback_daily(symbol: str, interval: str,
                    start_d: dt.date, end_d: dt.date) -> pd.DataFrame:
    """Medias24 daily bars (or official intraday aggregate as last resort)."""
    if symbol in INDEX_SYMBOLS:
        raw = medias24.index_history("masi" if symbol == "MASI" else "msi20")
        raw = raw.loc[(raw.index >= pd.Timestamp(start_d)) &
                      (raw.index <= pd.Timestamp(end_d))]
        df = pd.DataFrame(index=raw.index,
                          data={"Open": float("nan"), "High": float("nan"),
                                "Low": float("nan"), "Close": raw["Close"],
                                "Volume": float("nan")})
        warnings.warn(f"madfinance: Medias24 index fallback for {symbol} "
                      "provides Close only", stacklevel=3)
    else:
        isin = (bourse.get_fiche(symbol).get("instrument") or {}).get("isin")
        if not isin:
            raise SourceError(f"No ISIN for {symbol}")
        raw = medias24.price_history(isin, start_d, end_d)
        df = raw.reindex(columns=["Close", "High", "Low", "Volume"])
        df["Open"] = float("nan")
        df = df[["Open", "High", "Low", "Close", "Volume"]]
        warnings.warn(f"madfinance: Medias24 fallback for {symbol} has no Open "
                      "prices (Open column is NaN)", stacklevel=3)
    if interval.lower() in ("1wk", "1w", "1mo"):
        df = _resample(df, interval)
    return _finalize(df)


def _slice(df: pd.DataFrame, start_d: dt.date, end_d: dt.date) -> pd.DataFrame:
    if df.empty:
        return df
    mask = ((df.index >= pd.Timestamp(start_d)) &
            (df.index < pd.Timestamp(end_d) + pd.Timedelta(days=1)))
    return df.loc[mask]


def _warn_or_raise(symbol: str, exc: Exception, raise_errors: bool) -> None:
    if raise_errors:
        raise exc
    warnings.warn(f"madfinance: no data for {symbol} ({exc})", stacklevel=3)


def _empty_download() -> pd.DataFrame:
    df = pd.DataFrame(columns=_OHLCV)
    df.index = pd.DatetimeIndex([], name="Date")
    return df

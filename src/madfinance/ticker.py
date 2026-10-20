"""``Ticker`` — the yfinance-style entry point for one Moroccan instrument.

>>> import madfinance as mf
>>> t = mf.Ticker("ATW")                # company names work too: "Attijariwafa"
>>> t.history(period="1y").tail(2)
>>> t.info["market_cap"]
>>> t.financials                          # Revenue / Operating Income / Net Income
>>> t.dividends                           # full official dividend history

Fallback discipline: every network path has a second source and degrades with
a warning instead of an exception (unless ``raise_errors=True``).
``MASI``/``MSI20`` resolve to index mode automatically.
"""

from __future__ import annotations

import datetime as dt
import warnings
from typing import Dict, List, Optional, Union

import pandas as pd

from ._exceptions import MadFinanceError, SourceError
from .sources import bourse, medias24, tradingview, tvdatafeed
from .sources.bourse import _kv_key
from .utils import parse_fr_bool, parse_fr_number, period_to_range

#: symbols handled as indices rather than equities
INDEX_SYMBOLS = {"MASI", "MSI20"}

#: normalized official key-figure label (see ``bourse._kv_key``) -> English
_FIGURE_LABELS = {
    "chiffre_d_affaires": "Revenue",
    "resultat_d_exploitation": "Operating Income",
    "resultat_net": "Net Income",
    "capital_social": "Share Capital",
    "capitaux_propres": "Shareholders' Equity",
    "nombre_de_titres": "Shares Outstanding",
    "comptes_consolides": "Consolidated Accounts",
    "p_e_r": "P/E Ratio",
    "p_b_r": "P/B Ratio",
    "rendement_yield": "Dividend Yield %",
    "r_o_e": "ROE %",
    "pay_out": "Payout %",
}
_RATIOS = ("P/E Ratio", "P/B Ratio", "Dividend Yield %", "ROE %", "Payout %")
_INCOME_ROWS = ("Revenue", "Operating Income", "Net Income",
                "Share Capital", "Shareholders' Equity", "Shares Outstanding")
_EQUITY_ROWS = ("Shareholders' Equity", "Share Capital", "Shares Outstanding")


class Ticker:
    """Market data for a single listed company (or MASI/MSI20)."""

    def __init__(self, symbol: str) -> None:
        self._query = str(symbol)
        self._symbol: Optional[str] = None
        self._info: Optional[dict] = None

    # -- identity -----------------------------------------------------------
    @property
    def is_index(self) -> bool:
        return self.symbol in INDEX_SYMBOLS

    @property
    def symbol(self) -> str:
        """Canonical exchange ticker (resolved lazily on first use)."""
        if self._symbol is None:
            query = self._query.strip().upper()
            self._symbol = query if query in INDEX_SYMBOLS else bourse.resolve_symbol(self._query)
        return self._symbol

    def __repr__(self) -> str:
        return f"<madfinance.Ticker {self._query!r}>"

    # -- prices -------------------------------------------------------------
    def history(
        self,
        period: Optional[str] = "1y",
        interval: str = "1d",
        start: Optional[Union[str, dt.date]] = None,
        end: Optional[Union[str, dt.date]] = None,
        raise_errors: bool = False,
    ) -> pd.DataFrame:
        """Historical bars as a DataFrame (Open/High/Low/Close/Adj Close/Volume).

        Parameters mirror yfinance: ``period`` (``1d..10y``, ``ytd``, ``max``)
        or explicit ``start``/``end``; ``interval`` is one of
        ``5m, 15m, 30m, 60m, 1d, 1wk, 1mo``.

        Daily/weekly/monthly data comes from TradingView (full OHLCV, ~15-min
        delayed) with Medias24 as fallback (no ``Open`` — a warning says so).
        Sub-daily intervals use the official exchange API and only cover the
        most recent sessions.
        """
        symbol = self.symbol
        start_d, end_d = period_to_range(
            period=period if (start is None and end is None) else None,
            start=start, end=end,
        )
        try:
            if interval in ("1m", "5m", "15m", "30m", "60m"):
                df = self._intraday_history(symbol, interval)
            elif self.is_index:
                df = self._index_history(interval, start_d, end_d)
            else:
                df = self._daily_history(symbol, interval, start_d, end_d)
        except MadFinanceError as exc:
            if raise_errors:
                raise
            warnings.warn(f"madfinance: no history for {symbol} ({exc})", stacklevel=2)
            return _empty_history()
        if df.empty:
            return df
        mask = (df.index >= pd.Timestamp(start_d)) & (df.index <= pd.Timestamp(end_d) + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1))
        return df.loc[mask]

    def _daily_history(self, symbol: str, interval: str,
                       start_d: dt.date, end_d: dt.date) -> pd.DataFrame:
        days = max((end_d - start_d).days, 1)
        count = min(5000, max(60, int(days * 0.75) + 20))
        df = pd.DataFrame()
        try:
            frames = tvdatafeed.fetch_bars([symbol], interval=interval, count=count)
            df = frames.get(symbol, pd.DataFrame())
        except (SourceError, ValueError) as exc:
            warnings.warn(f"madfinance: TradingView history failed for {symbol} ({exc}); "
                          "falling back to Medias24", stacklevel=3)
        if not df.empty:
            return _finalize(df)

        # Fallback: Medias24 daily bars (close/high/low/volume, no open).
        isin = self._isin(symbol)
        raw = medias24.price_history(isin, start_d, end_d)
        df = raw.reindex(columns=["Close", "High", "Low", "Volume"])
        df["Open"] = float("nan")
        df = df[["Open", "High", "Low", "Close", "Volume"]]
        warnings.warn(f"madfinance: Medias24 fallback for {symbol} has no Open prices "
                      "(Open column is NaN)", stacklevel=3)
        if interval in ("1wk", "1W", "1w", "1mo", "1M"):
            df = _resample(df, interval)
        return _finalize(df)

    def _index_history(self, interval: str,
                       start_d: dt.date, end_d: dt.date) -> pd.DataFrame:
        days = max((end_d - start_d).days, 1)
        count = min(5000, max(60, int(days * 0.75) + 20))
        try:
            frames = tvdatafeed.fetch_bars([self.symbol], interval=interval, count=count)
            df = frames.get(self.symbol, pd.DataFrame())
            if not df.empty:
                return _finalize(df)
        except (SourceError, ValueError) as exc:
            warnings.warn(f"madfinance: TradingView index history failed ({exc}); "
                          "falling back to Medias24", stacklevel=3)
        raw = medias24.index_history("masi" if self.symbol == "MASI" else "msi20")
        raw = raw.loc[(raw.index >= pd.Timestamp(start_d)) &
                      (raw.index <= pd.Timestamp(end_d))]
        # Only the close is published for indices on the fallback source.
        df = pd.DataFrame(index=raw.index,
                          data={"Open": float("nan"), "High": float("nan"),
                                "Low": float("nan"), "Close": raw["Close"],
                                "Volume": float("nan")})
        warnings.warn(f"madfinance: Medias24 index fallback for {self.symbol} "
                      "provides Close only (OHLC are NaN)", stacklevel=3)
        if interval in ("1wk", "1W", "1w", "1mo", "1M"):
            df = _resample(df, interval)
        return _finalize(df)

    def _intraday_history(self, symbol: str, interval: str) -> pd.DataFrame:
        if self.is_index:
            raise SourceError("Intraday index bars are not available; use interval='1d'")
        df = bourse.get_intraday_bars(symbol)  # official ~5-min bars, recent sessions
        minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}[interval]
        if minutes > 5 and not df.empty:
            df = df.resample(f"{minutes}min").agg(
                {"Open": "first", "High": "max", "Low": "min",
                 "Close": "last", "Volume": "sum"},
            ).dropna(subset=["Close"])
        return _finalize(df)

    def get_intraday(self) -> pd.DataFrame:
        """Raw official intraday bars (≈5 min) for the most recent sessions."""
        if self.is_index:
            raise SourceError("Intraday data is only available for equities")
        return _finalize(bourse.get_intraday_bars(self.symbol))

    # -- profile ------------------------------------------------------------
    @property
    def info(self) -> dict:
        """Merged profile/valuation dict (official site + TradingView).

        Raises only if every source fails; otherwise returns the best
        partial result and warns about what is missing.
        """
        if self._info is not None:
            return self._info
        symbol = self.symbol
        if self.is_index:
            self._info = {
                "symbol": symbol,
                "name": ("MASI — Moroccan All Shares Index" if symbol == "MASI"
                         else "MSI20 — Moroccan Leading 20 Index"),
                "currency": "MAD", "exchange": "Casablanca Stock Exchange",
            }
            return self._info

        out: Dict[str, object] = {"symbol": symbol, "currency": "MAD",
                                  "exchange": "Casablanca Stock Exchange"}
        errors: List[str] = []

        # TradingView fundamentals first, then official values take precedence.
        try:
            out.update(tradingview.symbol_fundamentals(symbol))
        except MadFinanceError as exc:
            errors.append(f"tradingview: {exc}")

        try:
            action = bourse.get_action(symbol)
            official_price = action.get("dernierCours")
            out.update({
                "long_name": action.get("emetteur", {}).get("fr") or out.get("long_name"),
                "sector": (action.get("secteur") or {}).get("fr") or out.get("sector"),
                "market_compartment": action.get("compartiment"),
                "status": action.get("statut"),
                "last_price": official_price or out.get("last_price"),
                "open": action.get("ouverture"),
                "day_high": action.get("plusHaut"),
                "day_low": action.get("plusBas"),
                "reference_price": action.get("reference"),
                "change_pct": action.get("variation"),
                "volume_value": action.get("volume"),
                "volume_qty": action.get("quantite"),
                "trades": action.get("nbTransactions"),
                "market_cap": action.get("capitalisation") or out.get("market_cap"),
                "shares_outstanding": action.get("nombreTitres") or out.get("shares_outstanding"),
                "bid": (action.get("achat") or {}).get("prix"),
                "ask": (action.get("vente") or {}).get("prix"),
            })
        except MadFinanceError as exc:
            errors.append(f"official quote: {exc}")

        try:
            fiche = bourse.get_fiche(symbol)
            instrument = fiche.get("instrument") or {}
            company = fiche.get("company") or {}
            contact = fiche.get("contact") or {}
            out.update({
                "isin": instrument.get("isin"),
                "market": instrument.get("marche"),
                "sector_official": instrument.get("secteur"),
                "par_value": parse_fr_number(instrument.get("valeur_nominale")),
                "headquarters": company.get("siege"),
                "auditor": company.get("auditeur"),
                "incorporated": company.get("constitution"),
                "listed": company.get("introduction"),
                "website": contact.get("site_web"),
                "email": contact.get("courriel"),
                "phone": contact.get("telephone"),
                "investor_contact": contact.get("responsable"),
            })
            if out.get("shares_outstanding") is None:
                out["shares_outstanding"] = parse_fr_number(instrument.get("nombre_de_titres"))
            latest = _latest_figure_values(fiche.get("key_figures") or {})
            for key, label in (("pe_ratio_official", "p_e_r"),
                               ("pb_ratio_official", "p_b_r"),
                               ("dividend_yield_pct", "rendement_yield"),
                               ("roe_pct", "r_o_e"),
                               ("payout_pct", "pay_out")):
                if label in latest:
                    out[key] = parse_fr_number(latest[label])
        except MadFinanceError as exc:
            errors.append(f"official fiche: {exc}")

        if len(out) <= 3:  # only the static defaults survived
            raise SourceError(f"All sources failed for {symbol}: " + "; ".join(errors))
        if errors:
            warnings.warn(f"madfinance: partial info for {symbol} ({'; '.join(errors)})",
                          stacklevel=2)
        self._info = out
        return out

    # -- fundamentals -------------------------------------------------------
    @property
    def key_figures(self) -> pd.DataFrame:
        """Official multi-year key figures: rows = French labels, columns = years.

        Source: casablanca-bourse.com fact sheet (three latest fiscal years).
        """
        if self.is_index:
            raise SourceError("key_figures is only available for equities")
        figures = bourse.get_fiche(self.symbol).get("key_figures") or {}
        rows = figures.get("rows") or {}          # label -> [value_year0, year1, ...]
        years = figures.get("years") or []
        if not rows or not years:
            raise SourceError(f"No key figures published for {self.symbol}")
        data = {}
        for pos, year in enumerate(years):
            data[year] = {label: _cell_value(vals[pos] if pos < len(vals) else None)
                          for label, vals in rows.items()}
        df = pd.DataFrame.from_dict(data, orient="index")
        df.index.name = "Year"
        return df.sort_index(ascending=False)

    @property
    def ratios(self) -> pd.DataFrame:
        """Valuation/return ratios only (P/E, P/B, yield, ROE, payout)."""
        df = self._figures_by_indicator()
        keep = [r for r in _RATIOS if r in df.index]
        if not keep:
            raise SourceError(f"No ratios published for {self.symbol}")
        return df.loc[keep]

    @property
    def financials(self) -> pd.DataFrame:
        """Income statement (annual, official): Revenue/Operating/Net Income…

        Columns are the three latest fiscal years published by the exchange
        (newest first). Rows additionally carry share capital, equity and
        share count for cross-checking.
        """
        df = self._figures_by_indicator()
        keep = [r for r in _INCOME_ROWS if r in df.index]
        if not keep:
            raise SourceError(f"No income statement figures for {self.symbol}")
        return df.loc[keep]

    @property
    def quarterly_financials(self) -> pd.DataFrame:
        """Latest fiscal-quarter figures from TradingView (may be partial)."""
        if self.is_index:
            raise SourceError("quarterly_financials is only available for equities")
        data = tradingview.symbol_fundamentals(
            self.symbol,
            fields=["revenue_fq", "net_income_fq", "total_assets_fq",
                    "earnings_per_share_basic_fq"],
        )
        rows = {"Revenue": data.get("revenue_fq"),
                "Net Income": data.get("net_income_fq"),
                "Total Assets": data.get("total_assets_fq"),
                "EPS": data.get("earnings_per_share_basic_fq")}
        rows = {k: v for k, v in rows.items() if v is not None}
        if not rows:
            raise SourceError(f"No quarterly figures on TradingView for {self.symbol}")
        return _items_frame(rows, "Latest Quarter")

    @property
    def balance_sheet(self) -> pd.DataFrame:
        """Balance sheet: official equity/share-capital history (3 years)
        plus TradingView's current assets/debt figures."""
        if self.is_index:
            raise SourceError("balance_sheet is only available for equities")
        frames: List[pd.DataFrame] = []
        try:
            figs = self._figures_by_indicator()
            keep = [r for r in _EQUITY_ROWS if r in figs.index]
            if keep:
                frames.append(figs.loc[keep])
        except MadFinanceError:
            pass
        try:
            data = tradingview.symbol_fundamentals(
                self.symbol,
                fields=["total_assets", "total_debt", "net_debt",
                        "current_ratio", "debt_to_equity_fq"])
            rows = {k: data.get(k) for k in
                    ("total_assets", "total_debt", "net_debt",
                     "current_ratio", "debt_to_equity_fq")}
            rows = {k: v for k, v in rows.items() if v is not None}
            if rows:
                frames.append(_items_frame(rows, "Latest"))
        except MadFinanceError:
            pass
        if not frames:
            raise SourceError(f"No balance-sheet data available for {self.symbol}")
        merged = pd.concat(frames, axis=0)
        merged.index.name = "Item"
        return merged

    @property
    def cashflow(self) -> pd.DataFrame:
        """Cash-flow figures (TradingView free cash flow; AMMC adds detail)."""
        if self.is_index:
            raise SourceError("cashflow is only available for equities")
        data = tradingview.symbol_fundamentals(
            self.symbol, fields=["free_cash_flow", "operating_cash_flow"])
        rows = {k: data.get(k) for k in ("free_cash_flow", "operating_cash_flow")}
        rows = {k: v for k, v in rows.items() if v is not None}
        if not rows:
            raise SourceError(f"No cash-flow data for {self.symbol}")
        return _items_frame(rows, "Latest")

    @property
    def dividends(self) -> pd.Series:
        """Full dividend history (amount per ex-date), official source."""
        if self.is_index:
            raise SourceError("dividends is only available for equities")
        rows = bourse.get_fiche(self.symbol).get("dividends") or []
        records = [(r["ex_date"], r["amount"]) for r in rows
                   if r.get("ex_date") and r.get("amount") is not None]
        if not records:
            return pd.Series(dtype=float, name="Dividends")
        series = pd.Series(dict(records), name="Dividends").sort_index()
        series.index = pd.to_datetime(series.index)
        return series[~series.index.duplicated(keep="last")]

    @property
    def shareholders(self) -> pd.DataFrame:
        """Declared shareholdings (name + percent), official source."""
        if self.is_index:
            raise SourceError("shareholders is only available for equities")
        rows = bourse.get_fiche(self.symbol).get("shareholders") or []
        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame(columns=["name", "part_pct"])
        return df[["name", "part_pct"]]

    # -- helpers ------------------------------------------------------------
    def _figures_by_indicator(self) -> pd.DataFrame:
        """Key figures transposed: rows = English indicators, columns = years."""
        raw = self.key_figures.T
        raw.index = [_kv_key(str(i)) for i in raw.index]
        rename = {k: v for k, v in _FIGURE_LABELS.items() if k in raw.index}
        return raw.rename(index=rename)

    def _isin(self, symbol: str) -> str:
        isin = (bourse.get_fiche(symbol).get("instrument") or {}).get("isin")
        if not isin:
            raise SourceError(f"No ISIN for {symbol} (needed for Medias24 fallback)")
        return isin


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------

def _empty_history() -> pd.DataFrame:
    df = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Adj Close", "Volume"])
    df.index = pd.DatetimeIndex([], name="Date")
    return df


def _finalize(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize columns/order/index; attach ``Adj Close`` (= Close: exchange
    feeds are split-adjusted, dividends are not applied)."""
    out = df.copy()
    if "Adj Close" not in out.columns and "Close" in out.columns:
        out["Adj Close"] = out["Close"]
    ordered = [c for c in ("Open", "High", "Low", "Close", "Adj Close", "Volume")
               if c in out.columns]
    out = out[ordered]
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)
    out.index = out.index.sort_values()
    out.index.name = "Date"
    return out


def _resample(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Weekly (Friday close) / monthly resampling for Medias24 fallback."""
    rule = "W-FRI" if interval.lower() in ("1wk", "1w") else "ME"
    agg: Dict[str, str] = {"Open": "first", "High": "max", "Low": "min",
                           "Close": "last", "Volume": "sum"}
    if "Adj Close" in df.columns:
        agg["Adj Close"] = "last"
    return df.resample(rule).agg(agg).dropna(subset=["Close"])


def _cell_value(raw):
    """Keep bool-ish cells (``Oui``) intact; parse numeric French cells."""
    if isinstance(raw, str) and raw.strip().lower() in ("oui", "non"):
        return parse_fr_bool(raw)
    parsed = parse_fr_number(raw)
    return parsed if parsed is not None else raw


def _latest_figure_values(figures: dict) -> dict:
    """Latest-year slice of the official key-figures matrix, keyed by
    normalized label."""
    rows = figures.get("rows") or {}
    years = figures.get("years") or []
    if not years:
        return {}
    return {_kv_key(label): values[0]
            for label, values in rows.items() if values}


def _items_frame(rows: Dict[str, object], column: str) -> pd.DataFrame:
    """One-item-dict -> DataFrame with indicator rows and a single column."""
    df = pd.DataFrame.from_dict({column: rows}, orient="index").T
    df.index.name = "Item"
    return df

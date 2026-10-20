"""TradingView scanner REST API — market-wide fundamentals for CSEMA symbols.

Two endpoints (both public, no key):

* ``POST scanner.tradingview.com/morocco/scan`` — bulk rows for every
  Moroccan stock (prices, P/E, EPS, market cap, sector/industry, margins,
  revenue/net income, total assets/debt, FCF…). Cached 1 h.
* ``GET scanner.tradingview.com/symbol?symbol=CSEMA:X&fields=…`` — the same
  fields for one symbol.

Known gaps on this endpoint (verified): dividend yield, beta, 52-week range
come back ``null`` — madfinance sources those from the official fiche and
computed price history instead.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from .. import _http
from .._exceptions import SourceError
from ..cache import Cache

SCAN_URL = "https://scanner.tradingview.com/morocco/scan"
SYMBOL_URL = "https://scanner.tradingview.com/symbol"
_TTL_SCAN = 3600.0
_TTL_SYMBOL = 3600.0

_cache = Cache("tradingview")

# column -> madfinance field name
OVERVIEW_COLUMNS: List[str] = [
    "name", "description", "close", "currency",
    "market_cap_basic",
    "price_earnings_ttm", "earnings_per_share_basic_ttm",
    "price_book_fq", "price_sales_ratio",
    "sector", "industry", "type",
    "total_revenue", "net_income", "gross_profit", "ebitda",
    "total_assets", "total_debt", "net_debt", "free_cash_flow",
    "gross_margin", "operating_margin", "net_margin",
    "debt_to_equity_fq", "current_ratio",
    "revenue_fq", "net_income_fq", "average_volume_10d_calc",
]

_FIELDS = {
    "name": "ticker",
    "description": "long_name",
    "close": "last_price",
    "market_cap_basic": "market_cap",
    "price_earnings_ttm": "pe_ttm",
    "earnings_per_share_basic_ttm": "eps_ttm",
    "price_book_fq": "price_book",
    "price_sales_ratio": "price_sales",
    "sector": "sector_en",
    "industry": "industry_en",
    "type": "instrument_type",
    "total_revenue": "revenue",
    "net_income": "net_income",
    "gross_profit": "gross_profit",
    "ebitda": "ebitda",
    "total_assets": "total_assets",
    "total_debt": "total_debt",
    "net_debt": "net_debt",
    "free_cash_flow": "free_cash_flow",
    "gross_margin": "gross_margin_pct",
    "operating_margin": "operating_margin_pct",
    "net_margin": "net_margin_pct",
    "debt_to_equity_fq": "debt_to_equity",
    "current_ratio": "current_ratio",
    "revenue_fq": "revenue_quarter",
    "net_income_fq": "net_income_quarter",
    "average_volume_10d_calc": "avg_volume_10d",
    "currency": "currency",
}


def scan_market() -> List[Dict]:
    """Fundamentals rows for every Moroccan stock (dict per symbol). Cached 1 h."""
    cached = _cache.get_json("scan:market", _TTL_SCAN)
    if cached:
        return cached
    payload = _http.post_json(SCAN_URL, {"columns": OVERVIEW_COLUMNS, "range": [0, 200]})
    rows = payload.get("data") or []
    if not rows:
        raise SourceError("TradingView scanner returned no rows")
    out: List[Dict] = []
    for row in rows:
        values = row.get("d") or []
        item = {}
        for i, col in enumerate(OVERVIEW_COLUMNS):
            if i < len(values):
                item[_FIELDS.get(col, col)] = values[i]
        symbol = str(item.get("ticker") or row.get("s", "")).strip()
        item["ticker"] = symbol
        item["tv_symbol"] = str(row.get("s") or f"CSEMA:{symbol}")
        if symbol:
            out.append(item)
    _cache.set_json("scan:market", out)
    return out


def symbol_fundamentals(ticker: str, fields: Optional[Iterable[str]] = None) -> Dict:
    """Single-symbol fundamentals (defaults to the overview field set)."""
    ticker = ticker.strip().upper()
    key = f"symbol:{ticker}:{','.join(fields) if fields else 'default'}"
    cached = _cache.get_json(key, _TTL_SYMBOL)
    if cached:
        return cached
    columns = list(fields) if fields else OVERVIEW_COLUMNS
    data = _http.get_json(SYMBOL_URL, params={
        "symbol": f"CSEMA:{ticker}",
        "fields": ",".join(columns),
        "no_404": "true",
    })
    if not isinstance(data, dict) or not data:
        raise SourceError(f"No TradingView fundamentals for {ticker}")
    out = {}
    for i, col in enumerate(columns):
        if col in data:
            out[_FIELDS.get(col, col)] = data[col]
    _cache.set_json(key, out)
    return out

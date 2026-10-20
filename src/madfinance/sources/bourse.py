"""Official source: casablanca-bourse.com (Drupal site, French).

Three data shapes are scraped here:

* **Cours page** ``/live-market/actions/cours?symbol=X`` — embeds a
  ``drupal-settings-json`` blob with the full market ticker list
  (``live_market.ticker.items``) and the live quote for ``X``
  (``live_market.action_data``, including the ``ce`` instrument code).
* **Historical API** ``/api/live-market/historical?instrument=X`` — recent
  intraday (≈5-min) bars with OHLCV.
* **Fiche page** ``/live-market/actions/fiche/{symbol}/{ce}`` — server-rendered
  fact sheet: ISIN, sector, HQ, auditor, shareholders, three years of key
  figures/ratios, full dividend history, capital operations, bonds.

Everything is cached; one user never issues more than a handful of requests
per symbol per day.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Optional

import pandas as pd

from .. import _http
from .._exceptions import MadFinanceError, SourceError, SymbolNotFound
from ..cache import Cache
from ..utils import parse_fr_bool, parse_fr_number

BASE = "https://www.casablanca-bourse.com"
COURS_PATH = "/live-market/actions/cours"
HISTORICAL_API = BASE + "/api/live-market/historical"

# TTLs (seconds)
_TTL_QUOTE = 60.0        # live quote during/after session
_TTL_UNIVERSE = 300.0    # market-wide ticker list
_TTL_FICHE = 86_400.0    # official fact sheet (changes daily at most)
_TTL_INTRADAY = 60.0

_cache = Cache("bourse")

_SETTINGS_RE = re.compile(r'drupal-settings-json">([\s\S]*?)</script>')
_TABLE_RE = re.compile(r"<table[^>]*>([\s\S]*?)</table>", re.I)
_ROW_RE = re.compile(r"<tr[^>]*>([\s\S]*?)</tr>", re.I)
_CELL_RE = re.compile(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# page plumbing
# ---------------------------------------------------------------------------

def _page_settings(path: str, params: Optional[dict] = None) -> dict:
    """Fetch a site page and return its parsed drupal-settings-json."""
    query = ("?" + "&".join(f"{k}={v}" for k, v in (params or {}).items())) if params else ""
    text = _http.get_text(BASE + path + query, retries=2)
    match = _SETTINGS_RE.search(text)
    if not match:
        raise SourceError(f"drupal-settings-json not found on {path}")
    try:
        return json.loads(match.group(1))
    except ValueError as exc:
        raise SourceError(f"Unparseable drupal settings on {path}: {exc}") from exc


def get_action(symbol: str) -> dict:
    """Live quote + instrument code (``ce``) for one symbol. Cached 60 s."""
    symbol = normalize_symbol(symbol)
    key = f"action:{symbol}"
    cached = _cache.get_json(key, _TTL_QUOTE)
    if cached:
        return cached
    settings = _page_settings(COURS_PATH, {"symbol": symbol})
    action = (settings.get("live_market") or {}).get("action_data")
    if not action or not action.get("symbol"):
        raise SymbolNotFound(f"No instrument named {symbol!r} on casablanca-bourse.com")
    action = {**action, "symbol": normalize_symbol(str(action.get("symbol", symbol)))}
    _cache.set_json(key, action)
    return action


def get_universe() -> list[dict]:
    """All listed instruments: symbol, labels (fr/en/ar), price, change, volume.

    Source: the embedded ticker list on any cours page (one fetch covers the
    whole market). Cached 5 minutes.
    """
    cached = _cache.get_json("universe", _TTL_UNIVERSE)
    if cached:
        return cached
    # Any valid symbol gives the full market list; ATW always exists.
    settings = _page_settings(COURS_PATH, {"symbol": "ATW"})
    items = (settings.get("live_market") or {}).get("ticker", {}).get("items") or []
    if not items:
        raise SourceError("Ticker list missing from cours page")
    universe = []
    for item in items:
        label = item.get("label") or {}
        universe.append({
            "symbol": normalize_symbol(str(item.get("symbol", ""))),
            "name": (label.get("fr") or label.get("en") or "").strip(),
            "name_en": (label.get("en") or "").strip(),
            "name_ar": (label.get("ar") or "").strip(),
            "price": item.get("price"),
            "change_pct": item.get("change_pct"),
            "volume_value": item.get("volume"),
            "currency": item.get("currency") or "MAD",
        })
    _cache.set_json("universe", universe)
    return universe


def get_session_status() -> str:
    """Market session status: ``open`` or ``closed`` (from the cours page)."""
    cached = _cache.get_json("session", _TTL_QUOTE)
    if cached:
        return cached
    settings = _page_settings(COURS_PATH, {"symbol": "ATW"})
    status = (settings.get("live_market") or {}).get("session", {}).get("status") or "unknown"
    _cache.set_json("session", status)
    return status


# ---------------------------------------------------------------------------
# intraday bars
# ---------------------------------------------------------------------------

def get_intraday_bars(symbol: str) -> pd.DataFrame:
    """Recent intraday (≈5 min) OHLCV bars from the official API. Cached 60 s.

    The endpoint ignores range parameters and returns roughly the last two
    sessions; it is the only free source of ``Open`` outside TradingView.
    """
    symbol = normalize_symbol(symbol)
    key = f"intraday:{symbol}"
    cached = _cache.get_df(key, _TTL_INTRADAY)
    if cached is not None:
        return cached
    data = _http.get_json(HISTORICAL_API, params={"instrument": symbol}, retries=2)
    items = data.get("items") or []
    if not items:
        raise SourceError(f"No intraday data for {symbol}")
    records = []
    for bar in items:
        ts = float(bar.get("timestamp", 0))
        if ts < 10_000_000_000:  # seconds -> ms
            ts *= 1000
        records.append({
            "Open": parse_fr_number(bar.get("ouverture")),
            "High": parse_fr_number(bar.get("plusHaut")),
            "Low": parse_fr_number(bar.get("plusBas")),
            "Close": parse_fr_number(bar.get("dernierCours")),
            "Volume": parse_fr_number(bar.get("volumeEchanges")),
        } | {"_ts": pd.to_datetime(ts, unit="ms")})
    df = pd.DataFrame.from_records(records)
    df = df.set_index("_ts").sort_index()
    # quote snapshots can repeat a timestamp (pre-open/continuous updates):
    # keep the last value for each instant
    df = df[~df.index.duplicated(keep="last")]
    df.index.name = "Datetime"
    _cache.set_df(key, df)
    return df


# ---------------------------------------------------------------------------
# fiche (fact sheet)
# ---------------------------------------------------------------------------

def get_fiche(symbol: str, ce: Optional[str] = None) -> dict:
    """Parse the official fact sheet for a symbol. Cached 24 h.

    Returns a dict with keys: ``instrument``, ``company``, ``shareholders``,
    ``key_figures`` (years + rows), ``contact``, ``capital_operations``,
    ``dividends``, ``bonds``, ``thresholds``.
    """
    symbol = normalize_symbol(symbol)
    key = f"fiche:{symbol}"
    cached = _cache.get_json(key, _TTL_FICHE)
    if cached:
        return cached
    ce = ce or get_action(symbol).get("ce")
    if not ce:
        raise SourceError(f"No instrument code (ce) for {symbol}")
    text = _http.get_text(f"{BASE}/live-market/actions/fiche/{symbol}/{ce}", retries=2)
    fiche = {"symbol": symbol, "ce": ce}
    for table in _TABLE_RE.findall(text):
        rows = _table_rows(table)
        if not rows:
            continue
        parsed = _classify_table(rows)
        if parsed:
            fiche.update(parsed)
    fiche.setdefault("instrument", {})
    if "isin" not in fiche["instrument"]:
        raise SourceError(f"Fiche for {symbol} did not contain an ISIN table")
    _cache.set_json(key, fiche)
    return fiche


def _table_rows(table_html: str) -> list[list[str]]:
    """Cell texts of a table as a list of rows (tags stripped, spaces collapsed)."""
    rows = []
    for row_html in _ROW_RE.findall(table_html):
        cells = [_TAG_RE.sub("", c).replace("\xa0", " ").strip()
                 for c in _CELL_RE.findall(row_html)]
        cells = [c for c in cells if c != ""]
        if cells:
            rows.append(cells)
    return rows


def _classify_table(rows: list[list[str]]) -> Optional[dict]:
    """Map a table to its structured payload by inspecting its header row."""
    header = rows[0]
    head = " ".join(header).lower()
    body = rows[1:]

    # Key-value tables (instrument/company/contact) have no header row —
    # every row is data. Tabular tables lead with a header row.
    if "isin" in head:
        return {"instrument": _kv_row(rows)}
    if head.startswith("société"):
        return {"company": _kv_row(rows)}
    if header[0].lower().startswith("responsable"):
        return {"contact": _kv_row(rows)}

    if body and any("aucun résultat trouvé" in " ".join(row).lower() for row in body):
        body = []

    if header[0].lower().startswith("déclarant") and "part" in head:
        return {"shareholders": [
            {"name": r[0], "part_pct": parse_fr_number(r[1])}
            for r in body if len(r) >= 2
        ]}
    if "indicateur" in header[0].lower():
        years = [int(y) for y in (parse_fr_number(h) for h in header[1:]) if y is not None]
        matrix: dict[str, list] = {}
        for row in body:
            if len(row) >= 2:
                matrix[row[0]] = [row[i] if i < len(row) else None
                                  for i in range(1, len(header))]
        return {"key_figures": {"years": years, "rows": matrix}}
    if "nature de l'opération" in head:
        return {"capital_operations": [
            {"date": _iso_date(r[0]), "nature": r[1] if len(r) > 1 else None,
             "share_change": parse_fr_number(r[2]) if len(r) > 2 else None,
             "par_value": parse_fr_number(r[3]) if len(r) > 3 else None}
            for r in body if len(r) >= 2
        ]}
    if "détachement" in head:
        return {"dividends": [
            {"year": int(year) if (year := parse_fr_number(r[0])) else r[0],
             "amount": parse_fr_number(r[1]) if len(r) > 1 else None,
             "type": r[2] if len(r) > 2 else None,
             "ex_date": _iso_date(r[3]) if len(r) > 3 else None}
            for r in body if len(r) >= 2
        ]}
    if "libellé" in head and "taux" in head:
        return {"bonds": [
            {"label": r[0], "type": r[1] if len(r) > 1 else None,
             "rate": parse_fr_number(r[2]) if len(r) > 2 else None,
             "amount_issued": parse_fr_number(r[3]) if len(r) > 3 else None}
            for r in body if len(r) >= 2
        ]}
    if "seuil franchi" in head:
        return {"thresholds": [
            {"date": _iso_date(r[0]), "declarant": r[1] if len(r) > 1 else None,
             "threshold": r[2] if len(r) > 2 else None, "sens": r[3] if len(r) > 3 else None}
            for r in body if len(r) >= 2
        ]}
    return None


def _kv_row(body: list[list[str]]) -> dict:
    """Flatten ``[key, value]`` rows (or ``[k, v, k, v]``) into a dict."""
    out: dict[str, Any] = {}
    for row in body:
        for i in range(0, len(row) - 1, 2):
            out[_kv_key(row[i])] = row[i + 1]
    return out


def _kv_key(label: str) -> str:
    """Normalize a French table label into a stable snake_case key."""
    key = label.lower()
    key = (key.replace("è", "e").replace("é", "e").replace("ê", "e")
              .replace("à", "a").replace("â", "a").replace("î", "i")
              .replace("ô", "o").replace("û", "u").replace("ç", "c"))
    return re.sub(r"[^a-z0-9]+", "_", key).strip("_")


def _iso_date(text: Optional[str]) -> Optional[str]:
    """``'08/07/2026'`` -> ``'2026-07-08'``; ``None`` when unparseable."""
    if not text:
        return None
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(text).strip())
    if not match:
        return None
    day, month, year = match.groups()
    try:
        return dt.date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# symbol resolution
# ---------------------------------------------------------------------------

def normalize_symbol(symbol: str) -> str:
    """Uppercase + trim; official ticker items sometimes carry padding."""
    return str(symbol).strip().upper()


def resolve_symbol(query: str) -> str:
    """Resolve a ticker, ISIN or company name to an official ticker.

    Matching order: exact ticker → ISIN → case-insensitive company name
    (French or English label from the market list).
    """
    query = str(query).strip()
    if not query:
        raise SymbolNotFound("Empty symbol")
    universe = get_universe()
    symbols = {u["symbol"] for u in universe}
    norm = normalize_symbol(query)
    if norm in symbols:
        return norm
    if query.upper().startswith("MA") and len(query) == 12:
        for u in universe:
            if u["symbol"] and get_fiche(u["symbol"]).get("instrument", {}).get("isin", "").upper() == norm:
                return u["symbol"]
        raise SymbolNotFound(f"ISIN {query} not found")
    needle = _fold(query)
    for u in universe:
        for label in (u["name"], u["name_en"]):
            if label and _fold(label) == needle:
                return u["symbol"]
    for u in universe:
        for label in (u["name"], u["name_en"]):
            if label and (needle in _fold(label) or _fold(label) in needle):
                return u["symbol"]
    # Official labels are often just the abbreviation ("IAM"); fall back to
    # the TradingView long names ("Maroc Telecom SA") for full-name lookups.
    try:
        from . import tradingview

        candidates = []
        for row in tradingview.scan_market():
            name = row.get("long_name") or ""
            if not name:
                continue
            folded = _fold(name)
            if needle == folded:
                return row["ticker"]
            if needle in folded:
                candidates.append(row["ticker"])
        if len(candidates) == 1:
            return candidates[0]
    except MadFinanceError:
        pass
    raise SymbolNotFound(f"No listed company matches {query!r}")


def _fold(text: str) -> str:
    """Case/diacritic-folded comparison key."""
    text = text.lower()
    for src, dst in (("è", "e"), ("é", "e"), ("ê", "e"), ("ë", "e"),
                     ("à", "a"), ("â", "a"), ("î", "i"), ("ï", "i"),
                     ("ô", "o"), ("û", "u"), ("ù", "u"), ("ç", "c")):
        text = text.replace(src, dst)
    return re.sub(r"[^a-z0-9]", "", text)

"""Shared helpers: French-formatted number parsing, period math, cache paths.

Moroccan financial pages format numbers the French way — ``80 490 370 000,00``,
``3,01%`` — with regular or non-breaking spaces as thousand separators. Every
scraped value goes through :func:`parse_fr_number`.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys
from pathlib import Path
from typing import Optional, Union

_THOUSAND_SEPARATORS = "\u00a0\u202f \u2009"  # space, NBSP, narrow NBSP, thin space
_NUMBER_RE = re.compile(r"^[\s\u00a0\u202f]*-?[\d\u00a0\u202f ]*(?:,\d+)?")


def parse_fr_number(value) -> Optional[float]:
    """Parse a French-formatted number into a float.

    Handles thousand separators (spaces/NBSPs), comma decimals and a trailing
    ``%``. Returns ``None`` for empty cells, dashes and non-numeric text
    such as ``"Oui"``.

    >>> parse_fr_number("80 490 370 000,00")
    80490370000.0
    >>> parse_fr_number("3,01%")
    3.01
    >>> parse_fr_number("Aucun") is None
    True
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "").strip()
    if not text or text in {"-", "–", "—", "N/A", "n/a"}:
        return None
    match = _NUMBER_RE.match(text)
    if not match:
        return None
    token = match.group(0)
    for sep in _THOUSAND_SEPARATORS:
        token = token.replace(sep, "")
    token = token.replace(" ", "").replace(",", ".")
    if not token or token == "-":
        return None
    try:
        return float(token)
    except ValueError:
        return None


def parse_fr_bool(value) -> Optional[bool]:
    """Parse ``"Oui"``/``"Non"`` style cells; ``None`` when unclear."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"oui", "yes", "true", "1"}:
        return True
    if text in {"non", "no", "false", "0"}:
        return False
    return None


# Period arithmetic ---------------------------------------------------------

_PERIOD_DAYS = {
    "1d": 1, "5d": 5,
    "1mo": 30, "3mo": 91, "6mo": 182,
    "1y": 365, "2y": 730, "5y": 1826, "10y": 3652,
}
# Earliest date worth requesting for Moroccan market history (web sources
# effectively start in the mid-2000s; 2000 keeps "max" future-proof).
_MAX_START = dt.date(2000, 1, 1)


def period_to_range(
    period: Optional[str] = None,
    start: Optional[Union[str, dt.date]] = None,
    end: Optional[Union[str, dt.date]] = None,
    today: Optional[dt.date] = None,
) -> tuple[dt.date, dt.date]:
    """Resolve yfinance-style ``period``/``start``/``end`` into a date range.

    Precedence: explicit ``start``/``end`` win over ``period``.
    """
    today = today or dt.date.today()
    if start is not None or end is not None:
        start_d = _to_date(start) if start else _MAX_START
        end_d = _to_date(end) if end else today
    else:
        period = (period or "1y").lower()
        if period == "max":
            start_d, end_d = _MAX_START, today
        elif period == "ytd":
            start_d, end_d = dt.date(today.year, 1, 1), today
        elif period in _PERIOD_DAYS:
            start_d, end_d = today - dt.timedelta(days=_PERIOD_DAYS[period]), today
        else:
            raise ValueError(
                f"Unknown period {period!r}. "
                f"Valid: {', '.join(sorted(_PERIOD_DAYS))}, ytd, max"
            )
    if start_d > end_d:
        raise ValueError(f"start {start_d} is after end {end_d}")
    return start_d, end_d


def _to_date(value: Union[str, dt.date]) -> dt.date:
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def days_between(start: dt.date, end: dt.date) -> int:
    """Number of calendar days in [start, end]."""
    return (end - start).days


# Cache location ------------------------------------------------------------

def cache_dir() -> Path:
    """Platform-appropriate cache directory (override with MADFINANCE_CACHE)."""
    override = os.environ.get("MADFINANCE_CACHE")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / "madfinance" / "cache"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Caches" / "madfinance"
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        path = Path(base) / "madfinance"
    path.mkdir(parents=True, exist_ok=True)
    return path

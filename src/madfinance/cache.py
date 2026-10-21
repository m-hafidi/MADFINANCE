"""Tiny file-backed cache (JSON + DataFrames) with per-entry TTLs.

Everything scraped here is public and slow-changing; caching is what keeps
madfinance polite toward the exchange, Medias24 and TradingView.

Layout: ``<cache_dir>/<subdir>/<sha1(key)[:16]>.json`` with a sibling
``.meta`` storing the original key (so entries are debuggable).
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .utils import cache_dir


class Cache:
    """File cache scoped to a subdirectory (e.g. ``bourse``, ``tradingview``)."""

    def __init__(self, subdir: str) -> None:
        self.dir = cache_dir() / subdir
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- keys ---------------------------------------------------------------
    def _paths(self, key: str) -> tuple[Path, Path]:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return self.dir / f"{digest}.json", self.dir / f"{digest}.key"

    def get_json(self, key: str, ttl: Optional[float]) -> Optional[Any]:
        """Return the cached value or ``None`` if missing/expired.

        ``ttl=None`` means "never expires"; ``ttl=0`` disables reads.
        """
        if ttl == 0:
            return None
        path, meta = self._paths(key)
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if ttl is not None and age > ttl:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def set_json(self, key: str, value: Any) -> None:
        path, meta = self._paths(key)
        try:
            path.write_text(
                json.dumps(value, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            meta.write_text(key, encoding="utf-8")
        except (OSError, TypeError, ValueError):
            pass  # a full/read-only cache must never break data fetching

    # -- DataFrames ---------------------------------------------------------
    def get_df(self, key: str, ttl: Optional[float]) -> Optional[pd.DataFrame]:
        """Read a DataFrame written by :meth:`set_df` (ISO dates, UTC-agnostic)."""
        payload = self.get_json(key, ttl)
        if not payload:
            return None
        try:
            # pandas >= 2.1 treats bare strings as file paths — pass a buffer.
            df = pd.read_json(io.StringIO(payload), orient="split", dtype=False)
            df.index = pd.to_datetime(df.index)
            name = self._index_name(key)
            if name is not None:
                df.index.name = name          # split-orient JSON drops it
            return df
        except (ValueError, TypeError, OSError):
            return None

    def set_df(self, key: str, df: pd.DataFrame) -> None:
        # Dates stay naive (Moroccan trading days are plain calendar dates).
        out = df.copy()
        if not isinstance(out.index, pd.DatetimeIndex):
            out.index = pd.Index([str(i) for i in out.index], name=df.index.name)
        elif (out.index.normalize() != out.index).any():
            # intraday bars: keep the time-of-day (truncating to the date
            # would collapse every bar onto midnight on the next read)
            out.index = pd.Index([str(i) for i in out.index], name=df.index.name)
        else:
            out.index = pd.Index([str(i)[:10] for i in out.index],
                                 name=df.index.name)
        self.set_json(key, out.to_json(orient="split", date_format="iso"))
        try:
            _, meta = self._paths(key)
            meta.write_text(
                json.dumps({"key": key, "index_name": df.index.name}),
                encoding="utf-8")
        except OSError:
            pass

    def _index_name(self, key: str) -> Optional[str]:
        _, meta = self._paths(key)
        try:
            return json.loads(meta.read_text(encoding="utf-8")).get("index_name")
        except (OSError, ValueError, AttributeError):
            return None                      # legacy plain-key entry

    def clear(self) -> int:
        """Delete all entries; returns the number of files removed."""
        removed = 0
        for path in self.dir.glob("*"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

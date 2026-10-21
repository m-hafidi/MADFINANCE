"""Offline tests for the file-backed cache (isolated via MADFINANCE_CACHE)."""

import os
import time

import pandas as pd

from madfinance.cache import Cache


def make_cache(tmp_path, monkeypatch, subdir="unit") -> Cache:
    monkeypatch.setenv("MADFINANCE_CACHE", str(tmp_path))
    return Cache(subdir)


def test_json_roundtrip(tmp_path, monkeypatch):
    cache = make_cache(tmp_path, monkeypatch)
    cache.set_json("fiche/ATW", {"symbol": "ATW", "close": 155.2})
    assert cache.get_json("fiche/ATW", ttl=60) == {"symbol": "ATW", "close": 155.2}
    assert cache.get_json("fiche/MISSING", ttl=60) is None


def test_ttl_expiry(tmp_path, monkeypatch):
    cache = make_cache(tmp_path, monkeypatch)
    cache.set_json("k", {"v": 1})
    path, _ = cache._paths("k")
    old = time.time() - 100
    os.utime(path, (old, old))
    assert cache.get_json("k", ttl=50) is None       # expired
    assert cache.get_json("k", ttl=None) is not None  # ttl=None: never expires
    assert cache.get_json("k", ttl=0) is None         # ttl=0: read disabled


def test_corrupt_entry_returns_none(tmp_path, monkeypatch):
    cache = make_cache(tmp_path, monkeypatch)
    cache.set_json("k", {"v": 1})
    path, _ = cache._paths("k")
    path.write_text("{not json", encoding="utf-8")
    assert cache.get_json("k", ttl=60) is None


def test_df_roundtrip(tmp_path, monkeypatch):
    cache = make_cache(tmp_path, monkeypatch)
    df = pd.DataFrame(
        {"Open": [100.0, 101.5], "Close": [100.5, 102.0]},
        index=pd.to_datetime(["2026-01-05", "2026-01-06"]),
    )
    df.index.name = "Date"
    cache.set_df("bars", df)
    out = cache.get_df("bars", ttl=60)
    assert out is not None
    assert list(out.columns) == ["Open", "Close"]
    assert [str(i)[:10] for i in out.index] == ["2026-01-05", "2026-01-06"]
    assert out["Close"].tolist() == [100.5, 102.0]
    assert out.index.name == "Date"


def test_df_roundtrip_intraday_times(tmp_path, monkeypatch):
    # intraday bars must keep their time-of-day: truncating to the date would
    # collapse every bar onto midnight on the next read (all duplicates)
    cache = make_cache(tmp_path, monkeypatch)
    idx = pd.to_datetime(["2026-01-05 09:05:00", "2026-01-05 09:10:00",
                          "2026-01-05 15:30:00"])
    df = pd.DataFrame({"Close": [100.0, 101.0, 102.0]},
                      index=pd.DatetimeIndex(idx, name="Datetime"))
    cache.set_df("intraday:ATW", df)
    out = cache.get_df("intraday:ATW", ttl=60)
    assert out is not None
    assert list(out.index) == list(idx)
    assert not out.index.duplicated().any()
    assert out.index.name == "Datetime"


def test_df_roundtrip_legacy_meta(tmp_path, monkeypatch):
    # entries written before the index-name metadata existed still load
    cache = make_cache(tmp_path, monkeypatch)
    df = pd.DataFrame({"Close": [1.0]},
                      index=pd.to_datetime(["2026-01-05"]))
    cache.set_df("bars", df)
    path, meta = cache._paths("bars")
    meta.write_text("bars", encoding="utf-8")       # legacy plain-key meta
    out = cache.get_df("bars", ttl=60)
    assert out is not None
    assert out.index.name is None


def test_clear_removes_entries(tmp_path, monkeypatch):
    cache = make_cache(tmp_path, monkeypatch)
    cache.set_json("a", 1)
    cache.set_json("b", 2)
    assert cache.clear() >= 2                      # .json + .key per entry
    assert cache.get_json("a", ttl=None) is None
    assert cache.get_json("b", ttl=None) is None

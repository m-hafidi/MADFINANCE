"""Live smoke tests — every core fetch path against real sources.

Run:  pytest --live        (all live tests)
      pytest --live -k history   (a subset)
"""

import warnings

import pandas as pd
import pytest

import madfinance as mf

pytestmark = pytest.mark.live


def test_universe():
    syms = mf.tickers()
    assert len(syms) >= 75
    assert "ATW" in syms and "IAM" in syms


def test_history_ohlcv():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = mf.Ticker("ATW").history(period="6mo")
    assert len(df) > 50
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert df[["Open", "High", "Low", "Close"]].notna().all().all()
    assert df["Volume"].sum() > 0
    # OHLC sanity: high is the max of each bar
    assert (df["High"] >= df[["Open", "Close", "Low"]].max(axis=1) - 1e-9).all()


def test_history_increasing_index():
    df = mf.Ticker("IAM").history(period="3mo")
    assert df.index.is_monotonic_increasing
    assert df.index.max() >= df.index.min()


def test_info():
    info = mf.Ticker("ATW").info
    assert info.get("isin", "").startswith("MA")
    assert info.get("market_cap", 0) > 0
    assert info.get("pe_ttm") is None or info["pe_ttm"] > 0


def test_download_batch():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = mf.download(["ATW", "IAM", "MASI"], period="1mo")
    assert df.shape[1] == 18  # 3 tickers x (Open High Low Close Adj Close Volume)
    close = df.xs("Close", axis=1, level=1)
    assert close.notna().sum().min() > 10
    assert df.index.names == ["Date"] or df.index.name in ("Date", None)


def test_masi_index():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = mf.Ticker("MASI").history(period="6mo")
    assert len(df) > 50
    assert df[["Open", "High", "Low", "Close"]].notna().all().all()
    assert 5000 < df["Close"].mean() < 50000  # MASI range sanity


def test_intraday():
    df = mf.Ticker("ATW").get_intraday()
    assert len(df) > 20
    # The official API streams quote snapshots at actual trade times (not a
    # fixed 5-min grid), so assert the invariants it does promise:
    assert df.index.is_monotonic_increasing
    assert df.index.is_unique
    assert (df["High"] >= df["Low"]).all()
    assert (df.index.max() - df.index.min()) >= pd.Timedelta(hours=1)


def test_financials():
    fin = mf.Ticker("ATW").financials
    assert fin.shape[1] >= 1
    assert fin.loc["Revenue"].notna().any()


def test_dividends_and_shareholders():
    t = mf.Ticker("ATW")
    div = t.dividends
    assert len(div) >= 1
    sh = t.shareholders
    assert len(sh) >= 3


def test_symbol_resolution():
    assert mf.Ticker("maroc telecom").symbol == "IAM"
    assert mf.Ticker("MA0000011488").symbol == "IAM"
    with pytest.raises(mf.SymbolNotFound if hasattr(mf, "SymbolNotFound") else Exception):
        mf.Ticker("definitely not a company xyz").symbol


# -- AMMC regulatory filings (primary statements from PDFs) ------------------

def test_ammc_search_year():
    df = mf.ammc.search(year=2025, symbol="ATW")
    assert not df.empty
    assert set(df.columns) == {"issuer", "year", "type", "url"}
    assert (df["year"] == 2025).all()
    assert df["url"].str.contains(
        "/fr/espace-emetteurs/etats-financiers/").all()


def test_ammc_statements_crosscheck():
    stmts = mf.Ticker("ATW").statements(2025)
    assert {"balance", "income", "cashflow"} <= set(stmts)

    inc = stmts["income"]
    assert inc.columns[0] == 2025          # newest year first
    pnb = inc[inc.index.str.contains("PRODUIT NET BANCAIRE", case=False)]
    assert not pnb.empty
    assert pnb.iloc[0][2025] == pytest.approx(34_921_384)   # = official fiche

    total = stmts["balance"]
    total = total[total.index.str.contains("TOTAL PASSIF")]
    assert not total.empty and total.iloc[0][2025] > 0

    # no giant-number corruption from label/value column mixing
    for frame in stmts.values():
        assert not (frame.abs().max(axis=None) > 1e15)


def test_ammc_scanned_pdf_raises():
    # DARI's 2026 filing is an image-only scan: no text layer, OCR unsupported
    with pytest.raises(mf.SourceError):
        mf.Ticker("DARI").statements(2026)

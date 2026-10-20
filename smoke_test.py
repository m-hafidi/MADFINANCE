"""madfinance smoke test — checks every live fetch path, prints PASS/FAIL."""
import time
import warnings

import madfinance as mf

results = []


def check(name, fn):
    t0 = time.time()
    try:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            out = fn()
        warns = [str(x.message)[:90] for x in w]
        results.append((name, "OK", f"{out} ({time.time() - t0:.1f}s)", warns))
    except Exception as exc:
        results.append((name, "FAIL", f"{type(exc).__name__}: {exc}"[:150], []))


check("tickers() universe", lambda: f"{len(mf.tickers())} symbols")

def _hist():
    df = mf.Ticker("ATW").history(period="6mo")
    assert len(df) > 100 and df[["Open", "High", "Low", "Close"]].notna().all().all()
    assert df["Volume"].sum() > 0
    return f"{df.shape[0]} bars OHLCV, {df.index.min().date()}->{df.index.max().date()}"
check("Ticker.history (TV websocket)", _hist)

def _info():
    info = mf.Ticker("ATW").info
    assert info.get("isin") and info.get("market_cap")
    return f"{len(info)} fields (isin={info.get('isin')}, mktcap={info.get('market_cap'):.0f})"
check("Ticker.info (fiche+scanner)", _info)

def _batch():
    df = mf.download(["ATW", "IAM", "MASI"], period="1mo")
    assert df.shape[1] == 18  # 3 tickers x 6 fields
    assert df.xs("Close", axis=1, level=1).notna().sum().all()
    return f"{df.shape} MultiIndex"
check("download batch (3 symbols)", _batch)

def _index():
    df = mf.Ticker("MASI").history(period="6mo")
    assert df[["Open", "High", "Low", "Close"]].notna().all().all()
    return f"{df.shape[0]} bars OHLC"
check("MASI index history", _index)

def _intraday():
    df = mf.Ticker("ATW").get_intraday()
    assert len(df) > 50
    return f"{df.shape[0]} x 5min bars"
check("official intraday (5min)", _intraday)

def _financials():
    fin = mf.Ticker("ATW").financials
    assert fin.loc["Revenue"].notna().any()
    return f"{fin.shape[0]} lines x {fin.shape[1]} years"
check("financials (TV scanner)", _financials)

def _dividends():
    d = mf.Ticker("ATW").dividends
    return f"{len(d)} payments" if len(d) else "none on record"
check("dividends (fiche)", _dividends)

print("=" * 78)
for name, status, detail, warns in results:
    mark = "PASS" if status == "OK" else "FAIL"
    print(f"[{mark}] {name:<34} {detail}")
    for wmsg in warns:
        print(f"        warning: {wmsg}")
print("=" * 78)
failed = [r for r in results if r[1] != "OK"]
print(f"{len(results) - len(failed)}/{len(results)} fetch paths OK")
raise SystemExit(1 if failed else 0)

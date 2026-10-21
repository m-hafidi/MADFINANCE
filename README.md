# madfinance

**yfinance-style financial data for the Moroccan stock market** — prices,
fundamentals and regulatory filings for every company listed on the
Casablanca Stock Exchange, from free public sources only.

```python
import madfinance as mf

mf.Ticker("ATW").history(period="6mo").tail(3)
```

## Install

```bash
pip install madfinance              # core
pip install "madfinance[pdf]"       # + pdfplumber (AMMC statement parsing)
```

From source:

```bash
git clone https://github.com/m-hafidi/MADFINANCE.git
cd MADFINANCE
pip install -e ".[pdf,test]"
```

## Quickstart

```python
import madfinance as mf

# The universe: every company listed on the exchange (81 tickers);
# the indices live on their own handles: mf.MASI, mf.MSI20
mf.tickers()                        # ['AFM', 'AFI', 'GAZ', ..., 'WAA', 'ZDJ']

# ---- Prices -----------------------------------------------------------------
atw = mf.Ticker("ATW")              # ticker, ISIN or company name all resolve
atw.history(period="1y")            # OHLCV DataFrame (Open/High/Low/Close/
                                    # Adj Close/Volume), yfinance-style
atw.history(start="2025-01-01", end="2025-06-30", interval="1wk")
atw.get_intraday()                  # today's session, minute bars
mf.MASI.history(period="5y")        # index handles: mf.MASI, mf.MSI20

mf.download(["ATW", "IAM", "BCP"], period="6mo")   # MultiIndex columns

# ---- Fundamentals (official exchange fiche) ---------------------------------
atw.info                            # name, ISIN, sector, market cap, close...
atw.financials                      # income statement (SACF, yearly)
atw.quarterly_financials
atw.balance_sheet
atw.cashflow
atw.ratios
atw.key_figures                     # multi-year indicator matrix
atw.dividends                       # per-share dividends + detachment dates
atw.shareholders                    # declared shareholdings (> 5%)

# ---- Primary statements (AMMC filings, parsed from PDF) --------------------
atw.statements(2025)                # {income, balance, cashflow} DataFrames
atw.statements(2025, "income")      # one statement
mf.ammc.search(year=2025, symbol="ATW")        # the filing index itself
mf.ammc.search(year=2025, issuer="wafa", kind="annuel")

# ---- Market -----------------------------------------------------------------
mf.market_summary()                 # indices + most active / top gainers
mf.screener(by="market_cap", limit=10)          # fundamentals screener

mf.clear_cache()                    # empty the on-disk cache
```

### Statements example

```python
>>> stmts = mf.Ticker("ATW").statements(2025)
>>> stmts["income"].head()
                                     2025        2024
Item
Intérêts et produits assimilés  30944812.0  29792085.0
Intérêts et charges assimilés   -9832500.0 -10070761.0
MARGE D'INTERÊT                21112312.0  19721324.0
...
```

Line items keep the French labels of the filing; columns are fiscal years,
newest first.

## API

| Call | Returns | Source |
| --- | --- | --- |
| `mf.tickers()` | all 81 listed company tickers | bourse.ma |
| `mf.Ticker(x)` | `Ticker` (ticker, ISIN or name); `mf.MASI`, `mf.MSI20` are the indices | |
| `.history(period, interval, start, end)` | OHLCV bars | TradingView, Medias24 |
| `.get_intraday()` | today's minute bars | bourse.ma |
| `.info` | company profile dict | bourse.ma |
| `.financials` / `.quarterly_financials` | income statement | bourse.ma |
| `.balance_sheet` / `.cashflow` / `.ratios` | statements / ratios | bourse.ma |
| `.key_figures` | multi-year indicator matrix | bourse.ma |
| `.dividends` | dividend history | bourse.ma |
| `.shareholders` | declared shareholdings | bourse.ma |
| `.statements(year, statement=None)` | primary statements from the annual report PDF | ammc.ma |
| `mf.download(tickers, ...)` | batch OHLCV (`MultiIndex` columns) | TradingView |
| `mf.market_summary()` | indices + movers snapshot | bourse.ma |
| `mf.screener(by, ascending, limit, columns)` | fundamentals table | TradingView |
| `mf.ammc.search(year, symbol, issuer, kind)` | filing index (`issuer, year, type, url`) | ammc.ma |
| `mf.clear_cache()` | number of cache files removed | |

## Sources & delays

* **Casablanca Stock Exchange** ([bourse.ma](https://www.bourse.ma)) — official
  end-of-day prices, company fiches (profile, key figures, dividends,
  shareholders), intraday API and the full listing universe.
* **TradingView** — daily/weekly/monthly OHLCV for all listings and the
  MASI/MSI20 indices via its websocket feed (a few minutes delayed);
  sub-daily intervals come from the official exchange API and only cover the
  most recent sessions. Market hours: Mon–Fri 09:00–15:30 (GMT+1).
* **Medias24** — end-of-day price history by ISIN, used as fallback.
* **AMMC** ([ammc.ma](https://www.ammc.ma)) — the market regulator's filing
  index: annual, consolidated and semester reports (PDF), parsed into the
  primary statements.

Everything is cached on disk (per-OS cache directory; override with the
`MADFINANCE_CACHE` environment variable), so re-fetching is free and the
sources are not hammered. `mf.clear_cache()` empties it.

## Limitations

* **Morocco only** — the ~81 securities listed on the Casablanca Stock
  Exchange plus the two indices.
* **Statement values are as printed** — SACF-format filings (most banks and
  industrials) report thousands of dirhams, IFRS filers such as Maroc Telecom
  report millions. madfinance does not rescale; check the unit note in the
  filing.
* **Scanned PDFs are not parsed** — image-only filings have no text layer and
  OCR is out of scope; such filings raise `SourceError`.
* Fundamentals (fiche tables) are end-of-day snapshots from the official
  exchange site — they are not tick-by-tick.
* Sources occasionally reorganise pages; when that happens madfinance raises
  a typed error (`SourceError`, `SymbolNotFound`) rather than returning
  garbage.

## Testing

```bash
pytest              # offline unit tests (no network)
pytest --live       # + live smoke tests against the real sources
python smoke_test.py
```

## License

MIT

"""madfinance — yfinance-style financial data for Moroccan listed companies.

Sources (all free, public): the Casablanca Stock Exchange website, Medias24
and TradingView. Start with :class:`madfinance.Ticker` or
:func:`madfinance.download`:

>>> import madfinance as mf
>>> mf.tickers()[:5]
>>> mf.Ticker("IAM").history(period="1y").tail(3)
>>> mf.download(["ATW", "IAM", "BCP"], period="6mo")
"""

from ._exceptions import DependencyMissing, MadFinanceError, SourceError, SymbolNotFound
from ._version import __version__
from .market import clear_cache, download, market_summary, screener, tickers
from .ticker import Ticker

#: convenient index handle — ``mf.MASI.history(period="5y")``
MASI = Ticker("MASI")
MSI20 = Ticker("MSI20")

__all__ = [
    "Ticker", "MASI", "MSI20",
    "download", "tickers", "market_summary", "screener", "clear_cache",
    "MadFinanceError", "SourceError", "SymbolNotFound", "DependencyMissing",
    "__version__",
]

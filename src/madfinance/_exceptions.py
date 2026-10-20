"""Public exception hierarchy for madfinance."""


class MadFinanceError(Exception):
    """Base class for all madfinance errors."""


class SourceError(MadFinanceError):
    """A data source failed (network, blocked, unexpected payload).

    The message should identify the source and what was attempted so users
    can tell which fallback (if any) produced the returned data.
    """


class SymbolNotFound(MadFinanceError):
    """The requested ticker/ISIN is not known to any configured source."""


class DependencyMissing(MadFinanceError):
    """An optional dependency required for this feature is not installed."""

    def __init__(self, feature: str, extra: str) -> None:
        super().__init__(
            f"{feature} requires an optional dependency. "
            f"Install it with: pip install 'madfinance[{extra}]'"
        )

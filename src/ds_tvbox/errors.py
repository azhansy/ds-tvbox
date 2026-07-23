"""Stable error types used across collector and publisher trust zones."""


class DsTvboxError(Exception):
    """Base error for expected project failures."""


class ContractError(DsTvboxError):
    """An input or output violates a declared contract."""


class SecurityError(DsTvboxError):
    """An untrusted value violates the network or publication policy."""


class FetchError(DsTvboxError):
    """A bounded upstream request failed."""


class InconclusiveError(DsTvboxError):
    """The run cannot safely decide whether content should change."""


class PublishError(DsTvboxError):
    """The Git publication transaction failed."""

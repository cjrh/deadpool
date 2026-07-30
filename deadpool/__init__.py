"""A resilient process-pool executor and its optional remote interface."""

__version__ = "2026.7.1"

from . import _pool
from ._pool import (
    CancelledError,
    Deadpool,
    Future,
    PoolClosed,
    ProcessError,
    TimeoutError,
    as_completed,
)

__all__ = [
    "Deadpool",
    "Future",
    "CancelledError",
    "TimeoutError",
    "ProcessError",
    "PoolClosed",
    "as_completed",
]


def __getattr__(name: str):
    """Preserve access to implementation helpers exposed by the old module."""
    try:
        return getattr(_pool, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_pool)))

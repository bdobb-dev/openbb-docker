"""ArcticDB access for tick-lab.

This talks to ArcticDB directly rather than through OpenBB: the whole point of
the chapter is that the store is a shared network service usable from any
Python process, with no Platform install on the client.
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from datetime import time as time_type
from typing import Any

import pandas as pd

from tick_lab.config import S3Config


class LibraryNotFoundError(ValueError):
    """Raised by `TickStore.read` when the requested library does not exist.

    A `ValueError` subclass (not a bare `ValueError`) so callers that want to
    react specifically to "nothing has been loaded yet" can catch this type,
    while `pytest.raises(ValueError, ...)` and other broad catches upstream
    keep working unchanged.
    """


class StoreWriteError(RuntimeError):
    """Raised by `TickStore.write` when ArcticDB rejects a write.

    Wraps `arcticdb.exceptions.ArcticException` -- the library's own
    exception hierarchy, confirmed against the pinned arcticdb version (see
    `tick_lab/reference/yfinance_adapter.py` for the same principle applied
    to yfinance). Only ArcticDB's own exceptions are classified as a write
    problem; a bug in our code (AttributeError, TypeError, ...) is not a data
    or storage problem and must propagate unchanged.
    """


def to_bounds(start: Any, end: Any) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """Build an ArcticDB `date_range` pair.

    Whether `end` is widened to the end of its day is decided from the
    *parsed value*, not from how it was spelled: a `datetime.date` (that
    isn't also a `datetime`), or any parsed value whose time-of-day is
    exactly midnight, is treated as a whole-day bound. Anything with a
    non-zero time-of-day is honoured exactly. That makes "2023-05-12",
    "2023-05-12T00:00:00", and "2023-05-12 00:00:00" all mean the same
    thing -- the whole day -- regardless of punctuation.

    Trade-off: asking for exactly midnight as a single instant is not
    expressible through this function. That's deliberate -- whole-day is
    overwhelmingly the intended meaning of a date-shaped bound (e.g. the
    CLI's `--date` argument), so midnight is claimed by the widening rule.
    """
    start_ts = None if start is None else pd.Timestamp(start)

    if end is None:
        return start_ts, None

    end_ts = pd.Timestamp(end)
    is_pure_date = isinstance(end, date_type) and not isinstance(end, datetime)
    is_pure_date = is_pure_date or end_ts.time() == time_type(0, 0)
    if is_pure_date:
        end_ts = end_ts.normalize() + pd.Timedelta(1, unit="D") - pd.Timedelta(1, unit="ns")
    return start_ts, end_ts


class TickStore:
    """A thin, typed wrapper over an ArcticDB connection."""

    def __init__(self, cfg: S3Config):
        from arcticdb import Arctic

        self._cfg = cfg
        self._arctic = Arctic(cfg.uri)

    def _library(self, library: str, create: bool = True):
        return self._arctic.get_library(library, create_if_missing=create)

    def write(
        self,
        library: str,
        symbol: str,
        frame: pd.DataFrame,
        metadata: dict | None = None,
    ) -> None:
        """Overwrite `symbol`, so re-running a load is idempotent."""
        from arcticdb.exceptions import ArcticException

        try:
            self._library(library).write(symbol, frame, metadata=metadata)
        except ArcticException as err:
            raise StoreWriteError(
                f"ArcticDB rejected the write for {symbol!r} to library {library!r}: {err}"
            ) from err

    def read(
        self,
        library: str,
        symbol: str,
        start: Any = None,
        end: Any = None,
    ) -> pd.DataFrame:
        """Read a symbol, filtering by date range on the server where possible."""
        if not self._arctic.has_library(library):
            raise LibraryNotFoundError(
                f"ArcticDB library {library!r} does not exist. Check "
                "ARCTICDB_LIBRARY/--library, or run `tick-lab load` first "
                "if nothing has been written to it yet."
            )
        bounds = to_bounds(start, end)
        date_range = None if bounds == (None, None) else bounds
        return self._library(library, create=False).read(
            symbol, date_range=date_range
        ).data

    def list_symbols(self, library: str) -> list[str]:
        if not self._arctic.has_library(library):
            return []
        return sorted(self._library(library, create=False).list_symbols())

    def has(self, library: str, symbol: str) -> bool:
        return symbol in self.list_symbols(library)

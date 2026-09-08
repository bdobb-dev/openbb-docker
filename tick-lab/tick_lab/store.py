# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Delta Lake access for tick-lab.

This talks to Delta Lake directly (via the `deltalake` package) rather than
through OpenBB: the whole point of the chapter is that the store is a shared
network service usable from any Python process, with no Platform install on
the client.
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
    """Raised by `TickStore.write` when Delta Lake rejects a write.

    Wraps `deltalake.exceptions.DeltaError` -- the library's own exception
    hierarchy, confirmed against the pinned deltalake version (see
    `tick_lab/reference/yfinance_adapter.py` for the same principle applied
    to yfinance). Only DeltaError is classified as a write problem; a bug in
    our code (AttributeError, TypeError, ...) is not a data or storage
    problem and must propagate unchanged.
    """


def to_bounds(start: Any, end: Any) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """Build a Delta Lake date-range filter pair.

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


def _date_filter(schema, start_ts, end_ts):
    """Build a pyarrow dataset filter on the 'date' column, matching its tz."""
    if "date" not in schema.names or (start_ts is None and end_ts is None):
        return None
    # pylint: disable=import-outside-toplevel
    import pyarrow.dataset as ds

    field_type = schema.field("date").type
    tz = getattr(field_type, "tz", None)

    def _bound(ts):
        if tz is None:
            return ts.tz_localize(None) if ts.tzinfo else ts
        return ts.tz_localize(tz) if ts.tzinfo is None else ts.tz_convert(tz)

    expr = None
    if start_ts is not None:
        expr = ds.field("date") >= _bound(start_ts).to_pydatetime()
    if end_ts is not None:
        e = ds.field("date") <= _bound(end_ts).to_pydatetime()
        expr = e if expr is None else expr & e
    return expr


class TickStore:
    """A thin, typed wrapper over Delta tables at s3://<bucket>/<library>/<symbol>."""

    def __init__(self, cfg: S3Config, base: str | None = None):
        self._cfg = cfg
        self._base = (base or cfg.base_uri).rstrip("/")
        self._opts = {} if base else cfg.storage_options

    def _path(self, library: str, symbol: str) -> str:
        return f"{self._base}/{library}/{symbol}"

    def write(
        self,
        library: str,
        symbol: str,
        frame: pd.DataFrame,
        metadata: dict | None = None,
    ) -> None:
        """Overwrite `symbol`, so re-running a load is idempotent."""
        import json

        from deltalake import CommitProperties, write_deltalake
        from deltalake.exceptions import DeltaError

        out = frame.reset_index()
        if "date" not in out.columns:
            out = out.rename(columns={out.columns[0]: "date"})
        props = None
        if metadata:
            props = CommitProperties(
                custom_metadata={"openbb_meta": json.dumps(metadata, default=str)}
            )
        try:
            write_deltalake(
                self._path(library, symbol), out,
                mode="overwrite", schema_mode="overwrite",
                storage_options=self._opts, commit_properties=props,
            )
        except DeltaError as err:
            raise StoreWriteError(
                f"Delta rejected the write for {symbol!r} to library {library!r}: {err}"
            ) from err

    def read(
        self,
        library: str,
        symbol: str,
        start: Any = None,
        end: Any = None,
    ) -> pd.DataFrame:
        from deltalake import DeltaTable

        if not DeltaTable.is_deltatable(
            self._path(library, symbol), storage_options=self._opts
        ):
            if not self.list_symbols(library):
                raise LibraryNotFoundError(
                    f"Delta library {library!r} does not exist. Check "
                    "DELTA_LIBRARY/--library, or run `tick-lab load` first "
                    "if nothing has been written to it yet."
                )
            raise ValueError(f"symbol {symbol!r} not found in library {library!r}")
        dt = DeltaTable(self._path(library, symbol), storage_options=self._opts)
        dataset = dt.to_pyarrow_dataset()
        start_ts, end_ts = to_bounds(start, end)
        filt = _date_filter(dataset.schema, start_ts, end_ts)
        df = dataset.to_table(filter=filt).to_pandas()
        if "date" in df.columns:
            df = df.set_index("date").sort_index()
        return df

    def list_symbols(self, library: str) -> list[str]:
        from pyarrow import fs as pafs

        fsys, root = self._fs_and_root()
        sel = pafs.FileSelector(f"{root}/{library}", allow_not_found=True)
        return sorted(
            info.base_name
            for info in fsys.get_file_info(sel)
            if info.type == pafs.FileType.Directory
            and fsys.get_file_info(f"{info.path}/_delta_log").type
            != pafs.FileType.NotFound
        )

    def has(self, library: str, symbol: str) -> bool:
        return symbol in self.list_symbols(library)

    def _fs_and_root(self):
        from pyarrow import fs as pafs

        if self._base.startswith("s3://"):
            return (
                pafs.S3FileSystem(
                    access_key=self._cfg.access,
                    secret_key=self._cfg.secret,
                    endpoint_override=self._opts.get("aws_endpoint"),
                    region="us-east-1",
                    force_virtual_addressing=False,
                ),
                self._base[len("s3://") :],
            )
        return pafs.LocalFileSystem(), self._base

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Generic read/write API for arbitrary OpenBB / DataFrame data in Delta Lake.

The `provider="deltalake"` path is bound to OpenBB's fixed OHLCV models. This
store handles *any* shape of data — economy series, fundamentals, screeners,
plain DataFrames — and is the generic counterpart to the `.deltalake` accessor.

    from openbb_deltalake import store
    s = store(library="research")          # uri/library default to env/local path
    s.write("gdp", obb.economy.gdp.real(provider="oecd"))   # OBBject, DataFrame, or records
    s.write("notes", my_dataframe)
    df  = s.read("gdp", output="dataframe")
    obj = s.read("gdp")                     # OBBject (default) -> .to_df(), charting, etc.
    s.list_symbols(); s.has("gdp"); s.read_metadata("gdp"); s.delete("gdp")
"""

from typing import Any, Optional, Sequence


class DeltaStore:
    """Generic Delta Lake store for arbitrary tabular data."""

    def __init__(self, uri: Optional[str] = None, library: Optional[str] = None):
        """Resolve the connection (base/library/storage_options) from args, env, or defaults."""
        # pylint: disable=import-outside-toplevel
        from openbb_deltalake.utils import resolve_config

        self.base, self.library, self.storage_options = resolve_config(uri, library, None)

    # -- helpers ------------------------------------------------------------
    def _path(self, key: str) -> str:
        # pylint: disable=import-outside-toplevel
        from openbb_deltalake.utils import table_path

        return table_path(self.base, self.library, key)

    @staticmethod
    def _to_frame(data: Any):
        """Accept an OBBject, DataFrame, or records; return a frame with the
        DatetimeIndex (if any) moved into a 'date' column for Parquet."""
        # pylint: disable=import-outside-toplevel
        from pandas import DataFrame, DatetimeIndex, RangeIndex

        from openbb_deltalake.utils import normalize_index

        if hasattr(data, "to_dataframe"):  # OBBject
            df = data.to_dataframe()
        elif isinstance(data, DataFrame):
            df = data.copy()
        else:  # list[dict] / dict / array-like
            df = DataFrame(data)
        if df is None or df.empty:
            raise ValueError("No data to write to the Delta store.")
        df = normalize_index(df)
        if isinstance(df.index, DatetimeIndex):
            # Rename before reset_index: for an unnamed DatetimeIndex,
            # reset_index() emits a column literally named "index", and a
            # post-hoc rename({"date": "date"}) would be a no-op.
            df.index = df.index.rename(df.index.name or "date")
            df = df.reset_index()
        else:
            df = df.reset_index(drop=isinstance(df.index, RangeIndex))
        return df

    @staticmethod
    def _to_obbject(df, key: Optional[str], metadata: Any, library: str):
        """Wrap a stored frame in a generic OBBject."""
        # pylint: disable=import-outside-toplevel
        from pandas import RangeIndex

        from openbb_core.app.model.obbject import OBBject
        from openbb_core.provider.abstract.data import Data

        out = df.reset_index(drop=isinstance(df.index, RangeIndex))
        results = [
            Data.model_validate(
                {k: v for k, v in rec.items() if not (isinstance(v, float) and v != v)}
            )
            for rec in out.to_dict("records")
        ]
        return OBBject(
            results=results,
            provider="deltalake",
            extra={"symbol": key, "library": library, "metadata": metadata},
        )

    @staticmethod
    def _commit_props(metadata: Optional[dict]):
        # pylint: disable=import-outside-toplevel
        import json

        from deltalake import CommitProperties

        if not metadata:
            return None
        return CommitProperties(
            custom_metadata={"openbb_meta": json.dumps(metadata, default=str)}
        )

    def _table(self, key: str):
        # pylint: disable=import-outside-toplevel
        from deltalake import DeltaTable

        return DeltaTable(self._path(key), storage_options=self.storage_options)

    # -- write --------------------------------------------------------------
    def write(self, key: str, data: Any, *, metadata: Optional[dict] = None) -> dict[str, Any]:
        """Write any data as a new version of `key` (overwrites the symbol)."""
        # pylint: disable=import-outside-toplevel
        from deltalake import write_deltalake

        df = self._to_frame(data)
        write_deltalake(
            self._path(key),
            df,
            mode="overwrite",
            schema_mode="overwrite",
            storage_options=self.storage_options,
            commit_properties=self._commit_props(metadata),
        )
        return {
            "base": self.base,
            "library": self.library,
            "symbol": key,
            "version": self._table(key).version(),
            "rows": int(len(df)),
        }

    def append(self, key: str, data: Any) -> dict[str, Any]:
        """Append data to `key` (creates the table on first append)."""
        # pylint: disable=import-outside-toplevel
        from deltalake import write_deltalake

        df = self._to_frame(data)
        write_deltalake(
            self._path(key), df, mode="append", storage_options=self.storage_options
        )
        return {
            "base": self.base,
            "library": self.library,
            "symbol": key,
            "version": self._table(key).version(),
            "rows_appended": int(len(df)),
        }

    # -- read ---------------------------------------------------------------
    def read(
        self,
        key: str,
        *,
        start_date: Any = None,
        end_date: Any = None,
        columns: Optional[Sequence[str]] = None,
        as_of: Any = None,
        output: str = "obbject",
    ):
        """Read `key`; OBBject by default, DataFrame with output='dataframe'.

        `as_of` is an int Delta version, or a date/datetime/string for
        timestamp-based time travel.
        """
        # pylint: disable=import-outside-toplevel
        from pandas.api.types import is_datetime64_any_dtype

        from openbb_deltalake.utils import to_bounds

        if not self.has(key):
            raise FileNotFoundError(
                f"No Delta table for symbol '{key}' in library '{self.library}' "
                f"at '{self.base}'. Write some data first."
            )
        dt = self._table(key)
        if as_of is not None:
            if isinstance(as_of, int):
                dt.load_as_version(as_of)
            else:
                # load_as_version wants int | RFC3339-str | datetime; str()
                # of a date/naive-Timestamp/plain date-string is NOT RFC3339.
                from pandas import Timestamp

                ts = Timestamp(as_of)
                ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
                dt.load_as_version(ts.to_pydatetime())
        dataset = dt.to_pyarrow_dataset()
        start_ts, end_ts = to_bounds(start_date, end_date)
        filt = _date_filter(dataset.schema, start_ts, end_ts)
        cols = None
        if columns:
            cols = list(dict.fromkeys(["date", *columns])) \
                if "date" in dataset.schema.names else list(columns)
        df = dataset.to_table(columns=cols, filter=filt).to_pandas()
        if "date" in df.columns and is_datetime64_any_dtype(df["date"]):
            df = df.set_index("date").sort_index()
        if output == "dataframe":
            return df
        # read_metadata() always reflects the LATEST commit, even when `as_of`
        # points at an older version — accepted wrinkle (see SDD ledger).
        return self._to_obbject(df, key, self.read_metadata(key), self.library)

    # -- catalog ------------------------------------------------------------
    def list_symbols(self) -> list[str]:
        """List symbols (Delta tables) in the library."""
        # pylint: disable=import-outside-toplevel
        from pyarrow import fs as pafs

        from openbb_deltalake.utils import fs_and_root

        fsys, root = fs_and_root(self.base, self.storage_options)
        sel = pafs.FileSelector(f"{root.rstrip('/')}/{self.library}", allow_not_found=True)
        # ponytail: one _delta_log stat per child dir; fine for a per-episode
        # catalog, switch to a manifest table if libraries grow into thousands.
        out = []
        for info in fsys.get_file_info(sel):
            if info.type != pafs.FileType.Directory:
                continue
            log = fsys.get_file_info(f"{info.path}/_delta_log")
            if log.type != pafs.FileType.NotFound:
                out.append(info.base_name)
        return sorted(out)

    def read_trailing(self, key: str, n_rows: int, as_of: Any = None):
        """The newest n_rows, reading only the files the log says hold them.

        ArcticDB bounded an unfiltered read with `Library.tail`; Delta has no
        tail, so the bound comes from the transaction log's per-file stats.
        Falls back to a full read only when the log carries no usable bounds.
        """
        # pylint: disable=import-outside-toplevel
        import pyarrow.dataset as ds

        from openbb_deltalake.describe import trailing_fragment_paths
        from openbb_deltalake.utils import fs_and_root

        if as_of is not None:
            return self.read(key, as_of=as_of, output="dataframe").tail(n_rows)

        paths = trailing_fragment_paths(self, key, n_rows)
        if not paths:
            return self.read(key, output="dataframe").tail(n_rows)

        # An explicit filesystem, not a URI. pyarrow.dataset refuses an
        # "s3://..." path outright ("Expected a local filesystem path, got a
        # URI"), so passing self._path() through worked on a local tmp store
        # and failed on MinIO -- which is the only configuration that matters
        # in production. fs_and_root gives the handle and the scheme-less root
        # that list_symbols/delete already use.
        fsys, root = fs_and_root(self.base, self.storage_options)
        prefix = f"{root.rstrip('/')}/{self.library}/{key}"
        frame = ds.dataset(
            [f"{prefix}/{p}" for p in paths], format="parquet", filesystem=fsys
        ).to_table().to_pandas()
        if "date" in frame.columns:
            frame = frame.sort_values("date").set_index("date")
        return frame.tail(n_rows)

    def has(self, key: str) -> bool:
        """Whether a Delta table exists for `key`."""
        # pylint: disable=import-outside-toplevel
        from deltalake import DeltaTable

        return DeltaTable.is_deltatable(self._path(key), storage_options=self.storage_options)

    def delete(self, key: str) -> dict[str, Any]:
        """Delete a symbol's table entirely."""
        # pylint: disable=import-outside-toplevel
        from openbb_deltalake.utils import fs_and_root

        fsys, root = fs_and_root(self.base, self.storage_options)
        fsys.delete_dir(f"{root.rstrip('/')}/{self.library}/{key}")
        return {"base": self.base, "library": self.library, "deleted": key}

    def read_metadata(self, key: str) -> Any:
        """Metadata from the newest commit that carries `openbb_meta`."""
        # pylint: disable=import-outside-toplevel
        import json

        for entry in self._table(key).history():
            raw = entry.get("openbb_meta")
            if raw is not None:
                return json.loads(raw)
        return None


def _date_filter(schema, start_ts, end_ts):
    """Build a pyarrow dataset filter on the 'date' column, matching its tz."""
    if "date" not in schema.names or (start_ts is None and end_ts is None):
        return None
    # pylint: disable=import-outside-toplevel
    import pyarrow.dataset as ds

    field_type = schema.field("date").type
    tz = getattr(field_type, "tz", None)

    def _bound(ts):
        ts = ts.floor("us")  # to_pydatetime() on ns precision warns; Delta stores us anyway.
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


def store(uri: Optional[str] = None, library: Optional[str] = None) -> DeltaStore:
    """Convenience factory: `store(library="research")`."""
    return DeltaStore(uri=uri, library=library)

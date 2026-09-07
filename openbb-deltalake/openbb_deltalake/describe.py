# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Metadata answered from the Delta transaction log, never from rows.

ArcticDB gave the doors `list_libraries`, `get_description` and `tail`; Delta
gives none of them directly. All three come out of the add-actions in the
transaction log, which carry per-file row counts and per-column min/max — so
a symbol can be described, and a tail bounded, without opening a Parquet file.
"""
from __future__ import annotations


def _add_actions(dt):
    """Add-actions as a pandas frame.

    deltalake >= 1.6 returns an arro3 table, which has no `.to_pandas()`; the
    pyarrow wrap is required, not decorative. Verified on 1.6.3.
    """
    # pylint: disable=import-outside-toplevel
    import pyarrow as pa

    return pa.table(dt.get_add_actions(flatten=True)).to_pandas()


def list_libraries(base: str, storage_options: dict | None) -> list[str]:
    """Prefixes under `base` that themselves contain at least one Delta table.

    Delta has no catalog; a library is a directory holding symbol directories.
    This is `DeltaStore.list_symbols`' scan run one level higher, and stops at
    the first table it finds in a library rather than listing them all.
    """
    # pylint: disable=import-outside-toplevel
    from pyarrow import fs as pafs

    from openbb_deltalake.utils import fs_and_root

    fsys, root = fs_and_root(base, storage_options)
    out = []
    for lib in fsys.get_file_info(pafs.FileSelector(root.rstrip("/"), allow_not_found=True)):
        if lib.type != pafs.FileType.Directory:
            continue
        for child in fsys.get_file_info(pafs.FileSelector(lib.path, allow_not_found=True)):
            if child.type != pafs.FileType.Directory:
                continue
            if fsys.get_file_info(f"{child.path}/_delta_log").type != pafs.FileType.NotFound:
                out.append(lib.base_name)
                break
    return sorted(out)


def describe(store, symbol: str) -> dict:
    """Row count, stored date range, and schema. Reads zero rows."""
    dt = store._table(symbol)  # pylint: disable=protected-access
    adds = _add_actions(dt)
    row_count = int(adds["num_records"].sum()) if "num_records" in adds else 0

    date_range = None
    if len(adds) and "min.date" in adds and "max.date" in adds:
        date_range = [str(adds["min.date"].min()), str(adds["max.date"].max())]

    return {
        "library": store.library,
        "symbol": symbol,
        "row_count": row_count,
        "date_range": date_range,
        "columns": [{"name": f.name, "dtype": str(f.type)} for f in dt.schema().fields],
    }


def history(store, symbol: str) -> list[dict]:
    """Delta versions, newest first — the choices a time-travel control offers."""
    dt = store._table(symbol)  # pylint: disable=protected-access
    out = [
        {"version": int(e["version"]), "timestamp": str(e.get("timestamp", ""))}
        for e in dt.history()
    ]
    return sorted(out, key=lambda e: e["version"], reverse=True)


def trailing_fragment_paths(store, symbol: str, n_rows: int) -> list[str]:
    """The files holding the newest ~n_rows, chosen by log stats.

    ArcticDB's `Library.tail` bounded an unfiltered read; Delta has no tail, so
    the bound comes from taking files in descending `max.date` order until
    their row counts cover n_rows. Returns [] when the log carries no usable
    bounds, which the caller reads as "fall back to a full read".
    """
    dt = store._table(symbol)  # pylint: disable=protected-access
    adds = _add_actions(dt)
    if "max.date" not in adds or not len(adds):
        return []
    adds = adds.sort_values("max.date", ascending=False)

    taken, seen = [], 0
    for _, row in adds.iterrows():
        taken.append(row["path"])
        seen += int(row.get("num_records", 0))
        if seen >= n_rows:
            break
    return taken

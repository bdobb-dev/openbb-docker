# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Metadata answered from the transaction log — never from rows."""

import pandas as pd
import pytest
from deltalake import write_deltalake

from openbb_deltalake import describe as D
from openbb_deltalake.store import DeltaStore


@pytest.fixture
def store(tmp_path):
    frame = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=500, freq="D"),
        "close": range(500),
    })
    write_deltalake(f"{tmp_path}/ticks/AAPL", frame, mode="overwrite")
    write_deltalake(f"{tmp_path}/quotes/MSFT", frame, mode="overwrite")
    return DeltaStore(uri=str(tmp_path), library="ticks")


def test_list_libraries_finds_every_prefix_holding_a_table(store):
    assert D.list_libraries(store.base, store.storage_options) == ["quotes", "ticks"]


def test_describe_reports_rows_range_and_dtypes(store):
    out = D.describe(store, "AAPL")
    assert out["row_count"] == 500
    assert out["date_range"][0].startswith("2024-01-01")
    assert {c["name"] for c in out["columns"]} == {"date", "close"}


def test_describe_reads_no_rows(store, monkeypatch):
    """The D6 contract: metadata only. This is the assertion that enforces it."""
    from deltalake import DeltaTable

    def explode(self, *a, **k):
        raise AssertionError("describe must not materialize rows")

    monkeypatch.setattr(DeltaTable, "to_pyarrow_table", explode, raising=False)
    monkeypatch.setattr(DeltaTable, "to_pyarrow_dataset", explode, raising=False)
    D.describe(store, "AAPL")


def test_history_lists_versions_newest_first(store):
    store.write("AAPL", pd.DataFrame({"date": [pd.Timestamp("2024-01-01")], "close": [1]}))
    versions = [h["version"] for h in D.history(store, "AAPL")]
    assert versions == sorted(versions, reverse=True)
    assert len(versions) >= 2


def test_trailing_fragments_do_not_cover_the_whole_table(tmp_path):
    """R4: an unfiltered read must not need every file the symbol has."""
    path = f"{tmp_path}/ticks/AAPL"
    for i in range(5):
        frame = pd.DataFrame({
            "date": pd.date_range(f"202{i}-01-01", periods=100, freq="D"),
            "close": range(100),
        })
        write_deltalake(path, frame, mode="append" if i else "overwrite")

    store = DeltaStore(uri=str(tmp_path), library="ticks")
    picked = D.trailing_fragment_paths(store, "AAPL", n_rows=50)
    assert len(picked) == 1, "50 rows must not need all five files"


def test_trailing_read_passes_a_filesystem_not_a_uri(store, monkeypatch):
    """Regression: pyarrow.dataset refuses an "s3://..." path.

    Building paths from `self._path()` worked against a local tmp store and
    failed against MinIO with "Expected a local filesystem path, got a URI" --
    a bug no tmp-path test could see. read_trailing must therefore hand
    pyarrow an explicit filesystem plus scheme-less paths.
    """
    import pyarrow.dataset as ds

    seen = {}
    real = ds.dataset

    def spy(paths, **kw):
        seen["paths"] = paths
        seen["filesystem"] = kw.get("filesystem")
        return real(paths, **kw)

    monkeypatch.setattr(ds, "dataset", spy)
    store.read_trailing("AAPL", n_rows=10)

    assert seen["filesystem"] is not None, "read_trailing must pass a filesystem"
    assert not any(str(p).startswith(("s3://", "file://")) for p in seen["paths"]), seen["paths"]

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Pins the delta-rs API surface this package depends on.

If a deltalake upgrade renames any of these, THIS test fails first, with a
clear message, instead of the store failing obscurely.
"""

import json

import pandas as pd


def test_deltalake_api_surface(tmp_path):
    from deltalake import CommitProperties, DeltaTable, write_deltalake

    path = str(tmp_path / "lib" / "AAPL")
    df1 = pd.DataFrame({"date": pd.to_datetime(["2026-01-02"]), "close": [1.0]})
    df2 = pd.DataFrame({"date": pd.to_datetime(["2026-01-03"]), "close": [2.0]})

    props = CommitProperties(custom_metadata={"openbb_meta": json.dumps({"k": "v"})})
    write_deltalake(path, df1, mode="overwrite", schema_mode="overwrite", commit_properties=props)
    write_deltalake(path, df2, mode="overwrite", schema_mode="overwrite")

    assert DeltaTable.is_deltatable(path)
    dt = DeltaTable(path)
    assert dt.version() == 1

    # column projection + filter through a pyarrow dataset scan
    import pyarrow.dataset as ds

    table = dt.to_pyarrow_dataset().to_table(
        columns=["date", "close"],
        filter=ds.field("date") >= pd.Timestamp("2026-01-03").to_pydatetime(),
    )
    assert table.num_rows == 1

    # time travel by version, and commit metadata in history
    dt.load_as_version(0)
    assert dt.to_pyarrow_dataset().to_table().num_rows == 1
    assert any("openbb_meta" in h for h in dt.history())

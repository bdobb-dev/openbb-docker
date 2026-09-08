# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Spec gate: delta-rs commit atomicity on MinIO via conditional puts.

Eight writers appending concurrently must ALL land (no lost update) or fail
loudly — never silently drop a commit. Skipped without a MinIO env.
"""

import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DELTA_S3_ENDPOINT"), reason="needs MinIO (DELTA_S3_* unset)"
)

N_WRITERS = 8


def test_concurrent_appends_all_land():
    from openbb_deltalake.store import DeltaStore

    store = DeltaStore(library="it_concurrency")
    sym = "CONCURRENT"
    if store.has(sym):
        store.delete(sym)
    store.write(sym, pd.DataFrame({"date": pd.to_datetime(["2026-01-01"]), "n": [-1]}))

    def _append(i: int):
        df = pd.DataFrame(
            {"date": [pd.Timestamp("2026-01-02") + pd.Timedelta(minutes=i)], "n": [i]}
        )
        # delta-rs retries commit conflicts internally for blind appends;
        # a raised CommitFailedError here is a FINDING, not flake — it means
        # MinIO's conditional puts are not doing their job.
        DeltaStore(library="it_concurrency").append(sym, df)

    with ThreadPoolExecutor(max_workers=N_WRITERS) as pool:
        list(pool.map(_append, range(N_WRITERS)))

    back = store.read(sym, output="dataframe")
    assert sorted(back["n"].tolist()) == [-1, *range(N_WRITERS)]
    store.delete(sym)

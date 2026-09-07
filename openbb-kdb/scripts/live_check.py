# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Live check: real q, real PyKX, real store. Requires a license; not in CI.

This file is not shipped inside the image (the Dockerfile deletes the source
tree after `pip install`), so feed it to the running container's interpreter
on stdin. From the repo root:

    docker compose exec -T openbb-api python - < openbb-kdb/scripts/live_check.py
"""

import sys
import threading
from datetime import datetime, timedelta

import pandas as pd

from kdb_store.config import resolve_config
from kdb_store.session import KdbSession
from kdb_store.store import KdbStore

failures = []


def check(name, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}  {name} {detail}")
    if not condition:
        failures.append(name)


config = resolve_config()
session = KdbSession(config)
conn = session.connection()
store = KdbStore(session)


def q(expr):
    """Raw q, on the session's owner thread -- the only thread allowed to touch it."""
    return session.run(lambda: conn(expr).py())


check("q answers", q("1+1") == 2)

mem = store.memory()
check("memory keys are str", "heap" in mem and "wmax" in mem, str(mem)[:120])
check(
    "workspace is the configured headroom",
    mem["wmax"] == config.q_workspace_mb * 1024 * 1024,
    f"wmax={mem['wmax']}",
)

# Bars round-trip through the real PyKX conversions.
now = datetime(2025, 1, 10)
frame = pd.DataFrame({
    "t": [now - timedelta(days=i) for i in range(5)],
    "open": [1.0] * 5, "high": [2.0] * 5, "low": [0.5] * 5,
    "close": [1.5] * 5, "volume": [100] * 5,
})
store.write_bars("LIVECHK", "1d", frame)
back = store.read_bars("LIVECHK", "1d", now - timedelta(days=2), now)
check("bars round-trip", len(back) == 3, f"got {len(back)} rows")

# Writing twice must not duplicate rows.
store.write_bars("LIVECHK", "1d", frame)
back2 = store.read_bars("LIVECHK", "1d", now - timedelta(days=10), now)
check("upsert does not duplicate", len(back2) == 5, f"got {len(back2)} rows")

# Per-batch dtype drift, against a REAL q upsert. A provider column is float64
# in a batch that carries values and object in a short batch where every value
# is None; q's upsert refuses to coerce and answers 'type. Only a window
# reaching the present writes a second, small batch into an existing table, so
# this is the whole reason a live chart never cached.
drift_cold = pd.DataFrame({
    "t": [now - timedelta(days=i) for i in (2, 1)],
    "close": [1.5, 1.6], "volume": [100, 200],
    "dividend": [0.0, 0.0], "vwap": [None, None],
})
drift_tail = pd.DataFrame({
    "t": [now - timedelta(days=1), now],
    "close": [1.6, 1.7], "volume": [200.0, float("nan")],
    "dividend": [None, None], "vwap": [None, None],
})
store.write_bars("DRIFTCHK", "1d", drift_cold)
try:
    store.write_bars("DRIFTCHK", "1d", drift_tail)
    drift_rows = len(store.read_bars("DRIFTCHK", "1d", now - timedelta(days=5), now))
    check("a drifting batch dtype still upserts", drift_rows == 3, f"{drift_rows} rows")
except Exception as exc:  # noqa: BLE001
    check("a drifting batch dtype still upserts", False, f"{type(exc).__name__}: {exc}")
store.drop("DRIFTCHK", "1d")

# Coverage round-trip (real timestamp conversion).
store.record_coverage("LIVECHK", "1d", (now - timedelta(days=5), now))
cov = store.read_coverage("LIVECHK", "1d")
check("coverage round-trip", len(cov) == 1, str(cov)[:120])

# Eviction genuinely returns heap -- delete alone does not. The ballast has to
# exceed q's 64MB initial heap or nothing was ever allocated to give back:
# 20M floats is 160MB, which q rounds up to a 320MB heap.
q("ballast: 20000000 # 1.0")
grown = store.memory()["heap"]
q("delete ballast from `.")
q(".Q.gc[]")
after = store.memory()["heap"]
check("gc reclaims heap", after < grown, f"{grown} -> {after}")

store.drop("LIVECHK", "1d")
check("drop clears coverage", store.read_coverage("LIVECHK", "1d") == [])

# q reached from several threads at once (spec risk 3). PyKX is unusable from
# more than one thread, so the session marshals every call onto its owner
# thread; what is under test is that the marshalling holds and replies never
# cross between callers. (`threading` is imported at the top of the file.)

results = {}


def worker(tag):
    try:
        results[tag] = q(f"{tag}+1")
    except Exception as exc:  # noqa: BLE001
        results[tag] = f"ERR {type(exc).__name__}"


threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 4)]
[t.start() for t in threads]
[t.join() for t in threads]
ok = all(results.get(i) == i + 1 for i in range(1, 4))
check("concurrent threads reach q", ok, str(results))

# The decisive case: NO concurrency at all, just a different thread each round.
# Before the owner-thread fix this aborted the whole process with
# `free(): invalid size`; a lock would not have helped, there is nothing here
# to serialise.
rounds = [("2024-01-01", "2024-12-31"), ("2022-01-01", "2023-12-31"),
          ("2020-01-01", "2021-12-31"), ("2018-01-01", "2019-12-31")]
seen = []


def round_trip(first, last):
    span = pd.bdate_range(first, last)
    store.write_bars("THREADCHK", "1d", pd.DataFrame({
        "t": span, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100,
    }))
    got = store.read_bars("THREADCHK", "1d", datetime(2015, 1, 1), datetime(2026, 1, 1))
    seen.append(len(got))


for index, (first, last) in enumerate(rounds):
    t = threading.Thread(target=round_trip, args=(first, last), name=f"round{index}")
    t.start()
    t.join()

check(
    "sequential round-trips, a fresh thread each",
    len(seen) == len(rounds) and seen == sorted(seen) and seen[-1] > seen[0],
    f"rows after each round: {seen}",
)
store.drop("THREADCHK", "1d")

session.close()
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)

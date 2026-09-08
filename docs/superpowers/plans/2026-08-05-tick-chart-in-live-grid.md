<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Tick Recording and the Unified Chart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `live-grid` record the tick stream it already receives into kdb+, aggregate bars from those ticks with q's `xbar`, and serve one chart that stitches live tick-derived bars onto cached history — replacing the separate `cache-chart` service.

**Architecture:** The q plumbing (`config`, `session`, `store`, `ranges`) moves out of `openbb-kdb` into a shared `kdb-store` package that both `openbb-kdb` and `live-grid` depend on, so there is exactly one implementation of the single-owner-thread rule PyKX requires. `live-grid` gains a bounded tick buffer flushed to a `trades` table in batches, a rolling-window prune, `xbar` aggregation in q, and chart endpoints that join tick-derived bars to bars from the existing read-through cache at the first bar boundary fully covered by ticks.

**Tech Stack:** Python 3.12, PyKX 3.2 (IPC/unlicensed), kdb-x q 5.0, FastAPI, pandas, plotly.js (vendored), pytest.

## Global Constraints

- **Design doc:** `docs/tick-chart-design.md`. It supersedes the `cache-chart` parts of `docs/kdb-cache-design.md`.
- **The repo is PUBLIC.** No licence blob, real tailnet name, hostname, or key may enter it. `bash scripts/scrub-check.sh` must pass before every commit. All hostnames are `<your-tailnet>.ts.net` placeholders.
- **PyKX aborts the process when touched from more than one thread** — not merely under concurrency; strictly sequential calls from different threads abort with `free(): invalid size`. Every PyKX call goes through `KdbSession`'s single owner thread. This is measured, not theoretical.
- **Episode 8 must not regress.** With no reachable q, `live-grid` must stream its `live_grid` grid exactly as it does today. This has its own test.
- **q lambda parameters must not collide with column names.** A shadowed parameter silently returns wrong rows rather than erroring. All lambda parameters use the `qw` prefix (`qwsym`, `qwlo`, `qwhi`, `qwbucket`), matching the existing convention in `store.py`.
- **Ticks must be time-sorted before aggregating.** Verified against real q: aggregating an unsorted trades table returned `open=101, close=103` where the truth was `open=100, close=101` — silently wrong candles, no error.
- **q's `upsert` requires exact column-type matches** and pandas re-infers dtypes per batch. Batches are conformed to the stored schema before writing, using the existing `_conform_dtypes` helper.
- **Aggregation results come back KEYED.** `0!` them before `.pd()`.
- Style: `line-length = 100`; lazy imports inside functions where the surrounding code does so; tests under `tests/` with `pythonpath = ["."]`.
- Tests need no kdb licence, no API key, and no network. Use `python3`.

### Verified q facts (do not re-derive)

Bucket sizes in nanoseconds: `1m=60000000000`, `5m=300000000000`, `1h=3600000000000`, `1d=86400000000000`.

The aggregation lambda, verified against real q against an out-of-order source:

```q
{[qwsym;qwlo;qwhi;qwbucket]
  0!select open:first price, high:max price, low:min price, close:last price, volume:sum size
    by t: qwbucket xbar time
    from `time xasc select from trades where sym=qwsym, time within (qwlo;qwhi)}
```

Verified behaviour: correct OHLCV from unsorted input; other symbols excluded; an empty window returns a 0-row table with columns `t open high low close volume` rather than erroring; an unknown symbol returns 0 rows; `sum` over a null `size` yields `0` (which is what forex needs, since forex has no trade size).

---

## File Structure

**`kdb-store/` (new distribution, package `kdb_store`):**

| File | Responsibility |
|---|---|
| `kdb_store/config.py` | Moved from `openbb_kdb` unchanged, plus tick settings |
| `kdb_store/session.py` | Moved unchanged — owns the q process and the single owner thread |
| `kdb_store/store.py` | Moved, plus the `trades` table: write, prune, span |
| `kdb_store/ranges.py` | Moved unchanged |
| `kdb_store/aggregate.py` | **New** — `xbar` aggregation of ticks into bars |

**`openbb-kdb/`:** keeps `cache.py`, `upstream.py`, `models/`; imports from `kdb_store`.

**`live-grid/`:**

| File | Responsibility |
|---|---|
| `app/recorder.py` | **New** — bounded tick buffer, batching, drop counting |
| `app/series.py` | **New** — seam computation and stitching |
| `app/figure.py` | **New** — Plotly figure JSON (moved from `cache-chart`) |
| `app/openbb_client.py` | **New** — historical bars via the OpenBB API (moved) |
| `app/static/demo.html` | Moved from `cache-chart` |
| `app/quotes.py` | Gains an `on_tick` callback hook |
| `app/feeds.py` | Flush and prune cadence in `run()` |
| `app/main.py` | `/chart`, `/series`, `/demo`, static mount, `/health` counters |

**Deleted:** `cache-chart/` entirely, its compose service, image and Serve route.

---

### Task 1: Extract `kdb-store`

Move the four OpenBB-free modules into their own distribution. No behaviour changes.

**Files:**
- Create: `kdb-store/pyproject.toml`, `kdb-store/kdb_store/__init__.py`
- Move: `openbb-kdb/openbb_kdb/{config,session,store,ranges}.py` → `kdb-store/kdb_store/`
- Move: `openbb-kdb/tests/test_{config,session,store,ranges}.py` → `kdb-store/tests/`
- Modify: `openbb-kdb/pyproject.toml`, `openbb-kdb/openbb_kdb/cache.py`, `openbb-kdb/openbb_kdb/models/historical.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `kdb_store.config.{KdbConfig, resolve_config}`, `kdb_store.session.{KdbSession, KdbUnavailable}`, `kdb_store.store.KdbStore`, `kdb_store.ranges.{Range, coalesce, subtract, trim_tail, interval_step}` — identical signatures to their `openbb_kdb` originals.

- [ ] **Step 1: Create the package skeleton**

```bash
cd /Users/artcashin/Developer/openbb-docker
mkdir -p kdb-store/kdb_store kdb-store/tests
touch kdb-store/kdb_store/__init__.py kdb-store/tests/__init__.py
```

Create `kdb-store/pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "kdb-store"
version = "0.1.0"
description = "Shared kdb+ session and store: the single-owner-thread PyKX plumbing"
readme = "README.md"
requires-python = ">=3.10"
license = { text = "AGPL-3.0-only" }
dependencies = ["pykx>=3.2", "pandas"]

[tool.setuptools.packages.find]
include = ["kdb_store*"]

[project.optional-dependencies]
dev = ["ruff", "pytest"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]

[tool.ruff]
target-version = "py310"
line-length = 100

[tool.ruff.lint]
ignore = ["PLC0415"]
```

- [ ] **Step 2: Move the modules and their tests with git mv**

```bash
cd /Users/artcashin/Developer/openbb-docker
git mv openbb-kdb/openbb_kdb/config.py  kdb-store/kdb_store/config.py
git mv openbb-kdb/openbb_kdb/session.py kdb-store/kdb_store/session.py
git mv openbb-kdb/openbb_kdb/store.py   kdb-store/kdb_store/store.py
git mv openbb-kdb/openbb_kdb/ranges.py  kdb-store/kdb_store/ranges.py
git mv openbb-kdb/tests/test_config.py  kdb-store/tests/test_config.py
git mv openbb-kdb/tests/test_session.py kdb-store/tests/test_session.py
git mv openbb-kdb/tests/test_store.py   kdb-store/tests/test_store.py
git mv openbb-kdb/tests/test_ranges.py  kdb-store/tests/test_ranges.py
git mv openbb-kdb/tests/test_tail_types.py kdb-store/tests/test_tail_types.py
```

If `test_tail_types.py` does not exist under that name, find the test file covering `_conform_dtypes` (`grep -rl _conform_dtypes openbb-kdb/tests/`) and move it too.

- [ ] **Step 3: Rewrite the imports**

In the moved files and their tests, replace every `openbb_kdb.` import with `kdb_store.`:

```bash
cd /Users/artcashin/Developer/openbb-docker
grep -rl "openbb_kdb" kdb-store/ | xargs sed -i '' 's/openbb_kdb/kdb_store/g'
```

Then in `openbb-kdb`, point the remaining modules at the new package:

```bash
grep -rl "from openbb_kdb.config\|from openbb_kdb.session\|from openbb_kdb.store\|from openbb_kdb.ranges" openbb-kdb/ \
  | xargs sed -i '' \
    -e 's/from openbb_kdb\.config/from kdb_store.config/g' \
    -e 's/from openbb_kdb\.session/from kdb_store.session/g' \
    -e 's/from openbb_kdb\.store/from kdb_store.store/g' \
    -e 's/from openbb_kdb\.ranges/from kdb_store.ranges/g'
```

Check nothing was missed: `grep -rn "openbb_kdb\.\(config\|session\|store\|ranges\)" openbb-kdb/ kdb-store/` must return nothing.

- [ ] **Step 4: Add the dependency**

In `openbb-kdb/pyproject.toml`, add `"kdb-store"` to `dependencies`. Since it is a sibling path rather than a published package, installs use `pip install -e ../kdb-store` first; note that in the README and use it in CI (Task 9).

- [ ] **Step 5: Create the package README**

Create `kdb-store/README.md`:

````markdown
# kdb-store

The shared kdb+ plumbing: the `q` child process, its IPC connection, and the
store built on top. Used by both `openbb-kdb` (the read-through cache provider)
and `live-grid` (the tick recorder).

**Why this is its own package.** PyKX aborts the process when touched from more
than one thread — not merely under concurrency, but on strictly sequential
calls from different threads (`free(): invalid size`). `KdbSession` solves that
by marshalling every PyKX call onto one owner thread. Two consumers need that
guarantee, and there must be exactly one implementation of it.

## Test

    pip install -e .[dev] && pytest    # no kdb licence needed
````

- [ ] **Step 6: Run both suites**

```bash
cd /Users/artcashin/Developer/openbb-docker/kdb-store && pip install -e .[dev] && python3 -m pytest -q
cd /Users/artcashin/Developer/openbb-docker/openbb-kdb && pip install -e . && python3 -m pytest -q
```

Expected: the moved tests pass under `kdb-store`, and `openbb-kdb`'s remaining tests pass. Together they must still total 157 — **no test may be lost in the move.** Count them: `python3 -m pytest --collect-only -q | tail -1` in each directory, and confirm the two numbers sum to 157.

- [ ] **Step 7: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add -A kdb-store openbb-kdb
git commit -m "refactor: extract kdb-store, the shared single-owner-thread q plumbing"
```

---

### Task 2: The trades table

**Files:**
- Modify: `kdb-store/kdb_store/store.py`
- Modify: `kdb-store/tests/test_store.py`

**Interfaces:**
- Consumes: `KdbStore` from Task 1.
- Produces, on `KdbStore`:
  - `write_ticks(frame) -> int` — batch-insert a DataFrame with columns `time`, `sym`, `price`, `size`; returns rows written
  - `prune_ticks(cutoff: datetime) -> int` — delete ticks older than `cutoff`, `.Q.gc[]`, return rows remaining
  - `tick_span(symbol: str) -> tuple[datetime, datetime] | None` — earliest and latest tick held for a symbol, or `None`

- [ ] **Step 1: Write the failing tests**

Append to `kdb-store/tests/test_store.py`:

```python
def test_schema_init_creates_the_trades_table():
    s, conn = store_with()
    s.write_ticks(_ticks_frame())
    joined = " ".join(conn.queries)
    assert "trades" in joined


def test_write_ticks_sends_one_batch_not_one_insert_per_row():
    """Per-tick IPC cannot keep up with a live feed."""
    s, conn = store_with()
    n = s.write_ticks(_ticks_frame(rows=50))
    assert n == 50
    inserts = [q for q in conn.queries if "insert" in q]
    assert len(inserts) == 1, f"expected one batched insert, got {len(inserts)}"


def test_prune_ticks_deletes_below_the_cutoff_and_collects():
    s, conn = store_with({"count trades": 3})
    s.prune_ticks(D("2025-06-10T14:00:00"))
    joined = " ".join(conn.queries)
    assert "delete" in joined and "trades" in joined
    assert any(".Q.gc" in q for q in conn.queries)


def test_tick_span_returns_none_when_no_ticks():
    s, _ = store_with({"select min time": None})
    assert s.tick_span("NOPE") is None


def test_lambda_parameters_in_tick_queries_never_shadow_a_column():
    """A q lambda parameter matching a column name silently returns wrong rows."""
    import re

    s, conn = store_with()
    s.write_ticks(_ticks_frame())
    s.prune_ticks(D("2025-06-10T14:00:00"))
    s.tick_span("AAPL")
    columns = {"time", "sym", "price", "size", "t", "open", "high", "low", "close", "volume"}
    for query in conn.queries:
        for params in re.findall(r"\{\[([^\]]*)\]", query):
            for name in (p.strip() for p in params.split(";") if p.strip()):
                assert name not in columns, f"parameter {name!r} shadows a column in: {query}"
                assert name.startswith("qw"), f"parameter {name!r} lacks the qw prefix"
```

Add these helpers near the top of the same file, after the existing imports:

```python
def _ticks_frame(rows: int = 3):
    """A trades-shaped batch."""
    import pandas as pd

    base = D("2025-06-10T14:00:00")
    return pd.DataFrame({
        "time": [base + timedelta(seconds=i) for i in range(rows)],
        "sym": ["AAPL"] * rows,
        "price": [100.0 + i for i in range(rows)],
        "size": [1.0] * rows,
    })
```

and ensure `from datetime import timedelta` is imported in that file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd kdb-store && python3 -m pytest tests/test_store.py -q -k tick`
Expected: FAIL — `AttributeError: 'KdbStore' object has no attribute 'write_ticks'`

- [ ] **Step 3: Add the trades table to the schema**

In `kdb-store/kdb_store/store.py`, extend the `_INIT_SCHEMA` statement so it also creates the trades table, in the same idempotent `if[not ... in key ...]` style already used for `.cache.cov` and `.cache.lru`:

```q
if[not `trades in key `.; trades: ([] time:`timestamp$(); sym:`symbol$(); price:`float$(); size:`float$())]
```

Keep it in the same statement/list the existing schema init uses, so it re-runs on a new connection exactly as the others do (a respawned q is empty).

- [ ] **Step 4: Implement the three methods**

Add to `KdbStore` in `kdb-store/kdb_store/store.py`:

```python
    def write_ticks(self, frame) -> int:
        """Batch-insert ticks. One IPC round-trip per flush, never per tick.

        The batch is conformed to the stored column types first: q's `insert`
        rejects a type mismatch outright rather than coercing, and pandas
        re-infers dtypes per batch (an all-null size column arrives as object
        where the stored column is float).
        """
        if frame is None or getattr(frame, "empty", True):
            return 0

        def write(conn):
            prototype = conn("0#trades").pd()
            conn["incoming_ticks"] = _conform_dtypes(frame, prototype)
            conn("`trades insert incoming_ticks")
            conn("delete incoming_ticks from `.")
            return len(frame)

        return self.session.run(lambda: write(self.session.connection()))

    def prune_ticks(self, cutoff) -> int:
        """Drop ticks older than `cutoff` and return the row count remaining.

        `delete` frees `used` but not `heap`; only `.Q.gc[]` returns it.
        """
        def prune(conn):
            conn("{[qwcut] trades:: delete from trades where time < qwcut}", _q_timestamp(cutoff))
            conn(".Q.gc[]")
            return int(conn("count trades").py())

        return self.session.run(lambda: prune(self.session.connection()))

    def tick_span(self, symbol: str):
        """Earliest and latest tick held for a symbol, or None if there are none."""
        import pandas as pd

        def span(conn):
            got = conn(
                "{[qwsym] select lo: min time, hi: max time from trades where sym = qwsym}",
                _q_symbol(symbol),
            ).pd()
            if got is None or got.empty:
                return None
            lo, hi = got["lo"].iloc[0], got["hi"].iloc[0]
            if pd.isna(lo) or pd.isna(hi):
                return None
            return (lo.to_pydatetime(), hi.to_pydatetime())

        return self.session.run(lambda: span(self.session.connection()))
```

Move `import pandas as pd` to the top of `span`'s enclosing method body so it is bound before use.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd kdb-store && python3 -m pytest tests/test_store.py -q`
Expected: PASS — all existing store tests plus the 5 new ones.

- [ ] **Step 6: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add kdb-store/
git commit -m "feat(kdb-store): trades table with batched writes and rolling-window prune"
```

---

### Task 3: `xbar` aggregation

The single highest-value piece of q in this change.

**Files:**
- Create: `kdb-store/kdb_store/aggregate.py`
- Create: `kdb-store/tests/test_aggregate.py`

**Interfaces:**
- Consumes: `KdbStore` (Task 2).
- Produces:
  - `BUCKET_NS: dict[str, int]` — interval string → bucket width in nanoseconds
  - `bucket_ns(interval: str) -> int`
  - `aggregate_ticks(store, symbol, interval, start, end) -> list[dict]` — OHLCV rows with keys `date`, `open`, `high`, `low`, `close`, `volume`

- [ ] **Step 1: Write the failing tests**

Create `kdb-store/tests/test_aggregate.py`:

```python
"""Aggregating ticks into OHLCV buckets."""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from kdb_store.aggregate import aggregate_ticks, bucket_ns

D = lambda s: datetime.fromisoformat(s)  # noqa: E731


class FakeStore:
    """Returns a canned aggregation frame and records the call."""

    def __init__(self, frame=None):
        self.frame = frame if frame is not None else pd.DataFrame()
        self.calls = []

    def aggregate_frame(self, symbol, interval, start, end):
        self.calls.append((symbol, interval, start, end))
        return self.frame


def test_bucket_ns_for_supported_intervals():
    assert bucket_ns("1m") == 60_000_000_000
    assert bucket_ns("5m") == 300_000_000_000
    assert bucket_ns("1h") == 3_600_000_000_000
    assert bucket_ns("1d") == 86_400_000_000_000


def test_bucket_ns_rejects_unknown():
    with pytest.raises(ValueError):
        bucket_ns("1fortnight")


def test_aggregate_maps_q_columns_to_bar_rows():
    frame = pd.DataFrame({
        "t": [pd.Timestamp("2025-06-10T14:00:00")],
        "open": [100.0], "high": [103.0], "low": [100.0],
        "close": [101.0], "volume": [16.0],
    })
    rows = aggregate_ticks(FakeStore(frame), "AAPL", "1m", D("2025-06-10T14:00"), D("2025-06-10T15:00"))
    assert rows == [{
        "date": pd.Timestamp("2025-06-10T14:00:00"),
        "open": 100.0, "high": 103.0, "low": 100.0, "close": 101.0, "volume": 16.0,
    }]


def test_aggregate_returns_empty_list_for_no_ticks():
    assert aggregate_ticks(FakeStore(), "AAPL", "1m", D("2025-01-01"), D("2025-01-02")) == []


def test_aggregate_passes_the_window_through_unchanged():
    store = FakeStore()
    start, end = D("2025-06-10T14:00"), D("2025-06-10T15:00")
    aggregate_ticks(store, "AAPL", "5m", start, end)
    assert store.calls == [("AAPL", "5m", start, end)]


def test_rows_are_time_ordered():
    frame = pd.DataFrame({
        "t": [pd.Timestamp("2025-06-10T14:01:00"), pd.Timestamp("2025-06-10T14:00:00")],
        "open": [99.0, 100.0], "high": [99.0, 103.0], "low": [99.0, 100.0],
        "close": [99.0, 101.0], "volume": [7.0, 16.0],
    })
    rows = aggregate_ticks(FakeStore(frame), "AAPL", "1m", D("2025-06-10T14:00"), D("2025-06-10T15:00"))
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd kdb-store && python3 -m pytest tests/test_aggregate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kdb_store.aggregate'`

- [ ] **Step 3: Add the q query to the store**

Add to `KdbStore` in `kdb-store/kdb_store/store.py`. **This q text is verified against a real q server — use it exactly.** Note the mandatory `` `time xasc ``: aggregating an unsorted trades table returned `open=101, close=103` where the truth was `open=100, close=101`, silently, with no error.

```python
    def aggregate_frame(self, symbol: str, interval: str, start, end):
        """OHLCV buckets for one symbol, aggregated in q.

        `time xasc` is REQUIRED: ticks arrive out of order, and `first`/`last`
        on an unsorted table silently produce the wrong open and close.
        The result comes back keyed, so `0!` it before .pd().
        """
        from kdb_store.aggregate import bucket_ns

        width = bucket_ns(interval)

        def run(conn):
            return conn(
                "{[qwsym;qwlo;qwhi;qwbucket]"
                " 0!select open:first price, high:max price, low:min price,"
                " close:last price, volume:sum size"
                " by t: qwbucket xbar time"
                " from `time xasc select from trades"
                " where sym=qwsym, time within (qwlo;qwhi)}",
                _q_symbol(symbol),
                _q_timestamp(start),
                _q_timestamp(end),
                width,
            ).pd()

        return self.session.run(lambda: run(self.session.connection()))
```

- [ ] **Step 4: Implement `aggregate.py`**

Create `kdb-store/kdb_store/aggregate.py`:

```python
"""Aggregating recorded ticks into OHLCV bars.

The aggregation itself runs in q (`xbar`), next to the data. This module only
maps intervals to bucket widths and the returned frame to bar rows.
"""

BUCKET_NS = {
    "1s": 1_000_000_000,
    "1m": 60_000_000_000,
    "5m": 300_000_000_000,
    "15m": 900_000_000_000,
    "30m": 1_800_000_000_000,
    "1h": 3_600_000_000_000,
    "1d": 86_400_000_000_000,
}


def bucket_ns(interval: str) -> int:
    """Bucket width in nanoseconds for an interval q can aggregate."""
    width = BUCKET_NS.get(str(interval).strip())
    if width is None:
        raise ValueError(
            f"Interval {interval!r} cannot be aggregated from ticks. "
            f"Supported: {sorted(BUCKET_NS)}"
        )
    return width


def aggregate_ticks(store, symbol: str, interval: str, start, end) -> list[dict]:
    """OHLCV rows built from the ticks held for `symbol` within [start, end]."""
    frame = store.aggregate_frame(symbol, interval, start, end)
    if frame is None or getattr(frame, "empty", True):
        return []
    ordered = frame.sort_values("t")
    return [
        {
            "date": row.t,
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
        }
        for row in ordered.itertuples()
    ]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd kdb-store && python3 -m pytest tests/test_aggregate.py -q`
Expected: PASS, 6 passed

- [ ] **Step 6: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add kdb-store/
git commit -m "feat(kdb-store): xbar aggregation of ticks into OHLCV bars"
```

---

### Task 4: The tick recorder

Pure buffering logic. No kdb in the tests.

**Files:**
- Create: `live-grid/app/recorder.py`
- Create: `live-grid/tests/test_recorder.py`
- Modify: `live-grid/pyproject.toml`

**Interfaces:**
- Consumes: `KdbStore.write_ticks` / `prune_ticks` (Task 2).
- Produces: `TickRecorder(store, max_buffer=100_000, window=timedelta(days=1))` with `record(sym, price, size, stamp) -> None`, `flush() -> int`, `prune(now=None) -> int`, and the attributes `buffered: int`, `written: int`, `dropped: int`.

- [ ] **Step 1: Write the failing tests**

Create `live-grid/tests/test_recorder.py`:

```python
"""The tick buffer: bounded, batched, and honest about what it dropped."""

from datetime import datetime, timedelta

from app.recorder import TickRecorder

D = lambda s: datetime.fromisoformat(s)  # noqa: E731


class FakeStore:
    def __init__(self, fail=False):
        self.batches = []
        self.pruned = []
        self.fail = fail

    def write_ticks(self, frame):
        if self.fail:
            raise RuntimeError("closed IPC connection")
        self.batches.append(frame)
        return len(frame)

    def prune_ticks(self, cutoff):
        self.pruned.append(cutoff)
        return 0


def make(**kw):
    store = FakeStore(**{k: v for k, v in kw.items() if k == "fail"})
    rec = TickRecorder(store, max_buffer=kw.get("max_buffer", 1000),
                       window=kw.get("window", timedelta(days=1)))
    return rec, store


def test_record_buffers_without_writing():
    rec, store = make()
    rec.record("AAPL", 100.0, 1.0, D("2025-06-10T14:00:00"))
    assert rec.buffered == 1
    assert store.batches == []


def test_flush_writes_one_batch_and_empties_the_buffer():
    rec, store = make()
    for i in range(5):
        rec.record("AAPL", 100.0 + i, 1.0, D("2025-06-10T14:00:00") + timedelta(seconds=i))
    assert rec.flush() == 5
    assert len(store.batches) == 1
    assert list(store.batches[0].columns) == ["time", "sym", "price", "size"]
    assert rec.buffered == 0
    assert rec.written == 5


def test_flush_with_an_empty_buffer_does_not_call_the_store():
    rec, store = make()
    assert rec.flush() == 0
    assert store.batches == []


def test_buffer_is_bounded_and_drops_oldest():
    """An unbounded buffer growing while q is down is the failure to prevent."""
    rec, _ = make(max_buffer=3)
    for i in range(5):
        rec.record("AAPL", float(i), 1.0, D("2025-06-10T14:00:00") + timedelta(seconds=i))
    assert rec.buffered == 3
    assert rec.dropped == 2
    frame = rec._frame()
    assert list(frame["price"]) == [2.0, 3.0, 4.0]


def test_a_failed_flush_does_not_grow_the_buffer_without_bound():
    rec, _ = make(fail=True, max_buffer=3)
    for i in range(3):
        rec.record("AAPL", float(i), 1.0, D("2025-06-10T14:00:00"))
    assert rec.flush() == 0
    for i in range(3):
        rec.record("AAPL", float(i), 1.0, D("2025-06-10T14:00:00"))
    assert rec.buffered <= 3


def test_missing_size_records_zero():
    """Forex carries no trade size; sum over it must still be a number."""
    rec, store = make()
    rec.record("EURUSD", 1.08, None, D("2025-06-10T14:00:00"))
    rec.flush()
    assert list(store.batches[0]["size"]) == [0.0]


def test_prune_uses_the_configured_window():
    rec, store = make(window=timedelta(hours=2))
    rec.prune(now=D("2025-06-10T15:00:00"))
    assert store.pruned == [D("2025-06-10T13:00:00")]


def test_prune_survives_a_store_failure():
    rec, store = make(fail=True)
    store.prune_ticks = lambda cutoff: (_ for _ in ()).throw(RuntimeError("no q"))
    rec.prune(now=D("2025-06-10T15:00:00"))  # must not raise
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd live-grid && python3 -m pytest tests/test_recorder.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.recorder'`

- [ ] **Step 3: Implement `recorder.py`**

Create `live-grid/app/recorder.py`:

```python
"""Buffering ticks for batched writes into kdb.

Per-tick IPC cannot keep up with a live feed, so ticks accumulate here and are
written as one batch per flush. The buffer is bounded: when q is unreachable an
unbounded buffer would grow until the process died, which is a worse failure
than losing ticks a cache was never promised to keep.
"""

import logging
from collections import deque
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


class TickRecorder:
    """A bounded tick buffer with batched writes and a rolling-window prune."""

    def __init__(self, store, max_buffer: int = 100_000, window: timedelta = timedelta(days=1)):
        self.store = store
        self.window = window
        self._buf: deque = deque(maxlen=max_buffer)
        self.written = 0
        self.dropped = 0

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def record(self, sym: str, price: float, size, stamp: datetime) -> None:
        """Append one tick. Oldest is dropped when the buffer is full."""
        if len(self._buf) == self._buf.maxlen:
            self.dropped += 1
        self._buf.append((stamp, sym, float(price), float(size) if size is not None else 0.0))

    def _frame(self):
        import pandas as pd

        rows = list(self._buf)
        return pd.DataFrame(rows, columns=["time", "sym", "price", "size"])

    def flush(self) -> int:
        """Write the buffer as one batch. Returns rows written."""
        if not self._buf:
            return 0
        frame = self._frame()
        try:
            written = self.store.write_ticks(frame)
        except Exception as exc:  # noqa: BLE001 - a cache write must not kill the feed
            log.warning("tick flush failed, dropping %d buffered ticks: %s", len(frame), exc)
            self._buf.clear()
            return 0
        self._buf.clear()
        self.written += written
        return written

    def prune(self, now: datetime | None = None) -> int:
        """Drop ticks older than the rolling window. Never raises."""
        cutoff = (now or datetime.now()) - self.window
        try:
            return self.store.prune_ticks(cutoff)
        except Exception as exc:  # noqa: BLE001
            log.warning("tick prune failed: %s", exc)
            return 0

    def stats(self) -> dict:
        """Counters for /health."""
        return {"buffered": self.buffered, "written": self.written, "dropped": self.dropped}
```

Note `flush()` clears the buffer even on failure — that is what keeps it bounded when q is down, and it is the behaviour `test_a_failed_flush_does_not_grow_the_buffer_without_bound` pins.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd live-grid && python3 -m pytest tests/test_recorder.py -q`
Expected: PASS, 8 passed

- [ ] **Step 5: Add the dependency**

In `live-grid/pyproject.toml`, add `"kdb-store"` and `"pandas"` to `dependencies`.

- [ ] **Step 6: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add live-grid/
git commit -m "feat(live-grid): bounded tick buffer with batched writes"
```

---

### Task 5: Wire the recorder into the feed

**Files:**
- Modify: `live-grid/app/quotes.py`
- Modify: `live-grid/app/feeds.py`
- Modify: `live-grid/app/main.py`
- Modify: `live-grid/tests/test_quotes.py`, `live-grid/tests/test_feeds.py`

**Interfaces:**
- Consumes: `TickRecorder` (Task 4).
- Produces: `QuoteTable(..., on_tick=None)` where `on_tick(sym, price, size, stamp)` is called for every tick that updates a row; `FeedManager` flushes and prunes on its existing cadence.

- [ ] **Step 1: Write the failing tests**

Append to `live-grid/tests/test_quotes.py`:

```python
def test_apply_tick_reports_the_tick_to_the_recorder():
    seen = []
    table = QuoteTable(on_tick=lambda *a: seen.append(a))
    table.apply_tick("us", {"s": "AAPL", "p": 100.5, "q": 3, "t": 1749565200000})
    assert len(seen) == 1
    sym, price, size, stamp = seen[0]
    assert (sym, price, size) == ("AAPL", 100.5, 3)


def test_forex_ticks_are_recorded_at_the_mid():
    seen = []
    table = QuoteTable(on_tick=lambda *a: seen.append(a))
    table.apply_tick("forex", {"s": "EURUSD", "b": 1.08, "a": 1.10, "t": 1749565200000})
    assert seen[0][1] == pytest.approx(1.09)


def test_a_rejected_tick_is_not_recorded():
    seen = []
    table = QuoteTable(on_tick=lambda *a: seen.append(a))
    table.apply_tick("us", {"s": "AAPL"})  # no price
    assert seen == []


def test_a_failing_recorder_never_breaks_the_grid():
    """Episode 8's feature must survive anything the cache does."""
    def boom(*_):
        raise RuntimeError("kdb exploded")

    table = QuoteTable(on_tick=boom)
    assert table.apply_tick("us", {"s": "AAPL", "p": 100.5, "q": 3}) == "AAPL"
    assert table.rows["AAPL"]["price"] == 100.5
```

Ensure `import pytest` is present in that file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd live-grid && python3 -m pytest tests/test_quotes.py -q`
Expected: FAIL — `TypeError: QuoteTable() got an unexpected keyword argument 'on_tick'`

- [ ] **Step 3: Add the hook to `QuoteTable`**

In `live-grid/app/quotes.py`, give `QuoteTable.__init__` an `on_tick=None` parameter stored as `self._on_tick`. Then at the end of `apply_tick`, immediately before `return sym`, add:

```python
        if self._on_tick is not None:
            try:
                self._on_tick(sym, price, size if feed != "forex" else None, stamp)
            except Exception:  # noqa: BLE001 - recording must never break the grid
                log.debug("tick recorder rejected a tick", exc_info=True)
```

`size` is only bound in the non-forex branch, so initialise `size = None` before the `if feed == "forex":` branch. Add a module-level `log = logging.getLogger(__name__)` if the file does not already have one.

- [ ] **Step 4: Flush and prune on the feed cadence**

In `live-grid/app/feeds.py`, give `FeedManager.__init__` a `recorder=None` parameter. In `run()`, after `self._drain_all()`, add:

```python
                if self.recorder is not None:
                    # to_thread keeps the event loop free; the recorder's store
                    # marshals the actual PyKX call onto its own owner thread.
                    await asyncio.to_thread(self.recorder.flush)
                    now = asyncio.get_running_loop().time()
                    if now - self._last_prune >= PRUNE_INTERVAL:
                        self._last_prune = now
                        await asyncio.to_thread(self.recorder.prune)
```

Initialise `self._last_prune = 0.0` in `__init__` and define `PRUNE_INTERVAL = 60.0` at module level.

**This is load-bearing:** `to_thread` runs the flush on a worker thread which then calls into `KdbSession.run`, so the PyKX call still happens only on the session's single owner thread while the event loop stays responsive.

- [ ] **Step 5: Construct the recorder in the app**

In `live-grid/app/main.py`'s `create_app`, build the recorder when charting is enabled, and pass it to both `QuoteTable` and `FeedManager`:

```python
    recorder = None
    if os.getenv("LIVE_GRID_CHART", "true").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from kdb_store.config import resolve_config
            from kdb_store.session import KdbSession
            from kdb_store.store import KdbStore

            from app.recorder import TickRecorder

            config = resolve_config()
            session = KdbSession(config)
            window = timedelta(seconds=int(os.getenv("LIVE_TICK_WINDOW_SECONDS", "86400")))
            recorder = TickRecorder(KdbStore(session), window=window)
        except Exception as exc:  # noqa: BLE001 - the grid works without kdb
            log.warning("tick recording disabled: %s", exc)
            recorder = None
```

Pass `on_tick=recorder.record if recorder else None` to `QuoteTable`, and `recorder=recorder` to `FeedManager`. Expose the counters on `/health` by merging `recorder.stats()` into its response under a `"ticks"` key (omit the key when `recorder is None`).

- [ ] **Step 6: Run the full live-grid suite**

Run: `cd live-grid && python3 -m pytest -q`
Expected: PASS — the existing tests plus the new ones. **Every pre-existing live-grid test must still pass**; they are Episode 8's protection.

- [ ] **Step 7: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add live-grid/
git commit -m "feat(live-grid): record ticks from the feed into kdb"
```

---

### Task 6: Cache the REST snapshots

**Files:**
- Modify: `kdb-store/kdb_store/store.py`
- Modify: `live-grid/app/quotes.py`
- Modify: `kdb-store/tests/test_store.py`, `live-grid/tests/test_quotes.py`

**Interfaces:**
- Produces on `KdbStore`: `read_snapshot(symbol, max_age) -> dict | None`, `write_snapshot(symbol, payload) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `kdb-store/tests/test_store.py`:

```python
def test_write_snapshot_stores_a_fetch_time():
    s, conn = store_with()
    s.write_snapshot("AAPL", {"close": 100.0, "volume": 10.0})
    joined = " ".join(conn.queries)
    assert "snap" in joined


def test_read_snapshot_returns_none_when_absent():
    s, _ = store_with({"select from snap": None})
    assert s.read_snapshot("NOPE", 60.0) is None
```

Append to `live-grid/tests/test_quotes.py`:

```python
def test_seed_reuses_a_fresh_cached_snapshot_instead_of_calling_the_vendor():
    """A debounced rebuild must not re-hit the vendor for a symbol we just fetched."""
    class Cache:
        def read_snapshot(self, symbol, max_age):
            return {"close": 100.0, "volume": 5.0}

        def write_snapshot(self, symbol, payload):
            raise AssertionError("should not write when the cache was fresh")

    calls = []

    class Client:
        def get_live_stock_prices(self, ticker):
            calls.append(ticker)
            return {"close": 1.0}

    table = QuoteTable(snapshots=Cache())
    table.seed(["AAPL"], Client())
    assert calls == []


def test_seed_falls_back_to_the_vendor_on_a_cache_miss():
    class Cache:
        def read_snapshot(self, symbol, max_age):
            return None

        def write_snapshot(self, symbol, payload):
            self.written = payload

    calls = []

    class Client:
        def get_live_stock_prices(self, ticker):
            calls.append(ticker)
            return {"close": 1.0}

    table = QuoteTable(snapshots=Cache())
    table.seed(["AAPL"], Client())
    assert len(calls) == 1


def test_seed_works_with_no_snapshot_cache_at_all():
    calls = []

    class Client:
        def get_live_stock_prices(self, ticker):
            calls.append(ticker)
            return {"close": 1.0}

    QuoteTable().seed(["AAPL"], Client())
    assert len(calls) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd kdb-store && python3 -m pytest tests/test_store.py -q -k snapshot` then `cd ../live-grid && python3 -m pytest tests/test_quotes.py -q -k snapshot`
Expected: FAIL — missing `write_snapshot` / unexpected `snapshots` argument.

- [ ] **Step 3: Add the snapshot table and accessors**

Extend `_INIT_SCHEMA` in `kdb-store/kdb_store/store.py` with:

```q
if[not `snap in key `.; snap: ([sym:`symbol$()] fetched:`timestamp$(); payload:())]
```

Add to `KdbStore`:

```python
    def write_snapshot(self, symbol: str, payload: dict) -> None:
        """Store a REST snapshot with its fetch time, for TTL reuse."""
        import json

        def write(conn):
            conn(
                "{[qwsym;qwpayload] snap:: snap upsert (qwsym; .z.p; qwpayload)}",
                _q_symbol(symbol),
                json.dumps(payload),
            )

        self.session.run(lambda: write(self.session.connection()))

    def read_snapshot(self, symbol: str, max_age: float) -> dict | None:
        """Return a snapshot fetched within `max_age` seconds, else None."""
        import json

        def read(conn):
            got = conn(
                "{[qwsym] select fetched, payload from snap where sym = qwsym}",
                _q_symbol(symbol),
            ).pd()
            if got is None or got.empty:
                return None
            import pandas as pd

            fetched = got["fetched"].iloc[0]
            if pd.isna(fetched):
                return None
            age = (pd.Timestamp.now() - pd.Timestamp(fetched)).total_seconds()
            if age > max_age:
                return None
            raw = got["payload"].iloc[0]
            if isinstance(raw, bytes):
                raw = raw.decode()
            return json.loads(raw)

        return self.session.run(lambda: read(self.session.connection()))
```

Storing the payload as a JSON string keeps the q schema fixed, which avoids the per-batch dtype mismatch that `upsert` rejects.

- [ ] **Step 4: Use it in `seed`**

Give `QuoteTable.__init__` a `snapshots=None` parameter stored as `self._snapshots`, and a `SNAPSHOT_TTL = 60.0` module constant. In `seed`, before the `client.get_live_stock_prices(...)` call for each symbol:

```python
            snap = None
            if self._snapshots is not None:
                try:
                    snap = self._snapshots.read_snapshot(sym, SNAPSHOT_TTL)
                except Exception:  # noqa: BLE001 - the vendor call is the fallback
                    snap = None
            if snap is None:
                snap = client.get_live_stock_prices(ticker=snapshot_ticker(sym))
                if self._snapshots is not None and isinstance(snap, dict):
                    try:
                        self._snapshots.write_snapshot(sym, snap)
                    except Exception:  # noqa: BLE001
                        pass
```

Keep the existing body that reads fields off `snap` unchanged below this.

In `main.py`, pass `snapshots=KdbStore(session)` to `QuoteTable` when the recorder was built (reuse the same store instance).

- [ ] **Step 5: Run both suites**

```bash
cd kdb-store && python3 -m pytest -q
cd ../live-grid && python3 -m pytest -q
```
Expected: PASS in both.

- [ ] **Step 6: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add kdb-store/ live-grid/
git commit -m "feat: cache REST snapshots through kdb with a TTL"
```

---

### Task 7: The seam

Stitching tick-derived bars onto cached history. Pure logic, no I/O.

**Files:**
- Create: `live-grid/app/series.py`
- Create: `live-grid/tests/test_series.py`

**Interfaces:**
- Consumes: `bucket_ns` (Task 3).
- Produces:
  - `seam_boundary(first_tick: datetime, interval: str) -> datetime` — the first bar boundary at or after `first_tick`
  - `stitch(history: list[dict], ticks: list[dict], boundary: datetime) -> list[dict]`
  - `tick_capable(interval: str, window: timedelta) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `live-grid/tests/test_series.py`:

```python
"""The seam: where tick-derived bars meet cached history."""

from datetime import datetime, timedelta

import pytest

from app.series import seam_boundary, stitch, tick_capable

D = lambda s: datetime.fromisoformat(s)  # noqa: E731


def bar(stamp, close=1.0):
    return {"date": D(stamp), "open": close, "high": close, "low": close,
            "close": close, "volume": 1.0}


def test_boundary_rounds_up_to_the_next_bar():
    """A tick mid-bar cannot own that bar -- it is missing the bar's opening trades."""
    assert seam_boundary(D("2025-06-10T14:00:30"), "1m") == D("2025-06-10T14:01:00")


def test_boundary_of_a_tick_exactly_on_a_boundary_is_that_boundary():
    assert seam_boundary(D("2025-06-10T14:01:00"), "1m") == D("2025-06-10T14:01:00")


def test_boundary_for_five_minute_bars():
    assert seam_boundary(D("2025-06-10T14:02:10"), "5m") == D("2025-06-10T14:05:00")


def test_stitch_drops_history_at_or_after_the_boundary():
    """Otherwise the seam emits two bars for the same timestamp."""
    history = [bar("2025-06-10T13:58:00"), bar("2025-06-10T13:59:00"),
               bar("2025-06-10T14:01:00", close=9.0)]
    ticks = [bar("2025-06-10T14:01:00", close=5.0), bar("2025-06-10T14:02:00", close=6.0)]
    out = stitch(history, ticks, D("2025-06-10T14:01:00"))
    stamps = [r["date"] for r in out]
    assert stamps == sorted(stamps)
    assert len(stamps) == len(set(stamps)), "duplicate timestamps across the seam"
    assert [r["close"] for r in out if r["date"] == D("2025-06-10T14:01:00")] == [5.0]


def test_stitch_with_no_ticks_returns_history():
    history = [bar("2025-06-10T13:58:00")]
    assert stitch(history, [], D("2025-06-10T14:01:00")) == history


def test_stitch_with_no_history_returns_ticks():
    ticks = [bar("2025-06-10T14:01:00")]
    assert stitch([], ticks, D("2025-06-10T14:01:00")) == ticks


def test_stitch_output_is_time_ordered_even_if_inputs_are_not():
    history = [bar("2025-06-10T13:59:00"), bar("2025-06-10T13:58:00")]
    ticks = [bar("2025-06-10T14:02:00"), bar("2025-06-10T14:01:00")]
    out = stitch(history, ticks, D("2025-06-10T14:01:00"))
    assert [r["date"] for r in out] == sorted(r["date"] for r in out)


@pytest.mark.parametrize("interval,ok", [("1m", True), ("5m", True), ("1h", True),
                                         ("1d", False), ("1w", False)])
def test_tick_capable_rejects_intervals_wider_than_the_window(interval, ok):
    assert tick_capable(interval, timedelta(hours=6)) is ok
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd live-grid && python3 -m pytest tests/test_series.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.series'`

- [ ] **Step 3: Implement `series.py`**

Create `live-grid/app/series.py`:

```python
"""Joining tick-derived bars onto cached history.

The seam sits at the first bar boundary FULLY covered by ticks, not at the
first tick. A bar that ticks only partly cover would be missing its own opening
trades -- a wrong candle that looks entirely plausible on a chart -- so the
straddling bar comes from history and ticks own only what they cover whole.
"""

from datetime import datetime, timedelta

EPOCH = datetime(1970, 1, 1)


def seam_boundary(first_tick: datetime, interval: str) -> datetime:
    """The first bar boundary at or after `first_tick`."""
    from kdb_store.aggregate import bucket_ns

    width = timedelta(microseconds=bucket_ns(interval) / 1000)
    elapsed = first_tick - EPOCH
    buckets, remainder = divmod(elapsed, width)
    if remainder:
        buckets += 1
    return EPOCH + buckets * width


def tick_capable(interval: str, window: timedelta) -> bool:
    """True when a bar of this interval can fit inside the tick window."""
    from kdb_store.aggregate import bucket_ns

    try:
        width = timedelta(microseconds=bucket_ns(interval) / 1000)
    except ValueError:
        return False
    return width <= window


def stitch(history: list[dict], ticks: list[dict], boundary: datetime) -> list[dict]:
    """History strictly before `boundary`, then tick-derived bars, time-ordered."""
    if not ticks:
        return history
    kept = [row for row in history if row["date"] < boundary]
    if not kept:
        return sorted(ticks, key=lambda r: r["date"])
    return sorted(kept + list(ticks), key=lambda r: r["date"])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd live-grid && python3 -m pytest tests/test_series.py -q`
Expected: PASS, 12 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add live-grid/
git commit -m "feat(live-grid): seam computation and stitching"
```

---

### Task 8: Chart endpoints, and delete cache-chart

**Files:**
- Move: `cache-chart/app/{figure,openbb_client}.py` → `live-grid/app/`
- Move: `cache-chart/app/static/demo.html` → `live-grid/app/static/`
- Move: `cache-chart/tests/test_figure.py` → `live-grid/tests/`
- Modify: `live-grid/app/main.py`, `live-grid/widgets.json`, `live-grid/Dockerfile`
- Create: `live-grid/tests/test_chart_routes.py`
- Delete: `cache-chart/` entirely

**Interfaces:**
- Consumes: `stitch`, `seam_boundary`, `tick_capable` (Task 7); `aggregate_ticks` (Task 3); `fetch_series` (moved).
- Produces: `GET /chart`, `GET /series`, `GET /demo` on live-grid, and `build_series(...) -> tuple[list[dict], dict]`.

- [ ] **Step 1: Move the files**

```bash
cd /Users/artcashin/Developer/openbb-docker
mkdir -p live-grid/app/static
git mv cache-chart/app/figure.py         live-grid/app/figure.py
git mv cache-chart/app/openbb_client.py  live-grid/app/openbb_client.py
git mv cache-chart/app/static/demo.html  live-grid/app/static/demo.html
git mv cache-chart/tests/test_figure.py  live-grid/tests/test_figure.py
```

- [ ] **Step 2: Write the failing route tests**

Create `live-grid/tests/test_chart_routes.py`:

```python
"""Chart routes on live-grid, including the tick/history join."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

D = lambda s: datetime.fromisoformat(s)  # noqa: E731


def bar(stamp, close=1.0):
    return {"date": stamp, "open": close, "high": close, "low": close,
            "close": close, "volume": 1.0}


@pytest.fixture
def client(monkeypatch):
    async def fake_history(symbol, interval, start, end, provider="kdb"):
        return ([bar("2025-06-10T13:58:00"), bar("2025-06-10T13:59:00")],
                {"cache": "hit", "rows_from_cache": 2, "rows_from_upstream": 0,
                 "gaps_fetched": 0, "upstream_ms": 0.0, "kdb_ms": 1.0})

    monkeypatch.setattr("app.main.fetch_series", fake_history)
    return TestClient(create_app(api_key="test-key"))


def test_series_returns_bars_and_a_cache_block(client):
    body = client.get("/series", params={"symbol": "AAPL"}).json()
    assert "bars" in body and "cache" in body


def test_series_reports_rows_from_ticks(client):
    body = client.get("/series", params={"symbol": "AAPL"}).json()
    assert "rows_from_ticks" in body["cache"]


def test_chart_returns_plotly_figure_json(client):
    body = client.get("/chart", params={"symbol": "AAPL"}).json()
    assert "data" in body and "layout" in body


def test_demo_page_is_served(client):
    res = client.get("/demo")
    assert res.status_code == 200
    assert "plotly_relayout" in res.text


def test_live_grid_widget_is_still_registered(client):
    """Episode 8's widget must survive this change."""
    assert "live_grid" in client.get("/widgets.json").json()


def test_chart_widget_is_registered(client):
    assert any("chart" in key for key in client.get("/widgets.json").json())


def test_series_works_with_no_recorder(client):
    """No kdb: history only, and the route must not error."""
    body = client.get("/series", params={"symbol": "AAPL"}).json()
    assert body["cache"]["rows_from_ticks"] == 0
```

- [ ] **Step 3: Run to verify they fail**

Run: `cd live-grid && python3 -m pytest tests/test_chart_routes.py -q`
Expected: FAIL — 404s, since the routes do not exist.

- [ ] **Step 4: Implement `build_series` and the routes**

Add to `live-grid/app/main.py` (imports at the top, routes inside `create_app`):

```python
async def build_series(symbol, interval, start, end, recorder, window, provider="kdb"):
    """History joined to tick-derived bars at the first fully-covered bar."""
    from app.series import seam_boundary, stitch, tick_capable

    history, meta = await fetch_series(symbol, interval, start, end, provider)
    meta = dict(meta)
    meta["rows_from_ticks"] = 0
    meta["seam"] = None

    if recorder is None or not tick_capable(interval, window):
        return history, meta

    try:
        from kdb_store.aggregate import aggregate_ticks

        span = recorder.store.tick_span(symbol)
        if span is None:
            return history, meta
        boundary = seam_boundary(span[0], interval)
        ticks = await asyncio.to_thread(
            aggregate_ticks, recorder.store, symbol, interval, boundary, span[1]
        )
    except Exception as exc:  # noqa: BLE001 - the chart still works without ticks
        log.warning("tick aggregation unavailable for %s: %s", symbol, exc)
        return history, meta

    if not ticks:
        return history, meta
    meta["rows_from_ticks"] = len(ticks)
    meta["seam"] = boundary.isoformat()
    return stitch(history, ticks, boundary), meta
```

Then add the three routes inside `create_app`, alongside the existing ones:

```python
    _STATIC = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")

    def _window(start: str | None, end: str | None) -> tuple[str, str]:
        today = date.today()
        return (start or str(today - timedelta(days=365)), end or str(today))

    def _tick_window() -> timedelta:
        return recorder.window if recorder is not None else timedelta(0)

    @app.get("/series")
    async def series(symbol: str = "AAPL", interval: str = "1d",
                     start: str | None = None, end: str | None = None,
                     provider: str = "kdb"):
        s, e = _window(start, end)
        try:
            bars, meta = await build_series(
                symbol, interval, s, e, recorder, _tick_window(), provider
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("series failed for %s: %s", symbol, exc)
            return JSONResponse(
                {"symbol": symbol, "interval": interval, "start": s, "end": e, "bars": [],
                 "cache": {"cache": "error", "error": str(exc), "rows_from_cache": 0,
                           "rows_from_upstream": 0, "rows_from_ticks": 0,
                           "gaps_fetched": 0, "upstream_ms": 0.0, "kdb_ms": 0.0,
                           "seam": None}},
                status_code=502,
            )
        return {"symbol": symbol, "interval": interval, "start": s, "end": e,
                "bars": bars, "cache": meta}

    @app.get("/chart")
    async def chart(symbol: str = "AAPL", interval: str = "1d",
                    start: str | None = None, end: str | None = None,
                    provider: str = "kdb"):
        s, e = _window(start, end)
        try:
            bars, _ = await build_series(
                symbol, interval, s, e, recorder, _tick_window(), provider
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("chart failed for %s: %s", symbol, exc)
            return JSONResponse({"data": [], "layout": {"title": {"text": f"{symbol}: {exc}"}}},
                                status_code=502)
        return JSONResponse(build_figure(symbol, bars))

    @app.get("/demo", response_class=HTMLResponse)
    async def demo():
        return HTMLResponse((_STATIC / "demo.html").read_text())
```

Add the imports this needs at the top of `main.py`: `from datetime import date, timedelta`, `from pathlib import Path`, `from fastapi.responses import HTMLResponse, JSONResponse`, `from fastapi.staticfiles import StaticFiles`, `from app.figure import build_figure`, `from app.openbb_client import fetch_series`.

- [ ] **Step 5: Add the chart widget**

Add a second entry to `live-grid/widgets.json` alongside `live_grid`, following that file's existing shape, with `"type": "chart"`, `"endpoint": "chart"`, and `symbol` / `interval` params. **Do not modify the existing `live_grid` entry.**

- [ ] **Step 6: Vendor plotly into live-grid's image**

Add to `live-grid/Dockerfile`, before the `pip install`:

```dockerfile
# plotly.js is vendored: the page must render on a network with no CDN route.
ADD https://cdn.plot.ly/plotly-2.35.2.min.js /srv/app/static/plotly.min.js
```

and `RUN chmod 644 app/static/plotly.min.js` after the install. Copy `app/static/` in the `COPY app/ app/` step that already exists.

- [ ] **Step 7: Delete cache-chart**

```bash
cd /Users/artcashin/Developer/openbb-docker
git rm -r cache-chart
```

Remove the `cache-chart` service block from `docker-compose.yml`, and remove the `:6906` entries from `ts-config/serve.json` (leave `serve-funnel.json` untouched — it never had them). Verify: `python3 -m json.tool ts-config/serve.json > /dev/null && echo OK`.

- [ ] **Step 8: Run the suite**

Run: `cd live-grid && python3 -m pytest -q`
Expected: PASS — chart routes, recorder, series, figure, plus every pre-existing live-grid test.

- [ ] **Step 9: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add -A
git commit -m "feat(live-grid): unified chart from ticks and cache; remove cache-chart"
```

---

### Task 9: Real-q verification, compose, docs and CI

**Files:**
- Create: `kdb-store/scripts/tick_check.py`
- Modify: `docker-compose.yml`, `README.md`, `live-grid/README.md`, `.github/workflows/ci.yml`
- Modify: `docs/kdb-cache-design.md`

- [ ] **Step 1: Write the real-q check**

Create `kdb-store/scripts/tick_check.py` in the style of `openbb-kdb/scripts/live_check.py`: connect a real session, write a batch of **deliberately out-of-order** ticks, aggregate them, and assert the OHLCV is correct — this is the property mocks cannot verify.

```python
"""Real-q check for tick recording and aggregation. Needs a licence; not in CI."""

import sys
from datetime import datetime, timedelta

import pandas as pd

from kdb_store.aggregate import aggregate_ticks
from kdb_store.config import resolve_config
from kdb_store.session import KdbSession
from kdb_store.store import KdbStore

failures = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")
    if not ok:
        failures.append(name)


session = KdbSession(resolve_config())
store = KdbStore(session)
base = datetime(2025, 6, 10, 14, 0, 0)

# Deliberately out of order: true open is 100 (at :05), true close is 101 (at :55).
ticks = pd.DataFrame({
    "time": [base + timedelta(seconds=55), base + timedelta(seconds=5),
             base + timedelta(seconds=30), base + timedelta(seconds=70)],
    "sym": ["TICKCHK"] * 4,
    "price": [101.0, 100.0, 103.0, 99.0],
    "size": [5.0, 10.0, 1.0, 7.0],
})
check("write_ticks", store.write_ticks(ticks) == 4)

bars = aggregate_ticks(store, "TICKCHK", "1m", base, base + timedelta(minutes=5))
check("two buckets", len(bars) == 2, f"got {len(bars)}")
if bars:
    first = bars[0]
    check("open is time-ordered, not insertion-ordered", first["open"] == 100.0,
          f"open={first['open']} (100.0 expected; 101.0 means the sort was dropped)")
    check("close is time-ordered", first["close"] == 101.0, f"close={first['close']}")
    check("high", first["high"] == 103.0)
    check("low", first["low"] == 100.0)
    check("volume", first["volume"] == 16.0)

span = store.tick_span("TICKCHK")
check("tick_span", span is not None and span[0] <= span[1])

store.prune_ticks(datetime(2030, 1, 1))
check("prune clears the window", store.tick_span("TICKCHK") is None)

session.close()
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
```

- [ ] **Step 2: Run it against a real q**

```bash
SP=/private/tmp/claude-501/-Users-artcashin-Developer-openbb-docker/36d337ee-f0b7-45ab-bd8c-206f7577b0bf/scratchpad
cd /Users/artcashin/Developer/openbb-docker && docker build -t openbb-local:10.0.0 .
docker run --rm -v "$SP/kx:/opt/kx-license:ro" -v /Users/artcashin/Developer/openbb-docker/kdb-store:/src:ro \
  -e QLIC=/opt/kx-license --entrypoint sh openbb-local:10.0.0 \
  -c 'cp -r /src /tmp/kdb-store && pip install -q /tmp/kdb-store && python /tmp/kdb-store/scripts/tick_check.py'
```

Expected: `FAILURES: none`. Copy the source to a writable path first — `pip install` against a read-only mount silently falls through to a stale install already in the image.

**If "open is time-ordered" fails, the `` `time xasc `` was dropped from the aggregation query.** That is the exact bug this check exists to catch.

- [ ] **Step 3: Update compose**

In `docker-compose.yml`: remove the `cache-chart` service; add `LIVE_GRID_CHART`, `LIVE_TICK_WINDOW_SECONDS`, `QHOME`, `QLIC` and the `./kdb-license:/opt/kx-license:ro` mount to the `live-grid` service, matching how `openbb-api` declares them. `live-grid` reaches q on `127.0.0.1:5000` through the shared network namespace, so it needs no new port.

- [ ] **Step 4: Build live-grid from the repo root so it can copy kdb-store**

A Dockerfile cannot `COPY ../kdb-store` — nothing outside the build context is reachable. Change live-grid's build context to the repo root in `docker-compose.yml`:

```yaml
    build:
      context: .
      dockerfile: live-grid/Dockerfile
```

Then rewrite the `COPY` paths in `live-grid/Dockerfile` to be repo-root-relative, and install `kdb-store` before the service itself:

```dockerfile
COPY kdb-store/ /srv/kdb-store/
RUN pip install /srv/kdb-store
COPY live-grid/pyproject.toml ./
COPY live-grid/app/ app/
COPY live-grid/widgets.json ./
RUN pip install .
```

Verify with a real build, not just a config parse:

```bash
cd /Users/artcashin/Developer/openbb-docker
docker compose config > /dev/null && echo "compose OK"
docker build -t live-grid:10.0.0 -f live-grid/Dockerfile .
docker run --rm --entrypoint sh live-grid:10.0.0 -c \
  'python -c "import kdb_store, app.recorder; print(\"imports OK\")" && ls -la app/static/plotly.min.js'
```

Expected: `imports OK` and a multi-megabyte `plotly.min.js` (a few hundred bytes means the CDN fetch returned an error page).

- [ ] **Step 5: Update the docs**

- `live-grid/README.md`: document the chart endpoints, tick recording, the rolling window, the seam, and — plainly — that tick-derived bars and vendor bars will not agree exactly, volume especially, because vendor bars are consolidated and adjusted while a websocket feed is the prints it received.
- `README.md`: update the v10.0.0 section — the cache, tick recording and the unified chart, with no `cache-chart`.
- `docs/kdb-cache-design.md`: mark the `cache-chart` sections superseded by `docs/tick-chart-design.md`.

- [ ] **Step 6: Update CI**

In `.github/workflows/ci.yml`: replace the `cache-chart` job with a `kdb-store` job, and make the `openbb-kdb` and `live-grid` jobs install `kdb-store` first (`pip install -e ./kdb-store`) since both now depend on it. Neither job may need a licence, key or network.

- [ ] **Step 7: Verify everything**

```bash
cd /Users/artcashin/Developer/openbb-docker
(cd kdb-store && python3 -m pytest -q)
(cd openbb-kdb && python3 -m pytest -q)
(cd live-grid && python3 -m pytest -q)
bash scripts/scrub-check.sh
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))" && echo "CI yaml OK"
python3 -m json.tool ts-config/serve.json > /dev/null && echo "serve.json OK"
docker compose config > /dev/null && echo "compose OK"
test ! -d cache-chart && echo "cache-chart removed"
```

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: ship tick recording and the unified chart (Ep. 10)"
```

---

## Verification

1. All three suites pass with no licence, key or network.
2. `kdb-store/scripts/tick_check.py` reports `FAILURES: none` against a real q — in particular that `open`/`close` are time-ordered, proving the `` `time xasc `` survived.
3. `live-grid`'s pre-existing tests all still pass — Episode 8 has not regressed.
4. With the chart flag off, or with no reachable q, `/live_grid` and `/live_grid_ws` behave exactly as before.
5. `/series` reports `rows_from_ticks` and a `seam` timestamp when ticks are present.
6. No duplicate timestamps across the seam, and the series is strictly time-ordered.
7. `cache-chart/` no longer exists; nothing references it.
8. `bash scripts/scrub-check.sh` passes.

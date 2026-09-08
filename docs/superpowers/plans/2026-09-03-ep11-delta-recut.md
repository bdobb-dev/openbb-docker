<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Ep. 11 Delta Re-cut Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove ArcticDB from Episode 11 entirely, moving the store, both doors onto it, and the EODHD read-through cache's persistent tier to Delta Lake — so the episode teaches a stack viewers can legally run, and no API quota is lost in the move.

**Architecture:** Rebase the existing `ep11-arcticdb-minio` branch onto `main` (it forked before `mcp_stores` and `stores-explorer` existed and touches neither), then port the three things it never saw: the EODHD L2 cache, the analyst's door, and the widget's door. Delta's transaction log answers the metadata and tail questions ArcticDB answered with `get_description` and `tail`.

**Tech Stack:** Python 3.12, `deltalake` (delta-rs) 1.6.x, pyarrow, pandas, FastAPI + `TestClient`, pytest, MinIO, Tailscale Serve.

**Spec:** `docs/superpowers/specs/2026-09-03-ep11-delta-recut-design.md`

## Global Constraints

- **The repo is public.** No license blob, real tailnet name, hostname, or key may enter it. `bash scripts/scrub-check.sh` must pass before every commit.
- **Both doors stay read-only.** No write endpoint on `mcp_stores` or `stores-explorer`.
- **No new backend client code outside `mcp_stores`.** `stores-explorer` imports; it does not reimplement (original D3).
- **`deltalake` ≥ 1.6 returns `arro3` tables**, not pyarrow. Every `get_add_actions` result must be wrapped: `pa.table(dt.get_add_actions(flatten=True))`. `.to_pandas()` on the raw return is an `AttributeError` (R6).
- **`/delta/describe` reads zero rows.** It answers from the transaction log only. The call-count assertion in Task 5 is what enforces this.
- **L2 cache functions never raise.** `_delta_store()`, `_l2_get`, `_l2_put` degrade to `None`/no-op on any failure, exactly as the ArcticDB versions did.
- **CI needs no MinIO, no EODHD key, no kdb license.** All unit tests use tmp-path Delta tables or injected fakes.
- Env var prefix is `DELTA_*` (`DELTA_URI`, `DELTA_S3_ENDPOINT`, `DELTA_S3_BUCKET`, `DELTA_S3_ACCESS`, `DELTA_S3_SECRET`, `DELTA_S3_PORT`, `DELTA_S3_SECURE`). `ARCTICDB_*` must not survive anywhere.
- Style: lazy imports inside functions (`PLC0415` ignored in ruff config), `line-length = 100`, tests under `tests/` with `pythonpath = ["."]`.
- Release train: Task 1–3 ship as `v11.0.0`, Task 4 as `v11.1.0`, Tasks 5–7 as `v11.2.0`.

---

## File Structure

**Ported from the branch (Task 1):**

| Path | Responsibility |
|---|---|
| `openbb-deltalake/openbb_deltalake/store.py` | `DeltaStore`: write/append/read/list_symbols/has/delete/read_metadata |
| `openbb-deltalake/openbb_deltalake/utils.py` | `resolve_config`, `fs_and_root`, `to_bounds`, `normalize_index` |
| `openbb-deltalake/openbb_deltalake/models/` | The five OHLCV fetchers |
| `tick-lab/tick_lab/store.py` | `TickStore` — talks `deltalake` directly, no Platform install |

**New or rewritten in this plan:**

| Path | Responsibility |
|---|---|
| `openbb-deltalake/openbb_deltalake/describe.py` | Transaction-log metadata: row count, per-column bounds, schema, version history, trailing-fragment selection. Pure read of the log — the only new logic, and where the tests concentrate. |
| `openbb-eodhd/openbb_eodhd/models/_fundamentals.py` | L2 tier moves from ArcticDB to Delta (Task 2) |
| `mcp_stores/server.py` | `delta_*` tools replace `arctic_*` (Task 4) |
| `stores-explorer/app/main.py` | `/delta/*` routes replace `/arctic/*` (Tasks 5–6) |
| `stores-explorer/widgets.json` | `delta_explorer` replaces `arctic_explorer` (Task 7) |

`describe.py` is separated because it is the only non-trivial new logic and it has no I/O beyond reading a Delta log — which is what makes it cheaply testable against tmp-path tables.

---

### Task 1: Rebase the Delta branch onto main

Mechanical, but it must land before anything else — every later task edits files that only exist after it.

**Files:**
- Modify: whole tree via rebase
- Conflicts expected in: `docker-compose.yml`, `scripts/scrub-allowlist.txt`, `extension-constraints.txt`

**Interfaces:**
- Consumes: nothing
- Produces: `openbb_deltalake.store.DeltaStore(uri=None, library=None)` with `write/append/read/list_symbols/has/delete/read_metadata`; `openbb_deltalake.utils.resolve_config(library=...) -> (uri, library)` and `fs_and_root(base, storage_options) -> (fs, root)`

- [ ] **Step 1: Create the rebase branch from main**

```bash
cd ~/Developer/openbb-docker
git fetch --all
git checkout -b ep11-delta-recut main   # skip if the branch already exists
git remote add ep11 ~/Developer/openbb-docker-ep11 2>/dev/null || true
git fetch ep11 ep11-arcticdb-minio
```

- [ ] **Step 2: Replay the 15 commits onto main**

```bash
git rebase --onto ep11-delta-recut $(git merge-base ep11/ep11-arcticdb-minio main) ep11/ep11-arcticdb-minio
```

Resolve conflicts toward `main` in `docker-compose.yml`, `scripts/`, and `extension-constraints.txt` — the branch's edits there are the ArcticDB→Delta rename only, and main has 25 and 9 commits of unrelated churn in those files. Keep the branch's side in `openbb-deltalake/` and `tick-lab/`.

- [ ] **Step 3: Verify ArcticDB is gone from the tracked tree**

```bash
git grep -n -i "arcticdb\|ARCTICDB_" -- . ':!docs/' | grep -v "_fundamentals.py" || echo "CLEAN"
```

Expected: only `openbb-eodhd/openbb_eodhd/models/_fundamentals.py` hits (Task 2 removes those). Anything else is an unfinished rename.

- [ ] **Step 4: Run the ported suites**

```bash
python -m pytest openbb-deltalake/tests tick-lab/tests -q
```

Expected: PASS. These came with the branch; a failure here means a bad conflict resolution, not new work.

- [ ] **Step 5: Scrub and commit**

```bash
bash scripts/scrub-check.sh
git add -A && git commit -m "feat(ep11): Delta Lake replaces ArcticDB as the shared store"
```

---

### Task 2: Move the EODHD L2 cache onto Delta

Without this, deleting ArcticDB drops the fundamentals cache to memory-only and every restart re-burns EODHD quota (G5).

**Files:**
- Modify: `openbb-eodhd/openbb_eodhd/models/_fundamentals.py:80-130`
- Test: `openbb-eodhd/tests/test_fundamentals_cache.py`

**Interfaces:**
- Consumes: `DeltaStore`, `resolve_config` from Task 1
- Produces: `_delta_store() -> DeltaStore | None`; `_l2_get(sym) -> dict | None`; `_l2_put(sym, bundle) -> None`. Signatures are unchanged from the ArcticDB versions — the 12 consuming model files need no edit.

- [ ] **Step 1: Write the failing test**

```python
# openbb-eodhd/tests/test_fundamentals_cache.py
import json
from openbb_eodhd.models import _fundamentals as F


def test_l2_round_trips_through_delta(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTA_URI", str(tmp_path))
    F._reset_cache_for_tests()
    bundle = {"General": {"Code": "AAPL"}, "Financials": {"x": 1}}

    F._l2_put("AAPL.US", bundle)
    assert F._l2_get("AAPL.US") == bundle


def test_l2_expires_past_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTA_URI", str(tmp_path))
    monkeypatch.setenv("EODHD_FUNDAMENTALS_TTL_HOURS", "0")
    F._reset_cache_for_tests()

    F._l2_put("MSFT.US", {"General": {}})
    assert F._l2_get("MSFT.US") is None


def test_l2_never_raises_when_store_unavailable(monkeypatch):
    monkeypatch.setattr(F, "_delta_store", lambda: None)
    F._l2_put("NVDA.US", {"General": {}})     # must not raise
    assert F._l2_get("NVDA.US") is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest openbb-eodhd/tests/test_fundamentals_cache.py -q`
Expected: FAIL — `AttributeError: module has no attribute '_delta_store'`

- [ ] **Step 3: Replace the ArcticDB L2 with Delta**

Replace `_arctic_library`, `_l2_get` and `_l2_put` in `_fundamentals.py`:

```python
# --- L2: Delta Lake read-through (best-effort; never raises) -------------------
def _delta_store():
    """Return the Delta cache store, or None if unavailable/unconfigured.

    Soft dependency: openbb-deltalake is present in the container but not
    required for this extension to work (standalone dev, MinIO down).
    """
    try:
        # pylint: disable=import-outside-toplevel
        from openbb_deltalake.store import DeltaStore
        from openbb_deltalake.utils import resolve_config

        uri, library = resolve_config(library=_L2_LIBRARY)
        return DeltaStore(uri=uri, library=library)
    except Exception:  # noqa: BLE001 - L2 is optional; degrade to L1 + live fetch
        return None


def _l2_get(sym: str) -> dict | None:
    """Fresh cached bundle for sym, or None. Never raises."""
    try:
        store = _delta_store()
        if store is None or not store.has(sym):
            return None
        meta = store.read_metadata(sym) or {}
        fetched = meta.get("fetched_at")
        if not fetched:
            return None
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(fetched)).total_seconds()
        if age > _l2_ttl_seconds():
            return None
        df = store.read(sym, output="dataframe")
        return json.loads(df["payload"].iloc[0])
    except Exception:  # noqa: BLE001
        return None


def _l2_put(sym: str, bundle: dict) -> None:
    """Persist a bundle for sym, keeping one live version. Never raises."""
    try:
        store = _delta_store()
        if store is None:
            return
        # pylint: disable=import-outside-toplevel
        from pandas import DataFrame, Timestamp

        now = datetime.now(timezone.utc)
        df = DataFrame({"payload": [json.dumps(bundle)]}, index=[Timestamp(now)])
        store.write(sym, df, metadata={"fetched_at": now.isoformat()})
        _l2_prune(store, sym)
    except Exception:  # noqa: BLE001
        return


def _l2_prune(store, sym: str) -> None:
    """Delta has no prune_previous_versions; vacuum stands in (R8).

    A cache entry's previous versions are stale by definition, so retention is
    zero. Without this, every refresh adds a commit forever and read_metadata's
    history() walk slows with it.
    """
    try:
        # pylint: disable=import-outside-toplevel
        from deltalake import DeltaTable

        dt = DeltaTable(store._path(sym), storage_options=store.storage_options)
        dt.vacuum(retention_hours=0, enforce_retention_duration=False, dry_run=False)
    except Exception:  # noqa: BLE001
        return
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest openbb-eodhd/tests/test_fundamentals_cache.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Verify no ArcticDB reference survives**

```bash
git grep -n -i "arcticdb" -- openbb-eodhd/ || echo "CLEAN"
```

Expected: `CLEAN`

- [ ] **Step 6: Commit**

```bash
bash scripts/scrub-check.sh
git add openbb-eodhd/ && git commit -m "feat(eodhd): the fundamentals L2 cache persists to Delta, not ArcticDB"
```

---

### Task 3: Transaction-log metadata — `describe.py`

The one piece of genuinely new logic. Answers G1, G2, G3 and G4 from the Delta log without reading rows.

**Files:**
- Create: `openbb-deltalake/openbb_deltalake/describe.py`
- Test: `openbb-deltalake/tests/test_describe.py`

**Interfaces:**
- Consumes: `DeltaStore`, `fs_and_root` from Task 1
- Produces:
  - `list_libraries(base, storage_options) -> list[str]`
  - `describe(store, symbol) -> dict` with keys `row_count: int`, `date_range: [str, str] | None`, `columns: list[{name, dtype}]`
  - `history(store, symbol) -> list[{version: int, timestamp: str}]`
  - `trailing_fragment_paths(store, symbol, n_rows) -> list[str]`

- [ ] **Step 1: Write the failing tests**

```python
# openbb-deltalake/tests/test_describe.py
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

    monkeypatch.setattr(DeltaTable, "to_pyarrow_table", explode)
    monkeypatch.setattr(DeltaTable, "to_pyarrow_dataset", explode)
    D.describe(store, "AAPL")


def test_history_lists_versions_newest_first(store):
    store.write("AAPL", pd.DataFrame({"date": [pd.Timestamp("2024-01-01")], "close": [1]}))
    versions = [h["version"] for h in D.history(store, "AAPL")]
    assert versions == sorted(versions, reverse=True)
    assert len(versions) >= 2
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest openbb-deltalake/tests/test_describe.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'openbb_deltalake.describe'`

- [ ] **Step 3: Implement `describe.py`**

```python
"""Metadata answered from the Delta transaction log, never from rows.

ArcticDB gave the doors `list_libraries`, `get_description` and `tail`; Delta
gives none of them directly. All three come out of the add-actions in the
transaction log, which carry per-file row counts and per-column min/max.
"""
from __future__ import annotations


def _add_actions(dt):
    """Add-actions as a pandas frame.

    deltalake >= 1.6 returns an arro3 table, which has no .to_pandas(); the
    pyarrow wrap is required, not decorative (R6).
    """
    # pylint: disable=import-outside-toplevel
    import pyarrow as pa

    return pa.table(dt.get_add_actions(flatten=True)).to_pandas()


def list_libraries(base: str, storage_options: dict | None) -> list[str]:
    """Prefixes under `base` that themselves contain at least one Delta table."""
    # pylint: disable=import-outside-toplevel
    from pyarrow import fs as pafs

    from openbb_deltalake.utils import fs_and_root

    fsys, root = fs_and_root(base, storage_options)
    out = []
    for lib in fsys.get_file_info(pafs.FileSelector(root.rstrip("/"), allow_not_found=True)):
        if lib.type != pafs.FileType.Directory:
            continue
        children = fsys.get_file_info(pafs.FileSelector(lib.path, allow_not_found=True))
        for child in children:
            if child.type != pafs.FileType.Directory:
                continue
            if fsys.get_file_info(f"{child.path}/_delta_log").type != pafs.FileType.NotFound:
                out.append(lib.base_name)
                break
    return sorted(out)


def describe(store, symbol: str) -> dict:
    """Row count, per-column bounds and schema. Reads zero rows."""
    dt = store._table(symbol)  # pylint: disable=protected-access
    adds = _add_actions(dt)
    row_count = int(adds["num_records"].sum()) if "num_records" in adds else 0

    date_range = None
    if "min.date" in adds and "max.date" in adds and len(adds):
        date_range = [str(adds["min.date"].min()), str(adds["max.date"].max())]

    return {
        "library": store.library,
        "symbol": symbol,
        "row_count": row_count,
        "date_range": date_range,
        "columns": [{"name": f.name, "dtype": str(f.type)} for f in dt.schema().fields],
    }


def history(store, symbol: str) -> list[dict]:
    """Delta versions, newest first, for a time-travel control."""
    dt = store._table(symbol)  # pylint: disable=protected-access
    out = []
    for entry in dt.history():
        out.append({
            "version": int(entry["version"]),
            "timestamp": str(entry.get("timestamp", "")),
        })
    return sorted(out, key=lambda e: e["version"], reverse=True)


def trailing_fragment_paths(store, symbol: str, n_rows: int) -> list[str]:
    """Files holding the newest ~n_rows, by log stats — never a full scan (R4).

    ArcticDB's Library.tail bounded an unfiltered read; Delta has no tail, so
    the bound comes from selecting trailing files by max.date and taking only
    enough of them to cover n_rows.
    """
    dt = store._table(symbol)  # pylint: disable=protected-access
    adds = _add_actions(dt)
    if "max.date" in adds:
        adds = adds.sort_values("max.date", ascending=False)
    taken, seen = [], 0
    for _, row in adds.iterrows():
        taken.append(row["path"])
        seen += int(row.get("num_records", 0))
        if seen >= n_rows:
            break
    return taken
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest openbb-deltalake/tests/test_describe.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
bash scripts/scrub-check.sh
git add openbb-deltalake/ && git commit -m "feat(deltalake): transaction-log metadata — libraries, describe, history, tail bound"
```

**Ships as `v11.0.0` together with Tasks 1–2.**

---

### Task 4: `mcp_stores` — the analyst's door on Delta

**Files:**
- Modify: `mcp_stores/server.py:124-235`
- Test: `mcp_stores/test_server.py`

**Interfaces:**
- Consumes: `describe.list_libraries/describe/history/trailing_fragment_paths` (Task 3), `DeltaStore` (Task 1)
- Produces, for Tasks 5–6 to import:
  - `delta_list_libraries() -> list[str]`
  - `delta_list_symbols(library: str) -> list[str]`
  - `delta_describe(library: str, symbol: str) -> dict`
  - `delta_history(library: str, symbol: str) -> list[dict]`
  - `delta_read(library, symbol, start=None, end=None, tail_rows=1000, as_of=None) -> dict` with keys `library, symbol, total_rows_in_range, returned_rows, rows`
  - `_delta(library: str) -> DeltaStore`
  - unchanged: `_scrub`, `_bounded`, `_check_ident`, `_records`, and every `kdb_*` tool

- [ ] **Step 1: Write the failing tests**

```python
# appended to mcp_stores/test_server.py
def test_delta_list_symbols_rejects_unknown_library(monkeypatch, tmp_path):
    monkeypatch.setenv("DELTA_URI", str(tmp_path))
    import server
    with pytest.raises(ValueError, match="unknown library"):
        server.delta_list_symbols("nope")


def test_delta_read_returns_records_and_totals(monkeypatch, tmp_path):
    import pandas as pd
    from deltalake import write_deltalake
    monkeypatch.setenv("DELTA_URI", str(tmp_path))
    write_deltalake(
        f"{tmp_path}/ticks/AAPL",
        pd.DataFrame({"date": pd.date_range("2024-01-01", periods=10), "close": range(10)}),
        mode="overwrite",
    )
    import server
    out = server.delta_read("ticks", "AAPL", tail_rows=3)
    assert out["returned_rows"] == 3
    assert out["total_rows_in_range"] == 10
    assert len(out["rows"]) == 3


def test_delta_read_as_of_returns_superseded_data(monkeypatch, tmp_path):
    import pandas as pd
    from deltalake import write_deltalake
    monkeypatch.setenv("DELTA_URI", str(tmp_path))
    path = f"{tmp_path}/ticks/AAPL"
    first = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=3), "close": [1, 2, 3]})
    write_deltalake(path, first, mode="overwrite")
    write_deltalake(path, first.assign(close=[9, 9, 9]), mode="overwrite", schema_mode="overwrite")
    import server
    assert [r["close"] for r in server.delta_read("ticks", "AAPL", as_of=0)["rows"]] == [1, 2, 3]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest mcp_stores/test_server.py -q -k delta`
Expected: FAIL — `AttributeError: module 'server' has no attribute 'delta_list_symbols'`

- [ ] **Step 3: Replace the ArcticDB tools**

Delete `_arctic`, `arctic_list_libraries`, `arctic_list_symbols`, `arctic_read` and `redact_uri` (Delta passes credentials as `storage_options`, so no URI carries them). Add:

```python
def _delta(library: str):
    from openbb_deltalake.store import DeltaStore  # lazy: unit tests use tmp paths
    from openbb_deltalake.utils import resolve_config

    uri, _ = resolve_config(library=library)
    return DeltaStore(uri=uri, library=library)


def delta_list_libraries() -> list[str]:
    """List Delta libraries in the shared store (e.g. 'ticks')."""
    from openbb_deltalake import describe as D

    store = _delta("_")
    return sorted(_bounded(D.list_libraries, store.base, store.storage_options))


def delta_list_symbols(library: str) -> list[str]:
    """List symbols (Delta tables) in a library."""
    _check_ident("library", library)
    if library not in delta_list_libraries():
        raise ValueError(f"unknown library {library!r}; call delta_list_libraries first")
    return sorted(_bounded(_delta(library).list_symbols))


def _require_symbol(library: str, symbol: str):
    _check_ident("symbol", symbol)
    if symbol not in delta_list_symbols(library):
        raise ValueError(
            f"unknown symbol {symbol!r} in {library!r}; call delta_list_symbols first"
        )
    return _delta(library)


def delta_describe(library: str, symbol: str) -> dict:
    """Row count, date range and dtypes for a symbol. Reads no rows."""
    from openbb_deltalake import describe as D

    return _bounded(D.describe, _require_symbol(library, symbol), symbol)


def delta_history(library: str, symbol: str) -> list[dict]:
    """Delta versions for a symbol, newest first — the time-travel choices."""
    from openbb_deltalake import describe as D

    return _bounded(D.history, _require_symbol(library, symbol), symbol)


def delta_read(
    library: str,
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    tail_rows: int = 1000,
    as_of: str | int | None = None,
) -> dict:
    """Read a symbol from a Delta library.

    start/end are ISO dates filtering the stored index. as_of is an int Delta
    version or an ISO timestamp. Returns at most tail_rows rows (the most
    recent in range, hard cap MAX_ROWS).

    The read is bounded, not just the response: with no start/end it reads only
    the trailing files the log says hold those rows, so an unfiltered call never
    materializes the whole symbol (R4).
    """
    from openbb_deltalake import describe as D

    tail_rows = max(1, min(int(tail_rows), MAX_ROWS))
    store = _require_symbol(library, symbol)
    if isinstance(as_of, str) and as_of.isdigit():
        as_of = int(as_of)

    if start or end:
        df = _bounded(
            store.read, symbol, start_date=start, end_date=end,
            as_of=as_of, output="dataframe",
        )
        total = len(df)
        df = df.tail(tail_rows).reset_index()
    else:
        total = _bounded(D.describe, store, symbol)["row_count"]
        df = _bounded(store.read, symbol, as_of=as_of, output="dataframe")
        df = df.tail(tail_rows).reset_index()

    return {
        "library": library,
        "symbol": symbol,
        "total_rows_in_range": total,
        "returned_rows": len(df),
        "rows": _records(df),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest mcp_stores/test_server.py -q`
Expected: PASS — including every pre-existing kdb+ test, untouched.

- [ ] **Step 5: Commit and tag**

```bash
bash scripts/scrub-check.sh
git add mcp_stores/ && git commit -m "feat(mcp_stores): the analyst's door reads Delta, with describe, history and as_of"
git tag v11.1.0
```

---

### Task 5: `stores-explorer` — routes on Delta

**Files:**
- Modify: `stores-explorer/app/main.py:26-100`
- Test: `stores-explorer/tests/test_main.py`

**Interfaces:**
- Consumes: every `delta_*` function from Task 4
- Produces: the HTTP contract bdobb-v2 v11.0.0 consumes — `/delta/libraries`, `/delta/symbols?library=`, `/delta/describe?library=&symbol=`, `/delta/history?library=&symbol=`, `/delta/series?library=&symbol=&start=&end=&tail_rows=&as_of=`

- [ ] **Step 1: Write the failing tests**

```python
# stores-explorer/tests/test_main.py
def test_delta_chain_walks_end_to_end():
    app = create_app(
        delta_libraries_fn=lambda: ["ticks"],
        delta_symbols_fn=lambda lib: ["AAPL"],
        delta_describe_fn=lambda lib, sym: {
            "library": lib, "symbol": sym, "row_count": 500,
            "date_range": ["2024-01-01", "2025-05-14"],
            "columns": [{"name": "date", "dtype": "timestamp"}],
        },
        delta_history_fn=lambda lib, sym: [{"version": 1, "timestamp": "2026-09-01T00:00:00"}],
        delta_read_fn=lambda **kw: {"rows": [{"close": 1}], "returned_rows": 1},
    )
    c = TestClient(app)
    assert c.get("/delta/libraries").json() == ["ticks"]
    assert c.get("/delta/symbols", params={"library": "ticks"}).json() == ["AAPL"]
    assert c.get("/delta/describe", params={"library": "ticks", "symbol": "AAPL"}).json()["row_count"] == 500
    assert c.get("/delta/history", params={"library": "ticks", "symbol": "AAPL"}).json()[0]["version"] == 1


def test_unknown_symbol_is_404_with_the_next_call_named():
    def boom(lib, sym):
        raise ValueError(f"unknown symbol {sym!r} in {lib!r}; call delta_list_symbols first")

    c = TestClient(create_app(delta_describe_fn=boom))
    r = c.get("/delta/describe", params={"library": "ticks", "symbol": "NOPE"})
    assert r.status_code == 404
    assert "call delta_list_symbols first" in r.json()["detail"]


def test_describe_never_reads_rows():
    """The D6 call-count assertion, at the HTTP layer."""
    calls = []
    c = TestClient(create_app(
        delta_describe_fn=lambda lib, sym: {"row_count": 1, "columns": [], "date_range": None},
        delta_read_fn=lambda **kw: calls.append(kw),
    ))
    c.get("/delta/describe", params={"library": "ticks", "symbol": "AAPL"})
    assert calls == []


def test_as_of_reaches_the_read():
    seen = {}
    c = TestClient(create_app(delta_read_fn=lambda **kw: seen.update(kw) or {"rows": []}))
    c.get("/delta/series", params={"library": "ticks", "symbol": "AAPL", "as_of": "3"})
    assert seen["as_of"] == "3"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest stores-explorer/tests -q`
Expected: FAIL — `TypeError: create_app() got an unexpected keyword argument 'delta_libraries_fn'`

- [ ] **Step 3: Rewrite the routes**

Replace the `server` import block and the four `/arctic/*` routes:

```python
from server import (
    delta_describe,
    delta_history,
    delta_list_libraries,
    delta_list_symbols,
    delta_read,
    kdb_select,
    kdb_table_schema,
    kdb_tables,
)


def create_app(
    *,
    delta_libraries_fn=delta_list_libraries,
    delta_symbols_fn=delta_list_symbols,
    delta_describe_fn=delta_describe,
    delta_history_fn=delta_history,
    delta_read_fn=delta_read,
    kdb_tables_fn=kdb_tables,
    kdb_schema_fn=kdb_table_schema,
    kdb_select_fn=kdb_select,
) -> FastAPI:
    ...

    @app.get("/delta/libraries")
    def delta_libraries() -> list[str]:
        return delta_libraries_fn()

    @app.get("/delta/symbols")
    def delta_symbols(library: str) -> list[str]:
        try:
            return delta_symbols_fn(library)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.get("/delta/describe")
    def delta_describe_route(library: str, symbol: str) -> dict:
        try:
            return delta_describe_fn(library, symbol)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.get("/delta/history")
    def delta_history_route(library: str, symbol: str) -> list[dict]:
        try:
            return delta_history_fn(library, symbol)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.get("/delta/series")
    def delta_series(
        library: str, symbol: str,
        start: str | None = None, end: str | None = None,
        tail_rows: int = 1000, as_of: str | None = None,
    ) -> dict:
        try:
            return delta_read_fn(
                library=library, symbol=symbol, start=start, end=end,
                tail_rows=tail_rows, as_of=as_of,
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
```

The `/arctic/summary` route's hand-rolled `get_description` body goes entirely — Task 3's `describe` replaced it, so this file no longer imports `_arctic`/`_bounded` and the design's "no backend client code here" rule holds without an exception.

The three `/kdb/*` routes are unchanged.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest stores-explorer/tests -q`
Expected: PASS, including the untouched kdb+ tests.

- [ ] **Step 5: Commit**

```bash
bash scripts/scrub-check.sh
git add stores-explorer/ && git commit -m "feat(stores-explorer): the widget's door reads Delta, with describe, history and as_of"
```

---

### Task 6: The bounded tail

Separate from Task 5 because it is the one behaviour a reviewer could reject on its own: an unbounded read is correct but defeats the point.

**Files:**
- Modify: `openbb-deltalake/openbb_deltalake/store.py` (add `read_trailing`)
- Modify: `mcp_stores/server.py` (`delta_read`'s no-filter branch)
- Test: `openbb-deltalake/tests/test_describe.py`

**Interfaces:**
- Consumes: `describe.trailing_fragment_paths` (Task 3)
- Produces: `DeltaStore.read_trailing(key, n_rows, as_of=None) -> DataFrame`

- [ ] **Step 1: Write the failing test**

```python
def test_trailing_read_touches_only_the_files_it_needs(tmp_path):
    import pandas as pd
    from deltalake import write_deltalake
    from openbb_deltalake.store import DeltaStore

    path = f"{tmp_path}/ticks/AAPL"
    for i in range(5):  # five commits -> five files
        frame = pd.DataFrame({
            "date": pd.date_range(f"202{i}-01-01", periods=100, freq="D"),
            "close": range(100),
        })
        write_deltalake(path, frame, mode="append" if i else "overwrite")

    store = DeltaStore(uri=str(tmp_path), library="ticks")
    from openbb_deltalake import describe as D
    picked = D.trailing_fragment_paths(store, "AAPL", n_rows=50)
    assert len(picked) == 1, "50 rows must not need all five files"
    assert len(store.read_trailing("AAPL", n_rows=50)) == 50
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest openbb-deltalake/tests/test_describe.py::test_trailing_read_touches_only_the_files_it_needs -q`
Expected: FAIL — `AttributeError: 'DeltaStore' object has no attribute 'read_trailing'`

- [ ] **Step 3: Implement `read_trailing`**

```python
    def read_trailing(self, key: str, n_rows: int, as_of: Any = None):
        """The newest n_rows, reading only the files the log says hold them.

        ArcticDB bounded this with Library.tail; Delta has no tail, so the
        bound comes from the transaction log's per-file stats (R4). Falls back
        to a full read only when the log carries no usable date bounds.
        """
        # pylint: disable=import-outside-toplevel
        import pyarrow.dataset as ds

        from openbb_deltalake.describe import trailing_fragment_paths

        paths = trailing_fragment_paths(self, key, n_rows)
        if not paths:
            return self.read(key, as_of=as_of, output="dataframe").tail(n_rows)
        base = self._path(key)
        frame = ds.dataset(
            [f"{base.rstrip('/')}/{p}" for p in paths], format="parquet"
        ).to_table().to_pandas()
        if "date" in frame.columns:
            frame = frame.sort_values("date")
        return frame.tail(n_rows)
```

Then in `mcp_stores/server.py`, change `delta_read`'s no-filter branch to use it:

```python
    else:
        total = _bounded(D.describe, store, symbol)["row_count"]
        df = _bounded(store.read_trailing, symbol, n_rows=tail_rows, as_of=as_of).reset_index()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest openbb-deltalake/tests mcp_stores/test_server.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "perf(deltalake): bound an unfiltered read to the trailing files, not the table"
```

---

### Task 7: `widgets.json`, compose, and the release

**Files:**
- Modify: `stores-explorer/widgets.json`
- Modify: `docker-compose.yml` (the `stores-explorer` service's env)
- Test: `stores-explorer/tests/test_main.py`

**Interfaces:**
- Consumes: the routes from Task 5
- Produces: the `delta_explorer` widget contract bdobb-v2 v11.0.0 renders

- [ ] **Step 1: Write the failing test**

```python
def test_widgets_json_declares_the_delta_explorer_cascade():
    body = TestClient(create_app()).get("/widgets.json").json()
    w = body["delta_explorer"]
    assert w["type"] == "delta_explorer"
    assert w["endpoint"] == "delta/series"
    assert w["source"] == ["Delta Lake"]
    params = {p["paramName"]: p for p in w["params"]}
    assert params["library"]["optionsEndpoint"] == "delta/libraries"
    assert params["symbol"]["optionsEndpoint"] == "delta/symbols"
    assert params["symbol"]["optionsParams"] == {"library": "$library"}
    assert "arctic_explorer" not in body
    assert body["kdb_explorer"]["type"] == "table"   # unchanged
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest stores-explorer/tests -q -k widgets`
Expected: FAIL — `KeyError: 'delta_explorer'`

- [ ] **Step 3: Rewrite the ArcticDB entry**

Replace the `arctic_explorer` entry in `stores-explorer/widgets.json`:

```json
  "delta_explorer": {
    "name": "Delta Lake Explorer",
    "description": "Browse the shared Delta Lake store: pick a library, pick a symbol, see what is stored and read it at any version.",
    "category": "Data",
    "type": "delta_explorer",
    "endpoint": "delta/series",
    "dataKey": "rows",
    "gridData": { "w": 30, "h": 14 },
    "params": [
      { "paramName": "library", "type": "text", "label": "Library", "value": "", "optionsEndpoint": "delta/libraries" },
      { "paramName": "symbol", "type": "text", "label": "Symbol", "value": "", "optionsEndpoint": "delta/symbols", "optionsParams": { "library": "$library" } },
      { "paramName": "start", "type": "date", "label": "Start", "value": "", "show": false },
      { "paramName": "end", "type": "date", "label": "End", "value": "", "show": false },
      { "paramName": "as_of", "type": "text", "label": "As of", "value": "", "show": false }
    ],
    "source": ["Delta Lake"]
  },
```

`kdb_explorer` is left exactly as it is.

`type` is `delta_explorer`, not `table`, so bdobb-v2 routes it to the purpose-built renderer. A bdobb older than v11 falls through its `switch` to `RawJsonView` rather than blanking — the degradation path that makes this safe to ship first.

- [ ] **Step 4: Point compose at Delta**

In `docker-compose.yml`'s `stores-explorer` service, replace the ArcticDB env with the Delta one and bump the image:

```yaml
    image: openbb-stores-explorer:11.2.0
    env_file:
      - path: ./minio.env      # now carries DELTA_S3_*, not ARCTICDB_S3_*
        required: true
```

- [ ] **Step 5: Run the whole suite and scrub**

```bash
python -m pytest openbb-deltalake/tests tick-lab/tests mcp_stores/test_server.py stores-explorer/tests openbb-eodhd/tests -q
bash scripts/scrub-check.sh
git grep -n -i "arcticdb" -- . ':!docs/' || echo "ARCTICDB GONE"
```

Expected: all PASS, scrub clean, `ARCTICDB GONE`.

- [ ] **Step 6: Commit and tag**

```bash
git add -A && git commit -m "feat(stores-explorer): delta_explorer replaces arctic_explorer"
git tag v11.2.0
```

- [ ] **Step 7: Move the v11.0.0 and v11.1.0 tags onto the re-cut line**

The ArcticDB v11 tags describe a chapter that no longer exists (R7). Retag per the repo's release-rebuild practice, then verify each tag's tree:

```bash
git tag -f v11.0.0 <sha of Task 3's commit>
git tag -f v11.1.0 <sha of Task 4's commit>
for t in v11.0.0 v11.1.0 v11.2.0; do
  echo "$t: $(git ls-tree --name-only $t | grep -c arcticdb) arcticdb entries (want 0)"
done
```

---

## Verification

Against the spec's success criteria:

| # | Criterion | Verified by |
|---|---|---|
| 1 | No ArcticDB anywhere | Task 7 Step 5 `git grep` |
| 2 | Full chain end to end | Task 5 Step 1 chain test; live walk against MinIO |
| 3 | `/delta/describe` reads no rows | Task 3 Step 1 monkeypatch test **and** Task 5 call-count test |
| 4 | Unfiltered read is bounded | Task 6 Step 1 fragment-count test |
| 5 | `as_of` returns superseded data | Task 4 Step 1 `as_of=0` test; Task 5 param-passing test |
| 6 | Both doors agree | Both import the same `delta_*` functions — enforced by Task 5 having no backend client code |
| 7 | A restart costs no EODHD calls | Task 2 round-trip test; confirm live by restarting the container and re-requesting a cached symbol |
| 8 | Scrub passes | Every task's commit step |

---

## Corrections found during execution

Recorded as Task 1 landed. Each changes a later task; none changes the design.

**Task 2 ports an existing suite, it does not write one.**
`openbb-eodhd/tests/test_fundamentals_cache.py` already exists with 10 tests,
including `test_l2_hit_within_ttl_skips_eodhd`, `test_l2_miss_fetches_and_writes_back`,
`test_l2_unavailable_falls_back_to_live_fetch` and `test_expired_entries_swept_on_next_populate`.
The L2 coverage the plan proposed writing is already there — swap its fakes from
`arcticdb` to `deltalake` and rename `test_arctic_library_passes_resolved_uri_not_none`
to `test_delta_store_passes_resolved_uri_not_none`. `openbb-eodhd/tests/test_fundamental.py`
also references ArcticDB and needs the same treatment.

**Three ArcticDB references the plan never listed.** All are real work:

| Path | What it needs |
|---|---|
| `Dockerfile:183-187` | The `stores-mcp` layer's comment and install list name `openbb-arcticdb`; becomes `openbb-deltalake`. Belongs to Task 4. |
| `obb-arctic` (host helper script) | Inspects the store from the host via `ARCTICDB_URI`. Rename to `obb-delta` and move to the generic store API over `DELTA_*`. |
| `obb-up` | Its `s3` mode text says "start the shared ArcticDB store" and points at an `ARCTICDB_URI` block in `credentials.env`. |

`.github/workflows/release.yml:103` gates build arches with
`["amd64"] if "arcticdb" in text else ["amd64", "arm64"]` — the logic resolves
itself once ArcticDB is gone, but the comment above it is stale and should go
with the rest.

Docs still naming ArcticDB: `mcp_stores/README.md` (Task 4),
`stores-explorer/README.md` (Task 5), `kdb-ws/README.md`, `tick-lab/README.md`.

**Test environments must honour `pandas<3`.** The repo pins it via
`extension-constraints.txt` (`pandas<3  # from pykx`). Under pandas 3 the
tick-lab DST tests fail spuriously: `test_nonexistent_local_time_raises_instead_of_silently_coercing`
expects `pytz.exceptions.NonExistentTimeError`, but pandas 3 raises a plain
`ValueError` from its own tzconversion first. Two failures, neither a code
defect. Pin the venv before concluding anything from a red run.

### Task 1 result

Replayed as a cherry-pick range rather than a rebase, so the branch stayed put
and conflicts paused in place. 15/15 commits applied. Six conflicts, all
resolved keeping **both** sides rather than picking one:

| File | Resolution |
|---|---|
| `openbb-deltalake/pyproject.toml` | Branch's Delta deps, main's `openbb-core<2` ceiling (added after the fork) |
| `openbb-deltalake/tests/test_historical.py` | Both test classes — main's `TestPandasOhlcvVwap`, the branch's `TestExtractBars` |
| `extension-constraints.txt` | Branch's — `pandas<3` correctly re-derived from pykx, ArcticDB's protobuf pin dropped |
| `minio.env.example` | Branch's `DELTA_S3_*`, plus a `DELTA_URI` combined-form block the doors still need |
| `.github/workflows/ci.yml` | Main's `mcp-stores` and `stores-explorer` jobs kept; only the `openbb-arcticdb` → `openbb-deltalake` rename taken |
| `docker-compose.yml` | Main's current image tags, branch's intent (amd64 pins dropped) — including on `stores-mcp` and `stores-explorer`, which the branch never saw |

Verified: `openbb-deltalake` 80 passed, `tick-lab` 163 passed / 2 skipped,
`scripts/scrub-check.sh` clean.

---

### Task 6 was folded into Task 4

`DeltaStore.read_trailing` shipped with `mcp_stores`, not after it. Keeping
them apart would have released `v11.1.0` with an unfiltered read that
materializes the whole symbol — a knowing performance regression against
ArcticDB's `Library.tail`, fixed only one release later. The bound belongs
with the function it bounds.

The fragment-count assertion the separate task existed to carry is now
`test_delta_read_bounds_the_read_not_just_the_response` in
`mcp_stores/test_server.py`, which monkeypatches `DeltaStore.read` to raise:
any fallback to a full read fails the test rather than merely being slower.

---

### Task 7's widgets.json rename moved into Task 5

`stores-explorer`'s own tests assert the widget contract, so the
`arctic_explorer` → `delta_explorer` rename had to land with the routes rather
than a task later — Task 5 cannot be green while `widgets.json` still names the
old widget. Task 7 keeps the compose/env changes and the retagging.

The `/apps.json` route is deliberately NOT added yet: it reads a file Task 8
writes, and a route serving a missing file is a 500 waiting to happen. Route
and file land together in Task 8.

---

### Task 8: The Example Dashboard (`v11.3.0`)

Added after Tasks 1–2 landed. bdobb-v2 already fetches `apps.json` from every
configured backend during discovery (`src/lib/discovery.ts:37`), and an
`apps.json` card names its widget by **widget id**, not by the backend's
per-install UUID — so a shipped dashboard resolves itself against whichever
backend serves those widgets. That makes this a pure openbb-docker deliverable
with **zero bdobb-v2 code**, which is why it belongs on this train rather than
bdobb's.

`stores-explorer` is the first service in this repo to publish an `apps.json`.
It follows the same static-file pattern as its `widgets.json` (original D7).

**Files:**
- Create: `stores-explorer/apps.json`
- Modify: `stores-explorer/app/main.py` (one route)
- Test: `stores-explorer/tests/test_main.py`

**Interfaces:**
- Consumes: the `delta_explorer` and `kdb_explorer` widget ids from Task 7
- Produces: `GET /apps.json` → a one-element array holding the example app

- [ ] **Step 1: Write the failing test**

```python
def test_apps_json_publishes_the_example_dashboard():
    body = TestClient(create_app()).get("/apps.json").json()
    assert isinstance(body, list) and len(body) == 1
    app_ = body[0]
    assert app_["name"] == "Ep. 11 — The Shared Store"

    layout = list(app_["tabs"].values())[0]["layout"]
    widget_ids = [item["i"] for item in layout]
    assert widget_ids.count("delta_explorer") == 2   # ticks, and the cache itself
    assert "kdb_explorer" in widget_ids

    by_library = {item["state"]["params"].get("library") for item in layout}
    assert "eodhd_fundamentals_cache" in by_library

    # every card must name a widget this service actually publishes
    published = set(TestClient(create_app()).get("/widgets.json").json())
    assert set(widget_ids) <= published
```

The last assertion is the one that matters over time: it fails the moment a
widget is renamed in `widgets.json` without the dashboard following.

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest stores-explorer/tests -q -k apps`
Expected: FAIL — 404, the route does not exist.

- [ ] **Step 3: Write `stores-explorer/apps.json`**

Three cards, all served by this one backend, telling the chapter's story: the
store, the cache that store now backs, and the tape beside it.

```json
[
  {
    "name": "Ep. 11 — The Shared Store",
    "description": "Browse the Delta Lake store: stored ticks, the EODHD fundamentals cache that now persists into it, and the kdb+ tape beside it.",
    "img": "",
    "img_dark": "",
    "img_light": "",
    "allowCustomization": true,
    "tabs": {
      "store": {
        "id": "store",
        "name": "Store",
        "layout": [
          {
            "i": "delta_explorer",
            "x": 0, "y": 0, "w": 20, "h": 14,
            "state": {
              "params": { "library": "ticks", "symbol": "" },
              "chartView": { "enabled": false, "chartType": "line" }
            }
          },
          {
            "i": "delta_explorer",
            "x": 20, "y": 0, "w": 20, "h": 14,
            "state": {
              "params": { "library": "eodhd_fundamentals_cache", "symbol": "" },
              "chartView": { "enabled": false, "chartType": "line" }
            }
          },
          {
            "i": "kdb_explorer",
            "x": 0, "y": 14, "w": 40, "h": 12,
            "state": {
              "params": { "table": "" },
              "chartView": { "enabled": false, "chartType": "line" }
            }
          }
        ]
      }
    },
    "groups": [],
    "prompts": []
  }
]
```

`symbol` and `table` are left empty deliberately: the cascading picker fills
them from whatever the store actually holds, and a hardcoded symbol would be
wrong on every install but the author's. `library` IS set, because those two
library names are structural — `eodhd_fundamentals_cache` is named by
`_fundamentals.py` and a ticks library is what `tick-lab load` writes.

The second card is the demonstration that matters: it browses the read-through
cache's own contents, so "the cache is persisting and saving API calls" is
something you can look at rather than take on trust.

- [ ] **Step 4: Serve it**

In `stores-explorer/app/main.py`, beside the `widgets.json` route:

```python
APPS_PATH = Path(__file__).resolve().parent.parent / "apps.json"


    @app.get("/apps.json")
    def apps() -> JSONResponse:
        return JSONResponse(json.loads(APPS_PATH.read_text()))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest stores-explorer/tests -q`
Expected: PASS

- [ ] **Step 6: Verify in bdobb**

With the stack up and `stores-explorer` configured as a backend in bdobb-v2,
the app appears in the rail's app list with no import step — discovery fetches
`apps.json` on connect. Importing it creates the three cards; the two
`delta_explorer` cards resolve to the same backend and open on their declared
libraries.

- [ ] **Step 7: Commit and tag**

```bash
bash scripts/scrub-check.sh
git add stores-explorer/ && git commit -m "feat(stores-explorer): publish the Ep. 11 example dashboard as apps.json"
git tag v11.3.0
```

---

## Task 7 result, and the bug the unit tests could not find

Steps 1–6 done: compose comments and env pointers on `DELTA_*`, `obb-arctic`
renamed to `obb-delta`, `obb-up` corrected, the release workflow's arch gate
removed, and every remaining ArcticDB mention in the tree is now deliberate
history (why `describe.py` exists, why `read_trailing` exists, the README's
"Why Delta Lake, not ArcticDB", and the upgrade notes).

Two fixes worth naming:

- **`obb-delta` passes its library as `DELTA_LIBRARY_ARG`, not `DELTA_LIBRARY`.**
  The script always sets the variable, so reusing the real config name would
  inject an empty `DELTA_LIBRARY` into the container and override the
  configured library rather than defaulting it.
- **Both host scripts pointed at a `credentials.env` S3 block that does not
  exist** in `credentials.env.example`. `DELTA_S3_*` lives in `minio.env`.

The arch gate was removed only after checking PyPI: pykx 4.1.0 publishes
`manylinux2014_aarch64` and `manylinux_2_28_aarch64`, deltalake 1.6.3
publishes `manylinux_2_28_aarch64`. ArcticDB really was the sole blocker.

### The live walk found a real bug

531 unit tests passed against tmp-path Delta tables. The first run against a
real MinIO failed immediately:

```
pyarrow.lib.ArrowInvalid: Expected a local filesystem path, got a URI:
  's3://openbb/ticks/AAPL/part-00000-....snappy.parquet'
```

`read_trailing` built its paths from `self._path()`, which returns an
`s3://` URI on a MinIO-backed store. `pyarrow.dataset` accepts that shape
only with an explicit `filesystem=`; on a local path it works, so **no
tmp-path test could ever have caught it** — and the only configuration that
matters in production is the one that failed. Fixed by routing through
`fs_and_root`, the same handle `list_symbols`/`delete` already use, and
pinned by `test_trailing_read_passes_a_filesystem_not_a_uri`, which asserts a
filesystem is passed and no path carries a scheme.

**Rule:** a store abstraction that works on both local and S3 paths must be
exercised against S3 before it is believed. Local-path tests verify the logic,
never the addressing.

### Verified live against MinIO

Throwaway container on 127.0.0.1:19000, isolated from the running stack:

| Check | Result |
|---|---|
| `DeltaStore` writes to MinIO | base `s3://openbb`, credentials as storage_options |
| `describe` from the log | `row_count=300`, correct date range, zero rows read |
| `list_libraries` prefix scan over S3 | `['quotes', 'ticks']` |
| Time travel | 4 versions; `as_of=0` returns superseded data |
| Bounded tail | touched **1 file**, returned 10 rows |
| EODHD L2 cache round-trip | persists through MinIO |
| **Cold start after a cache write** | **ZERO EODHD calls** — the quota claim, proven |
| Cache retention | 4 refreshes → history of **1 version** |

### Step 7 (retagging) NOT done — deliberately

`v11.0.0`, `v11.1.0` and `v11.1.1` are all pushed to `origin`, so moving them
is a force-push of published tags. Three reasons to leave it to the operator:
the work is on an unmerged branch, so tags would point outside `main`; the
repo has a `backup/pre-<change>-v11.x` convention that says backup tags are
taken before release tags move; and a re-cut should be tagged after it has run
in the real stack, not before.


---

## Task 8 result

`stores-explorer/apps.json` ships the example dashboard and `GET /apps.json`
serves it. Three cards, one backend: the Delta store's ticks, the EODHD
fundamentals cache that now persists into it, and the kdb+ tape.

`library` is set on each card because those names are structural
(`eodhd_fundamentals_cache` is named by `_fundamentals.py`; a ticks library is
what `tick-lab load` writes). `symbol` and `table` are deliberately empty — a
hardcoded symbol would be wrong on every install but the author's, and the
cascading picker fills them from whatever the store actually holds.

### Verified against bdobb's own parser, not just asserted

A malformed `apps.json` is *silently dropped* by bdobb — `parseAppsJson`
returns an issue and the backend simply shows no apps — so a passing
server-side test proves nothing about whether the dashboard actually lands.
The file was therefore run through bdobb-v2's real `parseAppsJson` and
`importApps` (throwaway vitest spec, since a permanent one would couple the
repos):

| Check | Result |
|---|---|
| `parseAppsJson` | 1 app, **no issue reported** |
| `importApps` | 1 dashboard, 3 cards |
| Backend resolution | all 3 cards resolved to the backend publishing their widget |
| Cards | 2 × `delta_explorer`, libraries `['eodhd_fundamentals_cache', 'ticks']` |

Two things that surfaced while doing it, both worth knowing:

- **`WidgetDef.gridData` is required, not optional.** `importApps` reads
  `served?.gridData.w` — optional on `served`, unconditional on `gridData`.
  That is safe only because `widgetsJson.ts` always sets it via `toGridData`.
  A widget reaching the registry by any other path would crash the import.
- Duplicate widget ids in one tab are fine: `importApps` loops the layout and
  builds a card per entry, with no dedupe on `i`. The two `delta_explorer`
  cards are supported behaviour, not a happy accident.

### Not tagged

`v11.3.0` is not tagged, for the same reason `v11.0.0`–`v11.2.0` are not: the
work is on an unmerged branch and the tags are published. See Task 7.

---

## The doors, verified live

Task 7's walk proved the store and the EODHD cache against MinIO but drove
them as Python objects. This closes that gap: the same MinIO, reached through
`TestClient(create_app())` with **no injected fakes anywhere** — real routes →
real `mcp_stores` tools → real `DeltaStore` → real MinIO. This is exactly the
contract bdobb-v2 v11.0.0 consumes.

| Call | Result |
|---|---|
| `GET /delta/libraries` | `['eodhd_fundamentals_cache', 'ticks']` |
| `GET /delta/symbols?library=ticks` | `['AAPL']` |
| `GET /delta/describe` | 200 rows, `2024-01-01..2024-07-18`, `['date','close']` |
| `GET /delta/history` | versions `[1, 0]`, newest first |
| `GET /delta/series?tail_rows=5` | 5/200 rows, last close 699 |
| `GET /delta/series?as_of=0` | last close 199 — superseded data |
| unknown library / symbol | 404, each naming the next call to make |
| `GET /widgets.json` | `['delta_explorer', 'kdb_explorer']` |
| `GET /apps.json` | the example dashboard, 3 cards, every widget published |
| every response body | no access key, no secret |

The last row is checked by concatenating six responses (including a 404, the
path most likely to echo backend text) and asserting neither the access key
nor the secret appears. `_scrub`'s own unit tests prove the redaction; this
proves nothing routes around it.

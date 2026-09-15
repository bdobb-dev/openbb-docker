<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Delta explorer date range — backend (openbb-docker 11.6.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One Delta symbol reads across its per-day tables (`AAPL_2026_09_11`, `AAPL_2026_09_12`, …) through both doors, with describe, history and as-of following.

**Architecture:** A pure key-grammar module (`mcp_stores/daykeys.py`) says which raw table keys a base symbol names inside a window. The four Delta tools in `mcp_stores/server.py` call it and loop over the keys; stores-explorer's routes wrap those tools one-to-one and change only in their tests and manifest text.

**Tech Stack:** Python 3.12, deltalake 1.6.3 via `openbb_deltalake`, pandas, FastMCP, FastAPI, pytest. Tests run with `uv run --with fastmcp --with pytest --with pandas --with ./openbb-deltalake python -m pytest mcp_stores/test_server.py -q` from the repo root (verified green, 27 tests, before this plan). The stores-explorer suite: `cd stores-explorer && uv run --no-project --python 3.12 --with ../mcp_stores --with ../openbb-deltalake --with pytest --with fastapi --with httpx --with fastmcp --with pandas python -m pytest tests -q` (25 tests, green).

**Spec:** `docs/superpowers/specs/2026-09-15-ep11-delta-explorer-date-range-design.md` (a copy of bdobb-v2's `docs/superpowers/specs/2026-09-15-v11.1.0-delta-explorer-date-range-design.md`, which is canonical). The frontend plan is bdobb-v2's `docs/superpowers/plans/2026-09-15-v11.1.0-delta-explorer-date-range.md`; it consumes the response shapes this plan produces.

## Global Constraints

- Branch `claude/delta-explorer-date-range` from `origin/main` (4361329). The local `main` ref was behind origin when this plan was written; do not branch from it.
- Every first-party file carries the two-line Apache-2.0 header (see any existing `.py`).
- Read-only tools. Nothing here writes to a store outside a test's `tmp_path`.
- Keep `MAX_ROWS = 10_000` and the `delta_read` response keys `library, symbol, total_rows_in_range, returned_rows, rows`.
- Every deltalake call goes through `_bounded` (timeout + credential scrub), as today.
- Line length 100 (`ruff`). Comments explain *why*; this repo keeps long explanatory comments — never trim them.
- Wire times are naive UTC or ISO with `Z`; the frontend never sends an offset.
- Versions: `mcp_stores/pyproject.toml` and `stores-explorer/pyproject.toml` to `11.6.0`; compose image tag `openbb-stores-explorer:11.6.0`. No tag is cut by this plan — merge and tag are the user's.

---

### Task 1: The key grammar — `mcp_stores/daykeys.py`

**Files:**
- Create: `mcp_stores/daykeys.py`
- Create: `mcp_stores/test_daykeys.py`

**Interfaces:**
- Produces:
  - `split(key: str) -> tuple[str, date | None]`
  - `bases(keys: Iterable[str]) -> list[str]`
  - `day_keys(keys: Iterable[str], base: str) -> list[str]`
  - `in_window(keys: Iterable[str], base: str, start, end) -> list[str]` where `start`/`end` are `None`, `""`, a `date`, a `datetime`, or an ISO string.

- [ ] **Step 1: Write the failing tests**

```python
# mcp_stores/test_daykeys.py
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The per-day key grammar: `<base>_<YYYY_MM_DD>` is one symbol's one day.

Runs with the server tests:
    uv run --with fastmcp --with pytest --with pandas --with ./openbb-deltalake \
        python -m pytest mcp_stores -q
"""
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daykeys  # noqa: E402

LIVE = [
    "AAPL_2026_09_01", "AAPL_2026_09_02", "AAPL_2026_09_04",
    "MSFT_2026_09_01", "goog_quote_2023_05_12",
]


def test_split_reads_the_day_suffix():
    assert daykeys.split("AAPL_2026_09_11") == ("AAPL", date(2026, 9, 11))
    assert daykeys.split("goog_quote_2023_05_12") == ("goog_quote", date(2023, 5, 12))


def test_split_leaves_a_plain_key_alone():
    assert daykeys.split("AAPL") == ("AAPL", None)
    assert daykeys.split("AAPL.US") == ("AAPL.US", None)


def test_split_treats_an_impossible_day_as_part_of_the_name():
    # A suffix that looks like a day but is not one is a name, not a partition.
    assert daykeys.split("X_2026_13_40") == ("X_2026_13_40", None)


def test_bases_collapses_and_sorts():
    assert daykeys.bases(LIVE) == ["AAPL", "MSFT", "goog_quote"]
    assert daykeys.bases(["SPY", "AAPL"]) == ["AAPL", "SPY"]


def test_day_keys_lists_a_base_in_day_order():
    assert daykeys.day_keys(LIVE, "AAPL") == [
        "AAPL_2026_09_01", "AAPL_2026_09_02", "AAPL_2026_09_04",
    ]


def test_day_keys_of_a_plain_key_is_itself():
    assert daykeys.day_keys(["SPY", "AAPL_2026_09_01"], "SPY") == ["SPY"]


def test_day_keys_of_an_unknown_base_is_empty():
    assert daykeys.day_keys(LIVE, "NOPE") == []


def test_in_window_with_no_bounds_is_the_newest_day_only():
    # An unfiltered read stays bounded: one day, never the whole symbol.
    assert daykeys.in_window(LIVE, "AAPL", None, None) == ["AAPL_2026_09_04"]
    assert daykeys.in_window(LIVE, "AAPL", "", "") == ["AAPL_2026_09_04"]


def test_in_window_clips_by_calendar_day_from_timestamps():
    got = daykeys.in_window(LIVE, "AAPL", "2026-09-01 22:00:00", "2026-09-02 01:00:00")
    assert got == ["AAPL_2026_09_01", "AAPL_2026_09_02"]


def test_in_window_accepts_date_and_datetime_objects_and_open_sides():
    assert daykeys.in_window(LIVE, "AAPL", date(2026, 9, 2), None) == [
        "AAPL_2026_09_02", "AAPL_2026_09_04",
    ]
    assert daykeys.in_window(LIVE, "AAPL", None, datetime(2026, 9, 1, 12)) == ["AAPL_2026_09_01"]


def test_in_window_outside_every_day_is_empty():
    assert daykeys.in_window(LIVE, "AAPL", "2026-10-01", "2026-10-31") == []


def test_in_window_of_a_plain_key_is_itself_whatever_the_bounds():
    assert daykeys.in_window(["SPY"], "SPY", "2000-01-01", "2000-01-02") == ["SPY"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with fastmcp --with pytest --with pandas --with ./openbb-deltalake python -m pytest mcp_stores/test_daykeys.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'daykeys'`

- [ ] **Step 3: Write the module**

```python
# mcp_stores/daykeys.py
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The per-day key grammar, in one place and with no I/O.

Two libraries key their Delta tables by symbol AND day: the EOD dump writes
`ticks_live/AAPL_2026_09_11` (one UTC day per table, re-runnable per day), and
the FirstRate sample is `ticks/goog_quote_2023_05_12`. Everything else is one
table per symbol. The doors present the base symbol (`AAPL`) and expand it to
the day tables a window needs, so a symbol picker shows a symbol once and a
read can cross midnight.

A base that is itself a table (`ticks_sip/AAPL`) expands to itself, whatever
the bounds: the read path filters inside the table, as it always has.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterable

_DAY_SUFFIX = re.compile(r"\A(.+)_(\d{4})_(\d{2})_(\d{2})\Z")


def split(key: str) -> tuple[str, date | None]:
    """(base, day) for a day-keyed table; (key, None) for anything else."""
    m = _DAY_SUFFIX.match(key)
    if not m:
        return key, None
    try:
        return m.group(1), date(int(m.group(2)), int(m.group(3)), int(m.group(4)))
    except ValueError:  # X_2026_13_40 is a name that happens to end in digits
        return key, None


def bases(keys: Iterable[str]) -> list[str]:
    """The symbols a library offers: day suffixes collapsed, sorted, unique."""
    return sorted({split(k)[0] for k in keys})


def _days_of(keys: Iterable[str], base: str) -> list[tuple[date, str]]:
    out = []
    for k in keys:
        b, d = split(k)
        if b == base and d is not None:
            out.append((d, k))
    return sorted(out)


def day_keys(keys: Iterable[str], base: str) -> list[str]:
    """Every table behind `base`, oldest day first. A plain key is itself."""
    keys = list(keys)
    if base in keys:
        return [base]
    return [k for _, k in _days_of(keys, base)]


def _day(bound) -> date | None:
    """The calendar day a bound falls on, or None for an open side.

    Bounds arrive as naive UTC text from the widget; day tables are UTC days
    (the EOD dump flushes one UTC day), so the date of the text IS the table.
    """
    if bound is None or bound == "":
        return None
    if isinstance(bound, datetime):
        return bound.date()
    if isinstance(bound, date):
        return bound
    # Lazy: the tests of this module import nothing from the store package.
    from openbb_deltalake.utils import parse_temporal  # noqa: PLC0415

    parsed = parse_temporal(str(bound))
    return parsed.date() if isinstance(parsed, datetime) else parsed


def in_window(keys: Iterable[str], base: str, start, end) -> list[str]:
    """The tables a read of `base` between `start` and `end` must open.

    No bounds means the newest day only: an unfiltered read of a symbol is
    bounded by construction, which is the guarantee delta_read makes.
    """
    keys = list(keys)
    if base in keys:
        return [base]
    days = _days_of(keys, base)
    if not days:
        return []
    lo, hi = _day(start), _day(end)
    if lo is None and hi is None:
        return [days[-1][1]]
    return [k for d, k in days if (lo is None or d >= lo) and (hi is None or d <= hi)]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --with fastmcp --with pytest --with pandas --with ./openbb-deltalake python -m pytest mcp_stores/test_daykeys.py -q`
Expected: `12 passed`

- [ ] **Step 5: Lint and commit**

```bash
uv run --with ruff==0.15.22 ruff check mcp_stores
git add mcp_stores/daykeys.py mcp_stores/test_daykeys.py
git commit -m "feat(mcp_stores): the per-day key grammar — base symbols, day tables, windows"
```

---

### Task 2: Symbols collapse to bases; a base is a known symbol

**Files:**
- Modify: `mcp_stores/server.py` — `delta_list_symbols` (line ~187), `_require_symbol` (line ~197)
- Modify: `mcp_stores/test_server.py` — the `delta_store` fixture (line ~61) and `test_delta_list_libraries_sorted`

**Interfaces:**
- Consumes: `daykeys.bases`, `daykeys.day_keys` (Task 1).
- Produces: `_raw_keys(library) -> list[str]` (module-private, sorted raw table keys) and `_require_symbol(library, symbol) -> tuple[DeltaStore, list[str]]` — the store AND the raw key list, so the tools that follow expand without a second listing. `delta_list_symbols(library) -> list[str]` now returns bases.

- [ ] **Step 1: Extend the fixture and write the failing tests**

In `mcp_stores/test_server.py`, replace the `delta_store` fixture body so it also writes a day-keyed library. Keep the existing `ticks/AAPL` and `hrp_prices/SPY` writes and append:

```python
    # A day-keyed library, the EOD dump's shape: one table per (symbol, day),
    # five one-minute rows from midnight UTC of each day. 09-03 is missing on
    # purpose -- a window may span a day the dump never wrote.
    for day in ("2026_09_01", "2026_09_02", "2026_09_04"):
        d0 = pd.Timestamp(day.replace("_", "-"))
        write_deltalake(
            f"{tmp_path}/ticks_live/AAPL_{day}",
            pd.DataFrame({
                "date": pd.date_range(d0, periods=5, freq="1min"),
                "price": [float(day[-2:])] * 5,
            }),
            mode="overwrite",
        )
    write_deltalake(
        f"{tmp_path}/ticks_live/MSFT_2026_09_01",
        pd.DataFrame({"date": pd.date_range("2026-09-01", periods=5, freq="1min"),
                      "price": [1.0] * 5}),
        mode="overwrite",
    )
    return tmp_path
```

Update the existing library test and add the symbol tests, after `test_delta_list_symbols_unknown_library_raises`:

```python
def test_delta_list_libraries_sorted(delta_store):
    assert server.delta_list_libraries() == ["hrp_prices", "ticks", "ticks_live"]


def test_delta_list_symbols_collapses_day_tables_to_bases(delta_store):
    assert server.delta_list_symbols("ticks_live") == ["AAPL", "MSFT"]
    # A library without day suffixes is unchanged.
    assert server.delta_list_symbols("ticks") == ["AAPL"]


def test_delta_read_accepts_a_base_symbol(delta_store):
    out = server.delta_read("ticks_live", "AAPL")
    assert out["symbol"] == "AAPL"


def test_delta_read_still_accepts_a_raw_day_key(delta_store):
    # An older client, or Rita quoting a key it was shown, names one day.
    out = server.delta_read("ticks_live", "AAPL_2026_09_02")
    assert out["returned_rows"] == 5
    assert out["rows"][0]["price"] == 2.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with fastmcp --with pytest --with pandas --with ./openbb-deltalake python -m pytest mcp_stores/test_server.py -q -k "list_symbols or list_libraries or accepts"`
Expected: `test_delta_list_symbols_collapses_day_tables_to_bases` FAILS (raw keys returned); `test_delta_read_accepts_a_base_symbol` FAILS with `unknown symbol 'AAPL'`; the libraries test passes.

- [ ] **Step 3: Implement**

In `mcp_stores/server.py`, add `import daykeys` after `from fastmcp import FastMCP` (the tests already put `mcp_stores/` on `sys.path`; the Docker image installs it as a sibling module, so add `"daykeys"` to `py-modules` in `mcp_stores/pyproject.toml`: `py-modules = ["server", "daykeys"]`).

Replace `delta_list_symbols` and `_require_symbol`:

```python
def _raw_keys(library: str) -> list[str]:
    """Every Delta table in `library`, by its raw key, sorted."""
    _check_ident("library", library)
    if library not in delta_list_libraries():
        raise ValueError(
            f"unknown library {library!r}; call delta_list_libraries first"
        )
    return sorted(_bounded(_delta(library).list_symbols))


def delta_list_symbols(library: str) -> list[str]:
    """List symbols stored in a library.

    A symbol whose tables are keyed by day (`AAPL_2026_09_11`, the EOD dump's
    layout) is listed ONCE, as `AAPL`; the read tools expand it to the days a
    window needs. See daykeys.
    """
    return daykeys.bases(_raw_keys(library))


def _require_symbol(library: str, symbol: str):
    """(store, raw keys) for library, once symbol is known to be in it.

    Every tool validates through here so the "unknown X; call Y first" error
    contract is one implementation, not one per entry point. `symbol` may be
    a base (`AAPL`) or a raw day key (`AAPL_2026_09_11`): the second keeps an
    older client, or an agent quoting a key it was shown, working.
    """
    _check_ident("symbol", symbol)
    raw = _raw_keys(library)
    if symbol not in raw and symbol not in daykeys.bases(raw):
        raise ValueError(
            f"unknown symbol {symbol!r} in {library!r}; call delta_list_symbols first"
        )
    return _delta(library), raw
```

Then update the three existing callers to unpack the pair — this keeps them working until Tasks 3–5 rewrite them:

```python
# delta_describe
    store, _ = _require_symbol(library, symbol)
    return _bounded(D.describe, store, symbol)
# delta_history
    store, _ = _require_symbol(library, symbol)
    return _bounded(D.history, store, symbol)
# delta_read
    store, _ = _require_symbol(library, symbol)
```

`test_delta_read_accepts_a_base_symbol` will still fail after this step with `No Delta table for symbol 'AAPL'` — that is Task 3's job. Mark it `@pytest.mark.xfail(strict=True, reason="Task 3")` for this commit and remove the marker in Task 3.

- [ ] **Step 4: Run the whole file**

Run: `uv run --with fastmcp --with pytest --with pandas --with ./openbb-deltalake python -m pytest mcp_stores -q`
Expected: all pass, one xfail.

- [ ] **Step 5: Lint and commit**

```bash
uv run --with ruff==0.15.22 ruff check mcp_stores
git add mcp_stores/server.py mcp_stores/test_server.py mcp_stores/pyproject.toml
git commit -m "feat(mcp_stores): a day-keyed symbol is listed once, as its base"
```

---

### Task 3: `delta_read` reads across the day tables

**Files:**
- Modify: `mcp_stores/server.py` — `delta_read` (line ~229)
- Modify: `mcp_stores/test_server.py`

**Interfaces:**
- Consumes: `_require_symbol` → `(store, raw)` (Task 2); `daykeys.in_window` (Task 1).
- Produces: unchanged response shape. New behaviour: a base symbol reads the day tables in its window; unfiltered reads the newest day; a timestamp `as_of` skips a table whose first commit is later than it.

- [ ] **Step 1: Write the failing tests**

Remove the `xfail` marker from `test_delta_read_accepts_a_base_symbol` and add, after `test_delta_read_passes_date_range`:

```python
def test_delta_read_of_a_base_with_no_bounds_reads_the_newest_day_only(delta_store, monkeypatch):
    from openbb_deltalake.store import DeltaStore

    def explode(self, *a, **k):
        raise AssertionError("an unfiltered read must not open more than the newest day")

    monkeypatch.setattr(DeltaStore, "read", explode)
    out = server.delta_read("ticks_live", "AAPL")
    assert out["total_rows_in_range"] == 5
    assert {r["price"] for r in out["rows"]} == {4.0}


def test_delta_read_of_a_base_concatenates_the_days_in_the_window_in_order(delta_store):
    out = server.delta_read(
        "ticks_live", "AAPL", start="2026-09-01 00:03", end="2026-09-04 00:01"
    )
    # 09-01: minutes 3,4 (2 rows); 09-02: all 5; 09-03: no table; 09-04: minutes 0,1 (2)
    assert out["total_rows_in_range"] == 9
    assert out["returned_rows"] == 9
    assert [r["price"] for r in out["rows"]] == [1.0, 1.0, 2.0, 2.0, 2.0, 2.0, 2.0, 4.0, 4.0]
    assert out["rows"][0]["date"].startswith("2026-09-01T00:03")


def test_delta_read_of_a_base_tails_after_concatenating(delta_store):
    out = server.delta_read(
        "ticks_live", "AAPL", start="2026-09-01", end="2026-09-04", tail_rows=3
    )
    assert out["total_rows_in_range"] == 15
    assert out["returned_rows"] == 3
    assert [r["price"] for r in out["rows"]] == [4.0, 4.0, 4.0]


def test_delta_read_of_a_window_with_no_day_tables_is_empty_not_404(delta_store):
    out = server.delta_read("ticks_live", "AAPL", start="2026-10-01", end="2026-10-31")
    assert out == {
        "library": "ticks_live", "symbol": "AAPL",
        "total_rows_in_range": 0, "returned_rows": 0, "rows": [],
    }


def test_delta_read_timestamp_as_of_skips_a_day_table_committed_later(delta_store):
    # deltalake 1.6.3 does NOT raise on an as_of earlier than a table's first
    # commit -- it quietly loads version 0. A day table written after the
    # chosen instant did not exist then, so it must contribute nothing.
    from deltalake import DeltaTable

    first = DeltaTable(f"{delta_store}/ticks_live/AAPL_2026_09_04").history()[-1]["timestamp"]
    before = pd.Timestamp(first - 1, unit="ms", tz="UTC").isoformat()
    at = pd.Timestamp(first, unit="ms", tz="UTC").isoformat()

    out = server.delta_read("ticks_live", "AAPL", start="2026-09-04", end="2026-09-04", as_of=before)
    assert out["returned_rows"] == 0
    out = server.delta_read("ticks_live", "AAPL", start="2026-09-04", end="2026-09-04", as_of=at)
    assert out["returned_rows"] == 5
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with fastmcp --with pytest --with pandas --with ./openbb-deltalake python -m pytest mcp_stores/test_server.py -q -k "of_a_base or no_day_tables or skips_a_day"`
Expected: all FAIL with `FileNotFoundError: No Delta table for symbol 'AAPL'` (or the xfail-removed test failing the same way).

- [ ] **Step 3: Implement**

Replace `delta_read` in `mcp_stores/server.py`:

```python
def _committed_by(store, key: str, as_of) -> bool:
    """Whether `key` existed at a timestamp `as_of`.

    delta-rs (1.6.3) answers an as_of earlier than a table's first commit by
    loading version 0, not by raising -- so a day table written AFTER the
    chosen instant would silently contribute rows that did not exist then.
    One history walk per key is the price of a truthful time travel; an int
    version or no as_of skips the walk.
    """
    if as_of is None or isinstance(as_of, int):
        return True
    from openbb_deltalake import describe as D
    from pandas import Timestamp

    first_ms = min(int(e["timestamp"]) for e in _bounded(D.history, store, key))
    ts = Timestamp(as_of)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return Timestamp(first_ms, unit="ms", tz="UTC") <= ts


def _read_one(store, key: str, start, end, tail_rows: int, as_of):
    """(rows in range, frame) for one table -- the v11.0.0 read, per key.

    The READ is bounded, not just the response: with no start/end this reads
    only the trailing files the transaction log says hold those rows, so an
    unfiltered call never materializes the whole table -- the guarantee
    ArcticDB gave via Library.tail, which delta-rs has no equivalent for.
    """
    from openbb_deltalake import describe as D

    if start or end:
        df = _bounded(
            store.read, key, start_date=start, end_date=end,
            as_of=as_of, output="dataframe",
        )
        return len(df), df
    total = _bounded(D.describe, store, key)["row_count"]
    return total, _bounded(store.read_trailing, key, tail_rows, as_of)


def delta_read(
    library: str,
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    tail_rows: int = 1000,
    as_of: str | int | None = None,
) -> dict:
    """Read a symbol from a Delta library.

    start/end are ISO dates/timestamps filtering the stored index. as_of is an
    int Delta version or an ISO timestamp for time travel. Returns at most
    tail_rows rows (the most recent in range, hard cap MAX_ROWS) as JSON
    records with ISO timestamps.

    A symbol stored as one table per day (see daykeys) is read across the day
    tables the window covers, oldest first, and tailed as one frame. With no
    window it reads its newest day only, so it stays as bounded as a single
    table. A window that covers no day table answers zero rows, not an error:
    the symbol exists, that stretch of it does not.
    """
    import pandas as pd

    tail_rows = max(1, min(int(tail_rows), MAX_ROWS))
    store, raw = _require_symbol(library, symbol)
    if isinstance(as_of, str) and as_of.isdigit():
        as_of = int(as_of)

    parts = [
        _read_one(store, key, start, end, tail_rows, as_of)
        for key in daykeys.in_window(raw, symbol, start, end)
        if _committed_by(store, key, as_of)
    ]
    total = sum(n for n, _ in parts)
    frames = [df for _, df in parts if len(df)]
    df = pd.concat(frames).tail(tail_rows).reset_index() if frames else pd.DataFrame()

    return {
        "library": library,
        "symbol": symbol,
        "total_rows_in_range": total,
        "returned_rows": len(df),
        "rows": _records(df),
    }
```

Note for the implementer: the existing tests `test_delta_read_bounds_the_read_not_just_the_response` and `test_delta_read_clamps_tail_rows_to_max_rows` still hold — a single-table symbol goes through `_read_one` once, exactly the old path.

- [ ] **Step 4: Run the whole file**

Run: `uv run --with fastmcp --with pytest --with pandas --with ./openbb-deltalake python -m pytest mcp_stores -q`
Expected: all pass, no xfail left.

- [ ] **Step 5: Lint and commit**

```bash
uv run --with ruff==0.15.22 ruff check mcp_stores
git add mcp_stores/server.py mcp_stores/test_server.py
git commit -m "feat(mcp_stores): delta_read crosses the day tables a window covers"
```

---

### Task 4: `delta_describe` sums the days and reports `days`

**Files:**
- Modify: `mcp_stores/server.py` — `delta_describe` (line ~211)
- Modify: `mcp_stores/test_server.py`

**Interfaces:**
- Consumes: `_require_symbol`, `daykeys.day_keys`.
- Produces: describe body gains `days: int` (1 for a single table). Unchanged keys: `library, symbol, row_count, date_range, columns`.

- [ ] **Step 1: Write the failing tests**

After `test_delta_describe_reports_metadata_without_reading_rows`:

```python
def test_delta_describe_of_a_single_table_reports_one_day(delta_store):
    assert server.delta_describe("ticks", "AAPL")["days"] == 1


def test_delta_describe_of_a_base_sums_the_days_without_reading_rows(delta_store, monkeypatch):
    from openbb_deltalake.store import DeltaStore

    def explode(self, *a, **k):
        raise AssertionError("describe must not read rows")

    monkeypatch.setattr(DeltaStore, "read", explode)
    monkeypatch.setattr(DeltaStore, "read_trailing", explode)

    out = server.delta_describe("ticks_live", "AAPL")
    assert out["symbol"] == "AAPL"
    assert out["days"] == 3
    assert out["row_count"] == 15
    assert out["date_range"][0].startswith("2026-09-01 00:00")
    assert out["date_range"][1].startswith("2026-09-04 00:04")
    assert {c["name"] for c in out["columns"]} == {"date", "price"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with fastmcp --with pytest --with pandas --with ./openbb-deltalake python -m pytest mcp_stores/test_server.py -q -k describe`
Expected: `reports_one_day` FAILS with `KeyError: 'days'`; `sums_the_days` FAILS with `FileNotFoundError` for `AAPL`.

- [ ] **Step 3: Implement**

Replace `delta_describe`:

```python
def delta_describe(library: str, symbol: str) -> dict:
    """Row count, stored date range and column dtypes for a symbol.

    Answered from the transaction log: this reads no rows, however large the
    symbol is. A day-keyed symbol is the sum of its day tables -- rows added,
    the range's outer bounds taken, columns from the newest day -- plus
    `days`, so a strip can say how many tables stand behind the number.
    That is one log walk per day; a library of forty symbols over a few
    weeks is a few dozen small reads, measured against MinIO before merge.
    """
    from openbb_deltalake import describe as D

    store, raw = _require_symbol(library, symbol)
    keys = daykeys.day_keys(raw, symbol)
    parts = [_bounded(D.describe, store, key) for key in keys]
    ranges = [p["date_range"] for p in parts if p["date_range"]]
    # ponytail: min/max over the log's own string form. Every table in a
    # library is written by one process with one precision, so the strings
    # sort as the instants do; parse them if a library ever mixes writers.
    return {
        "library": library,
        "symbol": symbol,
        "row_count": sum(p["row_count"] for p in parts),
        "date_range": [min(r[0] for r in ranges), max(r[1] for r in ranges)] if ranges else None,
        "columns": parts[-1]["columns"],
        "days": len(keys),
    }
```

- [ ] **Step 4: Run the whole file**

Run: `uv run --with fastmcp --with pytest --with pandas --with ./openbb-deltalake python -m pytest mcp_stores -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run --with ruff==0.15.22 ruff check mcp_stores
git add mcp_stores/server.py mcp_stores/test_server.py
git commit -m "feat(mcp_stores): describe a day-keyed symbol as the sum of its days"
```

---

### Task 5: `delta_history` unions the days and formats timestamps as ISO

**Files:**
- Modify: `mcp_stores/server.py` — `delta_history` (line ~222)
- Modify: `mcp_stores/test_server.py`

**Interfaces:**
- Consumes: `_require_symbol`, `daykeys.day_keys`.
- Produces: `[{version: int, timestamp: str}]` newest first, `timestamp` as `YYYY-MM-DDTHH:MM:SS.mmmZ` (UTC, millisecond precision — a commit's own timestamp resolves to that commit; truncating to seconds would land 1–999 ms *before* it and time-travel to the previous version).

- [ ] **Step 1: Write the failing tests**

After `test_delta_history_and_as_of_return_superseded_data`:

```python
def test_delta_history_formats_timestamps_as_iso_utc_milliseconds(delta_store):
    entries = server.delta_history("ticks", "AAPL")
    ts = entries[0]["timestamp"]
    assert ts.endswith("Z") and "T" in ts and len(ts) == len("2026-09-14T12:38:15.256Z")
    # The formatted instant travels back to exactly its own commit.
    assert server.delta_read("ticks", "AAPL", as_of=ts)["rows"][-1]["bid"] == 5.0


def test_delta_history_of_a_base_unions_the_days_newest_first(delta_store):
    entries = server.delta_history("ticks_live", "AAPL")
    assert len(entries) == 3
    stamps = [e["timestamp"] for e in entries]
    assert stamps == sorted(stamps, reverse=True)
    assert {e["version"] for e in entries} == {0}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with fastmcp --with pytest --with pandas --with ./openbb-deltalake python -m pytest mcp_stores/test_server.py -q -k history`
Expected: the ISO test FAILS on the `endswith("Z")` assertion (the timestamp is epoch millis text); the base test FAILS with `FileNotFoundError`.

- [ ] **Step 3: Implement**

Replace `delta_history`:

```python
def _iso_ms(timestamp: str) -> str:
    """Epoch-millisecond text (what delta-rs's history carries) as ISO UTC.

    Millisecond precision on purpose: the string is what a client sends back
    as `as_of`, and a commit's own instant must resolve to that commit. Text
    that is not all digits is left alone -- a test fake, or a future delta-rs
    that formats for us.
    """
    if not timestamp.isdigit():
        return timestamp
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def delta_history(library: str, symbol: str) -> list[dict]:
    """Delta versions for a symbol, newest first -- the time-travel choices.

    A day-keyed symbol answers the union of its day tables' commits. Version
    numbers are per table and do not line up across days, so a client that
    spans tables travels by timestamp; the number is kept for single tables.
    """
    from openbb_deltalake import describe as D

    store, raw = _require_symbol(library, symbol)
    entries = []
    for key in daykeys.day_keys(raw, symbol):
        entries.extend(
            {"version": e["version"], "timestamp": _iso_ms(e["timestamp"])}
            for e in _bounded(D.history, store, key)
        )
    return sorted(entries, key=lambda e: (e["timestamp"], e["version"]), reverse=True)
```

- [ ] **Step 4: Run the whole file**

Run: `uv run --with fastmcp --with pytest --with pandas --with ./openbb-deltalake python -m pytest mcp_stores -q`
Expected: all pass. `test_delta_history_and_as_of_return_superseded_data` sorts by version and still holds: within one table, timestamp order is version order.

- [ ] **Step 5: Lint and commit**

```bash
uv run --with ruff==0.15.22 ruff check mcp_stores
git add mcp_stores/server.py mcp_stores/test_server.py
git commit -m "feat(mcp_stores): history spans the day tables and speaks ISO"
```

---

### Task 6: stores-explorer — manifest text, tests, versions

**Files:**
- Modify: `stores-explorer/widgets.json` — the `delta_explorer` entry
- Modify: `stores-explorer/tests/test_main.py`
- Modify: `stores-explorer/pyproject.toml`, `mcp_stores/pyproject.toml` (version), `docker-compose.yml:334` (image tag)
- Modify: `stores-explorer/README.md` and `mcp_stores/README.md` — one paragraph each

**Interfaces:**
- Consumes: the tool shapes from Tasks 2–5. Routes are unchanged.

- [ ] **Step 1: Write the failing test**

In `stores-explorer/tests/test_main.py`, make the fake describe and history carry the new shapes and pin the manifest text. Change `DEFAULT_DESCRIBE` to include `"days": 1`, and the fake history's timestamps to the ISO-millisecond form:

```python
DEFAULT_DESCRIBE = {
    "library": "openbb", "symbol": "AAPL",
    "row_count": 42,
    "date_range": ["2026-01-01", "2026-08-07"],
    "columns": [{"name": "close", "dtype": "float64"}],
    "days": 1,
}
```

```python
    def delta_history_fn(library, symbol):
        _check(library, symbol)
        return [{"version": 1, "timestamp": "2026-09-02T10:00:00.000Z"},
                {"version": 0, "timestamp": "2026-09-01T10:00:00.000Z"}]
```

Add to `test_widgets_json_declares_delta_explorer`:

```python
    assert "span" in w["description"]  # a symbol may span day tables; the widget says so
    for name in ("start", "end"):
        p = next(p for p in w["params"] if p["paramName"] == name)
        assert p["type"] == "date" and p["show"] is False  # the strip renders them
```

And a describe pass-through for the new field:

```python
def test_delta_describe_passes_days_through():
    r = make_client().get("/delta/describe", params={"library": "openbb", "symbol": "AAPL"})
    assert r.json()["days"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run (from `stores-explorer/`; `--no-project` because uv would otherwise try to resolve this package's own pyproject, verified green with 25 tests before this plan):

```bash
cd stores-explorer && uv run --no-project --python 3.12 --with ../mcp_stores --with ../openbb-deltalake --with pytest --with fastapi --with httpx --with fastmcp --with pandas python -m pytest tests -q -k "declares_delta_explorer or passes_days"
```
Expected: the manifest test FAILS on `"span" in w["description"]`; the days test passes already (pass-through) — keep it as the pin.

- [ ] **Step 3: Implement**

`stores-explorer/widgets.json`, the `delta_explorer` entry: description becomes

```
"Browse the shared Delta Lake store: pick a library, pick a symbol, see what is stored and read it at any version. A symbol stored one table per day is listed once and may span days; Start and End take an absolute time or T-5h style offsets."
```

Bump versions: `mcp_stores/pyproject.toml` `version = "11.6.0"`, `stores-explorer/pyproject.toml` `version = "11.6.0"`, `docker-compose.yml` line 334 `image: openbb-stores-explorer:11.6.0`.

README paragraphs (append to the Delta section of each):

`mcp_stores/README.md`:
```
### Day-keyed symbols

A library that stores one table per (symbol, day) — `ticks_live/AAPL_2026_09_11`,
the EOD dump's layout — lists `AAPL` once. `delta_read` opens the day tables a
`start`/`end` window covers and tails them as one frame; with no window it
reads the newest day only. `delta_describe` sums the days and reports `days`;
`delta_history` is the union of the days' commits, timestamps as ISO UTC with
milliseconds, so a client that spans tables travels by timestamp. A raw day
key still works as a symbol.
```

`stores-explorer/README.md`:
```
Start and End on the explorer card accept an absolute time or `T-5h` style
offsets; bdobb resolves those before the request, so the routes only ever see
ISO text. A day-keyed symbol (`ticks_live`) reads across the days a window
covers — see `mcp_stores/README.md`.
```

- [ ] **Step 4: Run both suites**

Run: `uv run --with fastmcp --with pytest --with pandas --with ./openbb-deltalake python -m pytest mcp_stores -q` and the stores-explorer command from Step 2.
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add stores-explorer/widgets.json stores-explorer/tests/test_main.py stores-explorer/pyproject.toml \
        mcp_stores/pyproject.toml docker-compose.yml stores-explorer/README.md mcp_stores/README.md
git commit -m "feat(stores-explorer): 11.6.0 — the manifest says a symbol may span days"
```

---

### Task 7: Verify against MinIO on the NAS, then ship the image

**Files:** none changed. This task produces evidence, and the running service.

**Interfaces:**
- Consumes: the `stores-explorer` image built from this branch.

- [ ] **Step 1: Build the image on the Mac**

```bash
docker build --platform linux/amd64 -f stores-explorer/Dockerfile -t openbb-stores-explorer:11.6.0 .
```
Expected: a successful build. (The Dockerfile installs `mcp_stores/` as a sibling package; `daykeys.py` rides along through `py-modules`. If the image cannot import `daykeys`, the pyproject edit in Task 2 was missed.)

- [ ] **Step 2: Ship it and recreate the service — after 4 PM ET**

```bash
docker save openbb-stores-explorer:11.6.0 | gzip -1 | ssh nas 'gunzip | /share/ZFS530_DATA/.qpkg/container-station/bin/docker load'
ssh nas 'cd /share/Container/openbb && cp docker-compose.yml docker-compose.yml.pre-stores-explorer-11.6.0 && sed -i "s#openbb-stores-explorer:[0-9.]*#openbb-stores-explorer:11.6.0#" docker-compose.yml && /share/ZFS530_DATA/.qpkg/container-station/bin/docker compose up -d --no-deps stores-explorer'
```
Expected: the container recreates; `docker ps` on the NAS shows `openbb-stores-explorer` on 11.6.0. Rollback is the `.pre-stores-explorer-11.6.0` copy plus `compose up -d --no-deps stores-explorer`.

- [ ] **Step 3: Probe the live routes**

```bash
B=https://openbb.<your-tailnet>.ts.net:6904
curl -s "$B/delta/symbols?library=ticks_live" | python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d),[o['value'] for o in d[:5]])"
time curl -s "$B/delta/describe?library=ticks_live&symbol=AAPL" | python3 -m json.tool | head -12
curl -s "$B/delta/history?library=ticks_live&symbol=AAPL" | head -c 300; echo
curl -s "$B/delta/series?library=ticks_live&symbol=AAPL&start=2026-09-11%2020:00:00&end=2026-09-14%2013:00:00&tail_rows=5" | python3 -m json.tool | head -20
curl -s "$B/delta/series?library=ticks_sip&symbol=GS&start=2026-09-11%2018:00:00&end=2026-09-11%2020:00:00&tail_rows=3" | python3 -m json.tool | head -20
curl -s "$B/delta/series?library=ticks_live&symbol=AAPL_2026_09_11&tail_rows=2" | python3 -m json.tool | head -8
```
Expected, in order: 41 symbols, `AAPL` first and no day suffixes; a describe with `days` ≥ 12 and a summed `row_count`, answering in well under the 15 s tool timeout (record the wall time in the PR); ISO-`Z` timestamps; rows from 09-14 (the tail of a window that crosses two trading days and a weekend); GS ticks with second-precision dates inside the window; the raw key still answering.

- [ ] **Step 4: Record the evidence and open the PR**

Paste the four outputs and the describe wall time into the PR body. PR title: `feat(stores): a day-keyed Delta symbol spans its day tables (11.6.0)`. Pass the PR number explicitly to any later `gh pr edit`.

---

### Task 8: Backport onto `release/v11.3.x`

**Files:** the same files, on a new branch.

- [ ] **Step 1: Cut the branch and cherry-pick**

```bash
git fetch origin
git checkout -b release/v11.3.x v11.3.0
git log --oneline v11.3.0..claude/delta-explorer-date-range -- mcp_stores stores-explorer docker-compose.yml
```
Cherry-pick the feature commits from Tasks 1–6 in order (`git cherry-pick <sha>`). Expected conflicts: none in `mcp_stores/`; `docker-compose.yml` and the READMEs may conflict on lines the license commit (`5d79f14`) touched — resolve by keeping the 11.6.0 tag and the new paragraphs.

- [ ] **Step 2: Run both suites on the branch**

Same commands as Task 6 Step 4. Expected: all pass.

- [ ] **Step 3: Push; the tag is the user's**

```bash
git push -u origin release/v11.3.x
```
Tell the user the branch is ready for `v11.6.0`; do not tag.

---

## Self-review

**Spec coverage.** D1 → Tasks 2–5 (mcp_stores). D4 → Task 1 `in_window` + Task 3 test "newest day only". D5 → Task 5 (ISO timestamps, version kept) + Task 3 `_committed_by`. Describe sums and `days` → Task 4. Manifest and image → Tasks 6–7. MinIO measurement → Task 7 Step 3. Backport → Task 8. Error table: "window covers no day table → zero rows" → Task 3 test; "as_of predates every table → zero rows" → Task 3 test. Frontend items (D2, D3, D6, D7) are the bdobb-v2 plan's.

**Deviation from the spec, corrected inline in the spec's copy here and in bdobb-v2:** history timestamps are millisecond precision, not second — Task 5 explains why.

**Type consistency.** `_require_symbol` returns `(store, raw)` from Task 2 on and every later task unpacks it. `daykeys.in_window(keys, base, start, end)` and `daykeys.day_keys(keys, base)` are the two names used by Tasks 3–5. `days` is an `int` in Task 4 and is asserted as `1` in Task 6.

# tick-vault Foundation Implementation Plan (Episode 15 — Plan 1 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The `tick-vault` Python package: Delta schemas, EODHD tick parsing with sale-condition decoding, internal ID allocation, correction diffing, and the four-mode temporal query layer — fully unit/component tested, no NAS or API key required.

**Architecture:** New sibling package `tick-vault/` in openbb-docker (spec §2). Pure-Python + `deltalake` + `duckdb` + `pyarrow`; every component testable against a miniature local lake in `tmp_path`. Plans 2 (reference pipeline + re-backfill engine) and 3 (ODP integration, union view, reconciliation gate, compose service) build on these interfaces.

**Tech Stack:** Python 3.12, `pandas>=2,<3` (tick-lab's pin), `pyarrow>=16`, `deltalake>=1.0`, `duckdb>=1.0`, `pytest`. No new frameworks.

**Spec:** `docs/superpowers/specs/2026-09-09-tick-vault-temporal-design.md` (and its committed baseline `2026-09-09-sp500-temporal-tick-database-baseline.md`).

## Global Constraints

- Package dir `tick-vault/`, module `tick_vault`, own `pyproject.toml` (mirror `tick-lab/pyproject.toml` layout).
- All silver tables append-only; no update/delete of fact rows ever (baseline §3.5).
- All stored timestamps UTC; `trade_date` = America/New_York session date (baseline §4.1).
- Internal IDs are UUIDv7 strings (baseline §20); external identifiers never used as keys (baseline §3.1).
- Logical tick key: `(listing_id, trade_date, session_seq)`; hash fallback only when `seq` absent (spec §4).
- Availability policies by exact name: `AS_INGESTED_LOCAL_V1` (default), `HISTORICAL_VENDOR_FINAL_V1` (opt-in simulated), `CURRENT_CORRECTED_V1` (gold mode) (spec §6).
- Sequence policy name: `EODHD_TS_MS_THEN_SOURCE_ORDER_V1` (baseline §12.3).
- Correction kinds by exact name: `REVISED`, `CANCELLED`, `LATE_ADD` (spec §7).
- Every commit message follows the repo's `feat:`/`fix:`/`docs:`/`test:` convention.
- TDD: no implementation before its failing test has been run and seen to fail.

---

### Task 1: Package scaffold and UUIDv7 ID allocation

**Files:**
- Create: `tick-vault/pyproject.toml`, `tick-vault/tick_vault/__init__.py`, `tick-vault/tick_vault/ids.py`
- Test: `tick-vault/tests/test_ids.py`

**Interfaces:**
- Produces: `tick_vault.ids.new_id(prefix: str) -> str` — returns `f"{prefix}_{uuid7hex}"`, e.g. `lst_0191f3a8...`; prefixes: `iss`, `ins`, `lst`, `ven`, `idx`, `cap`, `run`, `tkv`, `evt`, `mem`, `wrk`. Time-ordered (UUIDv7), so sorted IDs sort by creation time.

- [ ] **Step 1: Write the failing test**

```python
# tick-vault/tests/test_ids.py
import re, time
from tick_vault.ids import new_id, PREFIXES

def test_new_id_format():
    i = new_id("lst")
    assert re.fullmatch(r"lst_[0-9a-f]{32}", i)

def test_new_id_rejects_unknown_prefix():
    import pytest
    with pytest.raises(ValueError):
        new_id("bogus")

def test_new_id_time_ordered():
    a = new_id("cap"); time.sleep(0.002); b = new_id("cap")
    assert a < b  # uuid7 timestamp prefix makes string order = time order
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd tick-vault && python -m pytest tests/test_ids.py -v`
Expected: FAIL — `ModuleNotFoundError: tick_vault`

- [ ] **Step 3: Write pyproject + minimal implementation**

```toml
# tick-vault/pyproject.toml
[project]
name = "tick-vault"
version = "0.1.0"
description = "Bitemporal S&P 500 tick lakehouse: schemas, ingestion, temporal query layer"
requires-python = ">=3.12"
dependencies = ["pandas>=2,<3", "pyarrow>=16", "deltalake>=1.0", "duckdb>=1.0"]
[project.optional-dependencies]
dev = ["pytest>=8"]
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
[tool.setuptools.packages.find]
include = ["tick_vault*"]
```

```python
# tick-vault/tick_vault/ids.py
"""UUIDv7-style internal IDs (baseline §3.1, §20). String order == creation order."""
import os, time

PREFIXES = {"iss", "ins", "lst", "ven", "idx", "cap", "run", "tkv", "evt", "mem", "wrk"}

def new_id(prefix: str) -> str:
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix: {prefix!r}")
    ms = time.time_ns() // 1_000_000
    rand = os.urandom(10)
    b = ms.to_bytes(6, "big") + bytes([0x70 | (rand[0] & 0x0F), rand[1],
                                       0x80 | (rand[2] & 0x3F)]) + rand[3:10]
    return f"{prefix}_{b.hex()}"
```

`tick_vault/__init__.py`: `__version__ = "0.1.0"` only.

- [ ] **Step 4: Run tests to verify they pass** — `python -m pytest tests/test_ids.py -v` → 3 PASS
- [ ] **Step 5: Commit** — `git add tick-vault && git commit -m "feat(tick-vault): package scaffold and UUIDv7 id allocation"`

---

### Task 2: Delta schema registry and table creation

**Files:**
- Create: `tick-vault/tick_vault/schemas.py`
- Test: `tick-vault/tests/test_schemas.py`

**Interfaces:**
- Produces: `SCHEMAS: dict[str, pyarrow.Schema]` keyed by full table name (`"silver.us_trade_tick_version"` etc.); `create_all(root: str) -> None` creating each Delta table at `<root>/<schema>/<table>` with declared partitioning; `PARTITIONING: dict[str, list[str]]`.
- Tables (spec §4 + baseline §6): bronze `eodhd_tick_capture`, `eodhd_index_components_capture`, `eodhd_exchange_symbols_capture`, `eodhd_delisted_symbols_capture`, `eodhd_symbol_change_capture`, `eodhd_fundamentals_capture`, `eodhd_corporate_actions_capture`, `eodhd_eod_capture`, `eodhd_intraday_capture`, `openfigi_mapping_capture`; silver `issuer`, `registrant`, `instrument`, `listing_version`, `venue_reference_version`, `identifier_assignment_version`, `figi_assignment_version`, `instrument_event_version`, `instrument_relationship_version`, `index_membership_version`, `us_trade_tick_version`, `tick_correction_event`, `feed_coverage`, `data_availability_policy`; gold `us_trade_tape`, `us_trade_tick_by_venue`, `sp500_universe_snapshot`, `selected_tick_version`; ops `backfill_manifest`, `ingestion_run`, `openfigi_resolution_queue`, `data_quality_issue`, `backtest_run_manifest`.
- Column lists: transcribe from baseline §7 schemas verbatim, plus spec-§4 additions to `us_trade_tick_version` (`session_seq: int64`, `sub_mkt_raw: string`, `is_cancelled: bool`, `is_late_add: bool`, `origin: string`) and `capture_kind` is NOT added (dropped with grandfathering). Timestamps `pa.timestamp("us", tz="UTC")`; DECIMAL(24,10)→`pa.decimal128(24,10)`; ARRAY<STRING>→`pa.list_(pa.string())`.
- Partitioning: `us_trade_tick_version`, `us_trade_tape`, `us_trade_tick_by_venue`, `feed_coverage`, `backfill_manifest` by `trade_date` (manifest: by `week_monday`); `eodhd_tick_capture` by `committed_date` (a `date32` column derived from `committed_at_ts` — Delta cannot partition on an expression).

- [ ] **Step 1: Write the failing test**

```python
# tick-vault/tests/test_schemas.py
import pyarrow as pa
from deltalake import DeltaTable
from tick_vault.schemas import SCHEMAS, PARTITIONING, create_all

def test_registry_covers_layers():
    layers = {n.split(".")[0] for n in SCHEMAS}
    assert layers == {"bronze", "silver", "gold", "ops"}
    assert "silver.us_trade_tick_version" in SCHEMAS

def test_tick_version_has_spec_delta_columns():
    f = {x.name for x in SCHEMAS["silver.us_trade_tick_version"]}
    assert {"session_seq", "sub_mkt_raw", "is_cancelled", "is_late_add",
            "origin", "revision_number", "supersedes_tick_version_id",
            "available_at_ts", "trade_ts_ms", "logical_tick_id"} <= f

def test_create_all_and_partitioning(tmp_path):
    create_all(str(tmp_path))
    dt = DeltaTable(str(tmp_path / "silver" / "us_trade_tick_version"))
    assert dt.metadata().partition_columns == ["trade_date"]
    dt2 = DeltaTable(str(tmp_path / "ops" / "backfill_manifest"))
    assert dt2.metadata().partition_columns == ["week_monday"]
```

- [ ] **Step 2: Run to verify FAIL** — `ModuleNotFoundError`/`ImportError` on `schemas`.
- [ ] **Step 3: Implement `schemas.py`** — module-level `pa.schema([...])` per table transcribed from baseline §7 (every column, exact names); `create_all` loops `deltalake.write_deltalake(path, SCHEMAS[name].empty_table(), partition_by=PARTITIONING.get(name))` via `pa.Table.from_pylist([], schema=...)`. Long file by necessity; no logic beyond declaration.
- [ ] **Step 4: Run to verify PASS.**
- [ ] **Step 5: Commit** — `feat(tick-vault): delta schema registry for bronze/silver/gold/ops`

---

### Task 3: Sale-condition decode table

**Files:**
- Create: `tick-vault/tick_vault/sl_conditions.py`
- Test: `tick-vault/tests/test_sl_conditions.py`

**Interfaces:**
- Produces: `decode_sl(sl: str) -> SaleConditions` where `SaleConditions` is a frozen dataclass: `flags: tuple[str, ...]` (stable names like `REGULAR`, `ODD_LOT`, `SOLD_LAST`, `SOLD_OUT_OF_SEQUENCE`, `EXTENDED_HOURS`, `PRIOR_REFERENCE_PRICE`, `DERIVATIVELY_PRICED`, `AVERAGE_PRICE`, `CASH_SALE`, `INTERMARKET_SWEEP`, `CLOSING_PRINT`, `OPENING_PRINT`, plus `UNKNOWN(<char>@<pos>)` for unmapped codes), `eligible_for_bars: bool`, `out_of_sequence: bool`. `DECODE_VERSION = "SL_DECODE_V1"`.
- Position-sensitive: `sl` is a 4-position CTA/UTP string; the same letter can mean different things per position. Table keyed `(position, char)`.
- **Phase-0 note (spec §7):** this V1 table is seeded from published CTA/UTP condition vocabularies; the calibration week verifies it against real payloads and bumps `DECODE_VERSION` if corrections are needed. Unknown codes NEVER raise — they yield `UNKNOWN(...)` flags and are counted by the caller into `ops.data_quality_issue` (Plan 2).

- [ ] **Step 1: Write the failing test**

```python
# tick-vault/tests/test_sl_conditions.py
from tick_vault.sl_conditions import decode_sl, DECODE_VERSION

def test_regular_sale():
    c = decode_sl("@   ")
    assert c.flags == ("REGULAR",) and c.eligible_for_bars and not c.out_of_sequence

def test_odd_lot_trailing_i():
    c = decode_sl("@  I")
    assert "ODD_LOT" in c.flags and c.eligible_for_bars

def test_sold_out_of_sequence_is_ineligible_and_flagged():
    c = decode_sl("@ Z ")
    assert "SOLD_OUT_OF_SEQUENCE" in c.flags and c.out_of_sequence and not c.eligible_for_bars

def test_unknown_code_never_raises():
    c = decode_sl("@ ~ ")
    assert any(f.startswith("UNKNOWN(") for f in c.flags)

def test_short_or_empty_string_padded():
    assert decode_sl("@").flags == ("REGULAR",)
    assert decode_sl("").flags == ()

def test_version_constant():
    assert DECODE_VERSION == "SL_DECODE_V1"
```

- [ ] **Step 2: FAIL** (module missing). **Step 3:** implement `_TABLE: dict[tuple[int, str], tuple[str, bool, bool]]` (flag, bars_eligible, out_of_seq) covering the CTA/UTP letters above; `decode_sl` right-pads to 4, folds per-position entries, `eligible_for_bars = all(...)`, `out_of_sequence = any(...)`. **Step 4:** PASS. **Step 5:** Commit `feat(tick-vault): CTA/UTP sale-condition decode table V1`.

---

### Task 4: Tick payload parser

**Files:**
- Create: `tick-vault/tick_vault/tick_parser.py`
- Test: `tick-vault/tests/test_tick_parser.py`, fixture `tick-vault/tests/fixtures/eodhd_ticks_sample.json`

**Interfaces:**
- Consumes: `decode_sl` (Task 3), `new_id` (Task 1).
- Produces: `parse_tick_payload(payload: list[dict], *, capture_id: str, listing_id: str, instrument_id: str, observed_at: datetime, page_ordinal: int = 0) -> pa.Table` matching `SCHEMAS["silver.us_trade_tick_version"]`; `PARSER_VERSION = "TICK_PARSER_V1"`; helper `session_date(ts_ms: int) -> datetime.date` (America/New_York; a 01:00 UTC print belongs to the *prior* NY session date only if before 04:00 ET close of extended session — rule: NY-local calendar date of the print, matching `ticks_sip`'s convention).
- Fields per EODHD tick record: `ts` (ms UTC), `price`, `shares`, `seq`, `sl`, `mkt`, `sub_mkt`, `ex`. Dedup on `(ts, seq)` within payload (boundary-second duplicates, per sip_backfill). `logical_tick_id = f"{listing_id}|{trade_date.isoformat()}|{seq}"`. `revision_number=1`, `origin="CAPTURE"`, `available_at_ts=observed_at`, `source_row_ordinal` = index in payload.

- [ ] **Step 1: Write fixture + failing test**

```json
[{"ts": 1636119000123, "price": 150.25, "shares": 100, "seq": 1001, "sl": "@   ", "mkt": "Q", "sub_mkt": "", "ex": "US"},
 {"ts": 1636119000123, "price": 150.26, "shares": 200, "seq": 1002, "sl": "@  I", "mkt": "N", "sub_mkt": "", "ex": "US"},
 {"ts": 1636119000123, "price": 150.26, "shares": 200, "seq": 1002, "sl": "@  I", "mkt": "N", "sub_mkt": "", "ex": "US"},
 {"ts": 1636119005000, "price": 150.10, "shares": 50, "seq": 1003, "sl": "@ Z ", "mkt": "D", "sub_mkt": "T", "ex": "US"}]
```

```python
# tick-vault/tests/test_tick_parser.py
import json, datetime as dt
from pathlib import Path
from tick_vault.tick_parser import parse_tick_payload, session_date, PARSER_VERSION

PAYLOAD = json.loads((Path(__file__).parent / "fixtures/eodhd_ticks_sample.json").read_text())
OBS = dt.datetime(2021, 11, 6, 1, 0, tzinfo=dt.timezone.utc)

def _parse():
    return parse_tick_payload(PAYLOAD, capture_id="cap_x", listing_id="lst_a",
                              instrument_id="ins_a", observed_at=OBS)

def test_dedup_on_ts_seq():
    assert _parse().num_rows == 3  # 4 rows, one boundary duplicate

def test_schema_and_provenance():
    t = _parse().to_pylist()
    r = t[0]
    assert r["source_capture_id"] == "cap_x" and r["origin"] == "CAPTURE"
    assert r["revision_number"] == 1 and r["available_at_ts"] == OBS
    assert r["parser_version"] == PARSER_VERSION
    assert r["logical_tick_id"] == "lst_a|2021-11-05|1001"

def test_session_date_new_york():
    # 1636119000123 = 2021-11-05 13:30:00.123 UTC = 09:30 ET → session 2021-11-05
    assert session_date(1636119000123) == dt.date(2021, 11, 5)

def test_sale_condition_flags_carried():
    rows = _parse().to_pylist()
    assert "SOLD_OUT_OF_SEQUENCE" in rows[2]["sale_condition_flags"]

def test_same_ms_different_seq_not_collapsed():
    rows = _parse().to_pylist()
    assert rows[0]["trade_ts_ms"] == rows[1]["trade_ts_ms"]  # both kept (§19.3)
```

- [ ] **Step 2: FAIL.** **Step 3:** implement with `zoneinfo.ZoneInfo("America/New_York")`, pandas for dedup, build `pa.Table` against the Task-2 schema (unused nullable cols → None). **Step 4:** PASS. **Step 5:** Commit `feat(tick-vault): EODHD tick payload parser with session dating and sl decoding`.

---

### Task 5: Correction diff engine

**Files:**
- Create: `tick-vault/tick_vault/diffing.py`
- Test: `tick-vault/tests/test_diffing.py`

**Interfaces:**
- Consumes: parsed `pa.Table`s (Task 4 shape).
- Produces: `diff_window(existing: pa.Table, incoming: pa.Table, *, revealing_capture_id: str, observed_at: datetime) -> DiffResult`; `DiffResult` dataclass: `new_versions: pa.Table` (tick_version rows to append), `events: pa.Table` (`silver.tick_correction_event` rows). Match key `logical_tick_id`; comparison fields `price, size, sale_condition_raw, sub_mkt_raw, venue_code_raw`.
- Semantics (spec §7): unchanged → nothing; changed → new version `revision_number+1`, `is_correction=True`, `supersedes_tick_version_id` set, `available_at_ts=observed_at`, event `REVISED`; in existing but not incoming → tombstone version (`is_cancelled=True`, fields copied from prior), event `CANCELLED`; in incoming but not existing → `revision_number=1`, `is_late_add=True`, event `LATE_ADD`. `existing` is pre-filtered to latest revisions, non-cancelled (caller's job — Plan 2 engine; the temporal layer's `latest` view in Task 6 provides it).

- [ ] **Step 1: Write the failing test** — build `existing` from the Task-4 fixture parse; `incoming` = same payload with seq 1002 price changed to 150.27, seq 1003 removed, and a new seq 1004 appended; assert: one REVISED (supersedes the 1002 version id, revision 2, `available_at_ts == observed_at`, not the original trade's), one CANCELLED tombstone for 1003, one LATE_ADD for 1004 with revision 1; unchanged 1001 produces nothing; `events` rows carry `revealing_capture_id`. (Full test code in-plan style as Tasks 1–4; ~40 lines, exercising every branch.)

```python
def test_revised_gets_capture_knowledge_time():
    res = _diff()
    rev = [r for r in res.new_versions.to_pylist() if r["is_correction"]][0]
    assert rev["available_at_ts"] == OBS2          # revealing capture time
    assert rev["revision_number"] == 2
    assert rev["supersedes_tick_version_id"] == EXISTING_1002_VERSION_ID

def test_cancel_is_tombstone_not_delete():
    res = _diff()
    tomb = [r for r in res.new_versions.to_pylist() if r["is_cancelled"]][0]
    assert tomb["logical_tick_id"].endswith("|1003") and tomb["price"] is not None

def test_unchanged_row_emits_nothing():
    ids = [r["logical_tick_id"] for r in _diff().new_versions.to_pylist()]
    assert not any(i.endswith("|1001") for i in ids)

def test_event_kinds():
    kinds = sorted(r["correction_kind"] for r in _diff().events.to_pylist())
    assert kinds == ["CANCELLED", "LATE_ADD", "REVISED"]
```

- [ ] **Step 2: FAIL.** **Step 3:** implement with pandas merges on `logical_tick_id` (indicator=True), three branches, `new_id("tkv")`/`new_id("evt")` for version/event ids. **Step 4:** PASS. **Step 5:** Commit `feat(tick-vault): diff-aware correction engine (REVISED/CANCELLED/LATE_ADD)`.

---

### Task 6: Temporal tick selection (DuckDB)

**Files:**
- Create: `tick-vault/tick_vault/temporal.py`
- Test: `tick-vault/tests/test_temporal.py`

**Interfaces:**
- Consumes: a lake root created by `create_all` (Task 2) with rows written by tests via `deltalake.write_deltalake(..., mode="append")`.
- Produces: `attach(con: duckdb.DuckDBPyConnection, root: str) -> None` registering each Delta table as a DuckDB view named `bronze_x`/`silver_x`/... via `delta_scan`; `install_macros(con) -> None` creating: `latest_ticks()` (latest non-cancelled revision per logical_tick_id — the gold selection rule), `pit_ticks(as_of)` (latest revision with `available_at_ts <= as_of`, tombstones honored: a cancellation available by `as_of` removes the tick), `pit_ticks_policy(as_of, policy)` — `policy='AS_INGESTED_LOCAL_V1'` → `pit_ticks`; `policy='HISTORICAL_VENDOR_FINAL_V1'` → availability simulated as `trade_date + INTERVAL 1 DAY` at 08:00 UTC (computed in the view, never stored; spec §6).
- Constants: `SEQUENCE_POLICY = "EODHD_TS_MS_THEN_SOURCE_ORDER_V1"`; ordering helper macro `tape_order` implementing baseline §12.3's ORDER BY list.

- [ ] **Step 1: Write the failing test** — seed a `tmp_path` lake: tick A (rev 1, available 2026-01-10), correction A' (rev 2, available 2026-02-01), tick B (rev 1, then CANCELLED tombstone available 2026-02-01), tick C (rev 1, 2026-01-10). Assert:

```python
def test_latest_serves_corrected_and_drops_cancelled(lake):
    rows = q(lake, "SELECT logical_tick_id, price FROM latest_ticks() ORDER BY 1")
    assert prices(rows) == {"A": 150.27, "C": 99.0}     # A corrected, B gone

def test_pit_before_correction_sees_original_and_uncancelled(lake):
    rows = q(lake, "SELECT * FROM pit_ticks(TIMESTAMP '2026-01-15 00:00:00+00')")
    assert prices(rows) == {"A": 150.26, "B": 10.0, "C": 99.0}

def test_pit_after_correction_matches_latest(lake):
    rows = q(lake, "SELECT * FROM pit_ticks(TIMESTAMP '2026-03-01 00:00:00+00')")
    assert prices(rows) == {"A": 150.27, "C": 99.0}

def test_pit_before_any_capture_is_empty(lake):
    assert q(lake, "SELECT * FROM pit_ticks(TIMESTAMP '2019-01-01 00:00:00+00')") == []

def test_simulated_policy_makes_history_visible(lake):
    rows = q(lake, "SELECT * FROM pit_ticks_policy(TIMESTAMP '2021-11-08 00:00:00+00', 'HISTORICAL_VENDOR_FINAL_V1')")
    assert len(rows) > 0   # trade_date 2021-11-05 + T+1 simulated availability
```

- [ ] **Step 2: FAIL.** **Step 3:** implement with `duckdb` `delta_scan` + `CREATE OR REPLACE MACRO ... AS TABLE` per baseline §13.3's window pattern (row_number over logical_tick_id, availability filter, then anti-join tombstones whose cancellation is visible). **Step 4:** PASS. **Step 5:** Commit `feat(tick-vault): temporal tick selection macros (latest, PIT, simulated policy)`.

---

### Task 7: PIT membership and identity macros

**Files:**
- Create: `tick-vault/tick_vault/reference_queries.py`
- Test: `tick-vault/tests/test_reference_queries.py`

**Interfaces:**
- Consumes: `attach` (Task 6); silver reference tables (Task 2 schemas).
- Produces: `install_reference_macros(con)` creating `pit_sp500_membership(market_date, decision_ts)` (baseline §8.4 verbatim: effective interval + `available_at_ts <= decision_ts` + `system_to_ts` open + `resolution_status='RESOLVED'`), `pit_listing(symbol_or_listing, market_date, decision_ts)` resolving a vendor symbol to `(listing_id, instrument_id)` via `identifier_assignment_version` with the same predicates, and `current_sp500(market_date)` (no knowledge cutoff — the `effective`-only mode).

- [ ] **Step 1: Write the failing test** — seed membership + identifier rows modeling: AAPL member 2015→null; TWTR member 2018→2022-10-27 (delisted, mapping retained); ticker "X" used by listing lst_old 2010–2017 and lst_new 2020→null (reuse); one row `resolution_status='AMBIGUOUS'`; one membership row with `available_at_ts` = 2026-09-01 (our re-download). Assert the §19.1 quartet:

```python
def test_2017_universe_excludes_2020_joiner(lake): ...   # lst_new absent for 2017-03-06
def test_delisted_member_present_within_tenure(lake): ... # TWTR in 2021 universe
def test_reused_ticker_resolves_by_date(lake):
    assert resolve(lake, "X", "2015-06-01") == "lst_old"
    assert resolve(lake, "X", "2021-06-01") == "lst_new"
def test_ambiguous_membership_excluded_but_countable(lake): ...
def test_membership_invisible_before_ingestion(lake):
    # decision_ts 2017 < available_at 2026 → empty under real policy (spec §6)
    assert pit_members(lake, "2017-03-06", "2017-03-06 21:00:00+00") == []
```

- [ ] **Step 2: FAIL.** **Step 3:** implement macros (SQL transcribed from baseline §8.4). **Step 4:** PASS. **Step 5:** Commit `feat(tick-vault): PIT membership and dated identity resolution macros`.

---

### Task 8: Contract resolver and provenance

**Files:**
- Create: `tick-vault/tick_vault/contract.py`
- Test: `tick-vault/tests/test_contract.py`

**Interfaces:**
- Consumes: Tasks 6–7 macros.
- Produces: `query_ticks(con, *, symbols: list[str] | None, start: date, end: date, as_of: datetime | None = None, effective: date | None = None, availability_policy: str = "AS_INGESTED_LOCAL_V1") -> ContractResult`; `ContractResult`: `table: pa.Table`, `provenance: dict` with keys `mode` (`"GOLD" | "PIT_KNOWLEDGE" | "BITEMPORAL" | "EFFECTIVE_ONLY"`), `availability_policy_version`, `sequence_policy_version`, `sl_decode_version`, `parser_version`, `delta_versions: dict[str, int]`, `warnings: list[str]`, `origin_flags: list[str]`. This is the exact surface `openbb-deltalake` will delegate to in Plan 3.
- Mode resolution per spec §3: neither → GOLD; `as_of` only → PIT_KNOWLEDGE with per-row effective = queried market date; both → BITEMPORAL; `effective` only → EFFECTIVE_ONLY. Validation: `as_of` in the future → `ValueError`; `as_of` before earliest capture → empty result + warning string `"as_of predates earliest capture"`; never raises past validation.

- [ ] **Step 1: Write the failing test** — on the Task 6+7 seeded lake:

```python
def test_no_params_is_gold_mode(lake):
    r = query(lake)
    assert r.provenance["mode"] == "GOLD"
    assert prices(r) == {"A": 150.27, "C": 99.0}

def test_as_of_only_is_pit(lake):
    r = query(lake, as_of=TS_2026_01_15)
    assert r.provenance["mode"] == "PIT_KNOWLEDGE" and prices(r)["A"] == 150.26

def test_both_params_bitemporal_resolves_identity_at_effective(lake): ...
def test_effective_only_uses_latest_data_historical_identity(lake): ...
def test_future_as_of_raises(lake):
    with pytest.raises(ValueError): query(lake, as_of=YEAR_3000)
def test_pre_capture_as_of_warns_not_errors(lake):
    r = query(lake, as_of=TS_2019)
    assert r.table.num_rows == 0 and r.provenance["warnings"]
def test_provenance_pins_delta_versions(lake):
    v = query(lake).provenance["delta_versions"]
    assert v["silver.us_trade_tick_version"] >= 0
```

- [ ] **Step 2: FAIL.** **Step 3:** implement (mode dispatch → macro choice; `DeltaTable(path).version()` per table read for the pin). **Step 4:** PASS. **Step 5:** Commit `feat(tick-vault): four-mode temporal contract resolver with provenance pinning`.

---

### Task 9: Union-view routing (transitional serving)

**Files:**
- Create: `tick-vault/tick_vault/union_view.py`
- Test: `tick-vault/tests/test_union_view.py`

**Interfaces:**
- Consumes: Task 6 `attach`; a legacy `ticks_sip/<SYM>` Delta table (tests create a miniature one with the old columns: `date` index, `price`, `size`, `mkt`, `sub_mkt`, `seq`, `sl`, partition `day`).
- Produces: `install_union_view(con, root, legacy_root, watermarks: dict[str, date])` creating view `vault_trade_tick`: silver rows for `(symbol, week) >=` watermark (i.e. re-downloaded), legacy rows below it, legacy rows tagged `origin='LEGACY_UNVERSIONED'` and identity-joined through `pit_listing` at trade date; plus `union_provenance(symbol) -> dict` reporting the displacement frontier. Contract resolver (Task 8) reads `vault_trade_tick` instead of silver directly when a `legacy_root` is configured.

- [ ] **Step 1: Write the failing test** — legacy table holds AAPL weeks W1+W2; silver holds re-downloaded W2 (one price deliberately corrected vs legacy). Assert: query spanning W1–W2 returns W1 rows flagged `LEGACY_UNVERSIONED` and W2 rows from silver (corrected price, `origin='CAPTURE'`); no W2 duplicates; moving the watermark back serves W2 from legacy again; `union_provenance("AAPL")["frontier"]` equals the watermark.
- [ ] **Step 2: FAIL.** **Step 3:** implement (UNION ALL of two SELECTs with week predicates against the watermark table registered from a dict). **Step 4:** PASS. **Step 5:** Commit `feat(tick-vault): transitional union view over silver and legacy ticks_sip`.

---

### Task 10: CI wiring and full-suite gate

**Files:**
- Modify: `.github/workflows/` CI config (locate the job matrix that installs `tick-lab`/`openbb-deltalake`; add a `tick-vault` job mirroring the tick-lab one — install `-e "tick-vault[dev]"`, run `python -m pytest tick-vault/tests -q`).
- Test: the suite itself.

- [ ] **Step 1:** Run the complete suite: `python -m pytest tick-vault/tests -q` → all tasks' tests PASS (expected ~35 tests).
- [ ] **Step 2:** Add the CI job (copy the existing tick-lab job block, substitute paths).
- [ ] **Step 3:** Commit `ci(tick-vault): test job for the tick-vault package`.

---

## Deferred to Plan 2 (reference pipeline + re-backfill engine — needs EODHD key/NAS)
Bronze HTTP capture writer; EODHD reference ingestion (symbols/changes/delisted/fundamentals/constituents); OpenFIGI queue; membership interval builder; `ops.backfill_manifest` generator; the engine loop (span-halving, blacklist, budget, calibration week); reconciliation gate vs EOD/intraday references.

## Deferred to Plan 3 (integration — needs NAS/compose)
`openbb-deltalake` `as_of`/`effective` params delegating to `contract.query_ticks`; compose service + `vault-status` CLI/widget; displacement + archive of `ticks_sip`; performance threshold freezing; §19 acceptance suite against the real validation slice.

## Self-review notes
Spec coverage: §2 items 1+4 fully covered (Tasks 1–10); §2 items 2–3 explicitly deferred to Plans 2–3 with their spec sections. Placeholder scan: Task 5/7/9 Step-1 tests abbreviated to signatures+assertions where the pattern is identical to fully-written neighbors — each names its exact cases and expected values, no "TBD". Type consistency: `query_ticks` signature (Task 8) matches macros (Tasks 6–7); `DiffResult`/`ContractResult` used consistently; `origin` values `CAPTURE`/`LEGACY_UNVERSIONED` consistent across Tasks 2, 4, 9.

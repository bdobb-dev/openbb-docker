# tick-vault Reference Pipeline + Re-Backfill Engine Implementation Plan (Episode 15 — Plan 2 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The data-producing half of tick-vault: EODHD reference captures, security master + PIT membership, OpenFIGI resolution, and the manifest-driven re-backfill engine with its diff-aware settle passes, reconciliation gate, and calibration week.

**Architecture:** Builds on Plan 1's foundation (schemas, parser, differ, macros, contract). Every network fetch goes through one bronze capture writer; every parse lands via one Delta append writer that completes the parser's pandas frame into the full silver schema. The engine is `sip_backfill.py`'s proven loop re-hosted on the medallion path. All components take a transport/clock/root as parameters, so every task is unit-testable with fakes — no EODHD key needed for tests, only for live runs.

**Tech Stack:** Python 3.12, pandas>=2,<3, pyarrow>=16, deltalake>=1.0, duckdb>=1.0, pytest. urllib (stdlib) for HTTP, matching sip_backfill — no new HTTP framework.

**Spec:** `docs/superpowers/specs/2026-09-09-tick-vault-temporal-design.md` (baseline: `2026-09-09-sp500-temporal-tick-database-baseline.md`). Reference for engine behaviors: `scripts/sip_backfill.py` on branch `sip-backfill` (span-halving, blacklist, budget, week-completeness — carry over verbatim semantics).

**Execution environment note:** This plan REQUIRES pyarrow/duckdb/deltalake/pytest (desktop or CI; the authoring sandbox was network-blocked). Live-run tasks additionally need `EOD_API_KEY` and the NAS Delta root. Unit tests use fakes throughout and need neither.

## Global Constraints

- All silver writes append-only; retries and re-pulls are NEW captures (baseline §3.4, §10.2).
- Every HTTP request → exactly one `bronze.*_capture` row + one immutable payload file at `<root>/_payloads/<capture_id>.json.zst` (zstandard if available, else .gz); sha256 recorded; failures captured too (http_status, error body hash).
- Every pipeline run → one `ops.ingestion_run` row (baseline §17): run_id `run_*`, run_type, code_git_commit, config_hash, parser_version, availability_policy_version, output Delta versions.
- `available_at_ts` = the capture's `observed_at_ts`, always (AS_INGESTED truth; spec §6).
- Identity: matching hierarchy baseline §9.4 — steps 1–4 may auto-resolve; steps 5–6 NEVER auto-merge (queue for review).
- Membership boundary policy: `MEMBERSHIP_BOUNDARY_V1` = "EODHD join date is effective at that session's open; leave date means last member session = leave date" — versioned constant, cited in every backtest manifest (baseline §8.5).
- Bar inclusion policy: `BAR_INCL_V1` = regular session 09:30–16:00 ET only; `eligible_for_bars` sale conditions only (Plan 1 `decode_sl`); cancelled ticks excluded; late adds included at their market time.
- Engine behaviors carried from sip_backfill verbatim: per-symbol span-halving memory; scoped blacklist; 429 → 30-min sleep, 12 waits → 6-hour Budget stop; week complete from Saturday 08:00 UTC; boundary-second dedup on (ts, seq); market-cap-descending priority.
- EODHD Tick API params: `s`, `from`/`to` (unix seconds, INCLUSIVE both ends), `limit`, `api_token`; response records: ts, price, shares, seq, sl, mkt, sub_mkt, ex.
- Commit convention: feat/fix/test/ci/docs(tick-vault): ...; TDD per task.

---

### Task 1: Bronze capture writer

**Files:**
- Create: `tick-vault/tick_vault/capture.py`
- Test: `tick-vault/tests/test_capture.py`

**Interfaces:**
- Consumes: `ids.new_id("cap")`, `schemas.SCHEMAS["bronze.eodhd_tick_capture"]` (and sibling capture schemas).
- Produces: `CaptureStore(root)` with `record(*, table: str, endpoint: str, request_params: dict, http_status: int, payload_bytes: bytes | None, error_body: bytes | None, observed_at: datetime, ingestion_run_id: str, parser_version: str, extra_cols: dict | None) -> CaptureRecord(capture_id, payload_path, sha256, row_count_hint)`; `fetch_and_capture(transport, url_params...) -> (payload_or_None, CaptureRecord)`. `Transport` protocol: `get(url: str, timeout: int) -> (status: int, body: bytes)` — live impl `UrlLibTransport` with the sip_backfill retry/budget behavior (Task 7 wires budget); tests use `FakeTransport(responses: list)`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_capture.py (key cases; write all)
def test_success_capture_writes_payload_and_row(tmp_path):
    store = CaptureStore(str(tmp_path)); _create_all(tmp_path)
    payload = b'[{"ts": 1, "seq": 1}]'
    rec = store.record(table="bronze.eodhd_tick_capture", endpoint="/api/ticks",
        request_params={"s": "AAPL", "from": 1, "to": 2}, http_status=200,
        payload_bytes=payload, error_body=None, observed_at=OBS,
        ingestion_run_id="run_x", parser_version="TICK_PARSER_V1",
        extra_cols={"request_symbol": "AAPL", "request_from_sec": 1, "request_to_sec": 2})
    assert rec.sha256 == hashlib.sha256(payload).hexdigest()
    assert Path(rec.payload_path).exists()
    df = _read_delta(tmp_path, "bronze/eodhd_tick_capture")
    assert df.iloc[0]["raw_payload_sha256"] == rec.sha256 and df.iloc[0]["http_status"] == 200

def test_failure_capture_has_no_payload_but_row(tmp_path): ...  # status 500, error body hashed, payload_uri None
def test_retry_is_new_capture(tmp_path): ...                    # two record() calls, two rows, two capture_ids
def test_payload_immutable(tmp_path): ...                       # record() never overwrites an existing payload path
```

- [ ] **Step 2: RED** — `pytest tick-vault/tests/test_capture.py -q` fails on import.
- [ ] **Step 3: Implement** — payload files under `<root>/_payloads/YYYY/MM/<capture_id>.json.gz` (gzip, stdlib); `record` builds a one-row pandas frame with every schema column (None where n/a), appends via `deltalake.write_deltalake(..., mode="append", schema_mode="merge"=NO — exact schema, cast via pa.Table.from_pandas with SCHEMAS[table])`; a shared `append_rows(root, table, df)` helper exported for later tasks.
- [ ] **Step 4: GREEN.**  - [ ] **Step 5: Commit** `feat(tick-vault): bronze capture writer with immutable payload store`

---

### Task 2: Silver tick Delta writer (completes the parser frame)

**Files:**
- Create: `tick-vault/tick_vault/tick_writer.py`
- Test: `tick-vault/tests/test_tick_writer.py`

**Interfaces:**
- Consumes: `tick_parser.parse_tick_payload` (pandas, 29 cols), `capture.append_rows`, `schemas`.
- Produces: `write_tick_versions(root, df, *, source_system="EODHD", vendor_request_symbol: str, price_venue_type="CONSOLIDATED_US", committed_at: datetime) -> int` — fills the NOT NULL columns Plan 1's parser leaves out (flagged in tick_parser docstring: source_system, vendor_request_symbol, price_venue_type, committed_at_ts), casts to the silver arrow schema (Decimal price via `pa.compute` cast, list<string> flags), appends partitioned by trade_date, returns row count. Also `write_correction_events(root, events_df, committed_at)`.

- [ ] **Step 1: Failing tests** — parse the Plan-1 fixture; write; read back with deltalake; assert: row count, `origin=="CAPTURE"`, decimal price round-trips exactly (150.26), partition dir `trade_date=2021-11-05` exists, NOT NULL columns filled; second write appends (no replace); correction events write to `silver.tick_correction_event`.
- [ ] **Step 2: RED. Step 3: Implement. Step 4: GREEN. Step 5: Commit** `feat(tick-vault): silver tick writer completing parser frames into the delta schema`

---

### Task 3: EODHD reference clients and parsers

**Files:**
- Create: `tick-vault/tick_vault/eodhd_reference.py`
- Test: `tick-vault/tests/test_eodhd_reference.py` + `tests/fixtures/eodhd_ref/*.json` (handcrafted samples: exchange-symbol-list slice, delisted slice, symbol-change slice, GSPC.INDX components with HistoricalTickerComponents incl. one StartDate/EndDate pair, fundamentals slim slice with CUSIP+ISIN)

**Interfaces:**
- Consumes: `CaptureStore.fetch_and_capture`.
- Produces: `ReferenceClient(transport, capture_store, api_key)` with `get_exchange_symbols("US")`, `get_delisted("US")`, `get_symbol_changes("US")`, `get_index_components("GSPC.INDX")`, `get_fundamentals(symbol)` — each returns `(parsed_df, capture_record)`; parsed_df columns documented per endpoint (symbols: Code/Name/Exchange/Type/Isin; components: code, name, start_date, end_date, is_active, sector; changes: old, new, date; fundamentals: code, cusip, isin, cik, name, type, share_class). Endpoints (for live wiring): `https://eodhd.com/api/exchange-symbol-list/US`, `/api/fundamentals/GSPC.INDX` (Components + HistoricalTickerComponents), `/api/symbol-change-history`, `/api/fundamentals/<SYM>` — exact URLs verified during Phase-0/live smoke, parameterized in one `_ENDPOINTS` dict so a URL fix is one line.

- [ ] **Step 1: Failing tests** — with FakeTransport returning fixture bytes: each getter captures to the right bronze table, parses expected columns/values (incl. a component with `end_date` → is_active False; a fundamentals CUSIP surfaced).
- [ ] **Steps 2–5:** RED → implement → GREEN → commit `feat(tick-vault): EODHD reference clients with bronze capture`

---

### Task 4: Security master builder

**Files:**
- Create: `tick-vault/tick_vault/master.py`
- Test: `tick-vault/tests/test_master.py`

**Interfaces:**
- Consumes: reference parsed frames (Task 3), `ids.new_id`, `capture.append_rows`.
- Produces: `MasterBuilder(root)` with:
  - `upsert_from_symbols(symbols_df, capture_id, observed_at)` — for each vendor code not yet mapped: allocate `instrument_id`+`listing_id` (new `silver.instrument` + `silver.listing_version` rows) and an `identifier_assignment_version` row (namespace EODHD_SYMBOL, effective_from NULL = since listing start); idempotent — an existing open assignment for the same code+listing yields no new rows (test this).
  - `apply_symbol_changes(changes_df, capture_id, observed_at)` — old code assignment gets `effective_to_ts = change date`; new code gets a new assignment to the SAME listing_id (baseline §9.1); an unknown old code → review queue row, no guess.
  - `attach_issue_ids(fundamentals_df, ...)` — CUSIP/ISIN/CIK assignment rows where present (namespaces CUSIP, ISIN, SEC_CIK) attached per baseline §9.2 scopes.
  - `resolution: dict[str, str]` helper `resolve_symbol_at(code, date) -> listing_id | None` reading assignments (used by manifest generator before DuckDB is warm).
  - The persistent ID registry IS the identifier_assignment table — no side registry file (single source of truth).
- Review queue: unresolvable/ambiguous cases append `ops.openfigi_resolution_queue` rows with reason codes (`UNKNOWN_OLD_CODE`, `AMBIGUOUS_MATCH`, `NEEDS_FIGI`).

- [ ] **Step 1: Failing tests** (write all; key cases)

```python
def test_new_symbol_allocates_instrument_listing_assignment(lake): ...
def test_upsert_idempotent(lake): ...                    # run twice, same row counts
def test_symbol_change_closes_old_opens_new_same_listing(lake):
    # BK -> BNY: after apply, resolve_symbol_at("BK", 2026-05-01) == lst; ("BNY", 2026-08-01) == same lst;
    # ("BK", 2026-08-01) is None (closed)
def test_unknown_old_code_queued_not_guessed(lake): ...
def test_ticker_reuse_two_listings(lake): ...            # same code, disjoint effective windows, distinct listings
def test_cusip_attached_at_instrument_scope(lake): ...
```

- [ ] **Steps 2–5:** RED → implement → GREEN → commit `feat(tick-vault): security master builder with dated identifier assignments`

---

### Task 5: Membership interval builder

**Files:**
- Create: `tick-vault/tick_vault/membership.py`
- Test: `tick-vault/tests/test_membership.py`

**Interfaces:**
- Consumes: components frame (Task 3), `master.resolve_symbol_at`, `capture.append_rows`.
- Produces: `build_membership(root, components_df, *, capture_id, observed_at, boundary_policy="MEMBERSHIP_BOUNDARY_V1") -> MembershipReport(resolved, ambiguous, unresolved)` writing `silver.index_membership_version` rows per baseline §8.3: resolve each vendor code to a dated listing_id (resolution at `start_date`); unresolved → row with `resolution_status='UNRESOLVED'`, listing_id NULL, source text retained; overlapping resolved intervals for same (index, listing) → both rows written but flagged via `ops.data_quality_issue` + the later one `AMBIGUOUS` (never silently dropped); `membership_effective_from/to` per the boundary policy; `index_id='idx_sp500'`, vendor symbol `GSPC.INDX`.

- [ ] **Step 1: Failing tests** — resolved tenure row lands with dates; delisted member resolves via historical assignment; unknown code → UNRESOLVED retained; overlap → quality issue + AMBIGUOUS; re-run with identical input is idempotent (supersedes chain NOT created for byte-identical facts — dedup on (source_constituent_code, effective dates, capture content hash)); boundary policy string stored.
- [ ] **Steps 2–5:** commit `feat(tick-vault): bitemporal S&P 500 membership builder`

---

### Task 6: OpenFIGI resolution queue worker

**Files:**
- Create: `tick-vault/tick_vault/figi.py`
- Test: `tick-vault/tests/test_figi.py` + fixture `tests/fixtures/openfigi_response.json`

**Interfaces:**
- Consumes: `CaptureStore`, queue rows, identifier assignments (CUSIP/ISIN preferred keys, ticker+MIC fallback per baseline §9.4 order).
- Produces: `FigiWorker(transport, capture_store, root, api_key=None)` `.run_batch(limit=100)` — drains `ops.openfigi_resolution_queue` NEEDS_FIGI rows, POSTs OpenFIGI mapping requests (batched ≤100 jobs; rate limit: sleep 60/25 s between batches keyless, 60/250 with key), captures to `bronze.openfigi_mapping_capture`, writes `silver.figi_assignment_version` rows (figi_level from response: shareClassFigi→SHARE_CLASS, compositeFIGI→COMPOSITE, figi→VENUE) with match_status; no-match → queue row status NO_MATCH kept (visible), never fabricated.

- [ ] **Step 1: Failing tests** — fake transport returns fixture: three-level assignments written for a CUSIP-keyed job; keyless rate-limit sleep called (inject fake sleeper); no-match handled; batch caps at 100.
- [ ] **Steps 2–5:** commit `feat(tick-vault): OpenFIGI resolution worker`

---

### Task 7: Manifest generator + engine fetch core

**Files:**
- Create: `tick-vault/tick_vault/engine.py`
- Test: `tick-vault/tests/test_engine.py`

**Interfaces:**
- Consumes: membership (DuckDB `pit_sp500_membership` or direct read), `master.resolve_symbol_at`, `CaptureStore`, `tick_parser`, `tick_writer`, `schemas`.
- Produces:
  - `generate_manifest(root, first_week, last_week) -> int` — for each week (Monday) and each listing with membership tenure overlapping the week (spec: an add mid-week gets the whole week), one `ops.backfill_manifest` row: work_id `wrk_*`, listing_id, week_monday, vendor_symbol_at_date (resolved AS OF that week — resolution failure → status BLOCKED_IDENTITY), request_from_sec/to_sec (Monday 00:00 UTC .. Saturday 00:00 UTC minus 1s, inclusive-boundary rule), status PENDING, priority = market-cap rank. Idempotent: existing (listing_id, week_monday) rows untouched.
  - `fetch_week(transport, store, root, item, *, span_memory, budget) -> FetchResult` — the sip_backfill core re-hosted: request the whole week; 5xx-timeout → halve span (persisting per-symbol working span in `span_memory` dict, serialized into the manifest row's details); 429 → budget.wait() (30 min, Budget exception after 12); each HTTP call is one bronze capture; pages concatenated; dedup (ts, seq); parse via `parse_tick_payload` per capture (page ordinals); write via `write_tick_versions`; verify read-back row count + (trade_date, session_seq) uniqueness; manifest row → COMPLETE/PARTIAL/FAILED with attempts++, wall_minutes recorded.
  - `Budget` class replicating sip_backfill semantics (injectable clock/sleeper for tests).

- [ ] **Step 1: Failing tests** (fakes only)

```python
def test_manifest_week_overlap_includes_midweek_add(lake): ...
def test_manifest_blocked_identity_when_unresolvable(lake): ...
def test_fetch_week_happy_path_writes_bronze_silver_and_completes(lake): ...
def test_fetch_week_halves_span_on_slow_5xx_and_remembers(lake):
    # FakeTransport: full-week request -> slow 500; two half-week -> 200; span_memory["MEGA"] == 3.5 days
def test_fetch_week_budget_429_waits_then_succeeds(lake): ...   # fake sleeper records 1800s sleep
def test_boundary_second_dedup(lake): ...                        # same (ts,seq) across two window responses -> one row
def test_failed_week_leaves_manifest_retryable(lake): ...
def test_rerun_completed_tranche_is_idempotent(lake): ...        # delete-and-rewrite its partition slice, same counts
```

- [ ] **Steps 2–5:** commit `feat(tick-vault): manifest generator and re-backfill fetch core`

---

### Task 8: Diff-aware settle and daily passes

**Files:**
- Create: `tick-vault/tick_vault/settle.py`
- Test: `tick-vault/tests/test_settle.py`

**Interfaces:**
- Consumes: `fetch_week` internals (refactor: `engine._fetch_raw_week(transport, store, item) -> (df_parsed, captures)` shared), `diffing.diff_window`, `temporal` latest view (DuckDB) for existing rows, `tick_writer`.
- Produces: `settle_pass(root, transport, store, item, observed_at)` — re-fetch the window; load CURRENT silver latest non-cancelled versions for (listing, week) via DuckDB `latest_ticks()` filtered to the week; `diff_window(existing, incoming, revealing_capture_id=<the settle capture>, observed_at=...)`; append `new_versions` + `events` (NEVER replace); manifest row settle timestamps updated. `daily_pass(...)` = same mechanics over trailing 7 days for current members (late prints arrive as LATE_ADD; corrections as REVISED).

- [ ] **Step 1: Failing tests** — seeded silver week + fake re-fetch payload differing exactly as Plan 1's diff fixtures (changed 1002, vanished 1003, new 1004): after settle, silver holds rev-2 REVISED with settle-capture knowledge time, tombstone, late-add; correction events written; unchanged rows produced NO new versions (append-only row count check); a second identical settle is a no-op (diff empty).
- [ ] **Steps 2–5:** commit `feat(tick-vault): diff-aware settle and daily passes`

---

### Task 9: Reconciliation gate

**Files:**
- Create: `tick-vault/tick_vault/reconcile.py`
- Test: `tick-vault/tests/test_reconcile.py`

**Interfaces:**
- Consumes: `ReferenceClient`-style EOD/intraday getters (add `get_eod(symbol, from, to)`, `get_intraday(symbol, interval, from, to)` to Task 3's client — endpoints `/api/eod/<SYM>`, `/api/intraday/<SYM>`), `decode_sl` eligibility, DuckDB over silver.
- Produces:
  - `aggregate_bars(con, listing_id, trade_date, interval='1d'|'1m'|'5m', policy="BAR_INCL_V1") -> df` — DuckDB SQL over latest non-cancelled versions: regular session (trade_ts in 09:30–16:00 America/New_York), rows whose sale_condition_flags imply eligible (recompute eligibility from sale_condition_raw via decode_sl in a projected column at write time? NO — flags are stored; filter `NOT list_contains(flags,'...')` set derived from decode table; document policy constants in SQL).
  - `reconcile_tranche(root, con, client, item, *, tolerance=ReconcileTolerance(close_pct=0.001, volume_pct=0.02, minute_close_pct=0.005)) -> ReconcileReport(status: PASS|DIVERGENT, diffs_df)` — captures reference EOD + 1m bars (bronze), compares OHLCV; divergences → `ops.data_quality_issue` rows with per-field deltas.
  - `Gate(n_gated_tranches)` — `.check(manifest_row, report)`: within the first N tranches (config, default 2000) a DIVERGENT report holds the row at PARTIAL with reason RECONCILE_HOLD (never COMPLETE) until fixed or waived (`waive(work_id, reason)` writes a quality-issue row with the waiver); after N, DIVERGENT logs the issue but does not hold.
- [ ] **Step 1: Failing tests** — synthetic ticks (incl. one out-of-sequence print, one cancelled, one extended-hours) aggregate to known OHLCV excluding the right rows; matching reference → PASS; mismatched close beyond tolerance → DIVERGENT + issue row; gate holds inside N, passes-with-log outside N; waiver path.
- [ ] **Steps 2–5:** commit `feat(tick-vault): OHLCV reconciliation gate against EOD and intraday references`

---

### Task 10: Engine loop, calibration mode, CLI

**Files:**
- Create: `tick-vault/tick_vault/loop.py`, `tick-vault/tick_vault/cli.py` (entry point `vault` via pyproject [project.scripts])
- Test: `tick-vault/tests/test_loop.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `run_cycle(ctx) -> CycleAction` — priority order per spec §5/sip_backfill: (0) daily pass once per day after 02:05 NY; (1) newest complete unloaded week; (2) week due for settle (live-edge weeks only — trade_date within SETTLE_HORIZON_DAYS=14 of today; pre-2026 history NEVER settles, spec decision); (3) failed-retry (>24h); (4) next week backwards; idle → sleep signal. Pure function over manifest state + clock (fully testable).
  - `vault backfill --loop|--week 2026-09-01|--until 2016-01-01`, `vault settle --week`, `vault manifest --generate FIRST LAST`, `vault reference --sync` (symbols+delisted+changes+components+fundamentals for members), `vault figi --drain`, `vault status` (manifest progress, displacement frontier, open quality issues, budget spend — table output), `vault calibrate --week 2026-08-31` — runs ONE representative week end-to-end (reference sync assumed done), records per-call wall time, rows, bytes written; then reads legacy `ticks_sip/_progress/*.json` wall_minutes if reachable at --legacy-root; writes `docs/superpowers/verification/2026-XX-XX-ep15-calibration.md` with the frozen projection (calls, budget-days, wall-clock weeks, TB) computed from measured medians × 522 weeks × universe. Gate: `vault backfill --loop` REFUSES to start the historical walk unless a calibration report exists and reconciliation Phase-A is configured (override flag `--i-know-what-im-doing` logged to ingestion_run).
- [ ] **Step 1: Failing tests** — run_cycle priority table (each branch via synthetic manifest/clock states); calibrate assembles a projection from fake measurements; loop-refusal without calibration report; CLI arg wiring smoke tests (invoke handlers with monkeypatched cores).
- [ ] **Steps 2–5:** commit `feat(tick-vault): engine loop, calibration mode, and vault CLI`

---

### Task 11: Status endpoint (widgets.json pattern) + compose service

**Files:**
- Create: `tick-vault/tick_vault/status_app.py` (stdlib http.server or the repo's existing pattern — inspect `stores-explorer/` and mirror its widgets.json backend shape), `tick-vault/Dockerfile`
- Modify: `docker-compose.yml` (new `tick-vault` service, env: EOD_API_KEY, VAULT_ROOT, LEGACY_ROOT; profiles so it doesn't autostart until Art enables), `.github/workflows/ci.yml` if the compose lint job needs the new service listed.
- Test: `tick-vault/tests/test_status_app.py`

**Interfaces:**
- Produces: GET `/widgets.json` (status widget descriptor, stores-explorer pattern), GET `/status` JSON: {manifest: {pending, in_progress, complete, partial, failed, blocked_identity}, frontier: {min_complete_week, max_complete_week}, quality_issues_open, budget: {calls_today, last_429}, calibration: present|absent}. Dockerfile mirrors sip-backfill's (python:3.12-slim + tick-vault package), local-only image shipped by `docker save | ssh nas docker load`.
- [ ] **Step 1: Failing tests** — /status against a seeded manifest returns correct counts; /widgets.json parses.
- [ ] **Steps 2–5:** commit `feat(tick-vault): status endpoint and compose service`

---

### Task 12: Live smoke + Phase-0 contract verification (NAS/desktop, needs EODHD key)

**Files:**
- Create: `docs/superpowers/verification/2026-XX-XX-ep15-phase0.md` (findings doc)
- Test: `tick-vault/tests/live/test_live_smoke.py` (pytest -m live, skipped by default)

This is the task where Phase 0 of the spec closes:
- [ ] **Step 1:** One live tick request (SPY, one hour) through `fetch_and_capture` — verify real schema fields against parser expectations; record the ACTUAL `sl` codes seen; verify `seq` semantics (per-session uniqueness) on real data.
- [ ] **Step 2:** Hand-verify the `sl` decode table against the observed codes + EODHD docs; bump `SL_DECODE_V1` → V2 with corrections if needed (test fixtures updated).
- [ ] **Step 3:** One live GSPC.INDX components pull — verify HistoricalTickerComponents field names/date semantics against Task 3's parser; document the membership boundary evidence for `MEMBERSHIP_BOUNDARY_V1`.
- [ ] **Step 4:** Run `vault calibrate` on one representative recent week; commit the calibration report; review the frozen projection with Art BEFORE `--loop` starts (explicit approval gate — the ~300k-call walk is his money).
- [ ] **Step 5: Commit** `docs(tick-vault): phase-0 contract verification and calibration report`

---

## Not in this plan (Plan 3)
`openbb-deltalake` as_of/effective params delegating to `contract.query_ticks`; displacement/archival of ticks_sip; performance-threshold freezing into CI; §19 acceptance suite against the real validation slice (FB/META, TWTR, GE, GOOG/GOOGL, AAPL weeks); substack episode stub content.

## Self-review notes
Spec coverage: spec §2 item 2 (reference pipeline) = Tasks 3–6; item 3 (engine) = Tasks 1–2, 7–10; §8 gate = Task 9; calibration + Phase-0 = Tasks 10, 12; ops surface = Task 11; ingestion_run contract = Task 1 + Global Constraints (each CLI verb opens/closes a run — wired in Task 10's ctx). Placeholder scan: endpoint URLs are parameterized pending Phase-0 verification by design (documented, one dict), not TBDs; Task 9's flag-eligibility SQL decision is stated (stored flags filtered, not re-decoded). Type consistency: `fetch_week`/`settle_pass` share `_fetch_raw_week`; manifest columns match Task-2-of-Plan-1's schema (vendor_symbol_at_date, week_monday, wall_minutes present); `write_tick_versions` fills exactly the four columns tick_parser's docstring names. Sequencing hazard from Plan 1 carried: reference sync (Tasks 3–5) MUST land identity before manifest/backfill for a symbol — enforced structurally: generate_manifest resolves via assignments and BLOCKs otherwise.

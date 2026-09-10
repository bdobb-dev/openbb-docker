# tick-vault ODP Integration, Acceptance Suite + Cutover Plan (Episode 15 — Plan 3 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the temporal contract live on the OpenBB Platform surface, prove the system against the spec's §19 acceptance tests on real data, freeze performance thresholds, run the transition (displacement → retirement of `ticks_sip`), and ship the episode's series artifacts.

**Architecture:** `openbb-deltalake` gains `as_of`/`effective` and delegates to `tick_vault.contract.query_ticks` — no temporal logic in the provider. Acceptance and performance suites are pytest marked `acceptance`/`perf`, excluded from default CI, run against the real lake (NAS or Mac-over-share). Displacement and retirement are manifest-driven state transitions with explicit human gates.

**Tech Stack:** As Plans 1–2, plus the existing `openbb-deltalake` package (openbb provider extension — inspect `openbb-deltalake/openbb_deltalake/` and follow its patterns exactly).

**Spec:** `docs/superpowers/specs/2026-09-09-tick-vault-temporal-design.md` (§3 contract, §5 displacement/cutover, §10 testing; baseline §19 acceptance tests, §19.5 performance).

**Execution note:** Tasks 1–2 are desktop/CI-runnable with fakes; Tasks 3–8 need the real lake (Plan 2 backfill at least partially advanced — the validation-slice weeks must be re-downloaded first); Task 8's final step and Task 9 need Art.

## Global Constraints

- ODP behavior with NO temporal params is byte-identical to today's (non-breaking; spec §3 mode 1).
- Provider holds zero temporal logic: every mode decision, warning, and provenance field comes from `tick_vault.contract` (spec decision 4).
- Provenance block keys (already produced by contract): mode, availability_policy_version, sequence_policy_version, sl_decode_version, parser_version, delta_versions, warnings, origin_flags — surfaced VERBATIM in the ODP result metadata.
- Acceptance fixtures (approved): FB→META (rename), TWTR (cash acquisition/delist 2022-10-27), GE (spin-offs), GOOG/GOOGL (share classes), one reused ticker (pinned in Task 3 from real data), AAPL (liquid control). Exact dates pinned in Task 3 and recorded in `tick-vault/tests/acceptance/slice.py` as the single source of truth.
- Performance = measure-first: thresholds frozen FROM measurements with 25% margin, never invented (spec §10).
- Displacement gate: a week leaves legacy serving only when new silver reconciles against `ticks_sip` counts or the difference is explained by logged events (spec §6/§9 of the design sections).
- Retirement is Art's explicit call; archival before removal; nothing deleted in this plan (spec decision 9 — archived, not deleted).
- pytest markers: `acceptance`, `perf`, `live` — all excluded from the default CI job (`-m "not acceptance and not perf and not live"` added to the Plan-1 CI job in Task 3).
- Commits: feat/fix/test/docs(tick-vault|openbb-deltalake): ...; TDD where the deliverable is code.

---

### Task 1: openbb-deltalake temporal params (fake-lake TDD)

**Files:**
- Modify: `openbb-deltalake/openbb_deltalake/` — the query/fetcher path serving tick/historical reads (inspect `store.py`, `accessor.py`, `models/historical.py` first; follow the existing QueryParams pattern)
- Test: `openbb-deltalake/tests/test_temporal_params.py`
- Modify: `openbb-deltalake/pyproject.toml` (add dependency `tick-vault`)

**Interfaces:**
- Consumes: `tick_vault.contract.query_ticks(con, root, *, symbols, start, end, as_of, effective, availability_policy, legacy_root, watermarks)` and `ContractResult(df, provenance)`.
- Produces: the provider's tick/historical QueryParams gain `as_of: Optional[datetime] = None`, `effective: Optional[date] = None`, `availability_policy: str = "AS_INGESTED_LOCAL_V1"`; the fetcher branches: any temporal param present (or an env/config flag `VAULT_ROOT` set and `use_vault=True`) → delegate to `query_ticks`; result metadata/`extra` carries the provenance dict under key `"tick_vault_provenance"`; warnings surfaced through the provider's warning mechanism.

- [ ] **Step 1: Write the failing tests** — against a `tmp_path` mini-lake seeded with Plan-1's contract-test seed helper (import from `tick-vault/tests/deferred/test_contract.py`'s `_seed_contract_lake`, refactored into `tick_vault.testing` module so both packages can use it — do that refactor in this task, Files += `tick-vault/tick_vault/testing.py`):

```python
def test_no_temporal_params_bypasses_vault(monkeypatch): ...
    # fetcher with as_of/effective absent and use_vault unset -> legacy code path called (spy), query_ticks NOT called
def test_as_of_param_delegates_and_carries_provenance(tmp_path): ...
    # as_of given -> result.extra["tick_vault_provenance"]["mode"] == "PIT_KNOWLEDGE"
def test_both_params_bitemporal(tmp_path): ...
def test_warnings_surface(tmp_path): ...       # unresolved symbol warning reaches the provider warning list
def test_provider_has_no_mode_logic(): ...
    # ast check: openbb_deltalake source contains none of the strings "PIT_KNOWLEDGE"/"BITEMPORAL"/"EFFECTIVE_ONLY"
```

- [ ] **Step 2: RED. Step 3: Implement (thin delegation + param plumbing). Step 4: GREEN (`pytest openbb-deltalake/tests -q`). Step 5: Commit** `feat(openbb-deltalake): as_of/effective temporal params delegating to tick-vault`

---

### Task 2: Contract conformance suite (the delegation's safety net)

**Files:**
- Create: `tick-vault/tests/test_contract_conformance.py` (runs in default CI)

**Interfaces:** none new — this pins the contract surface `openbb-deltalake` now depends on.

- [ ] **Step 1: Failing tests** — signature stability (`inspect.signature(query_ticks)` matches the documented parameter list exactly); provenance dict keys exactly the eight documented; mode strings exactly the four; ContractResult fields (`df`, `provenance`). Breaking any of these must break CI *in tick-vault*, not only downstream.
- [ ] **Steps 2–5:** RED (a couple will pass immediately — acceptable here, the suite is a ratchet; note it in the report) → implement/adjust → GREEN → commit `test(tick-vault): contract conformance suite pinning the ODP delegation surface`

---

### Task 3: Pin the validation slice + acceptance scaffolding

**Files:**
- Create: `tick-vault/tests/acceptance/__init__.py`, `slice.py`, `conftest.py`
- Modify: `.github/workflows/ci.yml` (default job gains `-m "not acceptance and not perf and not live"`)

**Interfaces:**
- Produces: `slice.py` constants: `SLICE_SYMBOLS`, `SLICE_WEEKS` (2–3 weeks spanning a membership change), and per-fixture pinned dates: `META_RENAME_DATE` (2022-06-09, FB→META), `TWTR_DELIST_DATE` (2022-10-27 last session, cash acquisition — target ends), `GE_SPINOFF_DATES` (GEHC 2023-01-04, GEV 2024-04-02), `GOOG_GOOGL = ("GOOG","GOOGL")`, `REUSED_TICKER` + its two tenure windows, `CONTROL="AAPL"`. `conftest.py`: session fixture `real_lake` (VAULT_ROOT env, skip suite with a clear message if unset or if manifest lacks the slice weeks COMPLETE), DuckDB con with attach+macros installed.
- **The reused ticker is pinned from REAL membership data** in this task, not guessed: query `silver.index_membership_version`+assignments for a code with two disjoint listing tenures inside the backfilled range; record it with evidence in the task report and slice.py comment.

- [ ] **Step 1:** RED = the skip-message path (run suite without VAULT_ROOT → all acceptance tests skip with the documented message, 0 failures). **Step 2:** implement scaffolding. **Step 3:** with VAULT_ROOT set on the desktop, verify the slice-completeness check runs (PASS or a truthful "slice not yet backfilled" skip). **Step 4: Commit** `test(tick-vault): acceptance scaffolding and pinned validation slice`

---

### Task 4: §19.1 + §19.2 acceptance — membership and identity (real data)

**Files:**
- Create: `tick-vault/tests/acceptance/test_membership.py`, `test_identity.py`

- [ ] **Step 1: Write all eight tests** (marker `acceptance`; each cites its spec clause):

```python
# §19.1
def test_current_member_absent_from_2017_universe(real_lake): ...   # a post-2017 joiner from slice, 2017 market_date
def test_former_member_present_within_tenure(real_lake): ...        # TWTR present 2021-06-01, absent 2023-01-01
def test_reused_ticker_distinct_listings(real_lake): ...            # REUSED_TICKER windows -> different listing_id
def test_unresolved_membership_visible_and_excluded(real_lake): ... # count UNRESOLVED rows >= 0 via direct view; macro excludes them
# §19.2
def test_fb_meta_same_listing(real_lake): ...                       # pit_listing("FB", 2022-05-02) == pit_listing("META", 2022-07-01)
def test_goog_googl_distinct_instruments_same_issuer(real_lake): ...
def test_twtr_history_ends_unconcatenated(real_lake): ...           # no tick rows for TWTR listing after delist date
def test_share_class_identity_distinct(real_lake): ...              # GOOG vs GOOGL tick rows never share listing_id
```

- [ ] **Step 2:** Run on the real lake (desktop/NAS). Failures here are FINDINGS about the Plan-2 pipeline, not test bugs — file each as `ops.data_quality_issue` and fix in the pipeline (fix-loop through the responsible module), then re-run. **Step 3: Commit** `test(tick-vault): §19.1/§19.2 acceptance — membership and identity on the validation slice`

---

### Task 5: §19.3 + §19.4 acceptance — ticks and PIT (real data)

**Files:**
- Create: `tick-vault/tests/acceptance/test_ticks.py`, `test_pit.py`

- [ ] **Step 1: Write all nine tests:**

```python
# §19.3
def test_overlap_redownload_no_duplicates(real_lake): ...   # re-run one COMPLETE tranche via engine.fetch_week; gold/latest counts unchanged
def test_every_tick_traces_to_bronze(real_lake): ...        # sample 1000 rows: source_capture_id joins to bronze rows, source_row_ordinal NOT NULL
def test_same_ms_trades_not_collapsed(real_lake): ...       # find a same-ms same-price same-size pair in slice; both present (distinct session_seq)
def test_replay_determinism(real_lake): ...                 # tape_order over one (listing, date) twice -> identical sequence incl. synthetic_tape_seq assignment
def test_synthetic_seq_never_labeled_official(real_lake): ...  # gold sequence_method == 'SYNTHETIC'; policy version stamped
# §19.4
def test_correction_invisible_before_capture(real_lake): ...   # pick a real correction event from silver.tick_correction_event in slice;
                                                               # pit at (observed_at - 1s) serves superseded version; at (+1s) the superseding one
def test_delayed_feed_unavailable_before_available_at(real_lake): ...
def test_future_mapping_cannot_resolve_past(real_lake): ...    # META symbol at 2021 market_date with 2026 as_of -> resolves (known now);
                                                               # with as_of before our ingestion -> nothing (AS_INGESTED honesty, spec §6)
def test_pinned_rerun_bit_identical(real_lake): ...            # query_ticks twice with same params; delta_versions equal; df.equals(); sha256 of csv equal
```

- [ ] **Step 2:** Run, triage failures as pipeline findings (as Task 4). Note: if no natural correction event exists in the slice yet (young settle history), MANUFACTURE one honestly: run a settle pass against a week where EODHD returns a changed row, or inject a documented synthetic correction through the real diff path in a scratch listing — never fake the assertion. **Step 3: Commit** `test(tick-vault): §19.3/§19.4 acceptance — tick integrity and PIT on the validation slice`

---

### Task 6: Performance benchmarks + threshold freezing

**Files:**
- Create: `tick-vault/tests/perf/test_benchmarks.py`, `tick-vault/tick_vault/bench.py`
- Create (output): `docs/superpowers/verification/2026-XX-XX-ep15-benchmarks.md`

**Interfaces:**
- Produces: `bench.run_shapes(con, slice) -> dict[shape, seconds]` for the §19.5 shapes: `one_day_one_listing_replay` (tape_order over AAPL × 1 day), `one_month_universe_scan` (all slice listings × ~20 sessions, latest bars), `pit_resolution_per_date` (pit_sp500_membership once per date over a month — asserting it's called per-date not per-tick is a query-plan check: EXPLAIN output contains one membership scan), `gold_scan_prunes_partitions` (EXPLAIN ANALYZE shows partition pruning on trade_date).
- `test_benchmarks.py` (marker `perf`): runs shapes on BOTH paths where reachable (NAS root, Mac-over-share root — two env vars, skip absent ones); FIRST run writes measured numbers into the verification doc; thresholds = measured × 1.25 committed INTO the test file as constants in a follow-up edit (the freeze); subsequent runs assert against frozen thresholds.

- [ ] **Step 1:** implement bench + tests with thresholds `None` (report-only mode). **Step 2:** run on real hardware, commit the verification doc with measurements. **Step 3:** freeze: write thresholds into the test constants; re-run; assert green. **Step 4: Commit** `test(tick-vault): §19.5 performance shapes with frozen measured thresholds`

---

### Task 7: Displacement tracking (watermarks advance as re-download completes)

**Files:**
- Create: `tick-vault/tick_vault/displacement.py`
- Modify: `tick_vault/cli.py` (`vault displace` verb), `tick_vault/loop.py` (auto-displace after tranche COMPLETE)
- Test: `tick-vault/tests/test_displacement.py` (fake-lake, default CI)

**Interfaces:**
- Consumes: manifest (COMPLETE tranches), legacy `ticks_sip` counts, union-view watermarks, reconcile reports.
- Produces: `compute_watermarks(root, legacy_root) -> dict[symbol, date]` — per symbol, the EARLIEST week-monday such that every week from there to the live edge is COMPLETE **and displacement-gated**: new silver row count for the week reconciles against legacy count (exact match, or difference fully explained by `tick_correction_event` rows for that week — count cancellations/late-adds) — else the watermark stays above that week and a `ops.data_quality_issue(kind='DISPLACEMENT_MISMATCH')` row is filed. `persist_watermarks(root, wm)` → a small `ops.displacement_watermark` Delta table (Files += schema addition to `schemas.py`: symbol, watermark, computed_at_ts, gate_evidence_json — append-only, latest row per symbol wins); `load_watermarks(root)` consumed by `query_ticks`'s union path and the compose service env. `vault status` gains the displacement frontier per symbol.

- [ ] **Step 1: Failing tests** — synthetic manifest+legacy+silver states: contiguous COMPLETE+reconciled weeks advance the watermark; a count mismatch unexplained by events holds it and files the issue; an explained mismatch (one cancellation event) advances; persistence round-trips; latest-row-wins on re-compute.
- [ ] **Steps 2–5:** commit `feat(tick-vault): displacement watermarks with reconciliation gate`

---

### Task 8: Retirement gate and archival (nothing deleted)

**Files:**
- Create: `tick-vault/tick_vault/retirement.py`, docs section in `tick-vault/README.md`
- Modify: `cli.py` (`vault retire --check | --archive DEST | --collapse`)
- Test: `tick-vault/tests/test_retirement.py` (fake-lake)

**Interfaces:**
- `retire_check(root, legacy_root) -> RetireReport` — ALL symbols' watermarks at the earliest backfilled week; zero open DISPLACEMENT_MISMATCH issues; full-history reconciliation summary (count comparison per symbol-week, all explained); report rendered to a markdown file for Art.
- `archive(legacy_root, dest)` — tar (or `cp -a`) of `ticks_sip` to DEST with a manifest (per-symbol row counts + sha of Delta logs); VERIFY the archive (recount) before reporting success; never removes the source.
- `collapse(root)` — records `ops.ingestion_run` of type CUTOVER; from then on the compose env drops LEGACY_ROOT so `query_ticks` serves silver/gold only (the union view code stays until a later cleanup episode; behavior collapses via config, code removal is out of scope).
- **Human gates (explicit steps, not code):** `vault retire --check` output goes to Art; `--archive` runs only after his yes; physically removing/quiescing `ticks_sip` and stopping the old sip-backfill loop is Art's manual action at the NAS, documented in README — this plan performs no deletion.

- [ ] **Step 1: Failing tests** — retire_check fails with one symbol short / one open mismatch; passes on the full synthetic state; archive writes+verifies manifest; collapse records the run and the config flip is respected by a subsequent query_ticks call (legacy_root None → no legacy branches).
- [ ] **Steps 2–5:** commit `feat(tick-vault): retirement gate, verified archival, and cutover collapse`

---

### Task 9: Series ritual artifacts

**Files:**
- Create: `substack-articles/15-temporal-tick-vault/outline.md` (+ `screenshots/SHOT-LIST.md` stub) — in the substack-articles repo working tree (separate repo: coordinate with Art if it should ride a different branch; if unreachable from the execution environment, place under `docs/episode-15-article/` in openbb-docker and note the move)
- Modify: `docs/release-boundaries.md` (episode 15 row: what v15.0.0 ships), `README.md` (tick-vault section), `THIRD_PARTY_NOTICES.md` if new deps demand it
- Create: `docs/superpowers/verification/2026-XX-XX-v15.0.0-execution.md` (the episode's verification doc, series pattern)

- [ ] **Step 1:** Outline per the series voice (study `substack-articles/11-arcticdb-explorer/` + `episodes-10-13-plan.md` framing): the episode's spine = "the store that remembers what it knew and when" — survivorship bias demo (2017 universe query before/after), the correction that time-travels, the ~300k-call decision with the calibration numbers, honest limits (AS_INGESTED emptiness before 2026, simulated policy opt-in). Shot list: vault status frontier, a PIT query pair in the explorer, the reconciliation gate holding a tranche.
- [ ] **Step 2:** release-boundaries + README edits; verification doc skeleton listing the §19 suite results and benchmark numbers (filled by Tasks 4–6 outputs).
- [ ] **Step 3: Commit** `docs(tick-vault): episode 15 article outline, release boundaries, verification doc`
- [ ] **Step 4 (with Art, out of band):** tag v15.0.0 once he merges and the acceptance suites are green on the NAS.

---

## Self-review notes
Spec coverage: §3 contract on ODP = Tasks 1–2; §10 acceptance = Tasks 3–5 (§19.1–.4 all mapped, each test cites its clause) + Task 6 (§19.5, measure-first honored); transition/cutover (spec §5 displacement + decision 9 archival) = Tasks 7–8 with the human gates the spec requires; episode-done checklist items (substack stub, series artifacts) = Task 9. Placeholder scan: Task 3 pins the reused ticker from real data by procedure (not TBD — the procedure is the content); benchmark thresholds intentionally two-phase (report → freeze) per the approved measure-first decision; verification-doc dates use 2026-XX-XX placeholders because they are execution-day artifacts — named as such. Type consistency: `compute_watermarks` output feeds `query_ticks(watermarks=...)` (dict[str, date] — matches Plan 1's union signature); `ops.displacement_watermark` added via schemas.py in Task 7's Files list; conformance suite (Task 2) pins the exact surface Task 1 consumes. Sequencing: Tasks 4–6 require the slice weeks COMPLETE via Plan 2 — enforced by Task 3's conftest completeness check (skip, not fail), so the suite is safe to land before the backfill reaches the slice.

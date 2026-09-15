# tick-vault: The Temporal Tick Database — Episode 15.0.0 Design

**Date:** 2026-09-09 · **Status:** APPROVED (brainstormed section-by-section with Art; all sections approved)
**Baseline:** `2026-09-09-sp500-temporal-tick-database-baseline.md` (Art's reviewed design doc, committed
alongside this spec). The baseline's schemas, principles, and acceptance tests are adopted wholesale;
this spec records the episode's decisions, the deltas from the baseline, and the episode boundary.
**Supersedes in part:** the `ticks_sip` store as the permanent tick home (v11.4.x, `scripts/sip_backfill.py`).
The backfill *engine's* operational lessons carry forward; the store becomes transitional.

## 1. What this episode is

Episodes 9–14 built a tick store: EODHD Tick Data API → one Delta table per symbol
(`ticks_sip/<SYM>`), walked backwards week by week. It answers "what trades printed for AAPL
on this day" but not "what was the S&P 500 on 2017-03-06, which listing did ticker X denote
that day, and what was *known* at that moment." Episode 15 rebuilds the store as a bitemporal
medallion lakehouse — bronze/silver/gold on Delta Lake, queried through DuckDB — with
point-in-time index membership, corporate-action-aware identity, trade-correction versioning,
and a **temporal query contract on the OpenBB Platform (ODP) layer**:

- **No date given → gold**: latest corrected data, exactly what consumers get today. Non-breaking.
- **`as_of` given → point-in-time**: only what was known/available by that moment.
- **`effective` given → historical identity**: membership and symbol mappings as they stood on that market date.

### Decisions made during brainstorming (chronological, all Art-approved)

1. **Sibling package.** New `tick-vault/` package in openbb-docker beside the running backfill;
   cutover later. Episode framed as a refactoring.
2. ~~Refactor `ticks_sip` in place as source~~ — **superseded by decision 8.**
3. **Two-param temporal contract.** `as_of` (knowledge time) + `effective` (market date), both
   optional, `effective` nullable → single-as-of behavior.
4. **tick-vault owns the contract.** DuckDB macros + query API live in `tick-vault`;
   `openbb-deltalake` gains the two params and delegates. No temporal logic in the provider.
5. **FIGI in scope now** (OpenFIGI resolution). CUSIP/ISIN stored whenever present in payloads
   already captured (no new source). SEC EDGAR/CIK as an active source: hooks only, later episode.
6. **Episode boundary:** schemas + security master + PIT membership + temporal ODP contract +
   §19 acceptance tests + re-backfill *machinery*. The full 10-year walk completing is a
   background operation, not an acceptance criterion. Full-history gold tape: validation slice only.
7. **Both query paths:** NAS-side ODP endpoint in the compose AND direct Mac-side DuckDB
   against the NAS share. One package, two entry points.
8. **Re-download over grandfathering** ("drop the grandfathered ticks — I'd rather have accuracy
   even if it means more API calls"). All tick history re-fetched through the new bronze capture
   path: real capture lineage and honest knowledge time for every row. Confirmed with the cost
   picture in front of Art: **~300–325k API calls, ~3–6 EODHD budget-days, ~2–6 weeks wall-clock**
   (6 workers, span-halving; no settle passes for pre-2026 weeks — corrections there settled years
   ago). A **calibration week** runs first and freezes the projection from real measurements.
9. **`ticks_sip` is a transitional serving source** behind the union view, displaced week by week
   as re-download overtakes it, flagged `LEGACY_UNVERSIONED` in provenance, archived (not
   deleted) at completion.
10. **`HISTORICAL_VENDOR_FINAL_V1` survives as opt-in simulated availability** for backtests.
    Stored `available_at_ts` is always real (`AS_INGESTED_LOCAL_V1` semantics); simulated T+1
    availability is computed in the view only when a backtest explicitly asks, and labeled as such.
11. **Reconciliation is a gate, then a diagnostic.** EODHD EOD daily + intraday 1m/5m bars are
    captured through bronze as independent references; tick aggregations must reconcile during
    the calibration week and first tranches before the long walk starts.
12. **Corrections are first-class temporal facts** (Art: "look at the correction information for
    trades so that we can get the temporal data correct") — §7 below.

## 2. Architecture

`tick-vault/` (Python package + compose service) owns four things:

1. **Schemas & migrations** — the baseline's bronze/silver/gold/ops table inventory, with the
   deltas in §4, created and evolved by versioned migration scripts.
2. **Reference pipeline** — EODHD symbols / symbol-change / delisted / fundamentals / index
   constituents captures; OpenFIGI resolution queue; internal ID allocation
   (UUIDv7: `issuer_id`, `instrument_id`, `listing_id`, `venue_id`, `index_id`);
   `silver.index_membership_version` built per baseline §8. Small data, fully materialized first.
3. **Re-backfill engine** (§5) — manifest-driven re-download of the full tick history through
   bronze, newest-first, resumable; also the forward daily/settle ingestion, diff-aware (§7).
4. **Query layer** (§3) — DuckDB macros implementing the temporal contract; the transitional
   union view `vault.trade_tick`; the ODP delegation surface consumed by `openbb-deltalake`.

Deployment: one new compose service on the NAS (engine loop + ODP endpoint), same
build-and-`docker save`-to-NAS pattern as sip-backfill. The identical package pip-installs on
the Mac for direct DuckDB research over the NAS share. The v11.4.x sip-backfill loop keeps
running only until the new engine deploys, then stops (no dual-write).

Data flow, end state:
`EODHD → bronze captures → silver (append-only, bitemporal) → gold projections → temporal macros → ODP / DuckDB`.
Transition state: the same, plus `ticks_sip` behind the union view for not-yet-redownloaded weeks.

## 3. The temporal query contract

Every read — ODP call or direct macro — accepts two optional parameters beyond
symbol/date-range/columns:

| Param | Type | Meaning | Predicate |
|---|---|---|---|
| `as_of` | timestamp | knowledge time: "only what was known by this moment" | `available_at_ts <= as_of` on facts; `system_from_ts <= as_of < coalesce(system_to_ts, ∞)` on curated records; latest-version-per-logical-key below the cutoff |
| `effective` | date | market time: "identity and membership as they stood on this date" | `effective_from/effective_to` interval predicates on membership, listings, identifier assignments |

Resolution modes:

1. **Neither → gold** (`CURRENT_CORRECTED_V1`): serve `gold.*` directly; latest revision of every
   tick, current identity. Today's behavior — the new params are non-breaking.
2. **`as_of` only → PIT knowledge view.** Facts, mappings, universe filtered to what was
   available by `as_of`. Market time comes from the query's own date range; `effective` defaults
   **per-row to the queried market date** (a query for 2021-11-05 trades resolves identity as of
   2021-11-05), not to `as_of`'s date.
3. **Both → full bitemporal**: "as known on `as_of`, as effective on `effective`."
4. **`effective` only → current knowledge, historical identity**: latest corrected data with
   membership/tickers resolved as of the historical date.

PIT macros additionally accept `availability_policy` (default `AS_INGESTED_LOCAL_V1`;
opt-in `HISTORICAL_VENDOR_FINAL_V1` per decision 10). Under the default, an `as_of` earlier
than the re-download makes history return nothing — correct and intended; the simulated policy
exists precisely so labeled backtests can still do historical PIT research.

**Provenance block on every response:** resolved mode, availability/sequence/inclusion policy
versions, Delta table versions read, and origin flags (`CAPTURE` / `LEGACY_UNVERSIONED`).
Validation: future `as_of` → error; `as_of` before the earliest capture → empty + warning;
unresolved identity in range → excluded + noted. The ODP layer never 500s on temporal edge cases.

ODP surface: `as_of` / `effective` as query params on the existing `openbb-deltalake`
endpoints, echoed in result metadata. `openbb-deltalake` delegates to `tick-vault` and holds
no temporal logic.

## 4. Schema deltas from the baseline

Everything in baseline §7 is adopted as written except:

**Changed**

- `silver.us_trade_tick_version` — add `session_seq BIGINT` (EODHD's per-session `seq`,
  promoted because it is load-bearing for identity; unique per symbol-session, resets daily),
  `sub_mkt_raw STRING`, `is_cancelled BOOLEAN NOT NULL DEFAULT false`,
  `is_late_add BOOLEAN NOT NULL DEFAULT false`, and
  `origin STRING NOT NULL` ∈ {`CAPTURE`} — the enum retains room for future origins; with
  re-download, all silver tick rows are capture-backed. (`LEGACY_UNVERSIONED` appears only in
  union-view provenance, never in silver.)
- **Logical tick identity:** `(listing_id, trade_date, session_seq)` — the proven
  `(ts, seq)` dedup keyed on listing. Baseline's hash fallback applies only if `seq` is absent.
- `bronze.eodhd_tick_capture` — as baseline §7.1 (real HTTP captures only; the
  reconstructed-capture concept from the superseded grandfathering design is dropped).

**Added**

- `bronze.eodhd_eod_capture`, `bronze.eodhd_intraday_capture` — reference-bar captures for the
  reconciliation gate (decision 11). Same capture rules as all bronze (baseline §10.2).
- `ops.backfill_manifest` — work rows keyed `(listing_id, week)`: vendor symbol as-of that week,
  request window, status (baseline §7.12 vocabulary + `BLOCKED_IDENTITY`), attempts, priority,
  per-week `wall_minutes`, calibration metrics. Replaces the baseline's date-grain
  `tick_download_manifest` at the API's natural grain; the baseline table name is retired.
- `silver.tick_correction_event` — one row per detected re-pull difference:
  `correction_kind` ∈ {`REVISED`, `CANCELLED`, `LATE_ADD`}, superseded and superseding
  `tick_version_id`s, revealing `source_capture_id`, `observed_at_ts`.
- `vault.trade_tick` (view, not a table) — the transitional union: silver/gold for
  re-downloaded ranges, `ticks_sip` joined to the metadata overlay (identity resolved per trade
  date; provenance `LEGACY_UNVERSIONED`) for the rest. Watermarks from `ops.backfill_manifest`
  drive partition pruning. All macros and ODP reads go through this view; it collapses to
  silver/gold at cutover and is deleted.

**Trimmed / deferred**

- `bronze.sec_submissions_capture` — dropped this episode (CIK-as-source later).
- `silver.figi_assignment_version` — real table (FIGI in scope). OpenFIGI facts live *only*
  here; `identifier_assignment_version` carries a derived pointer, never a duplicate.
- `silver.registrant` — table created, not populated (EDGAR later).
- `gold.backtest_input_*` — schema defined, materialization deferred.
- `gold.eod_bar_current` — not built; reconciliation reads reference bars from bronze-derived
  silver directly this episode.

Physical layout, partitioning, clustering, file hygiene, retention: baseline §14–15 as written.

## 5. The re-backfill engine

`sip_backfill.py`'s operational core, pointed at the medallion path. Carried over verbatim:
per-symbol span-halving memory, the scoped blacklist, 429-budget backoff (30-min waits, 6-hour
budget stop), T+1 week-completeness gate (complete from Saturday 08:00 UTC), priority ordering
(newest week first, market-cap descending within a week), boundary-second dedup.

- **Tranche = symbol × week** (7-day API max). `ops.backfill_manifest` is the work queue and
  the watermark store.
- **Per tranche:** resolve vendor symbol *as of that week* from the reference layer — ambiguity
  blocks (`BLOCKED_IDENTITY` → review queue), never guesses → fetch in bounded windows → write
  real bronze captures (payload file, sha256, headers, page ordinals) → parse/append
  `silver.us_trade_tick_version` (revision 1, `available_at_ts` = capture `observed_at_ts`) →
  verify (row count; `(trade_date, session_seq)` uniqueness) → reconcile (gating during the
  early phase, §8) → `COMPLETE` → materialize the gold slice.
- **Idempotent re-runs:** a tranche failure leaves the watermark unmoved; re-running
  delete-and-rewrites exactly its partition slice.
- **Forward ingestion** (daily pass, settle passes near the live edge) runs through the same
  engine, diff-aware per §7. **No settle passes for pre-2026 historical weeks** — first pull
  already returns final state; this halves the projected call count.
- **Calibration week first:** one representative week through the full path measures per-call
  wall time, storage per week, and reads historical `wall_minutes` from the NAS `_progress`
  JSONs; the 10-year projection (calls, days, TB) is frozen into the plan before the walk starts.

Displacement: a week leaves `ticks_sip` serving only when its new silver rows reconcile against
the legacy store's counts or the difference is explained by logged correction/quality events.
At full completion: dual sources collapse, `ticks_sip` archived pending Art's explicit retirement call.

## 6. Availability policies

| Policy | Status | Semantics |
|---|---|---|
| `AS_INGESTED_LOCAL_V1` | **default** | `available_at_ts` = real local capture time. Honest; historical `as_of` before the re-download returns nothing. |
| `HISTORICAL_VENDOR_FINAL_V1` | opt-in, labeled | Simulated T+1-style availability computed **in the view** for backtests; never stored as fact. |
| `CURRENT_CORRECTED_V1` | implicit | The no-date gold mode. Not valid for PIT claims; provenance says so. |
| `LIVE_PRODUCTION_V1` | deferred | Phase 5, later episode. |

`silver.data_availability_policy` holds the versioned definitions; every backtest manifest and
provenance block cites one.

## 7. Corrections and the temporal model

EODHD's tick payloads contain no correction/cancel record type — only prints with the
four-position CTA/UTP-style `sl` condition string and per-session `seq`. Corrections reach us:

1. **Condition codes on the print** — late / out-of-sequence / prior-reference-price codes:
   market-time semantics. Decoded into `sale_condition_flags` so bar-building and strategies can
   exclude out-of-sequence prints. **Phase 0 confirms the exact `sl` vocabulary against real
   stored payloads before the parser hardens**; the decode table is a versioned, hand-verified
   test fixture.
2. **Re-pull differences** — the settle-pass signal the old loop computed and threw away
   (`replace_days`). The engine is diff-aware: before writing a window it compares against
   current silver by logical key.
   - unchanged → no new version
   - changed price/size/conditions → new version, `revision_number+1`, `is_correction=true`,
     `supersedes_tick_version_id` set
   - vanished seq → tombstone version, `is_cancelled=true`
   - new seq in an already-fetched window → `revision_number=1`, `is_late_add=true`
   Each diff also writes one `silver.tick_correction_event` row.

**Knowledge time rule:** a correction's `available_at_ts` is the *revealing capture's*
`observed_at_ts`, never the original trade time. PIT queries with `as_of` before the revealing
capture see the original print (or the not-yet-cancelled trade); after it, the corrected state.
Gold always carries the latest revision, excluding tombstoned rows.

Because all history is re-downloaded (decision 8), this model applies uniformly — there is no
grandfathered class of rows with unrecoverable correction lineage.

## 8. Reconciliation: gate, then diagnostic

Independent references: EODHD EOD daily OHLCV and intraday 1m/5m bars, captured through bronze
(cheap calls, one per symbol-window). Our ticks aggregate to daily and 1m/5m bars under a
versioned `bar_inclusion_policy` (regular session, eligible sale conditions, cancels excluded,
late-print handling documented) and compare systematically.

- **Phase A — gate.** During the calibration week and the first N tranches (N configurable;
  default ~2,000 tranches ≈ the four most recent weeks of data × the full universe), a tranche
  cannot reach `COMPLETE` — and the long walk cannot start —
  until divergences are fixed (expected early finds: `sl` interpretation, inclusion policy) or
  explicitly waived with a logged reason. The 1m comparison localizes a bad decode to the minute.
- **Phase B — diagnostic.** Scheduled checks writing `ops.data_quality_issue`; divergence
  flags, not fails (baseline §16.3: "a mismatch is a diagnostic, not automatically an error").
  Intraday comparison stays on for a random sample of tranches as an ongoing canary.

## 9. Error handling, data quality, operations

Every failure class has a named, queryable destination: HTTP failures → error-status bronze
captures (retries are new captures); identity ambiguity → `BLOCKED_IDENTITY` + review queue;
parse anomalies (unknown `sl`, non-monotonic in-page timestamps, seq collisions on differing
rows) → `ops.data_quality_issue` + `verification_status='SUSPECT'` quarantine.

Data-quality tiers (scheduled DuckDB checks → `ops.data_quality_issue`): **universe**
(member-count bounds, no overlapping resolved intervals, no membership without a listing
mapping, ticker-reuse scan); **tick coverage** (manifest expected-vs-complete, new-vs-`ticks_sip`
row counts during transition — also the displacement gate — first/last-tick sanity, seq-gap
stats); **reconciliation** (§8).

Operations: `ops.ingestion_run` per baseline §17 (code commit, config hash, parser and policy
versions, output Delta versions on every run). A `vault-status` CLI + matching widgets.json
endpoint (series pattern) shows manifest progress, displacement frontier, open quality issues,
budget spend. Retention per baseline §15: bronze indefinite; no aggressive VACUUM on
research-critical tables.

## 10. Testing and acceptance

TDD throughout. Three layers:

1. **Unit/component (pytest, anywhere):** parser against real captured payload fixtures +
   hand-verified `sl` decode table; logical-key dedup; correction diffing on synthetic
   before/after pairs (REVISED/CANCELLED/LATE_ADD with correct knowledge times); the four
   contract modes, the `availability_policy` opt-in, and union-view watermark routing against a
   miniature in-memory lake.
2. **Acceptance — baseline §19 as executable pytest** against the validation slice.
   Fixtures: FB→META (rename), TWTR (cash acquisition/delist), GE (spin-offs), one reused
   ticker, GOOG/GOOGL (share classes), AAPL (liquid control); 2–3 weeks spanning membership
   changes; exact dates finalized in the implementation plan.
   §19.1 membership, §19.2 identifier, §19.3 tick (incl. replay determinism across repeated
   materializations), §19.4 PIT (incl. a correction invisible before its capture time under the
   real policy and correctly ordered under the simulated one; pinned-version rerun bit-identical).
3. **Reconciliation gate (§8)** — the integration test, on real data, before ~300k calls commit.

**Performance:** measure-first. Benchmark baseline §19.5 shapes (one-day one-listing replay,
one-month universe scan, once-per-date PIT resolution) on the real NAS and Mac-over-share paths
during calibration; freeze observed numbers with margin as regression thresholds; CI re-runs the
small shapes, `vault-status` re-runs the big ones on demand.

**Episode 15.0.0 is done when:** all §19 suites green on the validation slice; calibration gate
passed and projection published; engine running unattended in the compose with the manifest
advancing; temporal contract live in `openbb-deltalake` (four modes + provenance);
union view serving with `LEGACY_UNVERSIONED` flagging; substack stub
(`substack-articles/15-temporal-tick-vault/`) and series artifacts in place.
**Explicitly not required:** the 10-year walk finishing.

## 11. Logistics

Branch `claude/v15-tick-vault` (this worktree), release tag `v15.0.0`. Spec + baseline committed
here; implementation plan next via writing-plans at `docs/superpowers/plans/`. New package
`tick-vault/` with its own pyproject + tests + CI wiring (the ep-11 pattern:
install `openbb-deltalake` first where needed). Cost projection of record: ~300–325k calls,
~3–6 budget-days, ~2–6 weeks wall-clock, pending calibration-week revision.

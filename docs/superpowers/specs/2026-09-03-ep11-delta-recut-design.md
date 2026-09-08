<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Ep. 11 re-cut — Delta Lake under both doors

Design spec for Adventures in OpenBB, Episode 11, re-cut. Supersedes the
storage choice in `2026-08-05-arcticdb-minio-design.md` and the backend half
of `2026-08-07-stores-explorer-design.md`; the MinIO/tailnet design and the
"two doors, one vault" framing both stand unchanged.

## Context

Episode 11 shipped on ArcticDB — tags `v11.0.0`, `v11.1.0`, `v11.1.1`, all
carrying `openbb-arcticdb`, `mcp_stores` and `stores-explorer`. ArcticDB is
BSL 1.1 with no additional-use grant: production use requires a paid license,
and each release converts to Apache 2.0 only two years after it ships. The
episode teaches a stack viewers cannot legally run. Full evaluation:
`docs/arcticdb-alternatives-evaluation.md`.

The replacement — Delta Lake via `deltalake` (delta-rs, Apache 2.0) — was
designed and built on branch `ep11-arcticdb-minio`
(`2026-09-01-deltalake-store-design.md`). That branch is **not** a rebase
candidate as-is: it forked on 2026-08-06, *before* `mcp_stores` and
`stores-explorer` were written, and its 15 commits touch neither. The store
was swapped; the two doors onto it never were.

This spec covers closing that gap and re-cutting the v11.x line so ArcticDB
never appears in Episode 11.

### Measured state

| Fact | Value |
|---|---|
| `ep11-arcticdb-minio` vs `main` | 15 ahead, 200 behind; forked 2026-08-06 |
| Branch touches `mcp_stores` / `stores-explorer` | Neither — both postdate the fork |
| `main` commits since fork in `openbb-arcticdb` / `tick-lab` / `tests` | 3 / 2 / 1 |
| `main` commits since fork in `docker-compose.yml` / `scripts` | 25 / 9 |

The conflict surface is therefore mechanical (compose, scripts) rather than
substantive — which is what makes a rebase cheaper than a re-application.

## Goals

1. ArcticDB is gone from Episode 11 entirely — image, extension, both doors,
   compose, constraints, and the re-cut tags.
2. The EODHD read-through cache keeps its persistent tier — removing ArcticDB
   costs no additional API calls.
3. Both doors answer from Delta: `mcp_stores` (the analyst's, via Rita) and
   `stores-explorer` (the widget's, via bdobb).
4. Zero duplicated discovery logic survives the port — `stores-explorer`
   keeps importing `mcp_stores`, per the original D3.
5. Delta's time travel reaches a caller for the first time.
6. `scripts/scrub-check.sh` stays green.

## Non-goals

- The bdobb-v2 explorer widget. That is bdobb-v2 v11.0.0, its own spec.
- Migration tooling for existing ArcticDB data. Stores are rebuilt by
  re-running the loaders, per the Delta spec.
- A storage-agnostic store interface behind the doors. ArcticDB is being
  deleted, so such an interface would have exactly one implementation.
- Any write endpoint. Both doors stay read-only.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| R1 | **Rebase `ep11-arcticdb-minio` onto `main`**, don't re-apply as a fresh squash | Measured conflict surface is 3+2+1 commits in the substance directories against 25+9 in mechanical config. Rebasing keeps 15 reviewed commits and two tests worth having (time-travel round-trip, MinIO concurrent-append gate). |
| R2 | **Three commit series: store → doors → widget-facing service**, matching the release split | Each is independently shippable and testable, and each maps to one re-cut tag (see *Release train*). |
| R3 | **`/delta/describe`, not `/delta/summary`** | The endpoint answers from `DeltaTable.get_add_actions`, which is a description of the table's files, not a summary of its data. Naming it for what it reads keeps the "reads no rows" contract legible at the call site. |
| R4 | **Stats-bounded tail**, not scan-then-tail | `arctic_read` relied on `Library.tail(symbol, n)` so an unfiltered read never materialized the symbol. delta-rs has no `tail`. Per-fragment `min.date`/`max.date` in the add-actions let the read select trailing fragments by stats and touch only those — preserving the original bound rather than quietly dropping it. |
| R5 | **`as_of` is exposed on `/delta/series` and the MCP read**, as an int version or an ISO timestamp | `DeltaStore.read` already accepts both. The Delta spec calls time travel "a headline segment"; it currently has no caller. |
| R6 | **`pa.table(...)` wraps every `get_add_actions` result** | Verified on deltalake 1.6.3: it returns an `arro3.core.Table`, which has no `.to_pandas()`. The branch already pins a delta-rs API canary; this is the same class of drift and belongs under the same pin. |
| R7 | **Re-cut the v11.x tags on Delta** rather than opening a v12 line | Episode 12 is already reserved for technical analysis / chart tools in the bdobb spec set. Moving tags to regenerated commits is this repo's established release-rebuild practice. |

| R8 | **The EODHD L2 cache vacuums on write** — `DeltaTable.vacuum` with a short retention after each `_l2_put` | Delta has no `prune_previous_versions`. Without a retention step the cache grows a commit per refresh forever, and `read_metadata`'s `history()` walk slows with it (G5). A cache is the one table in the store with no reason to keep history: the previous value is by definition stale. |
| R9 | **The cache's Delta library is the same `eodhd_fundamentals_cache`** and appears in the explorer like any other | It is browsable data in the shared store, and seeing the cache's own contents through the widget is a useful demonstration rather than something to hide — the `v11.3.0` example dashboard puts a card on it for exactly that reason. |

## Capability gaps and how each closes

ArcticDB gave Episode 11 five things Delta does not give directly. G1–G4 were
probed against `deltalake` 1.6.3 before this spec was written; G5 was found by
reading the shipped extension.

### G1 — no library catalog

`DeltaStore` is constructed *with* a library; `list_symbols()` scans
`<root>/<library>/` for child directories containing `_delta_log`. Delta has no
catalog above that.

**Close:** `delta_list_libraries()` runs the identical scan one level up —
children of `<root>/` that themselves contain at least one Delta table. Same
`pyarrow.fs` selector, same `_delta_log` probe.

> The existing `list_symbols` already carries a `ponytail:` note that it costs
> one stat per child directory. Listing libraries walks one level higher, so the
> two together are a two-level walk. Fine for a per-episode catalog; the note's
> stated upgrade path (a manifest table) covers both if it ever matters.

### G2 — no `get_description`

`/arctic/summary` promised metadata with **zero rows read** (original D6, with a
test asserting the row path is never invoked).

**Close — verified:** `DeltaTable.get_add_actions(flatten=True)` returns, from
the transaction log alone:

| Column | Answers |
|---|---|
| `num_records` | `row_count` (sum across files) |
| `min.<col>` / `max.<col>` | `date_range` — and, unlike ArcticDB, for *every* column, not only the index |
| `DeltaTable.schema().fields` | `columns` with dtypes |

The D6 contract survives, and the per-column bounds are strictly more than
`get_description` offered. Wrap in `pa.table(...)` per R6.

### G3 — no native `tail(n)`

Confirmed on a live table: `hasattr(dt, "tail")` is False.

**Close:** per R4, use `min.date`/`max.date` from the same add-actions to pick
the trailing fragment(s) and read only those, then tail within. The unfiltered
read stays bounded by fragment count, not table size.

### G4 — time travel has no door

`DeltaStore.read(as_of=...)` accepts an int version or a timestamp; nothing
calls it. Verified: `history()` lists versions, `load_as_version(0)` returns the
superseded data.

**Close:** per R5, an `as_of` param on `/delta/series` and on the MCP read tool,
plus a `/delta/history` endpoint listing versions with their timestamps so a
caller can populate a version control without guessing.

### G5 — the EODHD read-through cache is ArcticDB-backed

`openbb-eodhd/openbb_eodhd/models/_fundamentals.py` already implements a
two-tier read-through cache over the FMP look-alike endpoints — the surface
consumed by 12 model files (`fundamental`, `metrics`, `ownership`, `insider`,
`estimates`, `profile`, `etf`, `esg`, `news`, `quote`, `market_cap`,
`dividend_yield`). L1 is an in-memory dict under a per-symbol lock; **L2 is
ArcticDB**, via `_arctic_library()` → `openbb_arcticdb.utils.resolve_config`.

Deleting ArcticDB without porting L2 silently reduces the cache to L1 only:
every container restart would then re-fetch every symbol's fundamentals from
EODHD. The re-cut would *cause* quota waste rather than being neutral to it.
This is why the port folds into `v11.0.0` alongside the store itself rather
than trailing it.

**Close:** `_arctic_library()` becomes `_delta_store()`, returning a
`DeltaStore` over the same `_L2_LIBRARY = "eodhd_fundamentals_cache"`.

| Today | Becomes |
|---|---|
| `lib.has_symbol(sym)` | `store.has(sym)` |
| `lib.read_metadata(sym).metadata` | `store.read_metadata(sym)` — already a plain dict |
| `lib.read(sym).data["payload"].iloc[0]` | `store.read(sym, output="dataframe")["payload"].iloc[0]` |
| `lib.write(sym, df, metadata=..., prune_previous_versions=True)` | `store.write(sym, df, metadata=...)` — **no prune equivalent** |

The soft-dependency posture is preserved exactly: `_delta_store()` returns
`None` on any failure, and both `_l2_get` and `_l2_put` keep their "never
raises" contract, so a missing or unreachable store degrades to L1 plus a live
fetch rather than failing the request.

> **The prune gap is real.** ArcticDB's `prune_previous_versions=True` kept one
> version per symbol. Delta has no equivalent on write: every refresh appends a
> commit, so a cache entry refreshed daily accumulates a version per day
> forever. That matters twice — storage grows unbounded, and
> `DeltaStore.read_metadata` walks `history()` looking for `openbb_meta`, so
> reads get slower as history lengthens. The cache write path therefore takes a
> retention step (see R8); this is the one place in the re-cut where Delta is
> strictly worse than what it replaces, and it needs handling rather than
> noting.


## Architecture

### `mcp_stores` — the analyst's door

| Today | Becomes |
|---|---|
| `arctic_list_libraries()` | `delta_list_libraries()` — prefix scan (G1) |
| `arctic_list_symbols(library)` | `delta_list_symbols(library)` |
| `arctic_read(...)` | `delta_read(..., as_of=None)` — stats-bounded tail (G3, G4) |
| — | `delta_describe(library, symbol)` — metadata only (G2) |
| — | `delta_history(library, symbol)` — versions and timestamps (G4) |
| `_arctic()` | `_delta(library)` — `DeltaStore` from `DELTA_*` config |

`_scrub`, `_bounded`, `_check_ident`, `_decode_df`, `_records` and every kdb+
tool carry over unchanged. `redact_uri` retires: Delta takes credentials as
`storage_options`, so they never enter a URI in the first place.

### `stores-explorer` — the widget's door

Routes rename and gain two members. Everything structural — `create_app`'s
keyword injection, the CORS posture (D1/D2), the static `widgets.json` (D7),
loopback bind with Tailscale Serve as sole ingress — is unchanged.

| Endpoint | Wraps |
|---|---|
| `GET /delta/libraries` | `delta_list_libraries()` |
| `GET /delta/symbols?library=` | `delta_list_symbols(library)` |
| `GET /delta/describe?library=&symbol=` | `delta_describe(...)` |
| `GET /delta/history?library=&symbol=` | `delta_history(...)` |
| `GET /delta/series?library=&symbol=&start=&end=&tail_rows=&as_of=` | `delta_read(...)` |

The kdb+ chain (`/kdb/tables`, `/kdb/schema`, `/kdb/select`) is untouched.

Each endpoint keeps validating its argument against the corresponding list call
first, inheriting the existing "unknown X; call Y first" 404 contract.

### `widgets.json`

`arctic_explorer` becomes `delta_explorer`; `source` becomes `["Delta Lake"]`;
`optionsEndpoint`s follow the renamed routes. The cascading
`library` → `symbol` chain via `optionsParams: {"library": "$library"}` is
unchanged — bdobb-v2 already resolves `$`-references, so the generic path keeps
working while the purpose-built widget is built.

`kdb_explorer` is unchanged.

## Release train

Three tags, re-cut on Delta, one per commit series (R2):

| Tag | Contents |
|---|---|
| `v11.0.0` | The rebase: `openbb-deltalake` replaces `openbb-arcticdb`, tick-lab on Delta, compose/CI/constraints, `linux/amd64` pins dropped — **and the EODHD L2 cache ported onto Delta (G5)**, so removing ArcticDB never costs API quota |
| `v11.1.0` | `mcp_stores` on Delta, including G1/G2/G4 additions |
| `v11.2.0` | `stores-explorer` on Delta, including G3 — matches the `openbb-stores-explorer:11.2.0` image already named in compose |

| `v11.3.0` | An example dashboard published as `stores-explorer/apps.json` — the Delta store, the EODHD cache it now backs, and the kdb+ tape. Zero bdobb-v2 code: bdobb discovers `apps.json` per backend, and its cards name widgets by id, not by the backend's per-install UUID. |

bdobb-v2 v11.0.0 pairs against `v11.2.0`.

## Testing

- **Ported, not rewritten.** `stores-explorer`'s suite already injects every
  backend call through `create_app` keywords; the fakes swap, the assertions
  mostly stand.
- **G2 keeps its teeth.** The existing "row-read path never invoked" call-count
  assertion ports to `delta_describe` verbatim. It is the only guard that the
  endpoint stays metadata-only.
- **G3 earns a new test.** Assert an unfiltered `tail_rows` read touches fewer
  fragments than the table has — the assertion that would fail if someone
  simplified it to scan-then-tail.
- **G4 round-trip.** Write, overwrite, read back `as_of` both an int version and
  a timestamp, through the HTTP surface rather than only the store.
- **Retained from the branch:** time-travel round-trip and the MinIO
  concurrent-append gate (delta-rs commit atomicity over S3 conditional puts is
  verified, not assumed).
- **Scrub.** `scripts/scrub-check.sh` clean over both ported directories, run as
  a plan step rather than left to CI.

## Success criteria

1. No `arcticdb` import, image, constraint, env var or compose reference
   survives anywhere in the tree.
2. `/delta/libraries` → `/delta/symbols` → `/delta/describe` → `/delta/series`
   walks end to end against a real MinIO-backed store.
3. `/delta/describe` provably reads no rows (call-count assertion).
4. An unfiltered `/delta/series` reads a bounded number of fragments.
5. `as_of` returns superseded data through HTTP, by version and by timestamp.
6. Rita answers the same questions through `mcp_stores` that the widget asks
   through `stores-explorer` — same code, two doors, one vault.
7. A restart followed by the same fundamentals request issues **no** EODHD
   HTTP call — the L2 cache answered from Delta.
8. `scripts/scrub-check.sh` passes.

## Gotchas

| Symptom | Cause | Rule |
|---|---|---|
| `AttributeError: 'arro3.core._core.Table' has no attribute 'to_pandas'` | deltalake ≥1.6 returns arro3, not pyarrow, from `get_add_actions` | Wrap: `pa.table(dt.get_add_actions(flatten=True))` (R6) |
| `date` column loses its timezone across a write/read | Delta stores it as `timestamp_ntz` | `_date_filter` must match the stored tz, not assume the caller's |
| EODHD quota burns after a restart | The L2 cache fell back to L1 because `_delta_store()` returned `None` | L2 is best-effort by design; check MinIO reachability and `DELTA_*` config before blaming the cache logic |
| The fundamentals cache slows down over weeks | Delta keeps a commit per refresh; `read_metadata` walks `history()` | Vacuum on write (R8); there is no `prune_previous_versions` in Delta |
| A `tail_rows` read gets slow as a symbol grows | Someone replaced the stats-bounded tail with scan-then-tail | The fragment-count test (G3) exists to fail first |
| Rebase conflicts pile up in `docker-compose.yml` | The branch predates 25 commits of compose churn | Resolve toward `main`; the branch's compose edits are the rename only |

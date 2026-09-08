<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Tick recording and the unified chart, in live-grid

**Date:** 2026-08-05
**Status:** Approved
**Ships as:** part of v10.0.0 — *Adventures in OpenBB, Ep. 10*
**Supersedes:** the `cache-chart` service described in
[docs/kdb-cache-design.md](kdb-cache-design.md), which is removed before release.

## Purpose

Give `live-grid` a charting option whose bars are **aggregated from the tick
stream it already receives**, and make it the one place real-time market data
enters this stack. A chart then reads as one continuous series: cached history
on the left, bars built from live ticks at the right edge.

This is also the change that makes kdb+ earn its place. Caching daily bars is
something any key-value store could do; aggregating a trades table into OHLCV
buckets with `xbar` is what q is actually for.

## Why this replaces `cache-chart`

`cache-chart` was a separate container serving a chart over the historical
cache. Folding it into `live-grid` removes a container, puts the chart next to
the tick stream that feeds it, and gives one service that owns real-time data.
`cache-chart` is deleted — service, image, compose entry and Serve route — so it
never appears in a released tag.

## The starting point

`live-grid` (Ep. 9) streams EODHD websocket prices for equities, crypto and
forex. `QuoteTable.apply_message` updates one current-value row per symbol and
**discards the message**. There is no history to aggregate.

So the substantive change is not "add a chart endpoint" — it is that live-grid
becomes a **tick recorder**.

## Design decisions

### The q plumbing moves to a shared package

`openbb-kdb`'s `config.py`, `session.py`, `store.py` and `ranges.py` have no
OpenBB dependency; only `cache.py`, `upstream.py` and `models/` import
`openbb_core`. Those four move unchanged into a new `kdb-store/` distribution
that both `openbb-kdb` and `live-grid` depend on.

The alternative — having live-grid import `openbb_kdb` — would drag the entire
OpenBB dependency tree into a small `python:3.12-slim` service for four files
it does not use, because importing any submodule executes the package
`__init__`. Writing a second PyKX client for live-grid would be worse: it would
duplicate exactly the code whose failure modes were expensive to find.

**This is the load-bearing reason for the extraction:** PyKX aborts the process
when touched from more than one thread — not merely under concurrency, but on
strictly sequential calls from different threads. `KdbSession` solves that with
a single owner thread. live-grid runs websocket clients on threads, so it needs
that guarantee, and there must be exactly one implementation of it.

### live-grid and openbb-api share one q

Both connect to the same `q` on `127.0.0.1:5000`. Every service in this stack
shares the tailscale container's network namespace, so this is one warm store
holding both the historical bars and the ticks — which is what lets a chart
stitch them without crossing a process boundary. q is single-threaded and
serialises the two clients' requests.

### Ticks are recorded in batches, not per message

Per-tick IPC would not keep up. `QuoteTable.apply_message` appends to an
in-process buffer that is flushed to q as one batch on the existing ~250 ms
cadence.

The buffer is **bounded**: on overflow it drops oldest, counts the drops, and
reports them on `/health`. An unbounded buffer growing while q is down is the
failure this prevents.

Forex carries no trades, only bid/ask, so it records the same mid-price rule
`QuoteTable` already applies to its live rows.

### Retention is a rolling window

A pruning pass drops ticks older than `LIVE_TICK_WINDOW` and runs `.Q.gc[]`.
Ticks are bounded by their own policy — a time window — rather than by the
LRU watermark that governs cached bars.

**This is a retention policy, not isolation, and the distinction matters.**
`trades` lives in the same q process as every `bars_*` table, and `.Q.w[]`
`heap` — the number the watermark is measured against — is per *process*. So
the two do share one budget after all:

- A busy feed grows `heap`, which can push it past
  `KDB_CACHE_WATERMARK × KDB_MEMORY_MB` and **trigger eviction of cached
  bars**, even though the bars did nothing.
- Eviction cannot reach `trades`: `KdbStore.evict_until_below` walks
  `.cache.lru`, which only ever holds `(symbol, interval)` bar tables. If the
  ticks are what put the heap over budget, eviction drops every bar table it
  has, still fails to get under, and logs
  `evict_until_below exhausted the LRU without reaching budget`.

The containment that actually holds is q's `-w` (budget × 1.25) plus the
window: `LIVE_TICK_WINDOW` is what bounds `trades`, so sizing it — not the
LRU — is how an operator keeps a busy feed from evicting their cache. The
honest summary is that ticks are **exempt from** eviction rather than
isolated from it, which cuts the opposite way from what this section
originally claimed.

### Aggregation happens in q

`xbar` over the trades table, server-side, next to the data. Not pandas.

### REST snapshots cache through kdb

Snapshot seeding (previous close, opening quote) currently re-hits EODHD on
every debounced rebuild. Snapshots go into a small table keyed by symbol with a
fetch timestamp and are reused within a TTL, so re-subscribing to a symbol the
service already knows costs nothing at the vendor.

### Charting is optional

The chart endpoints and tick recording sit behind a flag. **With no reachable
q, `live-grid` must stream its grid exactly as it does today.** Regressing
Episode 8's feature would be the worst outcome of this change.

## State in q

| Structure | Contents |
|---|---|
| `trades` | `time` (timestamp), `sym` (symbol), `price` (float), `size` (float) |
| `snap` | Per-symbol REST snapshot plus its fetch timestamp, for TTL reuse |
| `bars_<SYMBOL>_<INTERVAL>`, `.cache.cov`, `.cache.lru` | Unchanged, from the historical cache |

## The seam

Let **W** be the earliest tick actually held for a symbol — not the configured
window. A feed that started twenty minutes ago holds twenty minutes.

For a request `(symbol, interval, start, end)`:

1. **B** = the first bar boundary at or after **W**.
2. `[start, B)` comes from the historical read-through cache.
3. `[B, end]` is aggregated from ticks.
4. Historical bars at or after **B** are dropped, so the seam emits no
   duplicate timestamps.

**Why B rather than W.** The bar containing W is only partly covered by ticks.
Aggregating it would produce a candle missing its own opening trades — wrong,
and entirely plausible-looking. Ticks own only the bars they cover completely;
the straddling bar comes from history.

Intervals whose bar span exceeds the tick window (weekly, monthly) never
aggregate from ticks. There could not be enough of them.

### The two sources will not agree exactly

Vendor bars are consolidated across venues and adjusted; a websocket feed is
the prints it happened to receive. Closes track closely; **volume can differ
visibly at the seam.** This is inherent to mixing the sources, not a defect to
engineer away, and the episode should say so rather than hide it.

## Endpoints

`live-grid` keeps `/live_grid`, `/live_grid_ws`, `/health` and its existing
widget unchanged, and gains:

| Endpoint | Purpose |
|---|---|
| `GET /chart` | Plotly figure JSON — the Workspace chart widget |
| `GET /series` | `{bars, cache}` for incremental loads |
| `GET /demo` | the standalone scroll page, moved from `cache-chart` |

`widgets.json` gains a chart widget alongside `live_grid`.

Responses report `rows_from_ticks` alongside `rows_from_cache` and
`rows_from_upstream`, plus the **seam timestamp** — which lets the demo draw a
marker where live data meets cached history.

## Failure modes

| Condition | Behaviour |
|---|---|
| No reachable q | Tick recording off; chart serves history only; **the grid streams normally** |
| q dies mid-session | Buffered ticks are lost (they are a cache); buffer stays bounded; chart's tick region shrinks, history still serves |
| Tick burst | Buffer drops oldest, counts drops, reports on `/health` |
| Write batch type mismatch | Batches are conformed to the stored schema before upsert |
| Watchlist churn | Ticks for unsubscribed symbols simply stop; pruning reclaims them |
| Feed busy enough to grow the heap past the watermark | Cached bar tables are evicted oldest-first; `trades` is not evictable, so eviction can exhaust the LRU and still log that it is over budget. Shrink `LIVE_TICK_WINDOW` or raise `KDB_MEMORY_MB` |

The dtype hazard is not hypothetical: q's `upsert` demands exact column-type
matches and pandas re-infers dtypes per batch. That combination made every live
chart fail on its second request during Episode 10, and the conforming helper
that fixed it already exists.

## Testing

Unit tests, all mocked and needing no licence, key or network:

- **Aggregation** — a known tick sequence produces known OHLCV per bucket.
  This is the highest-value test in the change.
- **The seam** — no partial boundary bar; no duplicate timestamps across the
  join; timestamps strictly increasing.
- **Retention** — pruning drops ticks outside the window and keeps those inside.
- **Buffer** — bounded under burst, drops counted and surfaced.
- **Degradation** — with no q, the grid still streams and the chart still
  returns historical bars.

**One test cannot be mocked.** The aggregation query gets a real-q test in the
`live_check.py` style, requiring a licence and therefore excluded from CI. Two
of the three worst bugs in Episode 10 were q-level and invisible to every mock:
a fake connection cannot tell you that `x` does not bind outside a lambda, or
that `upsert` will not coerce a dtype. Mocked tests are necessary here and not
sufficient.

## Rejected alternatives

| Approach | Why not |
|---|---|
| live-grid depends on `openbb-kdb` | Importing any submodule executes the package `__init__`, pulling `openbb_core` into a small service for four files it does not use. |
| A second PyKX client inside live-grid | Duplicates the thread-affinity handling whose bugs were the most expensive to find; the next person to fix one copy will not know about the other. |
| Aggregate ticks in pandas | Moves the work away from the data and discards the single best argument for kdb+ being in this stack. |
| Roll ticks straight into bars and discard them | Smaller footprint, but re-aggregating at a finer interval afterwards becomes impossible. |
| Let ticks compete under the existing LRU | Eviction granularity is per `(symbol, interval)`, which does not fit a single growing trades table — there is no meaningful "least recently used" slice of it to drop. Note this buys *retention* control, not heap isolation: `heap` is per q process and `trades` shares it, so a busy feed can still push the cache past its watermark (see "Retention is a rolling window"). |
| Keep `cache-chart` alongside | Two services and two charts to maintain and explain, for one feature. |

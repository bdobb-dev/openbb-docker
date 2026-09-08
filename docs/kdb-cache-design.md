<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# kdb+ read-through cache for the OpenBB Platform

**Date:** 2026-08-04
**Status:** Approved — **charting sections superseded, see below**
**Ships as:** v10.0.0 — *Adventures in OpenBB, Ep. 10*

> **Superseded.** Everything in this document about the read-through cache
> itself (`openbb-kdb`, `provider="kdb"`, eviction, coverage, the memory
> ceiling) still ships and is still accurate. But **the `cache-chart` service
> this document describes was deleted before release** and never shipped —
> its chart moved into `live-grid` instead, built from the tick stream that
> service already receives rather than from the historical cache alone. The
> "What already exists" plan below (a standalone chart container) was
> superseded during Ep. 10 by
> [docs/tick-chart-design.md](tick-chart-design.md), which is the accurate
> description of the shipped chart, the `/chart` / `/series` / `/demo`
> endpoints, and the `kdb-store` package the cache and the chart now share.
> Every `cache-chart/` reference below is history, not current state.

## Purpose

Put an in-memory kdb+ tier underneath OpenBB's historical price path so that
repeated and overlapping requests for the same series are served from RAM
instead of going out to the network. A new provider, `provider="kdb"`, is a
**read-through cache**: it serves what it has, fetches only what it is missing
from an upstream provider (EODHD by default), stores that, and returns the
merged series.

Two things must be true for this to be worth an episode:

1. Zooming a chart from 1 year to 3 years fetches **two years of bars, once** —
   not three years, and not again on the next zoom.
2. The stack still works for a reader with no kdb+ license at all.

## What already exists

- `openbb-docker-old/openbb-kdb` — a PyKX-over-IPC OpenBB extension with a
  write path (`res.kdb.write()`) and a `provider="kdb"` read path with
  tick→OHLCV resampling. It serves **only what is already stored**; there is no
  fetch-on-miss. The new extension is a cache-scoped descendant of it.
- `kdb-x` container images (`QHOME=/root/.kx`, `KX_PORT=5000`), with a kdb-x
  license baked in.
- `openbb-eodhd` (Ep. 9), in this repo — the default upstream.

## Design decisions

### q runs inside the OpenBB container, not as its own service

`q` is started as a **child process of the openbb-api container**, bound to
`127.0.0.1:5000`. PyKX connects to it as an IPC client over loopback.

True in-process embedding (PyKX embedded q) was tested and rejected: PyKX
refuses the kdb-x license with `licence error: kc.lic`, and PyKX's unlicensed
mode is IPC-client-only. Embedded q would require registering for a separate
kdb+/PyKX personal license, and would give every process its *own* heap.

The loopback child process is better here anyway, because **every service in
this stack shares the tailscale container's network namespace**
(`network_mode: service:tailscale`). One q on `127.0.0.1:5000` is therefore on
the same loopback as openbb-mcp, live-grid, key-maint and cache-chart — they
all share one warm cache — while remaining unreachable from every tailnet peer.

### The cache is memory-only

No persistence, no snapshot, no reload on start. A restart means a cold cache
that refills on demand. This is a cache; it is allowed to be empty.

### Configuration

**Amended in Ep. 10 (bring-your-own-q):** the table below described an
earlier design where the image carried a licenced kdb-x install and
`KDB_EMBEDDED` derived from whether `KDB_HOST` looked like loopback. Neither
is true of what shipped — see "Bring your own q, and licensing" below for
why. Current variables:

| Var | Default | Meaning |
|---|---|---|
| `KDB_EMBEDDED` | *derived* | Spawn q inside the container. With no explicit value it is derived from whether an executable `bin/q` is actually found at `KDB_LOCAL_QHOME` — spawning only makes sense for a q the operator supplied. Set it explicitly to override. |
| `KDB_LOCAL_QHOME` | `/opt/kx` | Where the operator mounted their own q (this repo ships none). `QHOME` is accepted as a fallback for the older variable name, read **once per process, before `import pykx`** — importing PyKX rewrites `QHOME` in place to point at its own bundled q, so a second read would silently answer with PyKX's lib instead of the operator's install. |
| `KDB_HOST` | *(unset)* | Point at an **existing kdb+ server** — only consulted once spawning and a loopback probe both fail; see the chain below. |
| `KDB_PORT` | `5000` | As above. |
| `KDB_MEMORY_MB` | `8192` | Cache budget. q is launched with `-w` = this × 1.25 as containment. |
| `KDB_CACHE_WATERMARK` | `0.75` | Fraction of budget (measured on `.Q.w[]` `heap`) that triggers LRU eviction. |
| `KDB_UPSTREAM` | `eodhd` | Provider used for cache misses. Any registered provider. |
| `QLIC` | *(none — set it)* | Directory q looks in for `kc.lic`. Falls back to `QHOME` (a legacy variable compose no longer sets, so in practice this default doesn't fire) then `/opt/kx`. Load-bearing: the bring-your-own-licence mount is inert unless this points at the directory the licence is actually mounted into. |

`KDB_EMBEDDED`, `KDB_LOCAL_QHOME`, `KDB_HOST`, `KDB_PORT`, `KDB_MEMORY_MB`,
`KDB_CACHE_WATERMARK` and `KDB_UPSTREAM` also accept an OpenBB credential
(`kdb_host`, `kdb_port`, …) which takes precedence over the environment.
`QLIC` does not: it is a property of the container, not of a caller.

Spawned-local and bring-your-own-server are the **same code path** — an IPC
connection to a host and port. Only the "do we start q ourselves" step differs.

### Bring your own q, and licensing

**This is no longer how it works — see below for why the approach this
section originally described was abandoned.** KX's licence does not permit
redistributing their `q` binary, full stop — not flattened out of a layer,
not whited out, not even temporarily present in a builder stage. So the
Dockerfile carries no kdb-x stage at all, the published image contains no
`q` binary in any layer, and `import pykx` is the only kdb+-related thing it
ships. The operator supplies q themselves, and `kdb_store.session.KdbSession`
resolves it through a chain, tried in order:

1. **Spawn.** If `KDB_LOCAL_QHOME` (default `/opt/kx`, bind-mounted from
   `./kdb` in `docker-compose.yml`) holds an executable `bin/q`, run it as a
   child of `openbb-api`, bound to `127.0.0.1:KDB_PORT`.
2. **Loopback.** Probe `127.0.0.1:KDB_PORT` directly — a q another service in
   the shared tailscale network namespace already spawned. This is how
   `live-grid` (which never spawns; `KDB_EMBEDDED=false`) reaches the same q
   `openbb-api` started.
3. **External host.** If `KDB_HOST` is set, connect to `KDB_HOST:KDB_PORT` —
   a kdb container the operator runs themselves, same machine or elsewhere.

The reader supplies their own kdb-x license too, mounted from a git-ignored
path (the `ts.env` / `api-auth.env` pattern) and pointed at by `QLIC` — which
must name the directory the licence is actually mounted into, or the mount
is silently inert. No license blob enters this repository or any image
published from it. `scripts/scrub-check.sh` gates the repository side by
filename, because its content patterns skip binary files.

With no q reachable through any link in the chain, no license, or an
unreachable external server, `provider="kdb"` **passes through to the
upstream provider**. The stack runs uncached rather than failing. This is
also what makes the episode's before/after comparison possible.

**Why the old approach — copying the runtime into a builder stage and
deleting the licence before the final `COPY` — was abandoned.** It relied on
Docker layers being additive: `COPY` the runtime and then `RUN rm` the
licence in the final stage, and the flattened filesystem looks clean, but the
`COPY` layer still holds the intact blob and the `rm` only adds a whiteout on
top — anyone who pulls the image can extract the licence with `docker save`.
(`ls /opt/kx/kc.lic` inside a running container never tested this; the real
test is that no layer tar in `docker save` contains a `kc.lic` entry.) That
was a licence-handling bug, not the actual constraint — the deeper problem is
that KX's licence does not permit redistributing the **`q` binary itself**,
licence file or not. Deleting only `kc.lic` from a shipped image still left
`q` in it. The bring-your-own-q chain above removes the binary from the
image entirely, which is the only fix that actually satisfies the licence.

## The read-through path

### State in q

| Structure | Contents |
|---|---|
| `bars_<SYMBOL>_<INTERVAL>` | One OHLCV table per `(symbol, interval)`, in the **root namespace** — `t`-sorted and **unkeyed** |
| `.cache.cov` | Coverage ranges: `(sym; iv; start; end)` — the windows actually asked for |
| `.cache.lru` | Last-access time per `(symbol, interval)` |

Bars live at root rather than under `.cache` because eviction drops whole
tables by name and `.Q.gc[]` reclaims their heap; a per-table root name keeps
that a one-liner. Deduplication is done on write by keying on `t`, upserting,
then unkeying again (`` `t xasc 0!(`t xkey …) ``), so the stored table is
sorted and unkeyed at rest — which is what range selects want.

`.cache.cov` is the load-bearing structure. Without it there is no way to
distinguish "the provider has no data in that window" from "we never asked",
and the cache degenerates into whole-window memoization.

Coverage records what was **asked for**, not what came back. That is the
deliberate choice that lets an empty range — a market holiday, or the pre-IPO
prefix of a zoomed-out chart — be remembered instead of refetched forever. The
price is trusting the provider not to truncate: a response shorter than the
range it was asked for leaves that hole permanently marked covered and it will
be served as an empty hit. Recording only up to the newest bar returned would
trade this for the worse bug — sparse symbols would become uncacheable — so a
truncated response is treated as the provider-contract violation it is. The
cache is memory-only and process-lifetime, which bounds the damage.

### Serving a request for `(symbol, interval, start, end)`

1. Look up coverage for `(symbol, interval)`. Compute `requested − covered` →
   a gap list, usually empty or a single range.
2. **The tail is never marked covered.** Coverage is recorded only up to the
   last *completed* bar boundary, so the current day (or current intraday bar)
   is refetched on every request. This solves the incomplete-bar problem with
   no TTL.
3. Fetch each gap from the upstream provider, resolved **by name** through
   OpenBB's registry: `RegistryLoader.from_extensions()` →
   `provider.fetcher_dict["EquityHistorical"].fetch_data(params, credentials)`.
   Nothing in this path is EODHD-specific, which is what makes `KDB_UPSTREAM`
   work for any provider.
4. Insert bars, coalesce contiguous coverage ranges, touch LRU.
5. Serve the merged window from q.

### Corporate-action backstop

A split or dividend retroactively rewrites *all* adjusted history, which would
otherwise leave the cache serving silently wrong prices.

Because step 2 refetches the tail regardless, the refetch is overlapped with a
few already-cached bars and the closes are compared. A mismatch means the
adjusted series was rewritten → drop that `(symbol, interval)` entirely and
refetch. The check costs no extra network traffic.

**Known limit: this does not fire for intraday intervals.** The overlap is
three bars' worth of *wall clock*, so for `1m`/`5m`/`1h` that window lands
inside the overnight gap and contains no bars at all — there is nothing to
compare and the check quietly passes. Splits are caught on the daily series
instead, which is where the adjustment that matters is visible.

### Eviction and the memory ceiling

**Measured, not assumed** (probes against `kdb-x` `.z.K=5`, `-w 512`):

- Exceeding `-w` **kills the q process outright**. There is no catchable
  `'wsfull` — the IPC handle dies, the process exits, and reconnect fails.
  This happens on ordinary gradual growth, not just on one huge allocation:
  q died on the allocation after `heap` reached `wmax`.
- `heap`, not `used`, is what approaches `wmax`. `heap` grows in large steps
  (67 MB at a 512 MB cap) and ran ~33 MB ahead of `used`.
- `delete` alone frees `used` but **not** `heap`. `.Q.gc[]` returns heap fully
  to baseline (335 MB → 67 MB in the probe).

Therefore `-w` is **containment, not cache policy**. Its job is to protect the
rest of the container: without it, a runaway cache grows until the OOM killer
takes down openbb-api along with q. q is launched with
`-w` = `KDB_MEMORY_MB` × 1.25 so that normal operation never approaches it.

The real budget is enforced by the extension: when `.Q.w[]``heap` exceeds
`KDB_CACHE_WATERMARK` × `KDB_MEMORY_MB`, whole `(symbol, interval)` tables are
dropped oldest-first, each followed by `.Q.gc[]`.

**Amended in Ep. 10: eviction no longer sees everything in the heap.** This
section was written when `bars_*` tables were the only thing in the q
process. `live-grid` now records ticks into a `trades` table in that *same*
process (see [docs/tick-chart-design.md](tick-chart-design.md)), and `.Q.w[]`
`heap` is per process — so the number the watermark is measured against now
includes tick data that eviction has no way to drop. `evict_until_below`
walks `.cache.lru`, which holds only bar tables. Two consequences:

- A busy tick feed can trigger eviction of cached bars that did nothing to
  earn it.
- If the ticks are what put the heap over budget, eviction drops every bar
  table it has and *still* cannot get under; it exhausts the LRU and logs
  `evict_until_below exhausted the LRU without reaching budget`. That warning
  therefore does not always mean "the cache is too small" — check
  `LIVE_TICK_WINDOW` before raising `KDB_MEMORY_MB`.

`trades` is bounded by its rolling window instead, which is a retention
policy and not a share of this budget.

### Surviving a dead q

Because q *can* die, the extension treats a dead connection as a normal state,
not an exception: it detects the closed handle, respawns q if it owns the
process (`KDB_EMBEDDED=true`), reconnects, and serves the request by
pass-through in the meantime. A cold cache after a death is correct behaviour —
the cache was never persisted anyway.

**With one deliberate exception: a failed connect suppresses the next
attempts.** If reaching q fails — the overwhelmingly common cause being no
licence — the session records a deadline (`_retry_after`, `_RETRY_AFTER_S` =
60s) and every request inside that interval goes straight to pass-through
without touching q. The alternative is paying a doomed process spawn and a
five-second connect budget on *every* request for a reader who simply has no
licence, which is the configuration the stack is explicitly designed to
tolerate.

**Amended in Ep. 10: the latch expires; it used to be permanent.** A
permanent latch cost a real failure once `live-grid` joined the same q. On a
cold `docker compose up`, `openbb-api` spawns q lazily — on the first
`provider=kdb` request — while `live-grid`'s drain loop reaches out about
0.2 s after start. Nothing has bound `:5000` yet, so live-grid's one refused
connect turned tick recording and the tick half of the chart off for its
whole process lifetime, recoverable only by restarting that container by hand
after something else warmed q. A deadline keeps the property that mattered
(attempts are capped at one per interval, however hot the caller's loop) and
drops the one that did not (never again). It is a deadline rather than an
attempt count because the hazard is a rate, not a total: a counter would
either exhaust itself during the very startup race it needs to survive, or
never expire at all. Mounting a licence into a running container now re-arms
the cache within a minute, no restart required.

### Concurrency

Two separate mechanisms, for two separate problems.

A per-`(symbol, interval)` asyncio lock deduplicates *work*: two widgets
loading the same symbol produce one upstream fetch rather than two.

Thread affinity keeps PyKX alive: every call into q is marshalled onto the
session's single owner thread (see risk 3 below). This is not an optimisation
and not a lock — PyKX aborts the process when touched from a second thread even
with no concurrency at all.

### Telemetry

`transform_data` returns
`AnnotatedResult(result=…, metadata={cache, rows_from_cache, rows_from_upstream, gaps_fetched, upstream_ms, kdb_ms})`,
which OpenBB surfaces as `OBBject.extra["results_metadata"]` and the REST API
serializes into every response. Cache statistics travel with the data; no side
channel is needed.

`cache` is one of `hit` (no upstream call), `partial` (gaps fetched),
`miss` (nothing cached), or `bypass` (cache unavailable, passed through).

## Components

**`cache-chart/` row below is superseded** — see the notice at the top of
this document; the chart lives in `live-grid/` instead.

| Path | What it is |
|---|---|
| `openbb-kdb/` | Provider extension registering `provider="kdb"` for equity / ETF / crypto / currency / index historical. Added to this repo's Dockerfile (PyKX is not currently in the image). |
| ~~`cache-chart/`~~ | ~~FastAPI service, `live-grid/`'s layout: the Workspace widget backend and the standalone demo page.~~ Deleted; superseded by the chart in `live-grid/` — see [docs/tick-chart-design.md](tick-chart-design.md). |
| `docker-compose.yml` | q startup in the openbb-api container, `mem_limit`, license mount. |
| `scripts/verify-isolation.sh` | Extended to check port 5000. |

Intervals in scope: daily **and** intraday (`1m`, `5m`, `1h`), matching EODHD's
coverage.

## The chart (superseded — kept for history, see the notice at the top)

This whole section describes the standalone `cache-chart/` service as
originally planned. It was deleted before release; the shipped chart lives in
`live-grid/` and is built from the tick stream, not solely from the
historical cache. See [docs/tick-chart-design.md](tick-chart-design.md) for
what actually shipped.

`cache-chart/` calls the OpenBB API on loopback `127.0.0.1:6900` with
`provider=kdb` and Basic auth, so the demo exercises exactly the path any
client would take, and reads `extra.results_metadata` for the HUD.

| Endpoint | Purpose |
|---|---|
| `GET /widgets.json` | Workspace / BDOBB widget contract |
| `GET /chart` | Plotly figure JSON — the Workspace widget |
| `GET /series` | `{bars, cache}` — incremental loads for the demo page |
| `GET /demo` | the standalone page |
| `GET /health` | upstream and cache reachability |

### The scroll gesture

The page opens on 1 year of daily bars. `plotly.js` — **vendored into the image
at build time, never a CDN** — with `scrollZoom: true`. A `plotly_relayout`
handler fires on wheel-zoom; when the new x-range reaches past what is loaded,
the page requests **only the missing prefix** `[newStart, loadedStart)` and
prepends it. Debounced ~150 ms so one continuous scroll produces one request at
rest rather than one per wheel tick, with in-flight coalescing and a 10-year
clamp.

The HUD reports, per request: window requested, rows from cache, rows from
upstream, `upstream_ms`, `kdb_ms`, bytes over the wire — plus a cumulative
count of **rows served from cache**.

A cumulative *saved-bytes* counter was built first and then removed as
dishonest. Every response — `hit`, `partial` or `miss` alike — crosses the
browser↔service link in full, so nothing is saved on the wire the reader can
actually see; a `hit` means only that the **backend** skipped the vendor. Rows
served from cache without the vendor being called is the claim the cache
genuinely supports, so that is what the HUD counts.

The demonstration is the **second** gesture. Scrolling 1y→3y the first time
reports `partial` with ~2 years fetched. Scrolling back **in** issues no
request at all — the window is inside what is already loaded, and only a gap
*outside* `[loadedStart, loadedEnd]` is ever fetched. Scrolling out again over
the same range fetches nothing from the vendor.

One honest caveat, stated the same way in `cache-chart/README.md`: any window
that reaches today always refetches that day's still-forming bar, so it reports
`partial`, not `hit`, even on an exact repeat. A clean `hit` needs a window
that ends in the past.

### Why there are two deliverables

A Workspace / BDOBB plotly widget receives figure JSON once and zooms
client-side; it will not refetch on scroll. The widget therefore benefits from
the cache on **parameter** changes — symbol, date range, interval — while the
scroll story requires the standalone page.

## Networking

`q` binds `127.0.0.1:5000` and is never published by Serve, never funneled.
(`cache-chart` was built and committed, then removed before release in favor
of folding its chart into `live-grid`, so the `:6906` Serve route describing
it never shipped in a tagged release — the chart is served by `live-grid` on
its existing `:6903` route instead.)

## Testing

The gap arithmetic is the one piece with real logic and no I/O: coverage
subtraction, range coalescing, tail exclusion and corporate-action detection
are unit-tested against a fake q connection. (The charting tests described
here as `cache-chart`'s never shipped under that name; the equivalent
coverage lives in `live-grid/tests` — see
[docs/tick-chart-design.md](tick-chart-design.md).) **CI needs neither a kdb
license nor an EODHD key.**

## Risks

1. **The loopback bind is load-bearing.** The default `KX_PORT=5000` binds all
   interfaces; inside the shared tailscale namespace that exposes an
   unauthenticated q — and q IPC executes arbitrary q — to every tailnet peer.
   Verified directly: a sibling container reaches `KX_PORT=5000` and cannot
   reach `KX_PORT=127.0.0.1:5000`. `verify-isolation.sh` gains a check for it.
2. **A bug in eviction kills q, it does not merely error.** Verified: crossing
   `-w` terminates the process, so the failure mode is "the cache tier
   vanished", not "a query failed". This is why eviction is preventive, why
   `-w` sits above the budget, and why respawn + pass-through are required
   rather than optional. The pass-through path must catch dead-connection
   errors (`RuntimeError`, `PyKXException`), not only connection-refused.
3. **PyKX connection sharing — settled, and it is the episode's headline
   finding.** The risk was written as "sharing one connection across concurrent
   tasks is unproven". What the investigation found is worse and more
   interesting than a concurrency bug: **PyKX cannot be used across threads at
   all — not merely concurrently.** Four *strictly sequential* write/read
   rounds, no overlap whatsoever, survive when they run on one thread and abort
   the whole process with `free(): invalid size` when each round runs on a
   fresh thread. A mutex cannot fix that; there is nothing to serialise, and
   serialising the calls leaves the same heap corruption while hiding its
   visible symptom.

   The resolution is **thread affinity**, not locking. `session.py` owns a
   single-worker `ThreadPoolExecutor(max_workers=1)` and marshals *every* PyKX
   interaction through it — spawning q, opening the connection, every query,
   every K-object conversion — onto that one owner thread for the process
   lifetime. Callers hand work in via `KdbSession.run()`. Affinity that begins
   after the connection was opened on some other thread is not affinity, so the
   connection is created on the owner thread too. A call already *on* the owner
   thread runs inline, because a one-worker pool would otherwise deadlock
   against itself.
4. **Adjusted-price drift.** Handled by the tail-overlap check; tested
   explicitly because it is the failure that would otherwise be silent.
5. **Container memory.** `mem_limit` on openbb-api must exceed
   `KDB_MEMORY_MB` plus OpenBB's own footprint (~2 GB), or the OOM killer takes
   the API down rather than just the cache.

## Rejected alternatives

| Approach | Why not |
|---|---|
| Whole-window memoization keyed on `(symbol, interval, start, end)` | A 1y→3y zoom is a total miss and refetches all three years. It defeats the entire point. |
| Eager prefetch of max history on first touch | The first request is slow and pulls far more than asked — the opposite of minimizing network traffic. Intraday max-history is enormous. |
| A caching proxy in front of the OpenBB API | Caches only the routes the proxy knows, and adds a second HTTP hop. The provider tier gets the cache to every consumer — API, MCP, widgets — for free. |
| A separate kdb container | Rejected in favour of q inside the openbb-api container; the shared network namespace already gives every service access to one loopback cache. |
| Persisting the cache to a volume | It is a cache. Cold-start-empty is simpler and honest, and avoids serving stale adjusted history from disk. |

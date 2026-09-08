<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# openbb-kdb

In-memory **kdb+ read-through cache** for the OpenBB Platform. Companion code
for *Adventures in OpenBB, Ep. 10*.

`provider="kdb"` serves bars it already holds, fetches only the date ranges it
is missing from an upstream provider, stores those, and returns the merged
series:

```python
obb.equity.price.historical("AAPL", provider="kdb",
                            start_date="2022-01-01", end_date="2025-01-01")
```

Every response carries cache telemetry in `extra["results_metadata"]`:

```python
{"cache": "partial", "rows_from_cache": 252, "rows_from_upstream": 504,
 "gaps_fetched": 1, "upstream_ms": 412.7, "kdb_ms": 3.1}
```

`cache` is `hit` (no upstream call), `partial` (gaps fetched), `miss` (nothing
cached) or `bypass` (kdb unavailable — served straight from upstream).

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `KDB_EMBEDDED` | *derived* | Spawn q inside this container. Unset, it follows whether a q is actually found at `KDB_LOCAL_QHOME` — see the chain below. Set it explicitly to override |
| `KDB_LOCAL_QHOME` | `/opt/kx` | Where the operator mounted their own q (bring-your-own — this repo ships none; see [../kdb/README.md](../kdb/README.md)). `QHOME` is accepted as a fallback for anyone carrying the older variable, read once per process, *before* `import pykx` rewrites it to PyKX's own bundled q |
| `KDB_HOST` | *(unset)* | Point at your own kdb+ container — only consulted once the chain's earlier links fail; see below |
| `KDB_PORT` | `5000` | |
| `KDB_MEMORY_MB` | `8192` | Cache budget; q gets `-w` 25% above it |
| `KDB_CACHE_WATERMARK` | `0.75` | Heap fraction that triggers LRU eviction |
| `KDB_UPSTREAM` | `eodhd` | Provider used for cache misses — any installed provider |
| `QLIC` | *(none — set it)* | Directory q looks in for `kc.lic`. Falls back to `QHOME` (a legacy variable compose no longer sets, so in practice this default doesn't fire) then `/opt/kx`, so set `QLIC` explicitly — if it points anywhere else the mount is silently inert |

**The resolution chain**, tried in order on every connect/respawn attempt:

1. **Spawn.** If `KDB_LOCAL_QHOME` (default `/opt/kx`) holds an executable
   `bin/q`, run it as a child bound to `127.0.0.1:KDB_PORT`.
2. **Loopback.** Probe `127.0.0.1:KDB_PORT` — a q another service in the same
   shared network namespace already spawned (e.g. `openbb-api`, from
   `live-grid`'s point of view).
3. **External host.** If `KDB_HOST` is set, connect to `KDB_HOST:KDB_PORT` —
   a kdb container the operator runs themselves.

If none of the three connect, the provider passes through to `KDB_UPSTREAM`
and reports `cache: "bypass"`.

Everything except `QLIC` also accepts an OpenBB credential
(`kdb_host`, `kdb_port`, …), which takes precedence over the environment.

## Requirements

- A kdb+/kdb-x **license** (`kc.lic`) for the q server. PyKX itself runs as an
  unlicensed IPC client and needs none.
- Without a license or a reachable q, the provider passes through to the
  upstream and reports `cache: "bypass"`. Nothing breaks.

## Notes

- **The shared session is built once per process, from the first caller's
  credentials.** All five fetchers reuse one q session/cache for the life of
  the process; a later call with different `kdb_*` credentials silently reuses
  whatever the first call resolved. Restart the process to pick up new
  credentials.
- **The cache is memory-only.** A restart means a cold cache. It is a cache.
- **Coverage records what was *asked for*, not what came back.** That is what
  lets an empty range — a market holiday, the pre-IPO prefix of a zoomed-out
  chart — be remembered instead of refetched forever. The trade-off is trusting
  the provider not to truncate: a response shorter than the range requested
  leaves that hole marked covered and it is served as an empty hit.
- **The corporate-action backstop does not fire for intraday intervals.** The
  tail refetch is overlapped with a few cached bars and their closes compared,
  which catches a split rewriting adjusted history. But the overlap window is
  wall-clock, so on `1m`/`5m`/`1h` it falls inside the overnight gap and
  contains no bars. Splits are caught on the daily series.
- **A failed *first* connect latches for the process lifetime.** If q cannot be
  reached on the first attempt — usually a missing licence — the session gives
  up permanently and every later request goes straight to pass-through, rather
  than paying a doomed spawn and connect timeout on each one. Recovering from
  that state needs a container restart; mounting a licence into a running
  container will not re-arm the cache.
- **PyKX is single-threaded, hard.** It aborts the process with
  `free(): invalid size` when used from a second thread even with no
  concurrency at all, so a lock cannot fix it. Every PyKX call is marshalled
  onto one owner thread that lives for the process lifetime.
- **q binds `127.0.0.1`.** Every service in this stack shares one network
  namespace, so a `0.0.0.0` bind would publish an unauthenticated q — which
  executes arbitrary q — to every peer on the tailnet.
- **Crossing q's `-w` kills the process**; there is no catchable `'wsfull`.
  Eviction is preventive and `-w` is containment for the rest of the container.

## Test

    pip install -e .[dev] && pytest    # no kdb license or provider key needed

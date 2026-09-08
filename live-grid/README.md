<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# live-grid

OpenBB Workspace `live_grid` backend streaming real-time **EODHD** prices —
US equities, crypto and forex in one watchlist. Companion code for
*Adventures in OpenBB, Ep. 9*, extended in **Ep. 10** with tick recording and
a chart that joins cached history to bars built from the live feed.

- `GET /widgets.json` — the widget contract (`live_grid` plus, when the chart
  is enabled, `kdb_cache_chart`).
- `GET /live_grid?symbol=A,B,C` — initial rows, seeded from REST snapshots
  (previous close is cached as the change baseline).
- `WS /live_grid_ws` — Workspace sends `{"params":{"symbol":"A,B"}}` on
  connect and on every param change; the backend streams one row dict per
  update, coalesced to ~4 flushes/second per connection.
- `GET /health` — per-feed connection state, plus a `ticks` block
  (`buffered`, `written`, `dropped`) when the chart is enabled.
- `GET /chart?symbol=AAPL&interval=1d` — Plotly figure JSON, the Workspace
  `kdb_cache_chart` widget.
- `GET /series?symbol=AAPL&interval=1d&start=&end=` — `{bars, cache}` for
  incremental loads; `cache` reports `rows_from_cache`, `rows_from_upstream`,
  `rows_from_ticks` and a `seam` timestamp (see below).
- `GET /demo` — a standalone scroll page for the chart, moved here from the
  now-removed `cache-chart` service.

Symbols route by shape: a dash means crypto (`BTC-USD`), two ISO currency
codes mean forex (`EURUSD`), everything else is a US equity. Feed clients are
rebuilt (debounced ~1s) when the subscribed union changes; the SDK's
signal-handler and grow-forever-buffer quirks are handled in `app/feeds.py`.

## Tick recording and the chart (Ep. 10)

Every trade this service already receives over the websocket is now also
recorded into whichever q the same **`kdb-store`** resolution chain finds —
in the stock compose setup, the **same kdb+ instance `openbb-api` spawns**,
reached at `127.0.0.1:5000` through the shared tailscale network namespace
(no new port, and this container never spawns its own q; see
`docker-compose.yml`'s `KDB_EMBEDDED=false`). See
[openbb-kdb/README.md](../openbb-kdb/README.md#configuration) for the full
chain (bring-your-own q dropped in `./kdb`, then loopback, then `KDB_HOST`) —
this service's `KDB_EMBEDDED=false` only opts it out of the *spawning* step,
so it always joins a q something else provides. `GET /health` reports which
link resolved, under `ticks.endpoint` — `"spawned"`, `"loopback"`, or
`"<host>:<port>"` — so you can tell which one is actually in use without
guessing. Ticks accumulate in a bounded in-process buffer
(`app/recorder.py`) and are flushed to q as one batch on the existing
~250 ms cadence — per-tick IPC would not keep up. On overflow the buffer
drops the oldest tick, counts the drop, and reports it on `/health`.

**Retention is a rolling window**, `LIVE_TICK_WINDOW_SECONDS` (default
86400 = 1 day). A pruning pass drops ticks older than the window so a busy
feed's tick storage cannot grow without bound or compete with the historical
cache under its own LRU.

**The seam.** A chart request stitches cached history to tick-derived bars at
the first bar boundary *fully* covered by ticks — not the first tick itself.
The bar straddling that boundary would be missing its own opening trades if
aggregated from ticks alone, so it comes from history instead; ticks own only
the bars they cover completely. `/series` reports the seam timestamp and
`rows_from_ticks` whenever ticks contributed to the response, and the joined
series is strictly time-ordered with no duplicate timestamps across the seam.

**The two sources will not agree exactly**, and this is inherent, not a bug:
vendor bars are consolidated across venues and adjusted after the fact, while
this service's bars are aggregated from whatever prints its own websocket
connection happened to receive. Closes track closely near the seam; **volume
in particular can differ visibly**, since a vendor's consolidated tape counts
trades this feed never saw. Don't expect — and don't advertise — a seamless
match; the seam marker exists precisely because the two sides are different
data.

**Turning it off.** Set `LIVE_GRID_CHART=false` (or leave kdb+ unreachable —
recording degrades to a logged warning) and `live_grid` / `live_grid_ws`
behave exactly as they did before Ep. 10; `/chart` and `/series` still serve
history-only data with no ticks contributed.

## Keys

`EODHD_API_KEY` — the public demo websocket key in `.env.example` covers
exactly the default watchlist for testing. Real watchlists need a paid EODHD
key with websocket access (free-tier keys 403 on connect; the REST-only
`demo` token fails the SDK's key-format validation).

### Authentication

Every live-grid surface — REST routes and both
websockets — requires HTTP Basic auth. The credentials are
`OPENBB_API_USERNAME` and `OPENBB_API_PASSWORD`, read from the same
`api-auth.env` that openbb-api uses and that compose already mounts here, so
there is one credential to rotate for the stack's HTTP surfaces. Auth is **on
by default in this stack**: compose loads `api-auth.env` into live-grid the
same way it does for openbb-api, and `api-auth.env.example` ships
`OPENBB_API_AUTH=true`, so the guard is live from the moment `api-auth.env`
is copied into place.

The guard is off only when `OPENBB_API_AUTH` is unset, or set to anything
other than `1`, `true`, `yes` or `on` (case-insensitive) — `false` counts as
"set" but still disables it. That's what local development and the test
suite rely on.

A CORS preflight is answered without credentials — the guard is registered
inside the CORS layer deliberately. A browser preflight carries no credentials
by definition, so an auth layer outside CORS would answer it with a bare 401 and
lock out every cross-origin client while curl still worked.

## Test

    pip install -e .[dev] && pytest          # all mocked, no key, no kdb+ needed
    python scripts/smoke_live.py             # real key: crypto feed, 24/7

The aggregation query itself (`` `time xasc `` before bucketing — ticks
arrive out of order and an unsorted trades table silently produces wrong
candles) is verified against a **real** q by
[`kdb-store/scripts/tick_check.py`](../kdb-store/scripts/tick_check.py), not
by anything mocked here. See that script and `kdb-store/README.md`.

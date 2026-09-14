# live-grid

OpenBB Workspace `live_grid` backend streaming real-time **EODHD** prices —
US equities, crypto and forex in one watchlist. Companion code for
*Adventures in OpenBB, Ep. 9*, extended in **Ep. 10** with tick recording and
a chart that joins cached history to bars built from the live feed.

- `GET /widgets.json` — the widget contract (`live_grid`, `live_chart`,
  `kdb_cache_chart`, `ta_chart` and `kdb_ticks`).
- `GET /live_grid?symbol=A,B,C` — initial rows, seeded from REST snapshots
  (previous close is cached as the change baseline).
- `WS /live_grid_ws` — Workspace sends `{"params":{"symbol":"A,B"}}` on
  connect and on every param change; the backend streams one row dict per
  update, coalesced to ~4 flushes/second per connection.
- `GET /snapshot?symbol=AAPL` — one symbol's delayed REST snapshot (EODHD,
  15-20 minutes behind), for callers that want a quote without the live feed.
- `GET /health` — per-feed connection state, plus a `ticks` block
  (`buffered`, `written`, `dropped`) when the chart is enabled.
- `GET /chart?symbol=AAPL&interval=1d` — Plotly figure JSON, the Workspace
  `kdb_cache_chart` widget.
- `GET /series?symbol=AAPL&interval=1d&start=&end=` — `{bars, cache}` for
  incremental loads; `cache` reports `rows_from_cache`, `rows_from_upstream`,
  `rows_from_ticks` and a `seam` timestamp (see below).
- `GET /ta_chart?symbol=AAPL&interval=1d&...` / `WS /ta_chart_ws` — indicator
  chart over cached OHLCV in stacked panes, the Workspace `ta_chart` widget.
- `GET /ticks?symbol=AAPL&limit=200` — the newest ticks held in the kdb+ tick
  cache for one symbol, newest first; the Workspace `kdb_ticks` widget.
- `GET /subscriptions` — the subscriptions management page (see
  `POST /subscribe` below).
- `GET /demo` — a standalone scroll page for the chart, moved here from the
  now-removed `cache-chart` service.

### `POST /subscribe`

Leases a live feed for symbols, keyed by symbol and renewable:

    curl -X POST http://127.0.0.1:6903/subscribe \
      -H 'content-type: application/json' \
      -d '{"symbols":["AAPL"],"ttl":300}'
    {"leases":{"AAPL":"2026-08-26T15:20:00+00:00"}}

Posting a symbol that is already leased extends its expiry rather than adding
a second registration. Leases lapse after their TTL unless renewed; a sweeper
drops them. Tailnet-only, like every route here — never funnel it.

Leasing a symbol that is not already fed rebuilds its EODHD feed, because the
SDK fixes symbols at construction. That briefly interrupts ticks for other
symbols on the same feed, which is why the TTL is generous rather than tight.

Symbols route by shape: a dash means crypto (`BTC-USD`), two ISO currency
codes mean forex (`EURUSD`), everything else is a US equity. Feed clients are
rebuilt (debounced ~1s) when the subscribed union changes; the SDK's
signal-handler and grow-forever-buffer quirks are handled in `app/feeds.py`.

All three lease values are configurable: `ttl` per request in the `/subscribe`
body (default 300s -- `LIVE_GRID_LEASE_TTL_S`), and `LIVE_GRID_LEASE_SWEEP_S`
(default 30s) for how often the sweeper checks for lapsed leases. `/health`
reports the current lease count.

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

Every live-grid surface — REST routes, the subscriptions page and both
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

Note for the subscriptions widget: it is declared `type: iframe`, and the
desktop client renders an iframe widget as a plain `<iframe src>` navigation
with no headers — the Connection's configured `Authorization` header is
attached only to fetch-path requests, which iframe widgets do not make. So with
auth enabled the widget's initial page load receives a 401 that the client
cannot satisfy. Resolving that is a design question, not a configuration one:
the options are exempting the page (its API stays guarded), moving the widget
off `type: iframe`, or adding a credential the iframe can carry. Until one is
chosen, the subscriptions widget (only present when `LIVE_GRID_PUBLIC_URL` is
set) does not work through the guard — and since auth is on by default in
this stack, that is the widget's default state, not an opt-in one.

## Test

    pip install -e .[dev] && pytest          # all mocked, no key, no kdb+ needed
    python scripts/smoke_live.py             # real key: crypto feed, 24/7

The aggregation query itself (`` `time xasc `` before bucketing — ticks
arrive out of order and an unsorted trades table silently produces wrong
candles) is verified against a **real** q by
[`kdb-store/scripts/tick_check.py`](../kdb-store/scripts/tick_check.py), not
by anything mocked here. See that script and `kdb-store/README.md`.

## Technical Chart (`ta_chart`)

Indicators over the same cached OHLCV the other charts use, in stacked Plotly
panes. 22 tier-1 indicators; 12 of them can also be drawn from EODHD's own
pre-calculated values by setting `source=eodhd`, and every one of those 12 is
checked against EODHD's numbers by a network-gated parity test.

CCI is deliberately local-only. EODHD's CCI disagrees with the standard
definition by a median of 28.5%, so offering both sources for it would mean the
line jumped when you toggled `source`. It computes locally and says so in the
legend.

Layouts are **macros** — YAML files in `macros/`, one pane per entry:

```yaml
label: Classic Momentum
panes:
  - id: price
    height: 3
    indicators:
      - {name: bbands, period: 20, k: 2.0}
  - id: rsi
    height: 1
    indicators: [{name: rsi, period: 14}]
```

`height` is a relative weight. Exactly one pane must have `id: price`. Drop a
file into `TA_MACRO_DIR` and it appears in the widget's Macro dropdown without
a rebuild — `/widgets.json` is generated, not static.

Indicators can also be listed directly:
`?indicators=rsi:period=14,sma:period=200`.

**On `source=eodhd`:** EODHD bills five API calls per indicator per request, so
those series are cached and refreshed only on bar close (`TA_EODHD_MIN_REFETCH_S`).
Locally computed series update at tick speed; EODHD-sourced ones step. Any
indicator without an EODHD equivalent falls back to local compute and says so
in the chart title. EODHD's technical endpoint is daily only, so on any
interval other than `1d` the whole request is computed locally and annotated
rather than fanning one daily value across every bar of the day.

Smoke check: `python scripts/smoke_ta.py http://127.0.0.1:6903`

### Subscriptions widget

`GET /subscriptions` serves a page, declared in `widgets.json` as an `iframe`
widget, that manages the EODHD live subscription list. Its API:

    GET    /api/subscriptions              current state
    POST   /api/subscriptions              {"symbol": "AAPL"} -> 201
    DELETE /api/subscriptions/{symbol}     -> 200

Pinned symbols persist in `LIVE_GRID_WATCHLIST` (default `/data/watchlist.json`,
which needs the `./live-grid-data:/data` mount) and are re-registered with the
feed manager at startup, so a restart restores the subscriptions rather than just
the list.

`LIVE_GRID_MAX_SYMBOLS` (default 50) caps the account-wide EODHD budget. The
count is everything actually subscribed at EODHD right now — pinned, leased,
*and* whatever a `/live_grid_ws` grid connection has registered, all read off
the feed manager's own view, so a symbol subscribed twice over is still one
slot. An add that would exceed the cap returns 507. Leased symbols appear in
the widget but cannot be removed there — they lapse on their own TTL. A
watchlist file with more symbols than the cap only has the first `cap` of
them (sorted) registered at startup; the rest are logged as dropped.

The widget itself needs `LIVE_GRID_PUBLIC_URL` set (e.g.
`https://openbb.<your-tailnet>.ts.net:6903`) — an `iframe` widget's endpoint
must be an absolute URL, so without it the subscriptions widget is left out
of `/widgets.json` entirely rather than shipping a broken one.

Tailnet-only, like every route here. Never funnel it. `POST`/`DELETE
/api/subscriptions` additionally reject a request whose browser `Origin`
header does not match `LIVE_GRID_PUBLIC_URL` (a request with no `Origin`,
e.g. curl, is unaffected) — CORS here is wide open for the Workspace origin's
reads, and this keeps some other page an operator has open from silently
mutating the durable watchlist.

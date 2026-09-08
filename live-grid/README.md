<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# live-grid

OpenBB Workspace `live_grid` backend streaming real-time **EODHD** prices —
US equities, crypto and forex in one watchlist. Companion code for
*Adventures in OpenBB, Ep. 9*.

- `GET /widgets.json` — the widget contract (`type: live_grid`,
  `wsEndpoint`, `wsRowIdColumn: symbol`).
- `GET /live_grid?symbol=A,B,C` — initial rows, seeded from REST snapshots
  (previous close is cached as the change baseline).
- `WS /live_grid_ws` — Workspace sends `{"params":{"symbol":"A,B"}}` on
  connect and on every param change; the backend streams one row dict per
  update, coalesced to ~4 flushes/second per connection.
- `GET /health` — per-feed connection state.

Symbols route by shape: a dash means crypto (`BTC-USD`), two ISO currency
codes mean forex (`EURUSD`), everything else is a US equity. Feed clients are
rebuilt (debounced ~1s) when the subscribed union changes; the SDK's
signal-handler and grow-forever-buffer quirks are handled in `app/feeds.py`.

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

    pip install -e .[dev] && pytest          # all mocked, no key needed
    python scripts/smoke_live.py             # real key: crypto feed, 24/7

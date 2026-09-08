<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# stores-explorer

Read-only widget backend for browsing the shared Delta Lake and kdb+ store —
the "explorer" widget from *Adventures in OpenBB, Ep. 11*. Design:
`../docs/superpowers/specs/2026-08-07-stores-explorer-design.md`.

Pairs with `stores-mcp`: same underlying discovery code
(`mcp_stores/server.py`), two doors — `stores-mcp` answers an agent (Rita),
this answers a widget (bdobb, over the tailnet). No app-level auth: loopback
bind + Tailscale Serve is the only ingress, matching `live-grid`.

## Endpoints

**Delta Lake:** `GET /delta/libraries`, `GET /delta/symbols?library=`,
`GET /delta/describe?library=&symbol=` (row count, date range, columns —
answered from the transaction log, no row data read),
`GET /delta/history?library=&symbol=` (versions, newest first),
`GET /delta/series?library=&symbol=&start=&end=&tail_rows=&as_of=`
(`as_of` is an int Delta version or an ISO timestamp).

**kdb+:** `GET /kdb/tables`, `GET /kdb/schema?table=`,
`GET /kdb/select?table=&symbol=&start_time=&end_time=&limit=`.

`GET /widgets.json` declares both `delta_explorer` and `kdb_explorer`
(`type: "table"` — bdobb's existing table/chart auto-detection handles the
"plotted series" step, no bespoke renderer needed).

## Test

    pip install -e ../mcp_stores && pip install -e .[dev] && pytest

All backend calls are injected in tests (see `tests/test_main.py`'s
`make_client`/`make_kdb_client`) — no real Delta store or kdb+ needed.

# stores-explorer

Read-only widget backend for browsing the shared ArcticDB and kdb+ store —
the "explorer" widget from *Adventures in OpenBB, Ep. 11*. Design:
`../docs/superpowers/specs/2026-08-07-stores-explorer-design.md`.

Pairs with `stores-mcp`: same underlying discovery code
(`mcp_stores/server.py`), two doors — `stores-mcp` answers an agent (Rita),
this answers a widget (bdobb, over the tailnet). No app-level auth: loopback
bind + Tailscale Serve is the only ingress, matching `live-grid`.

## Endpoints

**ArcticDB:** `GET /arctic/libraries`, `GET /arctic/symbols?library=`,
`GET /arctic/summary?library=&symbol=` (row count, date range, columns —
no row data read), `GET /arctic/series?library=&symbol=&start=&end=&tail_rows=`.

**kdb+:** `GET /kdb/tables`, `GET /kdb/schema?table=`,
`GET /kdb/select?table=&symbol=&start_time=&end_time=&limit=`.

`GET /widgets.json` declares both `arctic_explorer` and `kdb_explorer`
(`type: "table"` — bdobb's existing table/chart auto-detection handles the
"plotted series" step, no bespoke renderer needed).

## Test

    pip install -e ../mcp_stores && pip install -e .[dev] && pytest

All backend calls are injected in tests (see `tests/test_main.py`'s
`make_client`/`make_kdb_client`) — no real ArcticDB or kdb+ needed.

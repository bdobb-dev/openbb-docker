# stores-explorer — the widget's door into the shared store

Design spec for Adventures in OpenBB, Episode 11 (backend half). Companion
to `docs/superpowers/specs/2026-08-05-arcticdb-minio-design.md`, which built
the vault (`openbb-arcticdb`, MinIO) and explicitly deferred a Workspace
widget ("a widget would dilute" that chapter's plain-Python point) and kdb+
interaction ("orthogonal"). This spec is where both come back: `stores-mcp`
already gives Rita a door into both stores; this gives the widget one too.

## Context

`episodes-10-12-plan.md` (substack-articles project) frames Ep. 11 around
"two front doors, one vault": `stores-mcp` answers the analyst, this service
answers the widget, and both sit on the same read-only discovery code in
`mcp_stores/server.py` (`arctic_list_libraries`, `arctic_list_symbols`,
`arctic_read`, `kdb_tables`, `kdb_table_schema`, `kdb_select`). Building the
widget's door as a second implementation of that logic would be exactly the
kind of drift the plan's "same code, two doors" framing exists to avoid.

The bdobb-side widget itself (the picker UI, param wiring at the frontend)
is a separate, later phase — this spec covers only the service: the
endpoints it answers and the `widgets.json` contract it publishes.

## Goals

1. A `stores-explorer` HTTP service answers over Tailscale Serve, giving a
   bdobb widget a library/symbol picker into ArcticDB and a table picker
   into kdb+ — the vault and the tape, browsable without SSH or a REPL.
2. Zero duplicated discovery logic: every ArcticDB/kdb+ call goes through
   `mcp_stores`'s existing, already-scrubbed, already-timeout-bounded
   functions.
3. `scripts/scrub-check.sh` stays green — no MinIO/ArcticDB credential,
   bucket path, or host name in any tracked file this service adds.

## Non-goals

- The bdobb frontend widget itself (picker UI, param handling, any new
  renderer code). Separate phase.
- Write access to either store. Read-only, matching `stores-mcp`.
- A unified single endpoint across both stores. ArcticDB and kdb+ have
  genuinely different shapes (library/symbol vs. table/column); forcing one
  endpoint to cover both would just move the branching into the response
  shape instead of the URL.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **No app-level auth** — loopback bind + Tailscale Serve only, matching `live-grid`/`stores-mcp` | Read-only browsing of data already behind the tailnet; nothing to write, nothing beyond what `stores-mcp` already exposes unauthenticated to any tailnet client. Confirmed with Art — the alternative (`key-maint`'s tiered role/Basic-auth) exists in this repo but is solving a different problem (gating writes to secrets). |
| D2 | **CORS wide open**, matching `live-grid`, not `key-maint`'s narrow allowlist | Follows directly from D1: once topology is the real gate, an origin restriction adds no protection, only friction if a future client needs it. |
| D3 | **Reuse `mcp_stores` by import, not duplication** — `pip install ./mcp_stores` (repo-root build context) then `from server import arctic_list_libraries, arctic_list_symbols, arctic_read, kdb_tables, kdb_table_schema, kdb_select` | `mcp_stores` is already a real installable package (`pyproject.toml`, `py-modules = ["server"]`) with the credential-scrub (`_scrub`) and timeout-bound (`_bounded`) machinery already built and tested. Reimplementing any of it here is exactly the drift the "two doors, one vault" framing exists to prevent. |
| D4 | **Both stores, two widgets** — ArcticDB and kdb+ each get their own `widgets.json` entry, not one toggled widget | Matches this repo's established convention: `live_grid`, `kdb_cache_chart`, and `live_chart` are separate additive widgets, never one multi-mode widget. Each picker flow (library→symbol vs. table→columns) stays a simple, single-purpose param chain. |
| D5 | **`type: "table"` for both widgets**, no new bdobb renderer code required | bdobb's existing `chartShapes.ts`/`ChartRenderer` already auto-detects a date column plus OHLC or a single numeric column and offers a chart view (`canToggleChart`) — the same machinery `kdb_cache_chart` already uses. ArcticDB/kdb+ data is generic (a `ticks`/`quotes` library isn't OHLCV-shaped), so the widget must be a table browser first; charting falls out for free when the shape supports it. |
| D6 | **A cheap `/arctic/summary` endpoint, metadata only** (`row_count`, `date_range`, `columns`, `dtype` via ArcticDB's `get_description`) — no row data read | The plan's stated widget flow is "library picker → symbol list → **available range and datapoints** → the stored series plotted" — a distinct step before committing to fetching/plotting. `get_description` answers it without materializing any rows, so showing it costs nothing extra. How (or whether) Phase 3's UI surfaces it is that phase's call; the endpoint exists and is tested regardless. |
| D7 | **Static `widgets.json` file**, not an inline Python dict | Matches `live-grid`'s approach, the closest precedent for "multiple widgets, options-endpoint-driven params" (`key-maint`'s inline-dict approach fits its two simpler, non-cascading widgets better). |

## Architecture

### Endpoints

All GET, all JSON, all thin wrappers over the imported `mcp_stores`
functions — no new ArcticDB/kdb+ client code in this service.

**ArcticDB:**

| Endpoint | Wraps | Purpose |
|---|---|---|
| `GET /arctic/libraries` | `arctic_list_libraries()` | Library picker options |
| `GET /arctic/symbols?library=` | `arctic_list_symbols(library)` | Symbol picker options, scoped to the chosen library |
| `GET /arctic/summary?library=&symbol=` | `Library.get_description(symbol)` directly (not `arctic_read` — see D6) | `{row_count, date_range: [start, end], columns, dtype}` |
| `GET /arctic/series?library=&symbol=&start=&end=&tail_rows=` | `arctic_read(...)` | The plottable/tabular data |

**kdb+:**

| Endpoint | Wraps | Purpose |
|---|---|---|
| `GET /kdb/tables` | `kdb_tables()` | Table picker options |
| `GET /kdb/schema?table=` | `kdb_table_schema(table)` | Column/type list |
| `GET /kdb/select?table=&symbol=&start_time=&end_time=&limit=` | `kdb_select(...)` | Filtered rows |

`/arctic/summary` is the one endpoint that doesn't call an existing
`mcp_stores` function verbatim — `mcp_stores` has no metadata-only tool
(`arctic_read` always returns rows). It reuses the same `_arctic()` /
`_bounded()` / `_scrub()` machinery `mcp_stores` already exports as plain
functions, calling `Library.get_description` directly rather than adding a
new tool to `mcp_stores` itself (which is scoped to MCP tools for an agent,
not REST endpoints for a widget — the two callers can share the backend
helpers without sharing a route surface).

Every endpoint validates its `library`/`symbol`/`table` argument against the
corresponding list call first, inheriting `mcp_stores`'s existing "unknown
X; call Y first" error contract — same 4xx-with-clear-message behavior a
caller of `arctic_read`/`kdb_select` already gets today via MCP.

### `widgets.json`

```json
{
  "arctic_explorer": {
    "name": "ArcticDB Explorer",
    "description": "Browse the shared ArcticDB store: pick a library, pick a symbol, see the stored series.",
    "category": "Data",
    "type": "table",
    "endpoint": "arctic/series",
    "dataKey": "rows",
    "gridData": { "w": 30, "h": 14 },
    "params": [
      { "paramName": "library", "type": "text", "label": "Library", "value": "", "optionsEndpoint": "arctic/libraries" },
      { "paramName": "symbol", "type": "text", "label": "Symbol", "value": "", "optionsEndpoint": "arctic/symbols", "optionsParams": { "library": "$library" } },
      { "paramName": "start", "type": "date", "label": "Start", "value": "", "show": false },
      { "paramName": "end", "type": "date", "label": "End", "value": "", "show": false }
    ],
    "source": ["ArcticDB"]
  },
  "kdb_explorer": {
    "name": "kdb+ Explorer",
    "description": "Browse the kdb+ tables the tape writes to: pick a table, see the filtered rows.",
    "category": "Data",
    "type": "table",
    "endpoint": "kdb/select",
    "dataKey": "rows",
    "gridData": { "w": 30, "h": 14 },
    "params": [
      { "paramName": "table", "type": "text", "label": "Table", "value": "", "optionsEndpoint": "kdb/tables" },
      { "paramName": "symbol", "type": "text", "label": "Symbol", "value": "", "show": false },
      { "paramName": "start_time", "type": "date", "label": "Start", "value": "", "show": false },
      { "paramName": "end_time", "type": "date", "label": "End", "value": "", "show": false }
    ],
    "source": ["kdb+"]
  }
}
```

Exact param defaults/visibility are a starting point, refinable at
implementation time against what the picker flow actually needs — the
contract that matters is `optionsEndpoint`/`optionsParams` driving the
cascading library→symbol and table pickers, matching how a chained param
already works elsewhere in this codebase's widgets.json convention.

### Deployment

New `stores-explorer/` directory: `Dockerfile`, `app/` (FastAPI app,
mirroring `key-maint`'s `create_app`-factory shape), `widgets.json`,
`tests/`. Build context is the **repo root** (like `live-grid`'s), so the
Dockerfile can `COPY mcp_stores/` in and `pip install` it before installing
`stores-explorer` itself.

New `docker-compose.yml` service, following the established internal-service
convention (`key-maint`/`live-grid`/`stores-mcp`):

```yaml
stores-explorer:
  build:
    context: .
    dockerfile: stores-explorer/Dockerfile
  image: openbb-stores-explorer:11.2.0
  container_name: openbb-stores-explorer
  restart: unless-stopped
  network_mode: service:tailscale
  depends_on:
    - tailscale
  environment:
    - KX_HOST=127.0.0.1
    - KX_PORT=5000
    - PYKX_UNLICENSED=1
    - PYKX_IGNORE_QHOME=1
  env_file:
    - path: ./minio.env
      required: true
```

No `ports:` mapping (Tailscale Serve is the only ingress, per D1); no
`kdb-license` mount (an IPC client to an already-running kdb+ server needs
no license of its own, matching `stores-mcp`'s posture exactly).

## Testing

Test-driven, mirroring `key-maint`'s `TestClient`-per-service pattern and
`mcp_stores`'s proven fake-injection approach (`arcticdb`/`pykx` mocked via
`sys.modules`, so the suite needs neither a real ArcticDB nor a real kdb+ to
run):

- Every endpoint: success shape, and the "unknown library/symbol/table"
  4xx-with-message path.
- `/arctic/symbols`/`/kdb/schema` etc. against an unknown parent (e.g. a
  library that doesn't exist) return the same clear error `mcp_stores`
  already gives an MCP caller, not a raw backend exception.
- `/arctic/summary` returns metadata only — assert the backend's row-read
  path is never invoked for this endpoint (a mock call-count assertion, not
  just checking the response shape).
- `widgets.json` contract: both widgets present, `optionsEndpoint`/
  `optionsParams` wiring exactly as declared above.
- No credential (S3 access/secret, `ARCTICDB_URI`) ever reaches a response
  body or error message — mirrors `mcp_stores`'s own `_scrub` tests.
- `scripts/scrub-check.sh` run clean over the new directory as part of the
  implementation plan's verification, not left to CI to discover first.

## Success criteria

1. `GET /widgets.json` on the running service returns both widgets with the
   contract above.
2. `GET /arctic/libraries` → `GET /arctic/symbols?library=` → `GET
   /arctic/summary?library=&symbol=` → `GET /arctic/series?...` walks the
   full picker flow against a real (or CI-mocked) store and returns
   sensible data at each step.
3. The kdb+ equivalent (`/kdb/tables` → `/kdb/schema?table=` →
   `/kdb/select?...`) does the same.
4. `scripts/scrub-check.sh` passes.
5. No new ArcticDB/kdb+ client code exists outside `mcp_stores` — every
   backend call in `stores-explorer/app/` traces to an imported function
   (or, for `/arctic/summary`, the same imported `_arctic()`/`_bounded()`
   helpers `mcp_stores` already exposes).

## Out of scope

- The bdobb frontend widget (Phase 3 of the episode plan).
- Any write endpoint.
- Changes to `mcp_stores` itself — this spec is purely additive on top of
  it.
- A combined arctic+kdb single widget (D4).

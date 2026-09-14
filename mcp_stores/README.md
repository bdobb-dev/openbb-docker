# mcp_stores

Read-only MCP server exposing ArcticDB and kdb+ discovery/query tools —
companion code for *Adventures in OpenBB, Ep. 11* (the shared store: MinIO,
ArcticDB, tick-lab). Runs beside `openbb-api` in the shared tailscale network
namespace, serving streamable-http MCP on `127.0.0.1:6902` (path `/mcp`).

## Why it exists

The REST `arcticdb`/`kdb` providers only answer known-symbol OHLCV queries
and return 204 against this store's raw quote/trade tick data. These tools
give an agent discovery (libraries/symbols/tables/schemas) plus capped raw
reads:

- `arctic_list_libraries` / `arctic_list_symbols` / `arctic_read`
- `kdb_tables` / `kdb_table_schema` / `kdb_select`

All tools are read-only. kdb+ access never interpolates user input into q
source — table/symbol names are validated and passed as typed IPC arguments
to two fixed q lambdas (see `server.py`'s module docstring for the full
threat model: injection, credential scrubbing, and per-call timeouts).

## Configuration

| Var | Meaning |
|---|---|
| `ARCTICDB_URI` | Full S3 connection URI (`s3s://host:bucket?port=...&access=...&secret=...`). The same fallback `openbb-arcticdb` itself reads — see `minio.env.example`. Never let this or a raw backend exception reach a caller; `_scrub` in `server.py` redacts it. |
| `KX_HOST` / `KX_PORT` | kdb+ IPC target. `KX_PORT` also accepts the combined `host:port` form. |
| `STORES_HOST` / `STORES_PORT` | Bind address, default `127.0.0.1:6902`. |
| `STORES_TIMEOUT_S` | Hard wall-clock timeout (seconds) per backend call, default 15. |
| `PYKX_UNLICENSED=1` | This process only ever connects to an *existing* q as an IPC client (`pykx.SyncQConnection`) — it never spawns or embeds q, so it needs no kdb+ licence. Without this, `import pykx` looks for one and fails. |

`ARCTICDB_LIBRARY` is deliberately **not** read here — every tool takes an
explicit `library`/`table` argument (discovered via `arctic_list_libraries`
/ `kdb_tables`), because cross-library reads are the point.

## Test

    pip install -e .[dev] && pytest    # no ArcticDB/kdb+/licence needed

`arcticdb` and `pykx` are injected as fakes via `sys.modules` before any tool
touches them (see `test_server.py`), so the suite runs on a machine with
neither installed.

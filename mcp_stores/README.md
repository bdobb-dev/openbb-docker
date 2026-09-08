<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# mcp_stores

Read-only MCP server exposing Delta Lake and kdb+ discovery/query tools —
companion code for *Adventures in OpenBB, Ep. 11* (the shared store: MinIO,
Delta Lake, tick-lab). Runs beside `openbb-api` in the shared tailscale network
namespace, serving streamable-http MCP on `127.0.0.1:6902` (path `/mcp`).

## Why it exists

The REST `deltalake`/`kdb` providers only answer known-symbol OHLCV queries
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
| `DELTA_S3_*` / `DELTA_URI` | The store's endpoint/bucket/credentials, or a path or `s3://` URL. Credentials reach delta-rs as `storage_options` and never enter a URI — see `minio.env.example`. A raw backend exception can still echo them; `_scrub` in `server.py` redacts both shapes. |
| `KX_HOST` / `KX_PORT` | kdb+ IPC target. `KX_PORT` also accepts the combined `host:port` form. |
| `STORES_HOST` / `STORES_PORT` | Bind address, default `127.0.0.1:6902`. |
| `STORES_TIMEOUT_S` | Hard wall-clock timeout (seconds) per backend call, default 15. |
| `PYKX_UNLICENSED=1` | This process only ever connects to an *existing* q as an IPC client (`pykx.SyncQConnection`) — it never spawns or embeds q, so it needs no kdb+ licence. Without this, `import pykx` looks for one and fails. |

`DELTA_LIBRARY` is deliberately **not** read here — every tool takes an
explicit `library`/`table` argument (discovered via `arctic_list_libraries`
/ `kdb_tables`), because cross-library reads are the point.

## Test

    pip install -e .[dev] && pytest    # no MinIO/kdb+/licence needed

`pykx` is injected as a fake via `sys.modules` before any tool
touches them (see `test_server.py`), so the suite runs on a machine with
neither installed.

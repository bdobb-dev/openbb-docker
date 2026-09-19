<!-- Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Security Master API

`security-master-api` is the Security Master Browser's service: the one
place that owns MinIO access, executes guarded DuckDB SQL over the Delta
Lake medallion (bronze/silver/gold plus an `ops` layer for jobs and
receipts), resolves identity and calendar queries under an explicit
temporal context, projects the ODP (Open Data Products) model registry, and
issues signed receipts recording exactly what a result depended on. Every
read path is provider-silent — it never calls EODHD or any other provider
itself. A companion process, `security_master_api.acquire` (run as the
`security-master-worker` compose service), is the only thing in this line
that ever reaches a provider, and it does so through `openbb-api` with the
stack's one Basic credential rather than a key of its own.

## Temporal contract

Every request that touches data carries a `context` naming one of five
modes (`security_master_api.resolver.context.MODES`):

| Mode | Meaning |
|---|---|
| `current_corrected` | the latest known state, as corrected today (the default) |
| `known_at` | what was known as of `known_at`, an explicit instant |
| `effective_on` | what was in force on the calendar date `effective_at` |
| `delta_snapshot` | a specific Delta table version per relation, from `versions` |
| `captured_by` | what a specific capture had recorded, as of `known_at` |

Underneath, every silver/gold row carries the same interval columns
(`security_master_api.store.schemas.INTERVAL_FIELDS`): `effective_from` /
`effective_to` bound when a fact was true in the world, `observed_at` /
`available_at` bound when it was known, `system_from` / `system_to` bound
when the row itself was live in the store, and `capture_id`,
`assertion_id`, `assertion_status`, `supersedes_assertion_id` identify and
supersede individual assertions. A temporal query is a filter over these
columns, never a mutation of history.

Every response that reads data returns a **receipt**
(`security_master_api.store.receipts.new_receipt`): the temporal mode and
context it was answered under, the Delta version of every relation it
depended on, the projection version, and a request id — enough to
reproduce the exact same answer later, or to explain why it changed.

## API

All routes are under `/security-master/v1` and guarded by the same Basic
auth as the rest of the stack (`api-auth.env`).

| Route | Purpose |
|---|---|
| `GET /health` | liveness and the running code version |
| `GET /catalog` | declared relations, active policies, ODP version, whether newer Delta data has landed |
| `GET /relations/{layer}/{name}` | one relation's schema, latest version and row count |
| `GET /relations/{layer}/{name}/versions` | that relation's retained Delta version history |
| `GET /odp/models` | the ODP model registry |
| `GET /odp/models/{model_id}` | one model plus its golden-fixture scenario, if any |
| `POST /odp/models/{model_id}/query` | project one ODP model's parameters into rows, with a receipt |
| `POST /preview` | ad hoc filter/sort/page over one relation, no raw SQL |
| `POST /sql/plan` | validate and explain a guarded `SELECT`, no execution |
| `POST /sql/execute` | run a guarded `SELECT` (budgeted, paginated) |
| `GET /sql/executions/{id}/pages` | the next page of a running or completed execution |
| `DELETE /sql/executions/{id}` | cancel an execution |
| `POST /resolve` | resolve an identifier to candidate securities under a temporal context, offering a bounded provider lookup for review if none match |
| `POST /lineage` | the assertion chain behind one relation's value under a snapshot |
| `POST /compare` | the same selection compared across two temporal contexts |
| `POST /versions/compare` | schema and row-count diff between two Delta versions of a relation |
| `POST /acquisitions/preflight` | price and sign a bounded provider lookup before anyone approves it |
| `POST /acquisitions` | create a job from a signed preflight token |
| `GET /acquisitions/{job_id}` | a job's current state |
| `POST /acquisitions/{job_id}/cancel` | request cancellation of a pending job |
| `GET /acquisitions/{job_id}/events` | server-sent events for one job's progress |
| `GET /exchanges` | the exchange/calendar reference table |
| `GET /widgets.json`, `GET /apps.json` | OpenBB Workspace widget and app declarations |

## Policies and budgets

Configured entirely from the environment (`security_master_api.config`);
`security-master.env.example` documents every variable:

| Variable | Default | Meaning |
|---|---|---|
| `SECURITY_MASTER_PREFLIGHT_SECRET` | *(required)* | signs preflight tokens; rotate to invalidate outstanding preflights |
| `SECURITY_MASTER_SQL` | `enabled` | `enabled` \| `review` \| `disabled` — gates `/sql/plan` and `/sql/execute` |
| `SECURITY_MASTER_ACQUISITION` | `review` | `enabled` \| `review` \| `disabled` — gates `/acquisitions` job creation |
| `SECURITY_MASTER_FIRST_PAGE` | `100` | rows in a query's first page |
| `SECURITY_MASTER_MAX_ROWS` | `10000` | ceiling on total rows a single execution may return |
| `SECURITY_MASTER_TIMEOUT_MS` | `15000` | per-query wall-clock budget |
| `SECURITY_MASTER_PER_PRINCIPAL_QUERIES` | `8` | concurrent executions allowed per caller |
| `SECURITY_MASTER_SCAN_SLOTS` | `4` | DuckDB's concurrent scan budget |
| `SECURITY_MASTER_MEMORY_LIMIT` | `1GB` | DuckDB's `memory_limit` |
| `SECURITY_MASTER_THREADS` | `2` | DuckDB's `threads` |
| `SECURITY_MASTER_WORKER_POLL_S` | `2` | seconds between the worker's poll-for-queued-jobs cycles |
| `SECURITY_MASTER_ROOT` | *(unset)* | a local directory instead of MinIO — tests and smoke runs only |

Storage otherwise comes from the shared `DELTA_S3_*` variables in
`minio.env`; `OPENBB_URL` points the worker at `openbb-api` for every
acquisition.

## Seeding

Populate the golden fixtures and reference tables into an otherwise empty
store:

```sh
docker compose run --rm security-master-api python -m security_master_api.store
```

## Development

From this directory:

```sh
uv run --extra dev pytest -q
uv run --extra dev ruff check .
```

## Service boundary

The renderer (OpenBB Workspace, or any other REST client) never sees MinIO
or EODHD directly — it only ever talks to this service's HTTP surface. The
worker never holds a provider key of its own; every acquisition it performs
goes through `openbb-api` with the stack's Basic credential, so entitlement
and rate limits stay in one place. SQL is cache-only: `/sql/plan` and
`/sql/execute` run against relations already landed in Delta Lake, never
against a live provider — a query that needs data the store doesn't have
gets `/resolve`'s bounded-lookup-for-review path instead, never a silent
fetch.

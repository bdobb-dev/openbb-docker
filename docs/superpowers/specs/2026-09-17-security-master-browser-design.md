<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Security Master Browser — design

Date: 2026-09-17. Source: *Security Master Browser Implementation
Specification* v1.2 (2026-09-16). This document records the decisions
that turn that specification into code across two repositories. The
same file is committed to both.

## Decisions taken with the user (2026-09-17)

- One plan covers the read-only service, the renderer, and acquisition.
- bdobb-v2 lands on `release/v11`; openbb-docker lands on
  `release/v11.2.x`, with PR #52's fixture package cherry-picked onto the
  line first. The reset map assigns the cut versions later.
- Data is real Delta on MinIO, seeded from the golden fixtures, and the
  existing EODHD Delta libraries are registered as Bronze relations.
- The service deploys on the NAS compose stack behind Tailscale Serve.
- ODP models reach OpenBB users through the service API and through an
  OpenBB router extension installed in the openbb-api image.
- Acquisition of prices, exchange calendars and shares outstanding goes
  through this stack's openbb-api; the worker holds no EODHD key.
- Durable job state and receipts are Delta tables read through DuckDB.
- Service core: append-only Silver assertion tables plus context-bound
  DuckDB views (rejected: DuckDB `delta_scan` against MinIO, and
  per-policy materialized Gold).

## 1. Repositories, branches, packages, deployment

openbb-docker, branch `claude/security-master-api` from `release/v11.2.x`:

- `security-master-api/` grows from the fixture package into the service.
  Package layout: `security_master_api/app` (FastAPI routes, auth, error
  envelope), `resolver` (context, catalog, manifest, Gold view SQL,
  identity, lineage, compare), `sql` (AST policy, query session,
  budgets, cursors), `store` (Delta paths, seed, receipts, jobs),
  `acquire` (preflight, worker, openbb-api client, normalizers),
  `odp_registry.json`. Runtime deps: `fastapi`, `uvicorn`, `deltalake`,
  `duckdb`, `pyarrow`, `pandas`, `requests`, `pandas_market_calendars`,
  and the sibling `openbb-deltalake` for storage options.
- One image, `security-master-api/Dockerfile`, build context the repo
  root (it installs `openbb-deltalake` first). Two compose services from
  it: `security-master-api` (uvicorn on 6905) and `security-master-worker`
  (`python -m security_master_api.acquire`). Both on `openbb-internal`,
  `env_file` `api-auth.env` and `minio.env` (both required),
  `depends_on` `tailscale` and `minio`, `restart: unless-stopped`,
  `command` overriding the fail-closed `127.0.0.1` bind.
- `openbb-security-master/`: an OpenBB router extension (entry point
  `openbb_core_extension`) installed into the openbb-api image after
  openbb-eodhd, followed by `openbb.build()`.
- Serve: `:6905` proxy plus `svc:openbb-security-master:443` in
  `ts-config/serve.json` and `serve-funnel.json` (the checker only
  requires `svc:openbb-api`, so both files gain the route the same way).
- Auth: the live-grid `BasicAuthMiddleware` pattern, reading
  `OPENBB_API_AUTH` / `OPENBB_API_USERNAME` / `OPENBB_API_PASSWORD`,
  failing closed when unset, accepting the credential as a query
  parameter for SSE. `/health` is the one exempt path.
- CI: two jobs in the standard shape (`timeout-minutes: 20`,
  `PYTHONFAULTHANDLER=1 timeout -s ABRT 600 pytest -q --capture=sys`),
  each preceded by `pip install -e ./openbb-deltalake`. Ruff runs locally
  as a plan step. The release workflow discovers the image from compose.
- Apache-2.0 headers on every new file; the cherry-picked fixture files
  get theirs in this branch.

bdobb-v2, branch `claude/security-master-browser-v11` from `release/v11`:

- Renderer type `security_master_browser`, hook, client, CSS, help topic.
- Gates are this line's `.github/workflows/ci.yml` (typecheck, unit,
  build with a clean-tree check, scrub, Chromium e2e, reference backend).
  No lint gate exists on the line and none is added.

Deployment: NAS compose directory (the live stack's project directory; docker is not on PATH there, use the container-station binary); images built
on the Mac for `linux/amd64` and shipped with `docker save | ssh nas
docker load`; each service recreated with `docker compose up -d
--no-deps`, after 4 PM ET; seeding runs once with `docker compose run
--rm security-master-api python -m security_master_api.store`. Vercel is
not touched.

## 2. Data architecture

Physical layout: one Delta table per logical relation at
`s3://<DELTA_S3_BUCKET>/security_master/<layer>__<relation>`, resolved
with `openbb_deltalake.utils.s3_options_from_env`; tests use a local
directory through the same code. Logical names are the contract.

Identity keys: `issuer_id`, `instrument_id`, `listing_id`,
`security_id`, `calendar_id`, `exchange_id`. Tickers, EODHD symbols,
CUSIP, ISIN, FIGI, CIK, MIC, pandas aliases and index membership are
dated rows, never keys.

Every Silver relation carries `effective_from`, `effective_to`
(half-open, null = still effective), `observed_at`, `available_at`,
`system_from`, `system_to` (half-open supersession), `capture_id`,
`assertion_id`, `assertion_status` (`current | superseded | rejected |
unresolved`) and `supersedes_assertion_id`. Silver is append-only: a
correction appends the new assertion and appends a close row that carries
the old `assertion_id` with `system_to` set and `assertion_status =
superseded`. The resolver reads the latest row per `assertion_id`.

Relations in this release:

| Layer | Relations |
|---|---|
| bronze | `source_captures`, `request_log`, `payload_rows`, `ingestion_runs` |
| silver | `issuers`, `instruments`, `securities`, `listings`, `identifiers`, `fundamental_facts`, `prices_normalized`, `corporate_actions`, `universe_membership`, `exchanges`, `calendar_authorities`, `calendar_rules`, `calendar_exceptions`, `market_sessions` |
| gold (views) | `security_master`, `price_daily`, `corporate_actions`, `universe_membership`, `market_calendar`, `odp_equity_info` |
| ops | `jobs`, `job_events`, `receipts` |

Existing EODHD Delta data: every table under the `openbb`, `ticks` and
`ticks_live` libraries is listed in the catalog as
`bronze.<library>.<symbol>` with capabilities `delta_snapshot` and
`captured_by` only. They are browsable and queryable and claim no
effective truth.

Calendar assertions hold `assertion_domain` (`holiday_date` |
`market_session`), `evidence_status`, `authority_type`, `authority_name`,
`authority_verified_at`, `source_kind`, `source_version`, `rule_id`,
`market_effect`, `calendar_system`, `holiday_family`, and the interval
columns. Interruptions are a child relation row set, never numbered
columns.

Seeding: `python -m security_master_api.seed [--root PATH]` writes the
seven golden fixtures (trade correction, shares outstanding, CUSIP,
ticker, Eid lunar correction, Lunar New Year special session, negative
control) plus the India and Dubai historical fixtures as Bronze captures
and Silver assertions with deterministic ids, and the two ops tables
empty. The JSON fixtures under `security_master_api/fixtures/` are the
canonical shape; the bdobb-v2 TypeScript fixture adopts it (stage
names `T0`/`T1`/`T2`, fixture ids, session labels).

## 3. Temporal resolver and receipts

```
Context(mode, effective_at, known_at, availability_policy="local_as_ingested",
        timezone="UTC", versions: {relation: delta_version} | None)
```

| mode | effective filter | knowledge filter |
|---|---|---|
| `current_corrected` | request-defined or none | `system_to IS NULL AND assertion_status = 'current'` |
| `known_at` | `effective_from <= effective_at < effective_to` | `available_at <= known_at AND (system_to IS NULL OR system_to > known_at)` |
| `effective_on` | same as above | latest corrected |
| `delta_snapshot` | none | none; versions are explicit |
| `captured_by` | none, Bronze only | `observed_at <= known_at` |

Boundaries are inclusive at the lower end, half-open at the upper end,
matching `state_at`. `local_as_ingested` is the only availability policy;
anything else is `TEMPORAL_MODE_UNSUPPORTED`. The catalog declares
supported modes per relation, and the resolver validates every relation
in a request before DuckDB is touched. It never substitutes a mode.

`gold.market_calendar` joins the winning `holiday_date` assertion and the
winning `market_session` assertion per `(calendar_id, session_date)` under
one context. A holiday row with no market row yields `market_effect =
'pending'`. A `market_session` row is never derived from a religious or
government assertion.

Receipt, written to `ops.receipts` before the response returns and never
mutated:

```
{execution_id, request_id, kind, temporal_mode, effective_at, known_at,
 availability_policy, projection_version, dependencies: [{relation,
 delta_version}], sql_fingerprint, created_at}
```

Re-executing a receipt reopens its manifest versions or fails with
`VERSION_NOT_RETAINED`.

`resolve` returns every candidate with a reason: `active`,
`historical_alias`, `successor`, `predecessor`, `ambiguous`. None is
`IDENTITY_UNRESOLVED`; several without a date is `IDENTITY_AMBIGUOUS`.

`lineage` walks Gold column → `assertion_id` → `capture_id` →
`request_log` → `ingestion_runs` and returns the ordered chain.

`compare` runs one selection under two contexts and classifies each
field difference as `corrected`, `newly_known`, `became_effective`,
`expired`, `superseded`, `identity_successor` or `unresolved`.

## 4. API and SQL sandbox

Routes under `/security-master/v1`: `catalog`,
`relations/{layer}/{relation}`, `relations/{layer}/{relation}/versions`,
`odp/models`, `odp/models/{id}`, `odp/models/{id}/query`, `preview`,
`resolve`, `lineage`, `compare`, `sql/plan`, `sql/execute`,
`sql/executions/{id}/pages`, `DELETE sql/executions/{id}`,
`versions/compare`, `acquisitions/preflight`, `acquisitions`,
`acquisitions/{id}`, `acquisitions/{id}/events` (SSE),
`acquisitions/{id}/cancel`, `exchanges`. Also `/widgets.json`,
`/apps.json`, `/health`.

Errors use the spec envelope `{error: {code, message, details,
request_id}}` with the fourteen domain codes; FastAPI validation errors
are re-wrapped as `QUERY_REJECTED`.

Query session per request: validate modes and resolve the manifest; open
`duckdb.connect(":memory:")` with `enable_external_access=false`,
`lock_configuration=true`, `memory_limit` and `threads` from env;
register only the manifest's relations as pyarrow datasets from
`DeltaTable(path, version=N)` under schemas `bronze`, `silver`, `gold`,
`ops`; create the Gold views; run the statement in a worker thread;
cancel and timeout call `interrupt()`; page results; close the
connection.

SQL policy on the AST from `json_serialize_sql`: exactly one statement;
root `SELECT_NODE` (CTEs allowed); every `BASE_TABLE` registered; every
function in the allowlist (arithmetic, comparison, string, date/time,
aggregate, window); no `TABLE_FUNCTION`; `EXPLAIN` only when policy
allows. `enable_external_access=false` is the second wall.

Budgets from env, defaults per the spec: first page 100, `max_rows`
10,000, timeout 15 s, two concurrent queries per principal, one
service-wide scan semaphore. Counts return `{value, quality}` with
`exact`, `estimated` (Delta add-action stats) or `unknown`. Cursors are
opaque tokens binding execution id, offset and a hash of context,
filters and sort; a mismatch is `QUERY_REJECTED`.

Authorization: one principal from `api-auth.env`; `SECURITY_MASTER_SQL`
and `SECURITY_MASTER_ACQUISITION` are each `enabled | review | disabled`
and are reported in the catalog.

## 5. OpenBB router extension

`openbb-security-master/` registers a router under `/api/v1/reference`:
`market_calendar` (the custom `MarketCalendar` model with the spec's
parameters and fields, `provider="local_security_master"`, calling the
service's ODP query and placing the receipt in
`OBBject.extra["security_master_receipt"]`), `exchange_details` (EODHD
`exchange-details/{code}` through openbb-eodhd's `rest_json`), and
`security_master_resolve`. Configuration: `SECURITY_MASTER_URL` and the
`api-auth.env` pair. The standard ODP models are not re-registered as a
provider; the registry pins `openbb==4.7.2` as the projected model
version and `odp_registry.json` is the single source for both packages.

## 6. Acquisition worker

Same image, `python -m security_master_api.worker`; configuration
`OPENBB_URL` plus the Basic-auth pair, like tick-lab. Preflight resolves
identities under the pinned context, computes missing ranges against
`silver.prices_normalized`, the expected request count and the applicable
calendar assertion, and returns an HMAC-signed token (15 minutes).
Creating a job requires an unexpired, unmodified token; the fingerprint
(dataset, identities, ranges, policy, code version) makes re-submission
idempotent.

Datasets: `price_daily` (`/api/v1/equity/price/historical`,
`provider=eodhd`; missing-only, refresh or force), `exchange_calendar`
(`/api/v1/reference/exchange_details`), `shares_outstanding`
(`/api/v1/equity/profile` and
`/api/v1/equity/ownership/share_statistics`, `provider=eodhd`). Metadata
read-through: `cache_only` returns the local state; `review` returns
`ACQUISITION_REVIEW_REQUIRED` with a preflight.

Stages `queued → fetching → bronze_retained → normalizing → validating →
silver_ready → materializing → gold_ready`; terminal `partial`,
`rate_limited`, `entitlement_denied`, `validation_failed`,
`cancel_pending`, `cancelled`, `failed`. Each transition is one append to
`ops.job_events`; `ops.jobs` holds the summary. The worker polls every 2
s through DuckDB and claims a job with an append whose conditional put
fails if another worker won. HTTP 429 and 403 from openbb-api map to
`rate_limited` and `entitlement_denied` with the response retained.
Bronze is written before normalization and survives validation failure.
Completion never rewrites a pinned result; later catalog calls report
`newer_data_available`.

## 7. bdobb-v2 renderer

- `src/lib/securityMaster.ts`: request helpers over `fetchEndpointJson`
  and a new `postEndpointJson` in `dataClient.ts`, plus all-or-nothing
  hand parsers (`parseCatalog`, `parseOdpModel`, `parsePage`,
  `parseReceipt`, `parseJob`, `parseError`). Malformed bodies become a
  fixed-sentence error.
- `src/hooks/useSecurityMaster.ts`: per-card session state and
  per-execution state; a context change aborts or lets the in-flight
  request finish under its frozen context; responses apply only when
  their request id is still current.
- `src/components/renderers/SecurityMasterBrowserRenderer.tsx`: the
  prototype layout — catalog pane (ODP Models | Tables), context bar,
  workbench tabs (Contract, Fields, Mapping, Consumers, Temporal example;
  Data, Schema, SQL, Temporal compare, Versions, Lineage), inspector
  (model, record, scenario), acquisition review and download activity
  dialogs. Data tab reuses `TableRenderer` with server paging. The SQL
  editor is a textarea.
- `securityMasterTemporal.ts` keeps the Temporal example view-model,
  re-typed to the canonical fixture shape.
- `WidgetCard.tsx`: `case "security_master_browser"` beside
  `delta_explorer`, a `TYPE_LABELS` entry. Params `listing_id`,
  `effective_date`, `known_at`, `layer`, `temporal_mode` are declared so
  groups can bind them; execution and job state never publish.
- Persist per card only the spec's list; results are never saved.
- CSS in `styles.css` on existing tokens; a landscape-tablet breakpoint
  stacks the inspector under the workbench.
- Help topic `src/help/topics/security-master.md` in `TOPIC_ORDER`.

## 8. Testing

- Service unit: interval boundaries, mode filters, the calendar domain
  rule, resolver ambiguity, receipt canonicalization, AST policy with
  adversarial statements, fingerprint idempotency, job transitions. All
  seven golden fixtures asserted immediately before, at and after every
  cutoff.
- Service integration: seeded local Delta through the FastAPI test
  client for every route; cancellation of a slow query; worker restart
  with a stub openbb-api covering 429, 403, malformed payload and a
  validation failure that keeps Bronze.
- Contract: the service tests record responses to `tests/contract/*.json`;
  bdobb-v2 tests its parsers against copies, with a checked-in hash.
- Renderer: vitest through `startMockServer` for the whole card path,
  malformed-payload and stale-response tests; one Playwright spec via
  `browser-fixtures.mjs` covering ODP Models first, each model's
  scenario, the two-stage pending state, the negative control, no
  relabel on context change, no provider traffic.

## Out of scope

Direct MinIO mode, local DuckDB attachments, table administration,
automatic historical acquisition from SQL, unauthenticated access,
acquisition from MCP without preflight, a read-only MinIO credential
(the stack has one root credential; read-only-ness stays in code),
performance load testing and fault injection (hardening phase), the
vendored help guide, and Vercel.

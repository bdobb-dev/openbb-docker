<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Security Master Browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the Security Master Browser: a `security-master-api` service and worker in openbb-docker (Delta on MinIO, DuckDB, temporal resolver, guarded SQL, receipts, acquisition jobs), an OpenBB router extension, and a `security_master_browser` renderer in bdobb-v2.

**Architecture:** Silver relations are append-only Delta tables of assertions with effective and knowledge intervals. Every request opens pinned Delta versions as Arrow datasets in a fresh isolated DuckDB connection, creates Gold as context-bound views, and records an immutable receipt. The renderer talks only to the service over the app's existing transport; the worker acquires through this stack's openbb-api and never holds a provider key.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, deltalake (delta-rs), duckdb, pyarrow, pandas_market_calendars, requests; React 19 + TypeScript, vitest, Playwright; Docker Compose, Tailscale Serve.

**Spec:** `docs/superpowers/specs/2026-09-17-security-master-browser-design.md` (identical copy in both repositories).

## Global Constraints

- Two working trees. **Part A** (Tasks 1–14) runs in the openbb-docker worktree on branch `claude/security-master-api` (cut from `release/v11.2.x`, PR #52 cherry-picked); commands run from `security-master-api/` unless a task says otherwise. **Part B** (Tasks 15–23) runs in the bdobb-v2 worktree on branch `claude/security-master-browser-v11` (cut from `release/v11`).
- Every new file opens with the Apache-2.0 header in that file's comment syntax: `# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.` then `# SPDX-License-Identifier: Apache-2.0` (`//` in TypeScript, `<!-- -->` in Markdown, `#` in YAML/Dockerfile/shell; JSON files carry none).
- Python: `requires-python = ">=3.12"`, ruff `target-version = "py312"`, `line-length = 100`, `ruff==0.15.22` in the `dev` extra. Run tests with `uv run --extra dev pytest -q` and lint with `uv run --extra dev ruff check .`.
- The renderer never receives MinIO, EODHD or physical paths. The worker never receives an EODHD key.
- Browsing, preview, SQL, lineage, compare and versions never call openbb-api or EODHD.
- Silver is append-only. Nothing rewrites a row; a correction appends.
- Every mode failure is a domain error; the service never substitutes `current_corrected`.
- TypeScript parsers accept literal `true`/`false` only and never coerce; a malformed body yields the fixed sentence `"The security-master service answered with a malformed payload."`.
- bdobb-v2 `release/v11` has no lint gate; gates are `pnpm typecheck`, `pnpm test:run`, `pnpm build`, `bash scripts/scrub-check.sh`, `pnpm e2e`, `pnpm test:reference` (needs the reference backend).
- Commits: one per task step that says "Commit", message in the repo's `type(scope): summary` style, ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Do not touch Vercel, the NAS, or any release tag inside this plan. Deployment is Task 23's documented procedure, executed only on the user's word.

## File structure

openbb-docker (`security-master-api/`):

```
pyproject.toml                       deps, extras, pytest/ruff config
Dockerfile                           one image for API and worker
README.md                            service README (replaces the fixture one)
widgets.json / apps.json             the bdobb widget entry and an example dashboard
security_master_api/
  __init__.py
  errors.py                          DomainError + the code table
  config.py                          Settings, settings_from_env
  temporal_fixture.py                (existing) fixture loader
  store/paths.py                     relation_path, storage for a Settings
  store/schemas.py                   pyarrow schema per relation
  store/tables.py                    ensure, append, open_table, latest_version, history
  store/receipts.py                  new_receipt, record_receipt
  store/jobs.py                      create_job, append_event, claim, job, events, queued
  store/seed.py + __main__           seed the fixtures into Delta
  fixtures/*.json                    seven golden fixtures + India + Dubai
  resolver/context.py                Context, parse_context
  resolver/catalog.py                Relation registry + external discovery
  resolver/manifest.py               resolve_manifest
  resolver/views.py                  assertion filters and Gold view SQL
  resolver/identity.py               resolve
  resolver/lineage.py                lineage
  resolver/compare.py                compare
  resolver/odp.py                    registry + project
  resolver/calendar.py               pandas_market_calendars adapter
  odp_registry.json
  sql/policy.py                      validate_sql over the DuckDB AST
  sql/session.py                     QuerySession
  sql/executions.py                  Executions registry, cursors
  acquire/preflight.py               preflight, sign, verify
  acquire/client.py                  OpenbbClient
  acquire/normalize.py               dataset normalizers + validators
  acquire/worker.py + __main__       run_once, main
  app/auth.py                        BasicAuthMiddleware (live-grid pattern)
  app/errors.py                      error envelope handlers
  app/main.py                        create_app, routes
tests/                               one test file per module, tests/contract/*.json
```

openbb-docker (`openbb-security-master/`): `pyproject.toml`, `openbb_security_master/{__init__,router,client,models}.py`, `tests/`.

bdobb-v2:

```
src/lib/securityMaster.ts (+ .test.ts)          types, parsers, client
src/lib/dataClient.ts                           + postEndpointJson, deleteEndpoint
src/hooks/useSecurityMaster.ts (+ .test.ts)     session + execution state
src/components/renderers/SecurityMasterBrowserRenderer.tsx (+ .test.tsx)
src/components/renderers/securityMaster/{CatalogPane,ContextBar,ModelWorkbench,RelationWorkbench,Inspector,AcquisitionDialogs}.tsx
src/lib/securityMasterTemporal.ts, src/test/fixtures/securityMasterTemporal.ts  re-typed
src/test/contract/security-master/*.json        copies of the service's recorded responses
src/components/WidgetCard.tsx, src/lib/widgetTypes.ts, src/styles.css, src/help/topics/security-master.md, src/help/index.ts
scripts/browser-fixtures.mjs, e2e/security-master.spec.ts
```

---

## Part A — openbb-docker

### Task 1: Package scaffold, settings, errors

**Files:**
- Modify: `security-master-api/pyproject.toml`
- Create: `security-master-api/security_master_api/errors.py`, `security-master-api/security_master_api/config.py`
- Modify (headers only): `security-master-api/security_master_api/__init__.py`, `security_master_api/temporal_fixture.py`, `tests/test_temporal_calendar_fixtures.py`, `README.md`
- Test: `security-master-api/tests/test_config.py`, `security-master-api/tests/test_errors.py`

**Interfaces:**
- Produces: `DomainError(code: str, message: str, details: dict | None = None)` with `.status` from `STATUS_BY_CODE`; `Settings` dataclass; `settings_from_env(env: Mapping[str, str] | None = None) -> Settings`.

- [ ] **Step 1: Add the header lines to the four cherry-picked files**

Prepend to each `.py` file (before the module docstring):

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
```

and to `README.md` the two-line `<!-- -->` form. Add an empty `tests/__init__.py` so later tests can import helpers from `tests.test_app_routes` (the sibling packages all have one).

- [ ] **Step 2: Replace `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "security-master-api"
version = "0.2.0"
description = "Security-master browser service: temporal catalog, guarded DuckDB SQL over Delta, receipts, acquisition jobs"
requires-python = ">=3.12"
license = { text = "Apache-2.0" }
dependencies = [
  "fastapi>=0.115,<1",
  "uvicorn[standard]>=0.30,<1",
  "deltalake>=1.0",
  "duckdb>=1.1,<2",
  "pyarrow>=16",
  "pandas>=2.2",
  "pandas_market_calendars>=5.0",
  "requests>=2.32",
  "openbb-deltalake",
]

[project.optional-dependencies]
dev = [
  "pytest",
  "httpx",
  "ruff==0.15.22",
]

[tool.setuptools.packages.find]
include = ["security_master_api*"]

[tool.setuptools.package-data]
security_master_api = ["fixtures/*.json", "odp_registry.json"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.uv.sources]
openbb-deltalake = { path = "../openbb-deltalake", editable = true }
```

Then run `uv lock` from `security-master-api/` and confirm `uv.lock` now lists `duckdb`, `deltalake`, `fastapi`.

- [ ] **Step 3: Write the failing tests**

`tests/test_errors.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
from security_master_api.errors import CODES, DomainError


def test_every_spec_code_has_a_status():
    expected = {
        "NO_LOCAL_EVIDENCE", "NO_MATCHING_FACTS", "IDENTITY_AMBIGUOUS", "IDENTITY_UNRESOLVED",
        "TEMPORAL_MODE_UNSUPPORTED", "VERSION_NOT_RETAINED", "MATERIALIZATION_NOT_BUILT",
        "QUERY_REJECTED", "QUERY_BUDGET_EXCEEDED", "QUERY_CANCELLED",
        "ACQUISITION_REVIEW_REQUIRED", "PROVIDER_RATE_LIMITED", "PROVIDER_ENTITLEMENT_DENIED",
        "PROMOTION_VALIDATION_FAILED",
    }
    assert set(CODES) == expected


def test_domain_error_carries_status_and_envelope():
    err = DomainError("TEMPORAL_MODE_UNSUPPORTED", "known_at is not supported", {"relation": "x"})
    assert err.status == 422
    assert err.envelope("req_1") == {
        "error": {
            "code": "TEMPORAL_MODE_UNSUPPORTED",
            "message": "known_at is not supported",
            "details": {"relation": "x"},
            "request_id": "req_1",
        }
    }


def test_unknown_code_is_refused():
    try:
        DomainError("NOT_A_CODE", "x")
    except ValueError as exc:
        assert "NOT_A_CODE" in str(exc)
    else:
        raise AssertionError("unknown code accepted")
```

`tests/test_config.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import settings_from_env


def test_local_root_wins_and_defaults_apply(tmp_path):
    s = settings_from_env({"SECURITY_MASTER_ROOT": str(tmp_path), "SECURITY_MASTER_PREFLIGHT_SECRET": "k"})
    assert s.root == str(tmp_path)
    assert s.storage_options == {}
    assert s.delta_base is None
    assert (s.first_page, s.max_rows, s.timeout_ms) == (100, 10_000, 15_000)
    assert (s.per_principal_queries, s.scan_slots) == (2, 4)
    assert (s.sql_policy, s.acquisition_policy) == ("enabled", "review")
    assert s.openbb_url == "http://openbb-api:6900"


def test_s3_root_is_derived_from_delta_env():
    env = {
        "DELTA_S3_ENDPOINT": "minio.example", "DELTA_S3_BUCKET": "openbb",
        "DELTA_S3_ACCESS": "a", "DELTA_S3_SECRET": "b", "DELTA_S3_PORT": "9000",
        "DELTA_S3_SECURE": "false", "SECURITY_MASTER_PREFLIGHT_SECRET": "k",
    }
    s = settings_from_env(env)
    assert s.root == "s3://openbb/security_master"
    assert s.delta_base == "s3://openbb"
    assert s.storage_options["aws_endpoint"] == "http://minio.example:9000"
    assert s.storage_options["aws_conditional_put"] == "etag"


def test_missing_store_config_is_an_error():
    with pytest.raises(ValueError, match="SECURITY_MASTER_ROOT"):
        settings_from_env({"SECURITY_MASTER_PREFLIGHT_SECRET": "k"})


def test_policy_values_are_validated():
    with pytest.raises(ValueError, match="SECURITY_MASTER_SQL"):
        settings_from_env({"SECURITY_MASTER_ROOT": "/x", "SECURITY_MASTER_SQL": "maybe",
                           "SECURITY_MASTER_PREFLIGHT_SECRET": "k"})


def test_preflight_secret_is_required():
    with pytest.raises(ValueError, match="SECURITY_MASTER_PREFLIGHT_SECRET"):
        settings_from_env({"SECURITY_MASTER_ROOT": "/x"})
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run --extra dev pytest -q tests/test_errors.py tests/test_config.py`
Expected: FAIL with `ModuleNotFoundError: security_master_api.errors`.

- [ ] **Step 5: Write `errors.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Domain errors and the standard error envelope."""

from __future__ import annotations

STATUS_BY_CODE: dict[str, int] = {
    "NO_LOCAL_EVIDENCE": 404,
    "NO_MATCHING_FACTS": 404,
    "IDENTITY_AMBIGUOUS": 409,
    "IDENTITY_UNRESOLVED": 404,
    "TEMPORAL_MODE_UNSUPPORTED": 422,
    "VERSION_NOT_RETAINED": 410,
    "MATERIALIZATION_NOT_BUILT": 409,
    "QUERY_REJECTED": 422,
    "QUERY_BUDGET_EXCEEDED": 429,
    "QUERY_CANCELLED": 499,
    "ACQUISITION_REVIEW_REQUIRED": 409,
    "PROVIDER_RATE_LIMITED": 429,
    "PROVIDER_ENTITLEMENT_DENIED": 403,
    "PROMOTION_VALIDATION_FAILED": 422,
}
CODES = tuple(STATUS_BY_CODE)


class DomainError(Exception):
    """A structured, client-facing failure. Every code maps to one HTTP status."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        if code not in STATUS_BY_CODE:
            raise ValueError(f"unknown domain error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.status = STATUS_BY_CODE[code]

    def envelope(self, request_id: str) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "request_id": request_id,
            }
        }
```

- [ ] **Step 6: Write `config.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Service settings from the environment. Nothing here reads a file."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from openbb_deltalake.utils import s3_options_from_env

_POLICIES = ("enabled", "review", "disabled")


@dataclass(frozen=True)
class Settings:
    root: str
    storage_options: dict[str, str] = field(default_factory=dict)
    delta_base: str | None = None
    external_libraries: tuple[str, ...] = ("openbb", "ticks", "ticks_live")
    first_page: int = 100
    max_rows: int = 10_000
    timeout_ms: int = 15_000
    per_principal_queries: int = 2
    scan_slots: int = 4
    memory_limit: str = "1GB"
    threads: int = 2
    sql_policy: str = "enabled"
    acquisition_policy: str = "review"
    openbb_url: str = "http://openbb-api:6900"
    openbb_username: str = ""
    openbb_password: str = ""
    preflight_secret: str = ""
    preflight_ttl_s: int = 900
    code_version: str = "security-master-api/0.2.0"
    projection_version: str = "security-master-projection/1"


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = str(env.get(key, "")).strip()
    return int(raw) if raw else default


def _policy(env: Mapping[str, str], key: str, default: str) -> str:
    raw = str(env.get(key, "")).strip().lower() or default
    if raw not in _POLICIES:
        raise ValueError(f"{key} must be one of {_POLICIES}, got {raw!r}")
    return raw


def settings_from_env(env: Mapping[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    secret = str(e.get("SECURITY_MASTER_PREFLIGHT_SECRET", "")).strip()
    if not secret:
        raise ValueError("SECURITY_MASTER_PREFLIGHT_SECRET must be set")
    local = str(e.get("SECURITY_MASTER_ROOT", "")).strip()
    if local:
        root, options, base = local, {}, None
    else:
        s3 = s3_options_from_env(e)
        if s3 is None:
            raise ValueError("set SECURITY_MASTER_ROOT (local) or the DELTA_S3_* variables")
        base, options = s3
        root = f"{base}/security_master"
    return Settings(
        root=root,
        storage_options=dict(options),
        delta_base=base,
        first_page=_int(e, "SECURITY_MASTER_FIRST_PAGE", 100),
        max_rows=_int(e, "SECURITY_MASTER_MAX_ROWS", 10_000),
        timeout_ms=_int(e, "SECURITY_MASTER_TIMEOUT_MS", 15_000),
        per_principal_queries=_int(e, "SECURITY_MASTER_PER_PRINCIPAL_QUERIES", 2),
        scan_slots=_int(e, "SECURITY_MASTER_SCAN_SLOTS", 4),
        memory_limit=str(e.get("SECURITY_MASTER_MEMORY_LIMIT", "1GB")).strip() or "1GB",
        threads=_int(e, "SECURITY_MASTER_THREADS", 2),
        sql_policy=_policy(e, "SECURITY_MASTER_SQL", "enabled"),
        acquisition_policy=_policy(e, "SECURITY_MASTER_ACQUISITION", "review"),
        openbb_url=str(e.get("OPENBB_URL", "http://openbb-api:6900")).strip().rstrip("/"),
        openbb_username=str(e.get("OPENBB_API_USERNAME", "")).strip(),
        openbb_password=str(e.get("OPENBB_API_PASSWORD", "")).strip(),
        preflight_secret=secret,
    )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check .`
Expected: all pass (the 19 fixture tests plus the new eight), ruff clean.

- [ ] **Step 8: Commit**

```bash
git add security-master-api
git commit -m "feat(security-master): settings, domain errors and the service pyproject"
```

### Task 2: Delta store — schemas and tables

**Files:**
- Create: `security_master_api/store/__init__.py`, `store/paths.py`, `store/schemas.py`, `store/tables.py`
- Test: `tests/test_store_tables.py`

**Interfaces:**
- Produces: `RELATIONS: dict[str, pa.Schema]` keyed `"layer.name"`; `INTERVAL_FIELDS: list[pa.Field]`; `relation_path(settings, relation) -> str`; `ensure(settings, relation) -> None`; `append(settings, relation, rows: list[dict]) -> int` (new version); `open_table(settings, relation, version: int | None = None) -> DeltaTable`; `latest_version(settings, relation) -> int | None`; `history(settings, relation) -> list[dict]` (`[{version, timestamp}]`, newest first); `dataset(settings, relation, version) -> pyarrow.dataset.Dataset`.

- [ ] **Step 1: Write the failing test**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.store.schemas import INTERVAL_FIELDS, RELATIONS
from security_master_api.store.tables import (
    append, dataset, ensure, history, latest_version, open_table, relation_path,
)


@pytest.fixture
def settings(tmp_path):
    return Settings(root=str(tmp_path), preflight_secret="k")


def test_every_silver_relation_carries_the_interval_columns():
    names = {f.name for f in INTERVAL_FIELDS}
    assert names == {
        "effective_from", "effective_to", "observed_at", "available_at", "system_from",
        "system_to", "capture_id", "assertion_id", "assertion_status", "supersedes_assertion_id",
    }
    for key, schema in RELATIONS.items():
        if key.startswith("silver."):
            assert names <= set(schema.names), key


def test_relation_path_is_layer_double_underscore_name(settings):
    assert relation_path(settings, "silver.listings") == f"{settings.root}/silver__listings"


def test_ensure_then_append_versions_and_reads_back(settings):
    assert latest_version(settings, "silver.issuers") is None
    ensure(settings, "silver.issuers")
    assert latest_version(settings, "silver.issuers") == 0
    v = append(settings, "silver.issuers", [{
        "issuer_id": "iss_1", "name": "Example", "effective_from": "2020-01-01T00:00:00Z",
        "effective_to": None, "observed_at": "2020-01-02T00:00:00Z",
        "available_at": "2020-01-02T00:00:00Z", "system_from": "2020-01-02T00:00:00Z",
        "system_to": None, "capture_id": "cap_1", "assertion_id": "as_1",
        "assertion_status": "current", "supersedes_assertion_id": None,
    }])
    assert v == 1
    table = open_table(settings, "silver.issuers").to_pyarrow_table()
    assert table.num_rows == 1
    assert str(table.schema.field("effective_from").type) == "timestamp[us, tz=UTC]"
    assert open_table(settings, "silver.issuers", version=0).to_pyarrow_table().num_rows == 0
    assert [h["version"] for h in history(settings, "silver.issuers")] == [1, 0]
    assert dataset(settings, "silver.issuers", 1).count_rows() == 1


def test_missing_version_is_version_not_retained(settings):
    ensure(settings, "silver.issuers")
    with pytest.raises(DomainError) as exc:
        open_table(settings, "silver.issuers", version=7)
    assert exc.value.code == "VERSION_NOT_RETAINED"


def test_unknown_relation_is_rejected(settings):
    with pytest.raises(DomainError) as exc:
        ensure(settings, "silver.nope")
    assert exc.value.code == "QUERY_REJECTED"


def test_append_rejects_unknown_columns(settings):
    ensure(settings, "silver.issuers")
    with pytest.raises(ValueError, match="unknown column"):
        append(settings, "silver.issuers", [{"issuer_id": "x", "bogus": 1}])
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --extra dev pytest -q tests/test_store_tables.py`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write `store/schemas.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""One pyarrow schema per logical relation. The keys are the API's relation names."""

from __future__ import annotations

import pyarrow as pa

TS = pa.timestamp("us", tz="UTC")

INTERVAL_FIELDS = [
    pa.field("effective_from", TS),
    pa.field("effective_to", TS),
    pa.field("observed_at", TS),
    pa.field("available_at", TS),
    pa.field("system_from", TS),
    pa.field("system_to", TS),
    pa.field("capture_id", pa.string()),
    pa.field("assertion_id", pa.string()),
    pa.field("assertion_status", pa.string()),
    pa.field("supersedes_assertion_id", pa.string()),
]


def _silver(*fields: pa.Field) -> pa.Schema:
    return pa.schema([*fields, *INTERVAL_FIELDS])


S = pa.string
RELATIONS: dict[str, pa.Schema] = {
    "bronze.source_captures": pa.schema([
        pa.field("capture_id", S()), pa.field("provider", S()), pa.field("endpoint", S()),
        pa.field("request_fingerprint", S()), pa.field("captured_at", TS),
        pa.field("content_hash", S()), pa.field("status", pa.int32()),
        pa.field("payload", S()), pa.field("job_id", S()),
    ]),
    "bronze.request_log": pa.schema([
        pa.field("request_id", S()), pa.field("job_id", S()), pa.field("capture_id", S()),
        pa.field("requested_at", TS), pa.field("response_status", pa.int32()),
        pa.field("attempt", pa.int32()), pa.field("retry_after_s", pa.int32()),
        pa.field("error", S()),
    ]),
    "bronze.payload_rows": pa.schema([
        pa.field("capture_id", S()), pa.field("ordinal", pa.int32()),
        pa.field("source_key", S()), pa.field("payload", S()),
    ]),
    "bronze.ingestion_runs": pa.schema([
        pa.field("run_id", S()), pa.field("job_id", S()), pa.field("code_version", S()),
        pa.field("started_at", TS), pa.field("ended_at", TS), pa.field("outcome", S()),
    ]),
    "silver.issuers": _silver(pa.field("issuer_id", S()), pa.field("name", S())),
    "silver.instruments": _silver(
        pa.field("instrument_id", S()), pa.field("issuer_id", S()), pa.field("name", S()),
        pa.field("instrument_type", S()),
    ),
    "silver.securities": _silver(
        pa.field("security_id", S()), pa.field("instrument_id", S()), pa.field("issuer_id", S()),
        pa.field("cusip", S()), pa.field("predecessor_security_id", S()),
        pa.field("successor_security_id", S()), pa.field("reorganization_kind", S()),
    ),
    "silver.listings": _silver(
        pa.field("listing_id", S()), pa.field("instrument_id", S()), pa.field("issuer_id", S()),
        pa.field("security_id", S()), pa.field("exchange_id", S()), pa.field("mic", S()),
        pa.field("symbol", S()), pa.field("provider_symbol", S()), pa.field("currency", S()),
        pa.field("status", S()), pa.field("name", S()), pa.field("instrument_type", S()),
    ),
    "silver.identifiers": _silver(
        pa.field("identifier_type", S()), pa.field("identifier", S()),
        pa.field("listing_id", S()), pa.field("instrument_id", S()),
        pa.field("security_id", S()), pa.field("issuer_id", S()),
    ),
    "silver.fundamental_facts": _silver(
        pa.field("issuer_id", S()), pa.field("fact", S()), pa.field("period_end", pa.date32()),
        pa.field("value", pa.float64()), pa.field("unit", S()), pa.field("filing_kind", S()),
    ),
    "silver.prices_normalized": _silver(
        pa.field("listing_id", S()), pa.field("market_date", pa.date32()),
        pa.field("open", pa.float64()), pa.field("high", pa.float64()),
        pa.field("low", pa.float64()), pa.field("close", pa.float64()),
        pa.field("volume", pa.float64()), pa.field("price_basis", S()),
    ),
    "silver.corporate_actions": _silver(
        pa.field("listing_id", S()), pa.field("action_type", S()),
        pa.field("ex_date", pa.date32()), pa.field("ratio", pa.float64()),
        pa.field("amount", pa.float64()), pa.field("currency", S()),
    ),
    "silver.universe_membership": _silver(
        pa.field("universe_id", S()), pa.field("listing_id", S()),
    ),
    "silver.exchanges": _silver(
        pa.field("exchange_id", S()), pa.field("calendar_id", S()), pa.field("mic", S()),
        pa.field("name", S()), pa.field("timezone", S()), pa.field("calendar_alias", S()),
        pa.field("eodhd_code", S()),
    ),
    "silver.calendar_authorities": _silver(
        pa.field("authority_id", S()), pa.field("calendar_id", S()),
        pa.field("authority_type", S()), pa.field("authority_name", S()),
        pa.field("jurisdiction", S()), pa.field("holiday_family", S()),
    ),
    "silver.calendar_rules": _silver(
        pa.field("rule_id", S()), pa.field("calendar_id", S()), pa.field("calendar_alias", S()),
        pa.field("adapter", S()), pa.field("adapter_version", S()), pa.field("rule_kind", S()),
    ),
    "silver.calendar_exceptions": _silver(
        pa.field("calendar_id", S()), pa.field("session_date", pa.date32()),
        pa.field("assertion_domain", S()), pa.field("holiday_family", S()),
        pa.field("calendar_system", S()), pa.field("evidence_status", S()),
        pa.field("authority_type", S()), pa.field("authority_name", S()),
        pa.field("authority_verified_at", TS), pa.field("source_kind", S()),
        pa.field("source_version", S()), pa.field("rule_id", S()),
        pa.field("market_effect", S()), pa.field("holiday_name", S()),
        pa.field("special_open", pa.bool_()), pa.field("special_close", pa.bool_()),
        pa.field("market_open", TS), pa.field("market_close", TS),
        pa.field("candidate_branches", S()), pa.field("selected_branch", S()),
        pa.field("expiry_date", pa.date32()), pa.field("settlement_status", S()),
    ),
    "silver.market_sessions": _silver(
        pa.field("calendar_id", S()), pa.field("session_date", pa.date32()),
        pa.field("trade_date", pa.date32()), pa.field("market_open", TS),
        pa.field("market_close", TS), pa.field("break_start", TS), pa.field("break_end", TS),
        pa.field("rule_id", S()), pa.field("source_version", S()),
    ),
    "silver.session_interruptions": _silver(
        pa.field("calendar_id", S()), pa.field("session_date", pa.date32()),
        pa.field("interruption_start", TS), pa.field("interruption_end", TS),
    ),
    "ops.jobs": pa.schema([
        pa.field("job_id", S()), pa.field("kind", S()), pa.field("fingerprint", S()),
        pa.field("state", S()), pa.field("created_at", TS), pa.field("updated_at", TS),
        pa.field("request", S()), pa.field("summary", S()), pa.field("preflight_token_hash", S()),
        pa.field("seq", pa.int32()),
    ]),
    "ops.job_events": pa.schema([
        pa.field("event_id", S()), pa.field("job_id", S()), pa.field("seq", pa.int32()),
        pa.field("stage", S()), pa.field("at", TS), pa.field("detail", S()),
        pa.field("worker_id", S()),
    ]),
    "ops.receipts": pa.schema([
        pa.field("execution_id", S()), pa.field("request_id", S()), pa.field("kind", S()),
        pa.field("temporal_mode", S()), pa.field("effective_at", TS), pa.field("known_at", TS),
        pa.field("availability_policy", S()), pa.field("projection_version", S()),
        pa.field("dependencies", S()), pa.field("sql_fingerprint", S()),
        pa.field("created_at", TS),
    ]),
}
```

- [ ] **Step 4: Write `store/paths.py` and `store/tables.py`**

`store/__init__.py` is the header plus a one-line docstring.

`store/paths.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Where a logical relation lives. Physical paths never leave this module's callers."""

from __future__ import annotations

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.store.schemas import RELATIONS


def split(relation: str) -> tuple[str, str]:
    layer, _, name = relation.partition(".")
    if relation not in RELATIONS:
        raise DomainError("QUERY_REJECTED", f"unknown relation: {relation}", {"relation": relation})
    return layer, name


def relation_path(settings: Settings, relation: str) -> str:
    layer, name = split(relation)
    return f"{settings.root}/{layer}__{name}"
```

`store/tables.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Append-only Delta tables, one per relation, opened at exact versions."""

from __future__ import annotations

from datetime import date, datetime

import pyarrow as pa
import pyarrow.dataset as pads
from deltalake import DeltaTable, write_deltalake
from deltalake.exceptions import TableNotFoundError

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.store.paths import relation_path
from security_master_api.store.schemas import RELATIONS
from security_master_api.temporal_fixture import _instant


def _coerce(rows: list[dict], schema: pa.Schema) -> pa.Table:
    names = set(schema.names)
    out: list[dict] = []
    for row in rows:
        unknown = set(row) - names
        if unknown:
            raise ValueError(f"unknown column(s) for relation: {sorted(unknown)}")
        clean: dict = {}
        for field in schema:
            value = row.get(field.name)
            if value is None:
                clean[field.name] = None
            elif pa.types.is_timestamp(field.type):
                clean[field.name] = _instant(value) if isinstance(value, str) else value
            elif pa.types.is_date(field.type):
                clean[field.name] = date.fromisoformat(value) if isinstance(value, str) else value
            else:
                clean[field.name] = value
        out.append(clean)
    return pa.Table.from_pylist(out, schema=schema)


def _open(settings: Settings, relation: str, version: int | None) -> DeltaTable:
    path = relation_path(settings, relation)
    try:
        table = DeltaTable(path, storage_options=settings.storage_options or None)
    except TableNotFoundError as exc:
        raise DomainError("NO_LOCAL_EVIDENCE", f"{relation} has no local table",
                          {"relation": relation}) from exc
    if version is not None:
        if version < 0 or version > table.version():
            raise DomainError("VERSION_NOT_RETAINED",
                              f"{relation} has no retained version {version}",
                              {"relation": relation, "latest": table.version()})
        table.load_as_version(version)
    return table


def latest_version(settings: Settings, relation: str) -> int | None:
    try:
        return _open(settings, relation, None).version()
    except DomainError:
        return None


def ensure(settings: Settings, relation: str) -> None:
    schema = RELATIONS.get(relation)
    if schema is None:
        raise DomainError("QUERY_REJECTED", f"unknown relation: {relation}", {"relation": relation})
    if latest_version(settings, relation) is None:
        write_deltalake(relation_path(settings, relation), schema.empty_table(), mode="error",
                        storage_options=settings.storage_options or None)


def append(settings: Settings, relation: str, rows: list[dict]) -> int:
    schema = RELATIONS[relation]
    ensure(settings, relation)
    write_deltalake(relation_path(settings, relation), _coerce(rows, schema), mode="append",
                    storage_options=settings.storage_options or None)
    return _open(settings, relation, None).version()


def open_table(settings: Settings, relation: str, version: int | None = None) -> DeltaTable:
    return _open(settings, relation, version)


def dataset(settings: Settings, relation: str, version: int | None = None) -> pads.Dataset:
    return _open(settings, relation, version).to_pyarrow_dataset()


def history(settings: Settings, relation: str) -> list[dict]:
    out = []
    for entry in _open(settings, relation, None).history():
        ts = entry.get("timestamp")
        if isinstance(ts, int):
            ts = datetime.fromtimestamp(ts / 1000, tz=None).isoformat(timespec="seconds") + "Z"
        out.append({"version": int(entry["version"]), "timestamp": str(ts)})
    out.sort(key=lambda h: h["version"], reverse=True)
    return out
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q tests/test_store_tables.py && uv run --extra dev ruff check .`
Expected: PASS. If `deltalake` refuses `mode="error"` on a fresh directory, use `mode="overwrite"` in `ensure` only when `latest_version` is `None` (it already is), and keep the test.

- [ ] **Step 6: Commit**

```bash
git add security-master-api
git commit -m "feat(security-master): relation schemas and append-only Delta tables"
```

### Task 3: Golden fixtures and the seed command

**Files:**
- Create: `security_master_api/fixtures/trade_correction.json`, `shares_outstanding.json`, `cusip_change.json`, `ticker_change.json`, `eid_lunar_correction.json`, `lunar_new_year_special_session.json`, `religious_observance_no_closure.json`; move `tests/fixtures/india_bakri_eid_2023.json` and `dubai_eid_al_fitr_2024.json` to `security_master_api/fixtures/` (update the existing test's `FIXTURES` path to `Path(security_master_api.__file__).parent / "fixtures"`)
- Create: `security_master_api/store/seed.py`, `security_master_api/store/__main__.py`
- Test: `tests/test_seed.py`

**Interfaces:**
- Produces: `seed(settings) -> dict[str, int]` (relation → version after seeding); `python -m security_master_api.store --root PATH`.
- Golden fixture schema `security-master.golden-fixture.v1`: `{"schema", "fixture_id", "scenario", "captures": [bronze.source_captures rows], "assertions": {"<relation>": [rows]}, "expectations": [{"relation"|"model", "context": {...}, "where": {...}, "expect": {...} | "error": CODE}]}`. `expectations` are consumed by Task 6's tests, not by `seed`.

- [ ] **Step 1: Write the seven golden fixtures**

Every timestamp is UTC ISO with `Z`; ids are stable. Common capture rows use `provider: "fixture"`, `endpoint: "seed"`, `status: 200`, `payload: "{}"`, `content_hash` = `sha256` of the payload text.

`fixtures/trade_correction.json`:

```json
{
  "schema": "security-master.golden-fixture.v1",
  "fixture_id": "trade-correction",
  "scenario": "trade_correction",
  "captures": [
    {"capture_id": "cap_00421", "provider": "fixture", "endpoint": "seed", "request_fingerprint": "fx-tc-1", "captured_at": "2026-09-14T20:05:00Z", "content_hash": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a", "status": 200, "payload": "{}", "job_id": null},
    {"capture_id": "cap_00489", "provider": "fixture", "endpoint": "seed", "request_fingerprint": "fx-tc-2", "captured_at": "2026-09-15T09:20:00Z", "content_hash": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a", "status": 200, "payload": "{}", "job_id": null}
  ],
  "assertions": {
    "silver.issuers": [
      {"issuer_id": "iss_apple", "name": "Apple Inc.", "effective_from": "1980-12-12T00:00:00Z", "effective_to": null, "observed_at": "2026-09-14T20:05:00Z", "available_at": "2026-09-14T20:05:00Z", "system_from": "2026-09-14T20:05:00Z", "system_to": null, "capture_id": "cap_00421", "assertion_id": "as_iss_apple_1", "assertion_status": "current", "supersedes_assertion_id": null}
    ],
    "silver.instruments": [
      {"instrument_id": "ins_apple", "issuer_id": "iss_apple", "name": "Apple Inc. Common Stock", "instrument_type": "Common Stock", "effective_from": "1980-12-12T00:00:00Z", "effective_to": null, "observed_at": "2026-09-14T20:05:00Z", "available_at": "2026-09-14T20:05:00Z", "system_from": "2026-09-14T20:05:00Z", "system_to": null, "capture_id": "cap_00421", "assertion_id": "as_ins_apple_1", "assertion_status": "current", "supersedes_assertion_id": null}
    ],
    "silver.listings": [
      {"listing_id": "lst_apple", "instrument_id": "ins_apple", "issuer_id": "iss_apple", "security_id": "sec_apple", "exchange_id": "exch_xnas", "mic": "XNAS", "symbol": "AAPL", "provider_symbol": "AAPL.US", "currency": "USD", "status": "active", "name": "Apple Inc.", "instrument_type": "Common Stock", "effective_from": "1980-12-12T00:00:00Z", "effective_to": null, "observed_at": "2026-09-14T20:05:00Z", "available_at": "2026-09-14T20:05:00Z", "system_from": "2026-09-14T20:05:00Z", "system_to": null, "capture_id": "cap_00421", "assertion_id": "as_lst_apple_1", "assertion_status": "current", "supersedes_assertion_id": null}
    ],
    "silver.identifiers": [
      {"identifier_type": "ticker", "identifier": "AAPL", "listing_id": "lst_apple", "instrument_id": "ins_apple", "security_id": "sec_apple", "issuer_id": "iss_apple", "effective_from": "1980-12-12T00:00:00Z", "effective_to": null, "observed_at": "2026-09-14T20:05:00Z", "available_at": "2026-09-14T20:05:00Z", "system_from": "2026-09-14T20:05:00Z", "system_to": null, "capture_id": "cap_00421", "assertion_id": "as_id_aapl_1", "assertion_status": "current", "supersedes_assertion_id": null}
    ],
    "silver.prices_normalized": [
      {"listing_id": "lst_apple", "market_date": "2026-09-14", "open": 230.10, "high": 232.00, "low": 229.80, "close": 231.40, "volume": 51200000, "price_basis": "raw", "effective_from": "2026-09-14T00:00:00Z", "effective_to": null, "observed_at": "2026-09-14T20:05:00Z", "available_at": "2026-09-14T20:05:00Z", "system_from": "2026-09-14T20:05:00Z", "system_to": null, "capture_id": "cap_00421", "assertion_id": "as_px_apple_20260914_1", "assertion_status": "current", "supersedes_assertion_id": null},
      {"listing_id": "lst_apple", "market_date": "2026-09-14", "open": 230.10, "high": 232.00, "low": 229.80, "close": 231.40, "volume": 51200000, "price_basis": "raw", "effective_from": "2026-09-14T00:00:00Z", "effective_to": null, "observed_at": "2026-09-14T20:05:00Z", "available_at": "2026-09-14T20:05:00Z", "system_from": "2026-09-15T09:20:00Z", "system_to": "2026-09-15T09:20:00Z", "capture_id": "cap_00421", "assertion_id": "as_px_apple_20260914_1", "assertion_status": "superseded", "supersedes_assertion_id": null},
      {"listing_id": "lst_apple", "market_date": "2026-09-14", "open": 230.10, "high": 232.00, "low": 229.80, "close": 231.74, "volume": 51230000, "price_basis": "raw", "effective_from": "2026-09-14T00:00:00Z", "effective_to": null, "observed_at": "2026-09-15T09:20:00Z", "available_at": "2026-09-15T09:20:00Z", "system_from": "2026-09-15T09:20:00Z", "system_to": null, "capture_id": "cap_00489", "assertion_id": "as_px_apple_20260914_2", "assertion_status": "current", "supersedes_assertion_id": "as_px_apple_20260914_1"}
    ]
  },
  "expectations": [
    {"model": "EquityHistorical", "params": {"symbol": "AAPL", "start_date": "2026-09-14", "end_date": "2026-09-14"}, "context": {"mode": "known_at", "effective_at": "2026-09-14T20:00:00Z", "known_at": "2026-09-14T20:05:00Z"}, "expect": {"close": 231.40, "assertion_id": "as_px_apple_20260914_1", "capture_id": "cap_00421"}},
    {"model": "EquityHistorical", "params": {"symbol": "AAPL", "start_date": "2026-09-14", "end_date": "2026-09-14"}, "context": {"mode": "known_at", "effective_at": "2026-09-14T20:00:00Z", "known_at": "2026-09-15T09:19:59.999999Z"}, "expect": {"close": 231.40}},
    {"model": "EquityHistorical", "params": {"symbol": "AAPL", "start_date": "2026-09-14", "end_date": "2026-09-14"}, "context": {"mode": "known_at", "effective_at": "2026-09-14T20:00:00Z", "known_at": "2026-09-15T09:20:00Z"}, "expect": {"close": 231.74, "assertion_id": "as_px_apple_20260914_2", "capture_id": "cap_00489"}},
    {"model": "EquityHistorical", "params": {"symbol": "AAPL", "start_date": "2026-09-14", "end_date": "2026-09-14"}, "context": {"mode": "current_corrected"}, "expect": {"close": 231.74}},
    {"model": "EquityHistorical", "params": {"symbol": "AAPL", "start_date": "2026-09-14", "end_date": "2026-09-14"}, "context": {"mode": "known_at", "effective_at": "2026-09-14T20:00:00Z", "known_at": "2026-09-14T20:04:59Z"}, "error": "NO_LOCAL_EVIDENCE"}
  ]
}
```

`fixtures/shares_outstanding.json` (issuer `iss_example`, listing `lst_example` symbol `EXMP`, capture `cap_so_1` at `2026-08-05T12:00:00Z`, `cap_so_2` at `2026-08-20T12:00:00Z`): two `silver.fundamental_facts` current rows plus one close row, `fact: "shares_outstanding"`, `period_end: "2026-06-30"`, values `1.50e9` (`assertion_id: as_so_1`, `available_at: 2026-08-05T12:00:00Z`, `filing_kind: "10-Q"`) and `1.48e9` (`as_so_2`, `available_at: 2026-08-20T12:00:00Z`, `filing_kind: "10-Q/A"`, `supersedes_assertion_id: as_so_1`), the close row for `as_so_1` with `system_from = system_to = 2026-08-20T12:00:00Z`; issuer/instrument/listing/identifier rows like the trade fixture. Expectations on model `EquityInfo` params `{"symbol": "EXMP"}`: known_at `2026-07-15T00:00:00Z` → `error: NO_LOCAL_EVIDENCE`; known_at `2026-08-05T12:00:00Z` → `{"shares_outstanding": 1500000000}`; `2026-08-19T23:59:59Z` → `1500000000`; `2026-08-20T12:00:00Z` → `{"shares_outstanding": 1480000000, "filing_kind": "10-Q/A"}`; `current_corrected` → `1480000000`; every expectation also `"period_end": "2026-06-30"`.

`fixtures/cusip_change.json` (issuer `iss_042`; `silver.securities` rows `sec_100` cusip `000111AA1` `effective_from 2015-01-01T00:00:00Z` `effective_to 2026-07-01T00:00:00Z` `successor_security_id sec_101`; `sec_101` cusip `000222BB2` `effective_from 2026-07-01T00:00:00Z` `predecessor_security_id sec_100` `reorganization_kind "reorganization"`; `silver.identifiers` rows `identifier_type "cusip"` for both with the same intervals; capture `cap_cusip_1` at `2026-06-20T00:00:00Z`). Expectations on `resolve`: `{"identifier": "000111AA1", "context": {"mode": "effective_on", "effective_at": "2026-06-30T00:00:00Z"}}` → `{"security_id": "sec_100", "reason": "active"}`; `000222BB2` on `2026-07-02` → `sec_101 active`; `000111AA1` `current_corrected` with no date → `{"security_id": "sec_100", "reason": "historical_alias", "successor_security_id": "sec_101"}`; lineage of `sec_100` includes `cap_cusip_1`.

`fixtures/ticker_change.json` (listing `lst_000042`, instrument `ins_000042`, issuer `iss_meta`; `silver.identifiers` `ticker FB` `effective_from 2012-05-18T00:00:00Z` `effective_to 2022-06-09T00:00:00Z`; `ticker META` `effective_from 2022-06-09T00:00:00Z`; `silver.listings` two rows for `lst_000042`, `symbol FB` then `META` with the same intervals; `silver.prices_normalized` closes on `2022-06-07` (`184.10`) and `2022-06-10` (`175.57`) both on `lst_000042`; capture `cap_meta_1` at `2022-06-09T00:00:00Z`). Expectations on `resolve`: `FB` effective `2022-06-07` → `{"listing_id": "lst_000042", "instrument_id": "ins_000042", "reason": "active", "symbol": "FB"}`; `META` effective `2022-06-10` → same ids, `symbol META`; `FB` `current_corrected` → `reason historical_alias`, `symbol META`; expectation on model `EquityHistorical` params `{"symbol": "META", "start_date": "2022-06-01", "end_date": "2022-06-30"}` `current_corrected` → `{"row_count": 2}`.

`fixtures/eid_lunar_correction.json` (calendar `cal_tadawul`, exchange `exch_xsau` mic `XSAU` timezone `Asia/Riyadh` alias `XSAU`... use `calendar_alias: null`; authority row `auth_ksa_moon` type `religious_authority` name `Supreme Court of Saudi Arabia` family `eid_al_fitr`; `silver.calendar_exceptions` rows: T0 `as_eid_t0` domain `holiday_date` date `2027-03-10` `evidence_status provisional` `authority_type calendar_package` `source_kind pandas_rule` `available_at 2027-01-05T00:00:00Z` `market_effect null`; T1 `as_eid_t1` domain `holiday_date` date `2027-03-11` `authority_confirmed` `religious_authority` `authority_verified_at 2027-03-09T18:30:00Z` `available_at 2027-03-09T18:45:00Z` `supersedes as_eid_t0`, plus the close row of `as_eid_t0` at `2027-03-09T18:45:00Z`; T2 `as_eid_t2` domain `market_session` date `2027-03-11` `market_final` `exchange` `market_effect closed` `available_at 2027-03-10T08:00:00Z`; captures `cap_eid_t1`, `cap_eid_t2`). Expectations on model `MarketCalendar` params `{"calendar_id": "cal_tadawul", "start_date": "2027-03-01", "end_date": "2027-03-31", "include_closed": true}`: known_at `2027-03-09T18:44:59Z` → row `2027-03-10` `{"evidence_status": "provisional", "market_effect": "pending", "holiday_family": "eid_al_fitr"}`; known_at `2027-03-09T18:45:00Z` → row `2027-03-11` `{"evidence_status": "authority_confirmed", "market_effect": "pending", "authority_name": "Supreme Court of Saudi Arabia"}` and no `2027-03-10` holiday row; known_at `2027-03-10T08:00:00Z` → row `2027-03-11` `{"evidence_status": "market_final", "market_effect": "closed"}`; `current_corrected` → same as T2.

`fixtures/lunar_new_year_special_session.json` (calendar `cal_xhkg`; T0 `as_lny_t0` domain `market_session` date `2027-02-05` `estimated` `calendar_package` `market_effect none` `available_at 2027-01-05T00:00:00Z`; T1 `as_lny_t1` same date `market_final` `exchange` `market_effect early_close` `special_close true` `available_at 2027-01-20T09:00:00Z` supersedes T0 (plus T0's close row); T1 rows `as_lny_t1_d1..d3` for `2027-02-06`..`2027-02-08` `market_effect closed`). Expectations on `MarketCalendar` `cal_xhkg` February 2027: known_at `2027-01-20T08:59:59Z` → `2027-02-05` `{"market_effect": "none"}` and no rows for `02-06..02-08` with `closed`; known_at `2027-01-20T09:00:00Z` → `2027-02-05` `early_close`, `2027-02-06` `closed`.

`fixtures/religious_observance_no_closure.json` (calendar `cal_xtae`; T0 `as_rel_t0` domain `holiday_date` date `2027-05-27` `authority_confirmed` `government` `available_at 2027-05-01T00:00:00Z`; T1 `as_rel_t1` domain `market_session` same date `market_final` `exchange` `market_effect none` `available_at 2027-05-02T00:00:00Z`). Expectations: known_at `2027-05-01T12:00:00Z` → `2027-05-27` `{"evidence_status": "authority_confirmed", "market_effect": "pending"}`; known_at `2027-05-02T00:00:00Z` → `{"market_effect": "none"}`.

The India and Dubai files keep their existing `security-master.temporal-fixture.v1` shape and gain an `"assertions"` block in the same style (India: T0 holiday `2023-06-28` `exchange` `market_final` closed available `2022-12-08T18:00:00Z`; T1 holiday `2023-06-29` `government` `authority_confirmed` available `2023-06-26T12:00:00Z` superseding T0; T2 market rows `2023-06-28 open`, `2023-06-29 closed` `expiry_date 2023-06-28` `settlement_status revised` available `2023-06-27T12:00:00Z`. Dubai: T0 market row `2024-04-08` closed `provisional` with `candidate_branches` JSON text `[{"condition":"eid_first_day=2024-04-09","reopen_date":"2024-04-12"},{"condition":"eid_first_day=2024-04-10","reopen_date":"2024-04-15"}]` available `2024-04-04T12:00:00Z`; T1 holiday `2024-04-10` `religious_authority` `authority_confirmed` available `2024-04-08T20:00:00Z`; T2 market rows `2024-04-12 closed`, `2024-04-15 open` with `selected_branch` JSON text available `2024-04-08T20:00:05Z`). `load_temporal_fixture` must keep validating them: extend `_TOP_LEVEL_FIELDS` with nothing and let `assertions` pass through as an extra key.

- [ ] **Step 2: Write the failing seed test**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import json
import subprocess
import sys
from pathlib import Path

import pytest

import security_master_api
from security_master_api.config import Settings
from security_master_api.store.seed import FIXTURE_DIR, golden_fixtures, seed
from security_master_api.store.tables import latest_version, open_table


@pytest.fixture
def settings(tmp_path):
    return Settings(root=str(tmp_path), preflight_secret="k")


def test_fixture_dir_is_packaged():
    assert FIXTURE_DIR == Path(security_master_api.__file__).parent / "fixtures"
    names = sorted(p.name for p in FIXTURE_DIR.glob("*.json"))
    assert names == [
        "cusip_change.json", "dubai_eid_al_fitr_2024.json", "eid_lunar_correction.json",
        "india_bakri_eid_2023.json", "lunar_new_year_special_session.json",
        "religious_observance_no_closure.json", "shares_outstanding.json",
        "ticker_change.json", "trade_correction.json",
    ]


def test_golden_fixtures_validate():
    fixtures = golden_fixtures()
    assert {f["fixture_id"] for f in fixtures} == {
        "trade-correction", "shares-outstanding", "cusip-change", "ticker-change",
        "eid-lunar-correction", "lunar-new-year-special-session",
        "religious-observance-no-closure", "india-bakri-eid-2023", "dubai-eid-al-fitr-2024",
    }
    for f in fixtures:
        for relation, rows in f["assertions"].items():
            assert relation.startswith("silver."), relation
            for row in rows:
                assert row["assertion_id"], relation


def test_seed_writes_every_relation_once_and_is_idempotent(settings):
    versions = seed(settings)
    assert versions["silver.prices_normalized"] >= 1
    assert versions["ops.jobs"] == 0
    prices = open_table(settings, "silver.prices_normalized").to_pyarrow_table().to_pylist()
    assert {r["assertion_id"] for r in prices} >= {"as_px_apple_20260914_1", "as_px_apple_20260914_2"}
    again = seed(settings)
    assert again == versions
    assert latest_version(settings, "silver.prices_normalized") == versions["silver.prices_normalized"]


def test_seed_command(tmp_path):
    out = subprocess.run(
        [sys.executable, "-m", "security_master_api.store", "--root", str(tmp_path)],
        capture_output=True, text=True, check=True,
        env={"SECURITY_MASTER_PREFLIGHT_SECRET": "k", "PATH": "", "PYTHONPATH": str(Path.cwd())},
    )
    report = json.loads(out.stdout)
    assert report["silver.listings"] >= 1
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run --extra dev pytest -q tests/test_seed.py`
Expected: FAIL with `ModuleNotFoundError: security_master_api.store.seed`.

- [ ] **Step 4: Write `store/seed.py` and `store/__main__.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Seed the golden and historical fixtures into Delta. Idempotent by assertion_id."""

from __future__ import annotations

import json
from pathlib import Path

import security_master_api
from security_master_api.config import Settings
from security_master_api.store.schemas import RELATIONS
from security_master_api.store.tables import append, ensure, latest_version, open_table
from security_master_api.temporal_fixture import load_temporal_fixture

FIXTURE_DIR = Path(security_master_api.__file__).parent / "fixtures"
GOLDEN_SCHEMA = "security-master.golden-fixture.v1"


def golden_fixtures() -> list[dict]:
    out = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema") == GOLDEN_SCHEMA:
            for key in ("fixture_id", "scenario", "captures", "assertions", "expectations"):
                if key not in raw:
                    raise ValueError(f"{path.name} missing {key}")
        else:
            raw = load_temporal_fixture(path)
            raw.setdefault("captures", [])
            raw.setdefault("assertions", {})
            raw.setdefault("expectations", [])
        for relation in raw["assertions"]:
            if relation not in RELATIONS:
                raise ValueError(f"{path.name} names unknown relation {relation}")
        out.append(raw)
    return out


def _existing_ids(settings: Settings, relation: str, column: str) -> set[str]:
    if latest_version(settings, relation) is None:
        return set()
    table = open_table(settings, relation).to_pyarrow_table(columns=[column])
    return {v for v in table.column(column).to_pylist() if v is not None}


def seed(settings: Settings) -> dict[str, int]:
    for relation in RELATIONS:
        ensure(settings, relation)
    captures = _existing_ids(settings, "bronze.source_captures", "capture_id")
    for fixture in golden_fixtures():
        new_caps = [c for c in fixture["captures"] if c["capture_id"] not in captures]
        if new_caps:
            append(settings, "bronze.source_captures", new_caps)
            captures |= {c["capture_id"] for c in new_caps}
        for relation, rows in fixture["assertions"].items():
            seen = _existing_ids(settings, relation, "assertion_id")
            # A close row shares its assertion_id with the row it closes, so
            # idempotency keys on (assertion_id, system_from).
            existing = set()
            if latest_version(settings, relation) is not None and seen:
                table = open_table(settings, relation).to_pyarrow_table(
                    columns=["assertion_id", "system_from"])
                existing = {(a, s.isoformat()) for a, s in zip(
                    table.column("assertion_id").to_pylist(),
                    table.column("system_from").to_pylist(), strict=True)}
            fresh = [r for r in rows if (r["assertion_id"], _iso(r["system_from"])) not in existing]
            if fresh:
                append(settings, relation, fresh)
    return {relation: latest_version(settings, relation) or 0 for relation in RELATIONS}


def _iso(value: str) -> str:
    from security_master_api.temporal_fixture import _instant

    return _instant(value).isoformat()
```

`store/__main__.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""`python -m security_master_api.store [--root PATH]` seeds the fixtures and prints versions."""

from __future__ import annotations

import argparse
import json
import os

from security_master_api.config import settings_from_env
from security_master_api.store.seed import seed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", help="local directory instead of the DELTA_S3_* store")
    args = parser.parse_args()
    env = dict(os.environ)
    if args.root:
        env["SECURITY_MASTER_ROOT"] = args.root
    print(json.dumps(seed(settings_from_env(env)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check .`
Expected: PASS, including the moved fixture tests.

- [ ] **Step 6: Commit**

```bash
git add security-master-api
git commit -m "feat(security-master): the seven golden fixtures and the seed command"
```

### Task 4: Context, catalog and manifest

**Files:**
- Create: `security_master_api/resolver/__init__.py`, `resolver/context.py`, `resolver/catalog.py`, `resolver/manifest.py`
- Test: `tests/test_context.py`, `tests/test_catalog.py`

**Interfaces:**
- Produces: `MODES`; `Context` (frozen dataclass: `mode: str`, `effective_at: datetime | None`, `known_at: datetime | None`, `availability_policy: str`, `timezone: str`, `versions: dict[str, int] | None`); `parse_context(raw: dict | None) -> Context`; `Relation` (frozen dataclass: `layer, name, description, kind, modes: tuple[str, ...], keys: tuple[str, ...], depends_on: tuple[str, ...]`, property `full` = `"layer.name"`); `catalog(settings) -> list[Relation]`; `relation(settings, full: str) -> Relation`; `check_modes(settings, ctx, relations: Iterable[str]) -> None`; `resolve_manifest(settings, ctx, relations: Iterable[str]) -> dict[str, int]`; `expand(settings, relations) -> set[str]` (adds Gold view dependencies).

- [ ] **Step 1: Write the failing tests**

`tests/test_context.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime

import pytest

from security_master_api.errors import DomainError
from security_master_api.resolver.context import MODES, parse_context


def test_modes_are_the_spec_five():
    assert MODES == ("current_corrected", "known_at", "effective_on", "delta_snapshot", "captured_by")


def test_default_context_is_current_corrected_utc():
    ctx = parse_context(None)
    assert (ctx.mode, ctx.availability_policy, ctx.timezone) == ("current_corrected", "local_as_ingested", "UTC")
    assert ctx.effective_at is None and ctx.known_at is None


def test_known_at_requires_known_at_and_parses_z():
    ctx = parse_context({"mode": "known_at", "known_at": "2026-09-14T20:00:00Z",
                         "effective_at": "2026-09-14T00:00:00Z"})
    assert ctx.known_at == datetime(2026, 9, 14, 20, tzinfo=UTC)
    with pytest.raises(DomainError) as exc:
        parse_context({"mode": "known_at"})
    assert exc.value.code == "QUERY_REJECTED"


def test_effective_on_requires_effective_at():
    with pytest.raises(DomainError):
        parse_context({"mode": "effective_on"})


def test_unknown_mode_and_policy_are_unsupported():
    for raw in ({"mode": "latest"}, {"mode": "known_at", "known_at": "2026-01-01T00:00:00Z",
                                     "availability_policy": "archival"}):
        with pytest.raises(DomainError) as exc:
            parse_context(raw)
        assert exc.value.code == "TEMPORAL_MODE_UNSUPPORTED"


def test_naive_timestamp_is_rejected():
    with pytest.raises(DomainError):
        parse_context({"mode": "known_at", "known_at": "2026-01-01T00:00:00"})


def test_delta_snapshot_carries_versions():
    ctx = parse_context({"mode": "delta_snapshot", "versions": {"silver.listings": 3}})
    assert ctx.versions == {"silver.listings": 3}
```

`tests/test_catalog.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.catalog import catalog, check_modes, expand, relation
from security_master_api.resolver.context import parse_context
from security_master_api.resolver.manifest import resolve_manifest
from security_master_api.store.seed import seed


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return s


def test_catalog_lists_every_declared_relation_with_capabilities(settings):
    rels = {r.full: r for r in catalog(settings)}
    assert rels["silver.listings"].modes == ("current_corrected", "known_at", "effective_on", "delta_snapshot")
    assert rels["bronze.source_captures"].modes == ("captured_by", "delta_snapshot")
    assert rels["gold.market_calendar"].kind == "view"
    assert set(rels["gold.market_calendar"].depends_on) == {
        "silver.market_sessions", "silver.calendar_exceptions", "silver.exchanges",
        "silver.session_interruptions",
    }
    assert rels["ops.receipts"].modes == ("delta_snapshot",)


def test_external_libraries_appear_as_bronze_snapshots(tmp_path):
    base = tmp_path / "bucket"
    (base / "openbb" / "AAPL" / "_delta_log").mkdir(parents=True)
    (base / "openbb" / "notatable").mkdir(parents=True)
    s = Settings(root=str(base / "security_master"), delta_base=str(base), preflight_secret="k")
    seed(s)
    rels = {r.full: r for r in catalog(s)}
    assert rels["bronze.openbb.AAPL"].kind == "external_delta"
    assert rels["bronze.openbb.AAPL"].modes == ("delta_snapshot", "captured_by")
    assert "bronze.openbb.notatable" not in rels


def test_unknown_relation_is_a_domain_error(settings):
    with pytest.raises(DomainError) as exc:
        relation(settings, "gold.nope")
    assert exc.value.code == "QUERY_REJECTED"


def test_check_modes_refuses_without_substitution(settings):
    ctx = parse_context({"mode": "known_at", "known_at": "2026-01-01T00:00:00Z"})
    with pytest.raises(DomainError) as exc:
        check_modes(settings, ctx, ["silver.listings", "bronze.source_captures"])
    assert exc.value.code == "TEMPORAL_MODE_UNSUPPORTED"
    assert exc.value.details["relation"] == "bronze.source_captures"
    assert "supported_modes" in exc.value.details


def test_expand_adds_gold_dependencies(settings):
    assert "silver.calendar_exceptions" in expand(settings, ["gold.market_calendar"])


def test_manifest_pins_latest_or_requested_versions(settings):
    ctx = parse_context(None)
    manifest = resolve_manifest(settings, ctx, ["gold.security_master"])
    assert set(manifest) >= {"silver.listings", "silver.identifiers"}
    assert all(isinstance(v, int) for v in manifest.values())
    snap = parse_context({"mode": "delta_snapshot", "versions": {"silver.listings": 0}})
    assert resolve_manifest(settings, snap, ["silver.listings"]) == {"silver.listings": 0}
    bad = parse_context({"mode": "delta_snapshot", "versions": {"silver.listings": 99}})
    with pytest.raises(DomainError) as exc:
        resolve_manifest(settings, bad, ["silver.listings"])
    assert exc.value.code == "VERSION_NOT_RETAINED"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --extra dev pytest -q tests/test_context.py tests/test_catalog.py`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write `resolver/context.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The immutable temporal context every request is bound to."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from security_master_api.errors import DomainError
from security_master_api.temporal_fixture import _instant

MODES = ("current_corrected", "known_at", "effective_on", "delta_snapshot", "captured_by")
POLICIES = ("local_as_ingested",)


@dataclass(frozen=True)
class Context:
    mode: str
    effective_at: datetime | None
    known_at: datetime | None
    availability_policy: str = "local_as_ingested"
    timezone: str = "UTC"
    versions: dict[str, int] | None = None

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "effective_at": _iso(self.effective_at),
            "known_at": _iso(self.known_at),
            "availability_policy": self.availability_policy,
            "timezone": self.timezone,
            "versions": self.versions,
        }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def _when(raw: dict, key: str, required: bool) -> datetime | None:
    value = raw.get(key)
    if value is None:
        if required:
            raise DomainError("QUERY_REJECTED", f"context.{key} is required for mode {raw['mode']}",
                              {"field": key})
        return None
    try:
        return _instant(str(value))
    except ValueError as exc:
        raise DomainError("QUERY_REJECTED", f"context.{key} must be a timezone-aware instant",
                          {"field": key}) from exc


def parse_context(raw: dict | None) -> Context:
    raw = dict(raw or {})
    raw.setdefault("mode", "current_corrected")
    mode = raw["mode"]
    if mode not in MODES:
        raise DomainError("TEMPORAL_MODE_UNSUPPORTED", f"unknown temporal mode {mode!r}",
                          {"supported_modes": list(MODES)})
    policy = raw.get("availability_policy") or "local_as_ingested"
    if policy not in POLICIES:
        raise DomainError("TEMPORAL_MODE_UNSUPPORTED",
                          f"availability policy {policy!r} is not available",
                          {"supported_policies": list(POLICIES)})
    versions = raw.get("versions")
    if versions is not None and not (
        isinstance(versions, dict) and all(isinstance(v, int) for v in versions.values())
    ):
        raise DomainError("QUERY_REJECTED", "context.versions must map relation to integer version")
    return Context(
        mode=mode,
        effective_at=_when(raw, "effective_at", required=(mode == "effective_on")),
        known_at=_when(raw, "known_at", required=(mode in ("known_at", "captured_by"))),
        availability_policy=policy,
        timezone=str(raw.get("timezone") or "UTC"),
        versions=dict(versions) if versions else None,
    )
```

- [ ] **Step 4: Write `resolver/catalog.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The logical catalog: declared relations, their capabilities, and external Delta libraries."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from openbb_deltalake.utils import fs_and_root
from pyarrow import fs as pafs

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import Context
from security_master_api.store.schemas import RELATIONS

SILVER_MODES = ("current_corrected", "known_at", "effective_on", "delta_snapshot")
BRONZE_MODES = ("captured_by", "delta_snapshot")
OPS_MODES = ("delta_snapshot",)


@dataclass(frozen=True)
class Relation:
    layer: str
    name: str
    description: str
    kind: str
    modes: tuple[str, ...]
    keys: tuple[str, ...]
    depends_on: tuple[str, ...] = ()

    @property
    def full(self) -> str:
        return f"{self.layer}.{self.name}"

    def as_dict(self) -> dict:
        return {"layer": self.layer, "name": self.name, "description": self.description,
                "kind": self.kind, "modes": list(self.modes), "keys": list(self.keys),
                "depends_on": list(self.depends_on)}


_KEYS = {
    "silver.issuers": ("issuer_id",), "silver.instruments": ("instrument_id",),
    "silver.securities": ("security_id",), "silver.listings": ("listing_id",),
    "silver.identifiers": ("identifier_type", "identifier"),
    "silver.fundamental_facts": ("issuer_id", "fact", "period_end"),
    "silver.prices_normalized": ("listing_id", "market_date"),
    "silver.corporate_actions": ("listing_id", "action_type", "ex_date"),
    "silver.universe_membership": ("universe_id", "listing_id"),
    "silver.exchanges": ("exchange_id",), "silver.calendar_authorities": ("authority_id",),
    "silver.calendar_rules": ("rule_id",),
    "silver.calendar_exceptions": ("calendar_id", "session_date", "assertion_domain"),
    "silver.market_sessions": ("calendar_id", "session_date"),
    "silver.session_interruptions": ("calendar_id", "session_date", "interruption_start"),
    "bronze.source_captures": ("capture_id",), "bronze.request_log": ("request_id",),
    "bronze.payload_rows": ("capture_id", "ordinal"), "bronze.ingestion_runs": ("run_id",),
    "ops.jobs": ("job_id", "seq"), "ops.job_events": ("event_id",),
    "ops.receipts": ("execution_id",),
}

GOLD: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
    # name: (keys, depends_on, description)
    "security_master": (("listing_id",), ("silver.listings", "silver.identifiers",
                        "silver.instruments", "silver.issuers", "silver.securities"),
                        "Resolved securities, listings and local coverage."),
    "price_daily": (("listing_id", "market_date"), ("silver.prices_normalized", "silver.listings"),
                    "Correctable daily prices by stable listing."),
    "corporate_actions": (("listing_id", "action_type", "ex_date"),
                          ("silver.corporate_actions", "silver.listings"),
                          "Splits, dividends and reorganizations."),
    "universe_membership": (("universe_id", "listing_id"),
                            ("silver.universe_membership", "silver.listings"),
                            "Point-in-time index and universe tenures."),
    "market_calendar": (("calendar_id", "session_date"),
                        ("silver.market_sessions", "silver.calendar_exceptions",
                         "silver.exchanges", "silver.session_interruptions"),
                        "Exchange sessions joined to holiday and market-session evidence."),
    "odp_equity_info": (("listing_id",), ("silver.listings", "silver.issuers",
                        "silver.fundamental_facts"),
                        "The EquityInfo projection."),
}


def _declared() -> list[Relation]:
    out = []
    for full in RELATIONS:
        layer, name = full.split(".", 1)
        modes = {"silver": SILVER_MODES, "bronze": BRONZE_MODES, "ops": OPS_MODES}[layer]
        out.append(Relation(layer, name, f"{layer} relation {name}", "delta_table", modes,
                            _KEYS[full]))
    for name, (keys, deps, description) in GOLD.items():
        out.append(Relation("gold", name, description, "view", SILVER_MODES, keys, deps))
    return out


def external_relations(settings: Settings) -> list[Relation]:
    if not settings.delta_base:
        return []
    filesystem, root = fs_and_root(settings.delta_base, settings.storage_options)
    out = []
    for library in settings.external_libraries:
        try:
            infos = filesystem.get_file_info(pafs.FileSelector(f"{root}/{library}", recursive=False))
        except (FileNotFoundError, OSError):
            continue
        for info in infos:
            if info.type != pafs.FileType.Directory:
                continue
            symbol = info.base_name
            log = filesystem.get_file_info(f"{info.path}/_delta_log")
            if log.type == pafs.FileType.Directory:
                out.append(Relation("bronze", f"{library}.{symbol}",
                                    f"Delta table {library}/{symbol} (external evidence)",
                                    "external_delta", ("delta_snapshot", "captured_by"), ()))
    return out


def catalog(settings: Settings) -> list[Relation]:
    return _declared() + external_relations(settings)


def relation(settings: Settings, full: str) -> Relation:
    for rel in catalog(settings):
        if rel.full == full:
            return rel
    raise DomainError("QUERY_REJECTED", f"unknown relation: {full}", {"relation": full})


def expand(settings: Settings, relations: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for full in relations:
        rel = relation(settings, full)
        out.add(full)
        out |= set(rel.depends_on)
    return out


def check_modes(settings: Settings, ctx: Context, relations: Iterable[str]) -> None:
    for full in relations:
        rel = relation(settings, full)
        if ctx.mode not in rel.modes:
            raise DomainError(
                "TEMPORAL_MODE_UNSUPPORTED",
                f"{ctx.mode} is not supported by {full}",
                {"relation": full, "supported_modes": list(rel.modes)},
            )
```

- [ ] **Step 5: Write `resolver/manifest.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Pin every physical table a request opens. The manifest is what the receipt records."""

from __future__ import annotations

from collections.abc import Iterable

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.catalog import check_modes, expand, relation
from security_master_api.resolver.context import Context
from security_master_api.store.tables import latest_version


def resolve_manifest(settings: Settings, ctx: Context, relations: Iterable[str]) -> dict[str, int]:
    wanted = expand(settings, relations)
    check_modes(settings, ctx, wanted)
    manifest: dict[str, int] = {}
    for full in sorted(wanted):
        if relation(settings, full).kind == "view":
            continue
        latest = latest_version(settings, full)
        if latest is None:
            raise DomainError("NO_LOCAL_EVIDENCE", f"{full} has no local table", {"relation": full})
        requested = (ctx.versions or {}).get(full)
        if requested is None:
            manifest[full] = latest
        elif 0 <= requested <= latest:
            manifest[full] = requested
        else:
            raise DomainError("VERSION_NOT_RETAINED", f"{full} has no retained version {requested}",
                              {"relation": full, "latest": latest})
    return manifest
```

External relations (`kind == "external_delta"`) are pinned by the same code path; `latest_version` for them is resolved by Task 5's `external_path`, so for now `resolve_manifest` treats an unknown physical location as `NO_LOCAL_EVIDENCE`, which Task 5 corrects.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check .`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add security-master-api
git commit -m "feat(security-master): temporal context, catalog and dependency manifest"
```

### Task 5: DuckDB query session and the SQL policy

**Files:**
- Create: `security_master_api/sql/__init__.py`, `sql/policy.py`, `sql/session.py`
- Modify: `security_master_api/store/paths.py` (external paths), `store/tables.py` (`_open` uses `physical_path`)
- Test: `tests/test_sql_policy.py`, `tests/test_sql_session.py`

**Interfaces:**
- Produces: `PlanInfo(relations: list[str], functions: list[str])`; `validate_sql(con: duckdb.DuckDBPyConnection, sql: str, allowed: set[str], allow_explain: bool = False) -> PlanInfo`; `QuerySession(settings, ctx, manifest: dict[str, int], views: Iterable[str] = ())` context manager with `run(sql, params=None, timeout_ms=None) -> pa.Table`, `cancel()`, `describe(sql) -> list[dict]` (`[{name, dtype}]`), attribute `con`; `physical_path(settings, relation) -> str` (declared or external).
- `views` names Gold relations to create; their SQL comes from Task 6's `views.gold_sql(name, ctx)`. In this task the session creates views only from a `view_sql: dict[str, str]` argument; Task 6 wires `gold_sql`.

- [ ] **Step 1: Write the failing policy test**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import duckdb
import pytest

from security_master_api.errors import DomainError
from security_master_api.sql.policy import validate_sql

ALLOWED = {"gold.security_master", "silver.listings"}


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("CREATE SCHEMA gold; CREATE SCHEMA silver")
    c.execute("CREATE TABLE gold.security_master(symbol VARCHAR, name VARCHAR)")
    c.execute("CREATE TABLE silver.listings(listing_id VARCHAR, symbol VARCHAR)")
    return c


def test_plain_select_passes(con):
    info = validate_sql(con, "SELECT symbol, upper(name) AS n FROM gold.security_master ORDER BY symbol LIMIT 10", ALLOWED)
    assert info.relations == ["gold.security_master"]
    assert "upper" in info.functions


def test_cte_and_join_pass(con):
    sql = ("WITH l AS (SELECT listing_id, symbol FROM silver.listings) "
           "SELECT s.symbol, count(*) FROM gold.security_master s JOIN l USING (symbol) GROUP BY 1")
    assert sorted(validate_sql(con, sql, ALLOWED).relations) == ["gold.security_master", "silver.listings"]


@pytest.mark.parametrize("sql", [
    "SELECT 1; SELECT 2",
    "DROP TABLE gold.security_master",
    "INSERT INTO silver.listings VALUES ('x','y')",
    "COPY gold.security_master TO '/tmp/x.csv'",
    "ATTACH '/tmp/other.db' AS o",
    "INSTALL httpfs",
    "LOAD httpfs",
    "PRAGMA database_list",
    "SET memory_limit='1GB'",
    "CREATE SECRET (TYPE S3)",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM 's3://bucket/x.parquet'",
    "SELECT * FROM glob('*')",
    "SELECT getenv('HOME')",
    "SELECT current_setting('home_directory')",
    "SELECT * FROM silver.nope",
    "SELECT * FROM ops.receipts",
    "EXPLAIN SELECT 1",
    "SELECT * FROM sniff_csv('x')",
])
def test_forbidden_statements_are_rejected(con, sql):
    with pytest.raises(DomainError) as exc:
        validate_sql(con, sql, ALLOWED)
    assert exc.value.code == "QUERY_REJECTED"


def test_explain_allowed_only_by_policy(con):
    info = validate_sql(con, "EXPLAIN SELECT symbol FROM gold.security_master", ALLOWED, allow_explain=True)
    assert info.relations == ["gold.security_master"]


def test_unparseable_sql_is_rejected_with_the_parser_message(con):
    with pytest.raises(DomainError) as exc:
        validate_sql(con, "SELEC symbol FROM gold.security_master", ALLOWED)
    assert "syntax" in exc.value.message.lower() or "parser" in exc.value.message.lower()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --extra dev pytest -q tests/test_sql_policy.py`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write `sql/policy.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Read-only SQL policy, enforced on DuckDB's own AST rather than on the text."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import duckdb

from security_master_api.errors import DomainError

ALLOWED_FUNCTIONS = frozenset({
    # arithmetic / comparison / logic
    "+", "-", "*", "/", "//", "%", "**", "^", "=", "==", "<>", "!=", "<", "<=", ">", ">=",
    "and", "or", "not", "between", "in", "is", "coalesce", "nullif", "greatest", "least",
    "abs", "round", "floor", "ceil", "ceiling", "sign", "sqrt", "power", "pow", "ln", "log",
    "log10", "log2", "exp", "mod", "random",
    # strings
    "upper", "lower", "length", "len", "trim", "ltrim", "rtrim", "substr", "substring",
    "left", "right", "concat", "concat_ws", "||", "replace", "contains", "starts_with",
    "ends_with", "prefix", "suffix", "strip_accents", "regexp_matches", "regexp_replace",
    "regexp_extract", "like", "~~", "!~~", "ilike", "~~*", "split_part", "string_split",
    "format", "printf", "repeat", "reverse", "lpad", "rpad", "instr", "strpos", "position",
    # dates and times
    "date_trunc", "date_part", "datepart", "date_diff", "datediff", "date_add", "date_sub",
    "epoch", "epoch_ms", "strftime", "strptime", "year", "month", "day", "hour", "minute",
    "second", "dayofweek", "weekday", "dayofyear", "week", "quarter", "isodow", "make_date",
    "make_timestamp", "to_timestamp", "age", "now", "current_date", "current_timestamp",
    "today", "timezone", "interval",
    # casting / structs / lists / json
    "cast", "try_cast", "typeof", "list_value", "list_contains", "list_extract",
    "array_extract", "unnest", "struct_pack", "struct_extract", "json_extract",
    "json_extract_string", "->", "->>", "json", "from_json", "json_valid", "json_type",
    # aggregates and windows
    "count", "count_star", "sum", "avg", "mean", "min", "max", "median", "quantile",
    "quantile_cont", "quantile_disc", "stddev", "stddev_samp", "stddev_pop", "variance",
    "var_samp", "var_pop", "first", "last", "any_value", "arg_min", "arg_max", "string_agg",
    "list", "array_agg", "bool_and", "bool_or", "approx_count_distinct",
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile", "lag", "lead",
    "first_value", "last_value", "nth_value",
    # misc
    "hash", "md5", "sha256", "case", "if", "ifnull",
})

SELECT_NODES = {"SELECT_NODE", "SET_OPERATION_NODE", "RECURSIVE_CTE_NODE", "CTE_NODE"}


@dataclass
class PlanInfo:
    relations: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)


def _reject(message: str, **details) -> DomainError:
    return DomainError("QUERY_REJECTED", message, details or None)


def _walk(node, info: PlanInfo, cte_names: set[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, info, cte_names)
        return
    if not isinstance(node, dict):
        return
    node_type = node.get("type")
    if node_type == "TABLE_FUNCTION":
        raise _reject("table functions are not allowed",
                      function=node.get("function", {}).get("function_name"))
    if node_type == "BASE_TABLE":
        schema = node.get("schema_name") or ""
        table = node.get("table_name") or ""
        if not schema and table in cte_names:
            pass
        else:
            info.relations.append(f"{schema}.{table}" if schema else table)
    if node.get("class") == "FUNCTION":
        name = str(node.get("function_name", "")).lower()
        info.functions.append(name)
    if node_type in ("SELECT_NODE", "SET_OPERATION_NODE"):
        for name in (node.get("cte_map") or {}).get("map", []) or []:
            if isinstance(name, dict) and "key" in name:
                cte_names.add(str(name["key"]))
    for value in node.values():
        _walk(value, info, cte_names)


def validate_sql(con: duckdb.DuckDBPyConnection, sql: str, allowed: set[str],
                 allow_explain: bool = False) -> PlanInfo:
    text = sql.strip().rstrip(";").strip()
    if ";" in text:
        raise _reject("exactly one statement is allowed")
    body = text
    if text[:7].lower() == "explain":
        if not allow_explain:
            raise _reject("EXPLAIN is not allowed by policy")
        body = text[7:].strip()
    raw = con.execute("SELECT json_serialize_sql(?)", [body]).fetchone()[0]
    parsed = json.loads(raw)
    if parsed.get("error"):
        raise _reject(f"parser: {parsed.get('error_message', 'invalid SQL')}")
    statements = parsed.get("statements") or []
    if len(statements) != 1:
        raise _reject("exactly one statement is allowed")
    node = statements[0].get("node") or {}
    if node.get("type") not in SELECT_NODES:
        raise _reject("only SELECT statements are allowed", statement=node.get("type"))
    info = PlanInfo()
    _walk(node, info, set())
    for full in info.relations:
        if full not in allowed:
            raise _reject(f"relation is not registered for this context: {full}", relation=full)
    for fn in info.functions:
        if fn not in ALLOWED_FUNCTIONS:
            raise _reject(f"function is not allowed: {fn}", function=fn)
    info.relations = sorted(set(info.relations))
    info.functions = sorted(set(info.functions))
    return info
```

Note: `json_serialize_sql` returns `{"error": true, "error_message": ...}` for DDL/DML and unparseable input, so those cases reach the parser branch; the `';'` guard covers multi-statement text before the parser sees it. Run the parametrized test and, for any statement DuckDB serializes as a non-`SELECT_NODE` type, the `statement` branch catches it. If a statement in the list slips through both, add its node type to a `DENIED_NODES` check before the walk.

- [ ] **Step 4: Run the policy test to verify it passes**

Run: `uv run --extra dev pytest -q tests/test_sql_policy.py`
Expected: PASS.

- [ ] **Step 5: Write the failing session test**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import threading
import time

import pytest

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import parse_context
from security_master_api.resolver.manifest import resolve_manifest
from security_master_api.sql.session import QuerySession
from security_master_api.store.seed import seed


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k", timeout_ms=2000)
    seed(s)
    return s


def test_only_manifest_relations_are_visible(settings):
    ctx = parse_context(None)
    manifest = resolve_manifest(settings, ctx, ["silver.listings"])
    with QuerySession(settings, ctx, manifest) as session:
        rows = session.run("SELECT symbol FROM silver.listings ORDER BY symbol").to_pylist()
        assert {r["symbol"] for r in rows} >= {"AAPL", "META", "FB"}
        with pytest.raises(DomainError) as exc:
            session.run("SELECT * FROM silver.issuers")
        assert exc.value.code == "QUERY_REJECTED"


def test_external_access_is_off(settings):
    ctx = parse_context(None)
    with QuerySession(settings, ctx, resolve_manifest(settings, ctx, ["silver.listings"])) as s:
        assert s.con.execute("SELECT current_setting('enable_external_access')").fetchone()[0] is False
        with pytest.raises(Exception):
            s.con.execute("SET enable_external_access = true")


def test_pinned_version_is_what_is_read(settings):
    snap = parse_context({"mode": "delta_snapshot", "versions": {"silver.issuers": 0}})
    with QuerySession(settings, snap, resolve_manifest(settings, snap, ["silver.issuers"])) as s:
        assert s.run("SELECT count(*) AS n FROM silver.issuers").to_pylist()[0]["n"] == 0


def test_view_sql_is_created(settings):
    ctx = parse_context(None)
    manifest = resolve_manifest(settings, ctx, ["silver.listings"])
    with QuerySession(settings, ctx, manifest,
                      view_sql={"symbols": "SELECT symbol FROM silver.listings"}) as s:
        assert s.run("SELECT count(*) AS n FROM gold.symbols").to_pylist()[0]["n"] >= 3


def test_timeout_and_cancel_reach_duckdb(settings):
    ctx = parse_context(None)
    manifest = resolve_manifest(settings, ctx, ["silver.listings"])
    slow = "SELECT count(*) FROM range(200000000) a, range(200) b"
    with QuerySession(settings, ctx, manifest) as s:
        started = time.monotonic()
        with pytest.raises(DomainError) as exc:
            s.run(slow, timeout_ms=300)
        assert exc.value.code == "QUERY_BUDGET_EXCEEDED"
        assert time.monotonic() - started < 5
    with QuerySession(settings, ctx, manifest) as s:
        threading.Timer(0.2, s.cancel).start()
        with pytest.raises(DomainError) as exc:
            s.run(slow, timeout_ms=10_000)
        assert exc.value.code == "QUERY_CANCELLED"


def test_describe_returns_columns(settings):
    ctx = parse_context(None)
    with QuerySession(settings, ctx, resolve_manifest(settings, ctx, ["silver.listings"])) as s:
        cols = s.describe("SELECT listing_id, symbol FROM silver.listings")
        assert cols == [{"name": "listing_id", "dtype": "VARCHAR"}, {"name": "symbol", "dtype": "VARCHAR"}]
```

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run --extra dev pytest -q tests/test_sql_session.py`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 7: Write `sql/session.py`, and add `physical_path`**

Add to `store/paths.py`:

```python
def physical_path(settings: Settings, relation: str) -> str:
    """Declared relations live under root; `bronze.<library>.<symbol>` is an external table."""
    if relation in RELATIONS:
        return relation_path(settings, relation)
    layer, _, rest = relation.partition(".")
    library, _, symbol = rest.partition(".")
    if layer == "bronze" and symbol and settings.delta_base and library in settings.external_libraries:
        return f"{settings.delta_base}/{library}/{symbol}"
    raise DomainError("QUERY_REJECTED", f"unknown relation: {relation}", {"relation": relation})
```

and in `store/tables.py` change `_open` to `path = physical_path(settings, relation)` (import it), so external tables version and open through the same functions.

`sql/session.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""One isolated DuckDB connection per request, holding only the manifest's relations."""

from __future__ import annotations

import threading
from collections.abc import Mapping

import duckdb
import pyarrow as pa

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import Context
from security_master_api.sql.policy import validate_sql
from security_master_api.store.tables import dataset


class QuerySession:
    def __init__(self, settings: Settings, ctx: Context, manifest: Mapping[str, int],
                 view_sql: Mapping[str, str] | None = None):
        self.settings = settings
        self.ctx = ctx
        self.manifest = dict(manifest)
        self.view_sql = dict(view_sql or {})
        self.allowed: set[str] = set(self.manifest) | {f"gold.{n}" for n in self.view_sql}
        self.con: duckdb.DuckDBPyConnection | None = None
        self._cancelled = threading.Event()

    def __enter__(self) -> QuerySession:
        con = duckdb.connect(":memory:", config={
            "enable_external_access": "false",
            "memory_limit": self.settings.memory_limit,
            "threads": str(self.settings.threads),
            "lock_configuration": "true",
        })
        self.con = con
        for schema in ("bronze", "silver", "gold", "ops"):
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        for full, version in self.manifest.items():
            layer, _, name = full.partition(".")
            alias = f"{layer}__{name.replace('.', '__')}"
            con.register(alias, dataset(self.settings, full, version))
            con.execute(f'CREATE VIEW {layer}."{name}" AS SELECT * FROM "{alias}"')
        for name, sql in self.view_sql.items():
            con.execute(f'CREATE VIEW gold."{name}" AS {sql}')
        return self

    def __exit__(self, *exc) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None

    def cancel(self) -> None:
        self._cancelled.set()
        if self.con is not None:
            self.con.interrupt()

    def describe(self, sql: str) -> list[dict]:
        validate_sql(self.con, sql, self.allowed)
        rows = self.con.execute(f"DESCRIBE {sql}").fetchall()
        return [{"name": r[0], "dtype": r[1]} for r in rows]

    def run(self, sql: str, params: list | None = None, timeout_ms: int | None = None,
            allow_explain: bool = False) -> pa.Table:
        assert self.con is not None, "session not entered"
        validate_sql(self.con, sql, self.allowed, allow_explain=allow_explain)
        budget = (timeout_ms or self.settings.timeout_ms) / 1000
        timed_out = threading.Event()

        def _interrupt():
            timed_out.set()
            self.con.interrupt()

        timer = threading.Timer(budget, _interrupt)
        timer.start()
        try:
            return self.con.execute(sql, params or []).fetch_arrow_table()
        except duckdb.InterruptException as exc:
            if self._cancelled.is_set():
                raise DomainError("QUERY_CANCELLED", "the query was cancelled") from exc
            raise DomainError("QUERY_BUDGET_EXCEEDED", f"the query exceeded {budget:.1f}s",
                              {"timeout_ms": int(budget * 1000)}) from exc
        except duckdb.OutOfMemoryException as exc:
            raise DomainError("QUERY_BUDGET_EXCEEDED", "the query exceeded the memory budget") from exc
        finally:
            timer.cancel()
```

DuckDB raises `duckdb.InterruptException` when `interrupt()` fires during `execute`; if the installed version surfaces it as `duckdb.Error` with "INTERRUPT" in the message, catch `duckdb.Error` and test `"INTERRUPT" in str(exc).upper()` instead. Datasets registered through `con.register` are scanned lazily with projection and filter pushdown.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check .`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add security-master-api
git commit -m "feat(security-master): isolated DuckDB sessions and the AST-level SQL policy"
```

### Task 6: Temporal resolver — assertion filters and Gold views, proven on the golden fixtures

**Files:**
- Create: `security_master_api/resolver/views.py`
- Modify: `security_master_api/sql/session.py` (accept `views: Iterable[str]` and build `view_sql` from `gold_sql`)
- Test: `tests/test_views.py`, `tests/test_golden_calendar.py`

**Interfaces:**
- Produces: `assertions_sql(relation: str, ctx: Context) -> str` (the current-per-context rows of one Silver relation); `gold_sql(name: str, ctx: Context) -> str`; `GOLD_NAMES`; `QuerySession(settings, ctx, manifest, views=("market_calendar",))`.
- `open_session(settings, ctx, relations: Iterable[str]) -> QuerySession` helper in `sql/session.py`: expands Gold dependencies, resolves the manifest, and enters a session with every requested Gold view.

- [ ] **Step 1: Write the failing view tests**

`tests/test_views.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.resolver.context import parse_context
from security_master_api.resolver.views import GOLD_NAMES, assertions_sql, gold_sql
from security_master_api.sql.session import open_session
from security_master_api.store.seed import seed


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return s


def test_gold_names_match_the_catalog():
    assert GOLD_NAMES == ("security_master", "price_daily", "corporate_actions",
                          "universe_membership", "market_calendar", "odp_equity_info")


def test_known_at_filter_hides_close_rows_after_cutoff():
    ctx = parse_context({"mode": "known_at", "known_at": "2026-09-15T00:00:00Z"})
    sql = assertions_sql("silver.prices_normalized", ctx)
    assert "system_from <= TIMESTAMPTZ '2026-09-15T00:00:00+00:00'" in sql
    assert "available_at <= TIMESTAMPTZ '2026-09-15T00:00:00+00:00'" in sql
    assert "QUALIFY row_number() OVER (PARTITION BY assertion_id ORDER BY system_from DESC) = 1" in sql


def _closes(settings, ctx):
    with open_session(settings, ctx, ["gold.price_daily"]) as s:
        rows = s.run("SELECT close, assertion_id, capture_id FROM gold.price_daily "
                     "WHERE listing_id = 'lst_apple' AND market_date = DATE '2026-09-14'").to_pylist()
    return rows


def test_trade_correction_at_every_boundary(settings):
    before = parse_context({"mode": "known_at", "known_at": "2026-09-15T09:19:59.999999Z",
                            "effective_at": "2026-09-14T20:00:00Z"})
    at = parse_context({"mode": "known_at", "known_at": "2026-09-15T09:20:00Z",
                        "effective_at": "2026-09-14T20:00:00Z"})
    after = parse_context({"mode": "known_at", "known_at": "2026-09-15T09:20:00.000001Z",
                           "effective_at": "2026-09-14T20:00:00Z"})
    assert [r["close"] for r in _closes(settings, before)] == [231.40]
    assert _closes(settings, before)[0]["capture_id"] == "cap_00421"
    assert [r["close"] for r in _closes(settings, at)] == [231.74]
    assert [r["close"] for r in _closes(settings, after)] == [231.74]
    assert [r["close"] for r in _closes(settings, parse_context(None))] == [231.74]
    none = parse_context({"mode": "known_at", "known_at": "2026-09-14T20:04:59Z",
                          "effective_at": "2026-09-14T20:00:00Z"})
    assert _closes(settings, none) == []


def test_shares_outstanding_boundaries(settings):
    def value(known_at):
        ctx = parse_context({"mode": "known_at", "known_at": known_at})
        with open_session(settings, ctx, ["gold.odp_equity_info"]) as s:
            rows = s.run("SELECT shares_outstanding, filing_kind FROM gold.odp_equity_info "
                         "WHERE symbol = 'EXMP'").to_pylist()
        return rows
    assert value("2026-07-15T00:00:00Z") == [] or value("2026-07-15T00:00:00Z")[0]["shares_outstanding"] is None
    assert value("2026-08-05T12:00:00Z")[0]["shares_outstanding"] == 1.5e9
    assert value("2026-08-19T23:59:59Z")[0]["shares_outstanding"] == 1.5e9
    assert value("2026-08-20T12:00:00Z")[0] == {"shares_outstanding": 1.48e9, "filing_kind": "10-Q/A"}


def test_ticker_change_is_one_listing(settings):
    ctx = parse_context({"mode": "effective_on", "effective_at": "2022-06-07T00:00:00Z"})
    with open_session(settings, ctx, ["gold.security_master"]) as s:
        rows = s.run("SELECT symbol, listing_id FROM gold.security_master WHERE listing_id = 'lst_000042'").to_pylist()
    assert rows == [{"symbol": "FB", "listing_id": "lst_000042"}]
    ctx = parse_context({"mode": "effective_on", "effective_at": "2022-06-10T00:00:00Z"})
    with open_session(settings, ctx, ["gold.security_master"]) as s:
        rows = s.run("SELECT symbol FROM gold.security_master WHERE listing_id = 'lst_000042'").to_pylist()
    assert rows == [{"symbol": "META"}]
    with open_session(settings, parse_context(None), ["gold.price_daily"]) as s:
        n = s.run("SELECT count(*) AS n FROM gold.price_daily WHERE listing_id = 'lst_000042'").to_pylist()
    assert n[0]["n"] == 2


def test_gold_sql_names_only_declared_dependencies():
    ctx = parse_context(None)
    for name in GOLD_NAMES:
        sql = gold_sql(name, ctx)
        assert "gold." not in sql, name
```

`tests/test_golden_calendar.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.resolver.context import parse_context
from security_master_api.sql.session import open_session
from security_master_api.store.seed import seed


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return s


def rows(settings, calendar_id, known_at, start, end):
    ctx = parse_context({"mode": "known_at", "known_at": known_at, "effective_at": f"{start}T00:00:00Z"})
    with open_session(settings, ctx, ["gold.market_calendar"]) as s:
        out = s.run(
            "SELECT session_date, evidence_status, market_effect, authority_type, authority_name, "
            "holiday_family FROM gold.market_calendar WHERE calendar_id = ? "
            "AND session_date BETWEEN ? AND ? ORDER BY session_date",
            [calendar_id, start, end]).to_pylist()
    return {str(r["session_date"]): r for r in out}


def test_eid_before_at_after_religious_verification(settings):
    t1 = "2027-03-09T18:45:00Z"
    before = rows(settings, "cal_tadawul", "2027-03-09T18:44:59.999999Z", "2027-03-01", "2027-03-31")
    assert before["2027-03-10"]["evidence_status"] == "provisional"
    assert before["2027-03-10"]["market_effect"] == "pending"
    assert "2027-03-11" not in before or before["2027-03-11"]["holiday_family"] is None
    for cutoff in (t1, "2027-03-09T18:45:00.000001Z"):
        at = rows(settings, "cal_tadawul", cutoff, "2027-03-01", "2027-03-31")
        assert at["2027-03-11"]["evidence_status"] == "authority_confirmed"
        assert at["2027-03-11"]["market_effect"] == "pending"
        assert at["2027-03-11"]["authority_name"] == "Supreme Court of Saudi Arabia"
        assert "2027-03-10" not in at or at["2027-03-10"]["holiday_family"] is None


def test_eid_exchange_notice_makes_it_market_final(settings):
    t2 = "2027-03-10T08:00:00Z"
    before = rows(settings, "cal_tadawul", "2027-03-10T07:59:59.999999Z", "2027-03-01", "2027-03-31")
    assert before["2027-03-11"]["market_effect"] == "pending"
    for cutoff in (t2, "2027-03-10T08:00:00.000001Z"):
        at = rows(settings, "cal_tadawul", cutoff, "2027-03-01", "2027-03-31")
        assert at["2027-03-11"]["evidence_status"] == "market_final"
        assert at["2027-03-11"]["market_effect"] == "closed"
        assert at["2027-03-11"]["authority_type"] == "exchange"


def test_lunar_new_year_special_session(settings):
    t1 = "2027-01-20T09:00:00Z"
    before = rows(settings, "cal_xhkg", "2027-01-20T08:59:59.999999Z", "2027-02-01", "2027-02-28")
    assert before["2027-02-05"]["market_effect"] == "none"
    assert before.get("2027-02-06", {}).get("market_effect") != "closed"
    at = rows(settings, "cal_xhkg", t1, "2027-02-01", "2027-02-28")
    assert at["2027-02-05"]["market_effect"] == "early_close"
    assert at["2027-02-06"]["market_effect"] == "closed"
    assert at["2027-02-08"]["market_effect"] == "closed"


def test_religious_observance_never_closes_the_exchange(settings):
    before = rows(settings, "cal_xtae", "2027-05-01T12:00:00Z", "2027-05-01", "2027-05-31")
    assert before["2027-05-27"]["evidence_status"] == "authority_confirmed"
    assert before["2027-05-27"]["market_effect"] == "pending"
    at = rows(settings, "cal_xtae", "2027-05-02T00:00:00Z", "2027-05-01", "2027-05-31")
    assert at["2027-05-27"]["market_effect"] == "none"


def test_india_bakri_eid_correction(settings):
    early = rows(settings, "cal_xnse", "2023-06-01T00:00:00Z", "2023-06-27", "2023-06-30")
    assert early["2023-06-28"]["market_effect"] == "closed"
    mid = rows(settings, "cal_xnse", "2023-06-26T12:00:00Z", "2023-06-27", "2023-06-30")
    assert mid["2023-06-29"]["evidence_status"] == "authority_confirmed"
    assert mid["2023-06-29"]["authority_type"] == "government"
    assert mid["2023-06-29"]["market_effect"] == "pending"
    late = rows(settings, "cal_xnse", "2023-06-27T12:00:00Z", "2023-06-27", "2023-06-30")
    assert late["2023-06-28"]["market_effect"] == "open"
    assert late["2023-06-29"]["market_effect"] == "closed"


def test_dubai_branches_are_retained(settings):
    ctx = parse_context({"mode": "known_at", "known_at": "2024-04-08T20:00:05Z",
                         "effective_at": "2024-04-08T00:00:00Z"})
    with open_session(settings, ctx, ["gold.market_calendar"]) as s:
        out = s.run("SELECT session_date, market_effect, candidate_branches, selected_branch "
                    "FROM gold.market_calendar WHERE calendar_id = 'cal_xdfm' "
                    "AND session_date BETWEEN DATE '2024-04-08' AND DATE '2024-04-15' "
                    "ORDER BY session_date").to_pylist()
    by = {str(r["session_date"]): r for r in out}
    assert by["2024-04-15"]["market_effect"] == "open"
    assert by["2024-04-12"]["market_effect"] == "closed"
    assert "2024-04-09" in by["2024-04-08"]["candidate_branches"]
    assert by["2024-04-15"]["selected_branch"] is not None
```

The India (`cal_xnse`) and Dubai (`cal_xdfm`) assertions rows are those Task 3 added to the two historical fixtures; every calendar fixture also seeds one `silver.exchanges` row (`exch_xnse`/`cal_xnse`, `exch_xdfm`/`cal_xdfm`, `exch_xsau`/`cal_tadawul`, `exch_xhkg`/`cal_xhkg`, `exch_xtae`/`cal_xtae`) so the Gold view can join timezone and name.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --extra dev pytest -q tests/test_views.py tests/test_golden_calendar.py`
Expected: FAIL with `ModuleNotFoundError: security_master_api.resolver.views`.

- [ ] **Step 3: Write `resolver/views.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Context-bound SQL: which assertion rows count, and the Gold views built from them."""

from __future__ import annotations

from datetime import datetime

from security_master_api.resolver.context import Context

GOLD_NAMES = ("security_master", "price_daily", "corporate_actions", "universe_membership",
              "market_calendar", "odp_equity_info")

LATEST_PER_ASSERTION = (
    "QUALIFY row_number() OVER (PARTITION BY assertion_id ORDER BY system_from DESC) = 1"
)


def _ts(value: datetime) -> str:
    return f"TIMESTAMPTZ '{value.isoformat()}'"


def knowledge_filters(ctx: Context) -> tuple[str, str]:
    """(pre-qualify filter, post-qualify filter) for one Silver relation."""
    if ctx.mode == "known_at":
        k = _ts(ctx.known_at)
        return (f"system_from <= {k}",
                f"available_at <= {k} AND (system_to IS NULL OR system_to > {k})")
    if ctx.mode in ("current_corrected", "effective_on"):
        return ("TRUE", "system_to IS NULL AND assertion_status = 'current'")
    return ("TRUE", "TRUE")


def effective_filter(ctx: Context) -> str:
    if ctx.effective_at is None or ctx.mode in ("delta_snapshot", "captured_by"):
        return "TRUE"
    e = _ts(ctx.effective_at)
    return f"effective_from <= {e} AND (effective_to IS NULL OR effective_to > {e})"


def assertions_sql(relation: str, ctx: Context) -> str:
    layer, _, name = relation.partition(".")
    pre, post = knowledge_filters(ctx)
    return (f'SELECT * FROM (SELECT * FROM {layer}."{name}" WHERE {pre} {LATEST_PER_ASSERTION}) '
            f"WHERE {post} AND {effective_filter(ctx)}")


def _a(relation: str, ctx: Context, alias: str) -> str:
    return f"({assertions_sql(relation, ctx)}) AS {alias}"


def gold_sql(name: str, ctx: Context) -> str:
    if name == "security_master":
        return f"""
            SELECT l.listing_id, l.instrument_id, l.issuer_id, l.security_id, l.exchange_id, l.mic,
                   l.symbol, l.provider_symbol, l.currency, l.status, l.name, l.instrument_type,
                   i.name AS issuer_name, s.cusip, s.successor_security_id, s.predecessor_security_id,
                   l.assertion_id, l.capture_id, l.effective_from, l.effective_to,
                   l.available_at, l.system_from
            FROM {_a("silver.listings", ctx, "l")}
            LEFT JOIN {_a("silver.issuers", ctx, "i")} ON i.issuer_id = l.issuer_id
            LEFT JOIN {_a("silver.securities", ctx, "s")} ON s.security_id = l.security_id
        """
    if name == "price_daily":
        return f"""
            SELECT p.listing_id, p.market_date, p.open, p.high, p.low, p.close, p.volume,
                   p.price_basis, p.assertion_id, p.capture_id, p.available_at, p.system_from,
                   p.supersedes_assertion_id
            FROM (SELECT * FROM (SELECT * FROM silver."prices_normalized"
                  WHERE {knowledge_filters(ctx)[0]} {LATEST_PER_ASSERTION})
                  WHERE {knowledge_filters(ctx)[1]}) AS p
        """
    if name == "corporate_actions":
        return f"""
            SELECT c.listing_id, c.action_type, c.ex_date, c.ratio, c.amount, c.currency,
                   c.assertion_id, c.capture_id, c.available_at
            FROM {_a("silver.corporate_actions", ctx, "c")}
        """
    if name == "universe_membership":
        return f"""
            SELECT u.universe_id, u.listing_id, u.effective_from, u.effective_to,
                   u.assertion_id, u.capture_id, u.available_at
            FROM {_a("silver.universe_membership", ctx, "u")}
        """
    if name == "odp_equity_info":
        return f"""
            SELECT l.listing_id, l.symbol, l.name, l.issuer_id, l.mic, l.currency,
                   f.value AS shares_outstanding, f.period_end, f.filing_kind,
                   f.assertion_id AS shares_assertion_id, f.capture_id AS shares_capture_id,
                   f.available_at AS shares_available_at,
                   l.assertion_id, l.capture_id
            FROM {_a("silver.listings", ctx, "l")}
            LEFT JOIN (
                SELECT * FROM (SELECT * FROM (SELECT * FROM silver."fundamental_facts"
                    WHERE {knowledge_filters(ctx)[0]} {LATEST_PER_ASSERTION})
                    WHERE {knowledge_filters(ctx)[1]} AND fact = 'shares_outstanding')
                QUALIFY row_number() OVER (PARTITION BY issuer_id ORDER BY period_end DESC,
                                           available_at DESC) = 1
            ) AS f ON f.issuer_id = l.issuer_id
        """
    if name == "market_calendar":
        pre, post = knowledge_filters(ctx)
        ex = (f"(SELECT * FROM (SELECT * FROM silver.\"calendar_exceptions\" WHERE {pre} "
              f"{LATEST_PER_ASSERTION}) WHERE {post})")
        return f"""
            WITH holiday AS (
                SELECT * FROM {ex} WHERE assertion_domain = 'holiday_date'
                QUALIFY row_number() OVER (PARTITION BY calendar_id, session_date
                                           ORDER BY available_at DESC, system_from DESC) = 1
            ), market AS (
                SELECT * FROM {ex} WHERE assertion_domain = 'market_session'
                QUALIFY row_number() OVER (PARTITION BY calendar_id, session_date
                                           ORDER BY available_at DESC, system_from DESC) = 1
            ), sessions AS (
                SELECT * FROM {_a("silver.market_sessions", ctx, "s")}
            ), days AS (
                SELECT calendar_id, session_date FROM holiday
                UNION SELECT calendar_id, session_date FROM market
                UNION SELECT calendar_id, session_date FROM sessions
            )
            SELECT d.calendar_id, e.exchange_id, e.name AS exchange_name, e.mic, e.calendar_alias,
                   d.session_date, coalesce(s.trade_date, d.session_date) AS trade_date,
                   e.timezone,
                   CASE WHEN m.market_effect = 'closed' THEN 'closed'
                        WHEN m.market_effect IN ('early_close', 'late_open', 'special_session')
                             THEN 'special'
                        WHEN m.market_effect = 'interrupted' THEN 'interrupted'
                        WHEN m.market_effect IS NULL AND h.calendar_id IS NOT NULL THEN 'unknown'
                        WHEN m.market_effect = 'none' OR s.calendar_id IS NOT NULL THEN 'open'
                        ELSE 'unknown' END AS session_status,
                   coalesce(h.calendar_system, m.calendar_system, 'gregorian') AS calendar_system,
                   h.holiday_family,
                   CASE WHEN m.calendar_id IS NOT NULL THEN 'market_session'
                        WHEN h.calendar_id IS NOT NULL THEN 'holiday_date' END AS assertion_domain,
                   coalesce(m.evidence_status, h.evidence_status, 'estimated') AS evidence_status,
                   coalesce(m.market_open, s.market_open) AS market_open,
                   coalesce(m.market_close, s.market_close) AS market_close,
                   s.break_start, s.break_end,
                   h.holiday_name,
                   coalesce(m.special_open, FALSE) AS special_open,
                   coalesce(m.special_close, FALSE) AS special_close,
                   CASE WHEN m.market_effect IS NOT NULL THEN m.market_effect
                        WHEN h.calendar_id IS NOT NULL THEN 'pending'
                        ELSE 'none' END AS market_effect,
                   coalesce(m.authority_type, h.authority_type) AS authority_type,
                   coalesce(m.authority_name, h.authority_name) AS authority_name,
                   coalesce(m.authority_verified_at, h.authority_verified_at) AS authority_verified_at,
                   coalesce(m.source_kind, h.source_kind, s.source_version) AS source_kind,
                   coalesce(m.source_version, h.source_version, s.source_version) AS source_version,
                   coalesce(m.rule_id, h.rule_id, s.rule_id) AS rule_id,
                   coalesce(m.supersedes_assertion_id, h.supersedes_assertion_id) AS supersedes_assertion_id,
                   coalesce(m.effective_from, h.effective_from, s.effective_from) AS effective_from,
                   coalesce(m.effective_to, h.effective_to, s.effective_to) AS effective_to,
                   coalesce(m.available_at, h.available_at, s.available_at) AS known_from,
                   coalesce(m.system_to, h.system_to, s.system_to) AS known_to,
                   coalesce(m.capture_id, h.capture_id, s.capture_id) AS capture_id,
                   h.assertion_id AS holiday_assertion_id, m.assertion_id AS market_assertion_id,
                   m.candidate_branches, m.selected_branch, m.expiry_date, m.settlement_status
            FROM days d
            LEFT JOIN holiday h ON h.calendar_id = d.calendar_id AND h.session_date = d.session_date
            LEFT JOIN market m ON m.calendar_id = d.calendar_id AND m.session_date = d.session_date
            LEFT JOIN sessions s ON s.calendar_id = d.calendar_id AND s.session_date = d.session_date
            LEFT JOIN {_a("silver.exchanges", ctx, "e")} ON e.calendar_id = d.calendar_id
        """
    raise KeyError(name)
```

The holiday and market CTEs deliberately do not apply `effective_filter`: a calendar row is selected by `session_date` in the caller's WHERE clause, and the effective interval of a calendar assertion is the session itself.

- [ ] **Step 4: Add `views` and `open_session` to `sql/session.py`**

Change the constructor to `def __init__(self, settings, ctx, manifest, view_sql=None, views: Iterable[str] = ())` and, in `__init__`, `self.view_sql = {**{n: gold_sql(n, ctx) for n in views}, **dict(view_sql or {})}`. Add:

```python
def open_session(settings: Settings, ctx: Context, relations: Iterable[str]) -> QuerySession:
    from security_master_api.resolver.manifest import resolve_manifest

    wanted = list(relations)
    views = tuple(r.split(".", 1)[1] for r in wanted if r.startswith("gold."))
    manifest = resolve_manifest(settings, ctx, wanted)
    return QuerySession(settings, ctx, manifest, views=views).__enter__()
```

`open_session` returns an entered session; callers use it as `with open_session(...) as s:` because `__enter__` returns `self` again and is idempotent when `self.con` is already set (guard the connect with `if self.con is None`).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check .`
Expected: PASS. Fixture rows are the usual reason for a failure here: check `available_at` and the close rows before touching the SQL.

- [ ] **Step 6: Commit**

```bash
git add security-master-api
git commit -m "feat(security-master): context-bound Gold views proven on the seven golden fixtures"
```

### Task 7: Identity resolution, lineage and temporal comparison

**Files:**
- Create: `security_master_api/resolver/identity.py`, `resolver/lineage.py`, `resolver/compare.py`
- Test: `tests/test_identity.py`, `tests/test_lineage_compare.py`

**Interfaces:**
- Produces: `resolve(settings, ctx, identifier: str, identifier_type: str | None = None, include_historical: bool = True) -> dict` → `{"candidates": [{listing_id, instrument_id, issuer_id, security_id, symbol, identifier_type, identifier, reason, effective_from, effective_to, successor_security_id}], "receipt": ...}` where the receipt is Task 8's; in this task return `{"candidates": [...], "manifest": {...}}`; `lineage(settings, ctx, relation: str, assertion_id: str) -> dict` → `{"chain": [{"kind": "assertion"|"capture"|"request"|"run", ...row}]}`; `compare(settings, relation: str, selection: dict, left: Context, right: Context) -> dict` → `{"differences": [{key, field, left, right, classification}], "left_manifest", "right_manifest"}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_identity.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import parse_context
from security_master_api.resolver.identity import resolve
from security_master_api.store.seed import seed


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return s


def one(settings, identifier, raw_ctx, **kw):
    out = resolve(settings, parse_context(raw_ctx), identifier, **kw)
    assert len(out["candidates"]) == 1, out
    return out["candidates"][0]


def test_cusip_resolves_by_effective_date(settings):
    old = one(settings, "000111AA1", {"mode": "effective_on", "effective_at": "2026-06-30T00:00:00Z"})
    assert (old["security_id"], old["reason"]) == ("sec_100", "active")
    new = one(settings, "000222BB2", {"mode": "effective_on", "effective_at": "2026-07-02T00:00:00Z"})
    assert (new["security_id"], new["reason"]) == ("sec_101", "active")


def test_old_cusip_without_a_date_is_historical_not_current(settings):
    c = one(settings, "000111AA1", None)
    assert c["reason"] == "historical_alias"
    assert c["security_id"] == "sec_100"
    assert c["successor_security_id"] == "sec_101"


def test_ticker_change_keeps_the_listing(settings):
    fb = one(settings, "FB", {"mode": "effective_on", "effective_at": "2022-06-07T00:00:00Z"})
    meta = one(settings, "META", {"mode": "effective_on", "effective_at": "2022-06-10T00:00:00Z"})
    assert fb["listing_id"] == meta["listing_id"] == "lst_000042"
    assert fb["instrument_id"] == meta["instrument_id"] == "ins_000042"
    assert (fb["symbol"], meta["symbol"]) == ("FB", "META")
    current = one(settings, "FB", None)
    assert current["reason"] == "historical_alias" and current["symbol"] == "META"


def test_unresolved_and_ambiguous(settings):
    with pytest.raises(DomainError) as exc:
        resolve(settings, parse_context(None), "ZZZZ")
    assert exc.value.code == "IDENTITY_UNRESOLVED"
    out = resolve(settings, parse_context(None), "FB", include_historical=False)
    assert out["candidates"] == [] or all(c["reason"] != "historical_alias" for c in out["candidates"])
```

Ambiguity is exercised by seeding two current `ticker` rows for `"DUAL"` on different listings in `tests/conftest.py` via `append(settings, "silver.identifiers", ...)` and asserting `IDENTITY_AMBIGUOUS` with two `details["candidates"]`.

`tests/test_lineage_compare.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.resolver.compare import compare
from security_master_api.resolver.context import parse_context
from security_master_api.resolver.lineage import lineage
from security_master_api.store.seed import seed


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return s


def test_lineage_reaches_the_corrected_capture(settings):
    chain = lineage(settings, parse_context(None), "silver.prices_normalized", "as_px_apple_20260914_2")["chain"]
    kinds = [c["kind"] for c in chain]
    assert kinds[0] == "assertion" and "capture" in kinds
    capture = next(c for c in chain if c["kind"] == "capture")
    assert capture["capture_id"] == "cap_00489"
    assert chain[0]["supersedes_assertion_id"] == "as_px_apple_20260914_1"


def test_compare_classifies_a_correction(settings):
    left = parse_context({"mode": "known_at", "known_at": "2026-09-14T20:05:00Z"})
    right = parse_context({"mode": "known_at", "known_at": "2026-09-15T09:20:00Z"})
    out = compare(settings, "gold.price_daily",
                  {"listing_id": "lst_apple", "market_date": "2026-09-14"}, left, right)
    close = next(d for d in out["differences"] if d["field"] == "close")
    assert (close["left"], close["right"], close["classification"]) == (231.40, 231.74, "corrected")


def test_compare_classifies_newly_known(settings):
    left = parse_context({"mode": "known_at", "known_at": "2026-07-15T00:00:00Z"})
    right = parse_context({"mode": "known_at", "known_at": "2026-08-05T12:00:00Z"})
    out = compare(settings, "gold.odp_equity_info", {"symbol": "EXMP"}, left, right)
    so = next(d for d in out["differences"] if d["field"] == "shares_outstanding")
    assert so["classification"] == "newly_known"


def test_compare_classifies_identity_successor(settings):
    left = parse_context({"mode": "effective_on", "effective_at": "2026-06-30T00:00:00Z"})
    right = parse_context({"mode": "effective_on", "effective_at": "2026-07-02T00:00:00Z"})
    out = compare(settings, "gold.security_master", {"issuer_id": "iss_042"}, left, right)
    sec = next(d for d in out["differences"] if d["field"] == "security_id")
    assert sec["classification"] == "identity_successor"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --extra dev pytest -q tests/test_identity.py tests/test_lineage_compare.py`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write `resolver/identity.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Resolve an external identifier to stable ids under a context. Never guesses."""

from __future__ import annotations

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import Context
from security_master_api.sql.session import open_session

_TYPES = ("ticker", "eodhd_symbol", "cusip", "isin", "figi", "cik", "listing_id",
          "instrument_id", "security_id", "issuer_id")


def _guess_type(identifier: str) -> str | None:
    prefix = identifier.split("_", 1)[0] + "_"
    return {"lst_": "listing_id", "ins_": "instrument_id", "sec_": "security_id",
            "iss_": "issuer_id"}.get(prefix)


def resolve(settings: Settings, ctx: Context, identifier: str, identifier_type: str | None = None,
            include_historical: bool = True) -> dict:
    ident = identifier.strip()
    if not ident:
        raise DomainError("QUERY_REJECTED", "identifier is required")
    kind = identifier_type or _guess_type(ident)
    if kind is not None and kind not in _TYPES:
        raise DomainError("QUERY_REJECTED", f"unknown identifier_type {kind!r}",
                          {"supported": list(_TYPES)})
    with open_session(settings, ctx, ["gold.security_master", "silver.identifiers"]) as s:
        # Active under the context first.
        if kind in ("listing_id", "instrument_id", "security_id", "issuer_id"):
            active = s.run(f"SELECT *, 'active' AS reason, NULL AS identifier_type, "
                           f"NULL AS identifier FROM gold.security_master WHERE {kind} = ?",
                           [ident]).to_pylist()
            historical = []
        else:
            type_clause = "AND i.identifier_type = ?" if kind else ""
            params = [ident] + ([kind] if kind else [])
            active = s.run(
                "SELECT m.*, i.identifier_type, i.identifier, 'active' AS reason "
                "FROM silver.identifiers i JOIN gold.security_master m "
                "ON coalesce(i.listing_id, m.listing_id) = m.listing_id "
                f"WHERE i.identifier = ? {type_clause}", params).to_pylist()
            historical = []
            if not active and include_historical:
                # Every row ever asserted for this identifier, latest first, in
                # the unfiltered physical table: the alias is closed, the id is stable.
                historical = s.run(
                    "SELECT i.identifier_type, i.identifier, i.listing_id, i.instrument_id, "
                    "i.security_id, i.issuer_id, i.effective_from, i.effective_to "
                    "FROM silver.identifiers i "
                    f"WHERE i.identifier = ? {type_clause} AND i.effective_to IS NOT NULL "
                    "QUALIFY row_number() OVER (PARTITION BY i.identifier_type, i.identifier, "
                    "coalesce(i.listing_id, i.security_id) ORDER BY i.system_from DESC) = 1",
                    params).to_pylist()
        candidates = [_candidate(row) for row in active]
        for row in historical:
            current = s.run(
                "SELECT * FROM gold.security_master WHERE listing_id = ? OR security_id = ? LIMIT 1",
                [row.get("listing_id"), row.get("security_id")]).to_pylist()
            merged = {**(current[0] if current else {}), **{k: v for k, v in row.items() if v is not None}}
            merged["reason"] = "historical_alias"
            if current and row.get("security_id") and current[0].get("security_id") != row["security_id"]:
                merged["reason"] = "historical_alias"
            candidates.append(_candidate(merged))
    if not candidates:
        raise DomainError("IDENTITY_UNRESOLVED", f"{ident} resolves to no local security",
                          {"identifier": ident})
    stable = {(c["listing_id"], c["security_id"]) for c in candidates}
    if len(stable) > 1 and ctx.effective_at is None:
        raise DomainError("IDENTITY_AMBIGUOUS", f"{ident} matches {len(stable)} securities",
                          {"identifier": ident, "candidates": candidates})
    return {"candidates": candidates}


def _candidate(row: dict) -> dict:
    keys = ("listing_id", "instrument_id", "issuer_id", "security_id", "symbol", "name",
            "identifier_type", "identifier", "reason", "effective_from", "effective_to",
            "successor_security_id", "predecessor_security_id", "assertion_id", "capture_id")
    out = {k: row.get(k) for k in keys}
    for k in ("effective_from", "effective_to"):
        if out[k] is not None and hasattr(out[k], "isoformat"):
            out[k] = out[k].isoformat().replace("+00:00", "Z")
    return out
```

For the CUSIP fixture the `gold.security_master` row for `sec_100` is closed under `current_corrected`, so the historical branch finds the identifier row and merges it with the successor's current row: `security_id` stays `sec_100` (the row's own value wins over the merge) and `successor_security_id` comes from `silver.securities` through the current listing. If the merge order in `merged` leaves `security_id` as `sec_101`, swap the dict order so the identifier row's fields win.

- [ ] **Step 4: Write `resolver/lineage.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""From an assertion to its capture, request and run: the provenance chain."""

from __future__ import annotations

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import Context, parse_context
from security_master_api.sql.session import open_session


def _plain(row: dict, kind: str) -> dict:
    out = {"kind": kind}
    for k, v in row.items():
        out[k] = v.isoformat().replace("+00:00", "Z") if hasattr(v, "isoformat") else v
    return out


def lineage(settings: Settings, ctx: Context, relation: str, assertion_id: str) -> dict:
    if not relation.startswith("silver."):
        raise DomainError("QUERY_REJECTED", "lineage starts from a Silver assertion",
                          {"relation": relation})
    snap = parse_context({"mode": "delta_snapshot", "versions": ctx.versions})
    chain: list[dict] = []
    with open_session(settings, snap, [relation, "bronze.source_captures", "bronze.request_log",
                                       "bronze.ingestion_runs"]) as s:
        layer, name = relation.split(".", 1)
        rows = s.run(f'SELECT * FROM {layer}."{name}" WHERE assertion_id = ? '
                     "ORDER BY system_from DESC", [assertion_id]).to_pylist()
        if not rows:
            raise DomainError("NO_MATCHING_FACTS", f"no assertion {assertion_id} in {relation}",
                              {"relation": relation, "assertion_id": assertion_id})
        chain.append(_plain(rows[0], "assertion"))
        capture_id = rows[0].get("capture_id")
        if capture_id:
            caps = s.run("SELECT * FROM bronze.source_captures WHERE capture_id = ?",
                         [capture_id]).to_pylist()
            for cap in caps:
                cap = dict(cap)
                cap.pop("payload", None)
                chain.append(_plain(cap, "capture"))
            for req in s.run("SELECT * FROM bronze.request_log WHERE capture_id = ?",
                             [capture_id]).to_pylist():
                chain.append(_plain(req, "request"))
                if req.get("job_id"):
                    for run in s.run("SELECT * FROM bronze.ingestion_runs WHERE job_id = ?",
                                     [req["job_id"]]).to_pylist():
                        chain.append(_plain(run, "run"))
    return {"chain": chain}
```

- [ ] **Step 5: Write `resolver/compare.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The same selection under two contexts, diffed field by field and classified."""

from __future__ import annotations

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.catalog import relation as catalog_relation
from security_master_api.resolver.context import Context
from security_master_api.resolver.manifest import resolve_manifest
from security_master_api.sql.session import open_session

CLASSES = ("corrected", "newly_known", "became_effective", "expired", "superseded",
           "identity_successor", "unresolved")
_IDENTITY_FIELDS = {"security_id", "listing_id", "instrument_id", "issuer_id"}
_PROVENANCE = {"assertion_id", "capture_id", "available_at", "system_from", "known_from",
               "known_to", "holiday_assertion_id", "market_assertion_id",
               "supersedes_assertion_id"}


def _select(settings: Settings, relation: str, selection: dict, ctx: Context) -> list[dict]:
    if not selection:
        raise DomainError("QUERY_REJECTED", "compare needs a non-empty selection")
    layer, name = relation.split(".", 1)
    where = " AND ".join(f'"{k}" = ?' for k in selection)
    with open_session(settings, ctx, [relation]) as s:
        return s.run(f'SELECT * FROM {layer}."{name}" WHERE {where} ORDER BY 1',
                     list(selection.values())).to_pylist()


def _classify(field: str, left: dict | None, right: dict | None, l, r) -> str:
    if left is None and right is not None:
        return "newly_known" if r is not None else "unresolved"
    if left is not None and right is None:
        return "expired"
    if field in _IDENTITY_FIELDS and l is not None and r is not None and l != r:
        return "identity_successor"
    if l is None and r is not None:
        return "newly_known"
    if l is not None and r is None:
        return "expired"
    if right and right.get("supersedes_assertion_id") and right.get("supersedes_assertion_id") == (left or {}).get("assertion_id"):
        return "corrected"
    if (left or {}).get("assertion_id") != (right or {}).get("assertion_id"):
        return "corrected" if (right or {}).get("available_at") != (left or {}).get("available_at") else "superseded"
    return "became_effective"


def compare(settings: Settings, relation: str, selection: dict, left: Context, right: Context) -> dict:
    rel = catalog_relation(settings, relation)
    keys = list(rel.keys)
    lrows = {tuple(str(r.get(k)) for k in keys): r for r in _select(settings, relation, selection, left)}
    rrows = {tuple(str(r.get(k)) for k in keys): r for r in _select(settings, relation, selection, right)}
    differences = []
    for key in sorted(set(lrows) | set(rrows)):
        lrow, rrow = lrows.get(key), rrows.get(key)
        fields = sorted((set(lrow or {}) | set(rrow or {})) - _PROVENANCE)
        for field in fields:
            l = (lrow or {}).get(field)
            r = (rrow or {}).get(field)
            if l == r:
                continue
            differences.append({
                "key": dict(zip(keys, key, strict=True)), "field": field,
                "left": _json(l), "right": _json(r),
                "classification": _classify(field, lrow, rrow, l, r),
            })
    return {
        "differences": differences,
        "left_manifest": resolve_manifest(settings, left, [relation]),
        "right_manifest": resolve_manifest(settings, right, [relation]),
    }


def _json(v):
    return v.isoformat().replace("+00:00", "Z") if hasattr(v, "isoformat") else v
```

For the CUSIP comparison, `gold.security_master` keyed by `listing_id` yields one row whose `security_id` changes from `sec_100` to `sec_101`, which `_classify` labels `identity_successor` because `security_id` is an identity field.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check .`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add security-master-api
git commit -m "feat(security-master): identity resolution, lineage chains and temporal comparison"
```

### Task 8: ODP registry, projections, the pandas calendar adapter, receipts

**Files:**
- Create: `security_master_api/odp_registry.json`, `security_master_api/resolver/odp.py`, `resolver/calendar.py`, `security_master_api/store/receipts.py`
- Test: `tests/test_odp.py`, `tests/test_calendar_adapter.py`, `tests/test_receipts.py`

**Interfaces:**
- Produces: `registry() -> list[dict]`; `model(model_id) -> dict`; `project(settings, ctx, model_id, params: dict, request_id: str) -> dict` → `{"results": [rows], "columns": [{name, dtype}], "receipt": {...}}`; `new_receipt(settings, kind, ctx, manifest, request_id, sql_fingerprint="") -> dict`; `record_receipt(settings, receipt) -> None`; `generate_sessions(calendar_alias: str, start: date, end: date, calendar_id: str, rule_id: str) -> list[dict]` (rows for `silver.market_sessions`); `adapter_version() -> str`.
- Receipt shape: `{execution_id, request_id, kind, temporal_mode, effective_at, known_at, availability_policy, projection_version, dependencies: [{relation, delta_version}], sql_fingerprint, created_at}`; `execution_id` = `"qry_" + uuid4().hex[:16]`; `dependencies` sorted by relation.

- [ ] **Step 1: Write `odp_registry.json`**

```json
{
  "schema": "security-master.odp-registry.v1",
  "odp_version": "openbb==4.7.2",
  "projection_version": "security-master-projection/1",
  "models": [
    {"model_id": "EquityInfo", "route": "/api/v1/equity/profile", "kind": "standard",
     "backing_relation": "gold.odp_equity_info",
     "parameters": [{"name": "symbol", "type": "string", "required": true, "description": "Ticker or historical alias"}],
     "fields": [
       {"name": "symbol", "type": "string", "source": "gold.odp_equity_info.symbol"},
       {"name": "name", "type": "string", "source": "gold.odp_equity_info.name"},
       {"name": "shares_outstanding", "type": "number", "source": "gold.odp_equity_info.shares_outstanding"},
       {"name": "period_end", "type": "date", "source": "gold.odp_equity_info.period_end"},
       {"name": "filing_kind", "type": "string", "source": "gold.odp_equity_info.filing_kind"},
       {"name": "currency", "type": "string", "source": "gold.odp_equity_info.currency"},
       {"name": "mic", "type": "string", "source": "gold.odp_equity_info.mic"}
     ],
     "temporal_capabilities": ["current_corrected", "effective_on", "known_at"],
     "primary_temporal_scenario": "shares_outstanding",
     "consumers": ["bdobb-v2 security master browser", "obb.equity.profile"],
     "sql": "SELECT symbol, name, shares_outstanding, period_end, filing_kind, currency, mic, listing_id, assertion_id, capture_id, shares_assertion_id, shares_capture_id FROM gold.odp_equity_info WHERE listing_id = $listing_id",
     "resolve": "symbol"},
    {"model_id": "EquitySearch", "route": "/api/v1/equity/search", "kind": "standard",
     "backing_relation": "gold.security_master",
     "parameters": [{"name": "query", "type": "string", "required": true, "description": "Symbol or name fragment"}],
     "fields": [
       {"name": "symbol", "type": "string", "source": "gold.security_master.symbol"},
       {"name": "name", "type": "string", "source": "gold.security_master.name"},
       {"name": "listing_id", "type": "string", "source": "gold.security_master.listing_id"},
       {"name": "mic", "type": "string", "source": "gold.security_master.mic"}
     ],
     "temporal_capabilities": ["current_corrected", "effective_on"],
     "primary_temporal_scenario": "ticker_change",
     "consumers": ["bdobb-v2 security master browser", "obb.equity.search"],
     "sql": "SELECT symbol, name, listing_id, mic, instrument_id, assertion_id, capture_id FROM gold.security_master WHERE upper(symbol) LIKE upper($query) || '%' OR upper(name) LIKE '%' || upper($query) || '%' ORDER BY symbol LIMIT 100"},
    {"model_id": "EquityHistorical", "route": "/api/v1/equity/price/historical", "kind": "standard",
     "backing_relation": "gold.price_daily",
     "parameters": [
       {"name": "symbol", "type": "string", "required": true},
       {"name": "start_date", "type": "date", "required": true},
       {"name": "end_date", "type": "date", "required": true}
     ],
     "fields": [
       {"name": "date", "type": "date", "source": "gold.price_daily.market_date"},
       {"name": "open", "type": "number", "source": "gold.price_daily.open"},
       {"name": "high", "type": "number", "source": "gold.price_daily.high"},
       {"name": "low", "type": "number", "source": "gold.price_daily.low"},
       {"name": "close", "type": "number", "source": "gold.price_daily.close"},
       {"name": "volume", "type": "number", "source": "gold.price_daily.volume"}
     ],
     "temporal_capabilities": ["current_corrected", "known_at", "delta_snapshot"],
     "primary_temporal_scenario": "trade_correction",
     "consumers": ["bdobb-v2 security master browser", "obb.equity.price.historical"],
     "sql": "SELECT market_date AS date, open, high, low, close, volume, listing_id, assertion_id, capture_id, supersedes_assertion_id FROM gold.price_daily WHERE listing_id = $listing_id AND market_date BETWEEN $start_date AND $end_date ORDER BY market_date",
     "resolve": "symbol"},
    {"model_id": "ReferenceSecurity", "route": "/api/v1/reference/security", "kind": "custom",
     "backing_relation": "gold.security_master",
     "parameters": [{"name": "identifier", "type": "string", "required": true, "description": "CUSIP, ISIN, FIGI, ticker or internal id"}],
     "fields": [
       {"name": "security_id", "type": "string", "source": "gold.security_master.security_id"},
       {"name": "cusip", "type": "string", "source": "gold.security_master.cusip"},
       {"name": "issuer_id", "type": "string", "source": "gold.security_master.issuer_id"},
       {"name": "successor_security_id", "type": "string", "source": "gold.security_master.successor_security_id"},
       {"name": "predecessor_security_id", "type": "string", "source": "gold.security_master.predecessor_security_id"}
     ],
     "temporal_capabilities": ["current_corrected", "effective_on", "known_at"],
     "primary_temporal_scenario": "cusip_change",
     "consumers": ["bdobb-v2 security master browser"],
     "sql": "SELECT security_id, cusip, issuer_id, successor_security_id, predecessor_security_id, listing_id, symbol, assertion_id, capture_id FROM gold.security_master WHERE listing_id = $listing_id",
     "resolve": "identifier"},
    {"model_id": "ReferenceResolve", "route": "/api/v1/reference/resolve", "kind": "custom",
     "backing_relation": "silver.identifiers",
     "parameters": [{"name": "identifier", "type": "string", "required": true}, {"name": "identifier_type", "type": "string", "required": false}],
     "fields": [
       {"name": "listing_id", "type": "string", "source": "resolve.listing_id"},
       {"name": "instrument_id", "type": "string", "source": "resolve.instrument_id"},
       {"name": "security_id", "type": "string", "source": "resolve.security_id"},
       {"name": "symbol", "type": "string", "source": "resolve.symbol"},
       {"name": "reason", "type": "string", "source": "resolve.reason"}
     ],
     "temporal_capabilities": ["current_corrected", "effective_on", "known_at"],
     "primary_temporal_scenario": "ticker_change",
     "consumers": ["bdobb-v2 security master browser", "obb.reference.security_master_resolve"],
     "resolver": true},
    {"model_id": "MarketCalendar", "route": "/api/v1/reference/market-calendar", "kind": "custom",
     "backing_relation": "gold.market_calendar",
     "parameters": [
       {"name": "calendar_id", "type": "string", "required": false, "description": "Stable internal calendar identifier"},
       {"name": "exchange", "type": "string", "required": false, "description": "Exchange name or pandas alias"},
       {"name": "mic", "type": "string", "required": false},
       {"name": "start_date", "type": "date", "required": true},
       {"name": "end_date", "type": "date", "required": true},
       {"name": "include_closed", "type": "boolean", "required": false},
       {"name": "include_breaks", "type": "boolean", "required": false},
       {"name": "include_interruptions", "type": "boolean", "required": false},
       {"name": "session_label", "type": "string", "required": false},
       {"name": "timezone", "type": "string", "required": false},
       {"name": "as_of", "type": "date", "required": false},
       {"name": "knowledge_at", "type": "datetime", "required": false},
       {"name": "provider", "type": "string", "required": false}
     ],
     "fields": [
       {"name": "calendar_id", "type": "string", "source": "gold.market_calendar.calendar_id"},
       {"name": "exchange_id", "type": "string", "source": "gold.market_calendar.exchange_id"},
       {"name": "exchange_name", "type": "string", "source": "gold.market_calendar.exchange_name"},
       {"name": "mic", "type": "string", "source": "gold.market_calendar.mic"},
       {"name": "calendar_alias", "type": "string", "source": "gold.market_calendar.calendar_alias"},
       {"name": "session_date", "type": "date", "source": "gold.market_calendar.session_date"},
       {"name": "trade_date", "type": "date", "source": "gold.market_calendar.trade_date"},
       {"name": "timezone", "type": "string", "source": "gold.market_calendar.timezone"},
       {"name": "session_status", "type": "string", "source": "gold.market_calendar.session_status"},
       {"name": "calendar_system", "type": "string", "source": "gold.market_calendar.calendar_system"},
       {"name": "holiday_family", "type": "string", "source": "gold.market_calendar.holiday_family"},
       {"name": "assertion_domain", "type": "string", "source": "gold.market_calendar.assertion_domain"},
       {"name": "evidence_status", "type": "string", "source": "gold.market_calendar.evidence_status"},
       {"name": "market_open", "type": "datetime", "source": "gold.market_calendar.market_open"},
       {"name": "market_close", "type": "datetime", "source": "gold.market_calendar.market_close"},
       {"name": "break_start", "type": "datetime", "source": "gold.market_calendar.break_start"},
       {"name": "break_end", "type": "datetime", "source": "gold.market_calendar.break_end"},
       {"name": "interruptions", "type": "array", "source": "silver.session_interruptions"},
       {"name": "holiday_name", "type": "string", "source": "gold.market_calendar.holiday_name"},
       {"name": "special_open", "type": "boolean", "source": "gold.market_calendar.special_open"},
       {"name": "special_close", "type": "boolean", "source": "gold.market_calendar.special_close"},
       {"name": "market_effect", "type": "string", "source": "gold.market_calendar.market_effect"},
       {"name": "authority_type", "type": "string", "source": "gold.market_calendar.authority_type"},
       {"name": "authority_name", "type": "string", "source": "gold.market_calendar.authority_name"},
       {"name": "authority_verified_at", "type": "datetime", "source": "gold.market_calendar.authority_verified_at"},
       {"name": "source_kind", "type": "string", "source": "gold.market_calendar.source_kind"},
       {"name": "source_version", "type": "string", "source": "gold.market_calendar.source_version"},
       {"name": "rule_id", "type": "string", "source": "gold.market_calendar.rule_id"},
       {"name": "supersedes_assertion_id", "type": "string", "source": "gold.market_calendar.supersedes_assertion_id"},
       {"name": "effective_from", "type": "datetime", "source": "gold.market_calendar.effective_from"},
       {"name": "effective_to", "type": "datetime", "source": "gold.market_calendar.effective_to"},
       {"name": "known_from", "type": "datetime", "source": "gold.market_calendar.known_from"},
       {"name": "known_to", "type": "datetime", "source": "gold.market_calendar.known_to"},
       {"name": "capture_id", "type": "string", "source": "gold.market_calendar.capture_id"}
     ],
     "temporal_capabilities": ["current_corrected", "effective_on", "known_at", "delta_snapshot"],
     "primary_temporal_scenario": "lunar_holiday_correction",
     "consumers": ["bdobb-v2 security master browser", "obb.reference.market_calendar", "price download preflight"],
     "sql": "SELECT * EXCLUDE (candidate_branches, selected_branch) , candidate_branches, selected_branch FROM gold.market_calendar WHERE calendar_id = $calendar_id AND session_date BETWEEN $start_date AND $end_date AND ($include_closed OR market_effect <> 'closed') ORDER BY session_date"}
  ]
}
```

- [ ] **Step 2: Write the failing tests**

`tests/test_receipts.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.resolver.context import parse_context
from security_master_api.store.receipts import new_receipt, record_receipt
from security_master_api.store.seed import seed
from security_master_api.store.tables import open_table


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return s


def test_receipt_shape_and_ordering(settings):
    ctx = parse_context({"mode": "known_at", "known_at": "2026-09-14T20:00:00Z"})
    r = new_receipt(settings, "preview", ctx, {"silver.listings": 2, "bronze.source_captures": 1}, "req_1", "abc")
    assert r["execution_id"].startswith("qry_")
    assert r["dependencies"] == [{"relation": "bronze.source_captures", "delta_version": 1},
                                 {"relation": "silver.listings", "delta_version": 2}]
    assert r["temporal_mode"] == "known_at" and r["known_at"] == "2026-09-14T20:00:00Z"
    assert r["projection_version"] == "security-master-projection/1"
    assert r["availability_policy"] == "local_as_ingested"
    assert set(r) == {"execution_id", "request_id", "kind", "temporal_mode", "effective_at",
                      "known_at", "availability_policy", "projection_version", "dependencies",
                      "sql_fingerprint", "created_at"}


def test_record_appends_one_row(settings):
    ctx = parse_context(None)
    r = new_receipt(settings, "sql", ctx, {"silver.listings": 1}, "req_2")
    record_receipt(settings, r)
    rows = open_table(settings, "ops.receipts").to_pyarrow_table().to_pylist()
    assert [x["execution_id"] for x in rows] == [r["execution_id"]]
```

`tests/test_calendar_adapter.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
from datetime import date

from security_master_api.resolver.calendar import adapter_version, generate_sessions


def test_nyse_sessions_carry_provenance_and_skip_holidays():
    rows = generate_sessions("NYSE", date(2026, 12, 20), date(2027, 1, 5), "cal_xnys", "rule_xnys_1")
    dates = {str(r["session_date"]) for r in rows}
    assert "2026-12-25" not in dates and "2027-01-01" not in dates
    assert "2026-12-24" in dates
    row = next(r for r in rows if str(r["session_date"]) == "2026-12-24")
    assert row["rule_id"] == "rule_xnys_1"
    assert row["source_version"] == adapter_version()
    assert row["assertion_status"] == "current"
    assert row["market_close"].hour == 18  # 13:00 New York in UTC
    assert row["assertion_id"] == "as_sess_cal_xnys_2026-12-24"


def test_adapter_version_names_the_package():
    assert adapter_version().startswith("pandas_market_calendars/")
```

`tests/test_odp.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import parse_context
from security_master_api.resolver.odp import model, project, registry
from security_master_api.store.seed import golden_fixtures, seed


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return s


def test_registry_has_the_six_models_and_scenarios():
    models = {m["model_id"]: m for m in registry()}
    assert set(models) == {"EquityInfo", "EquitySearch", "EquityHistorical", "ReferenceSecurity",
                           "ReferenceResolve", "MarketCalendar"}
    assert models["MarketCalendar"]["primary_temporal_scenario"] == "lunar_holiday_correction"
    assert models["MarketCalendar"]["kind"] == "custom"
    assert len(models["MarketCalendar"]["fields"]) == 34
    assert model("EquityInfo")["route"] == "/api/v1/equity/profile"


def test_unknown_model_and_unsupported_mode(settings):
    with pytest.raises(DomainError) as exc:
        model("Nope")
    assert exc.value.code == "QUERY_REJECTED"
    ctx = parse_context({"mode": "known_at", "known_at": "2026-01-01T00:00:00Z"})
    with pytest.raises(DomainError) as exc:
        project(settings, ctx, "EquitySearch", {"query": "A"}, "req")
    assert exc.value.code == "TEMPORAL_MODE_UNSUPPORTED"


def test_missing_required_parameter_fails_before_duckdb(settings):
    with pytest.raises(DomainError) as exc:
        project(settings, parse_context(None), "EquityHistorical", {"symbol": "AAPL"}, "req")
    assert exc.value.code == "QUERY_REJECTED" and "start_date" in exc.value.message


def test_every_fixture_expectation_holds(settings):
    for fixture in golden_fixtures():
        for exp in fixture["expectations"]:
            if "model" not in exp:
                continue
            ctx = parse_context(exp["context"])
            if "error" in exp:
                with pytest.raises(DomainError) as exc:
                    project(settings, ctx, exp["model"], exp["params"], "req")
                assert exc.value.code == exp["error"], (fixture["fixture_id"], exp)
                continue
            out = project(settings, ctx, exp["model"], exp["params"], "req")
            rows = out["results"]
            if "row_count" in exp["expect"]:
                assert len(rows) == exp["expect"]["row_count"], (fixture["fixture_id"], exp)
                continue
            want = exp["expect"]
            if exp["model"] == "MarketCalendar":
                by_date = {str(r["session_date"]): r for r in rows}
                target = next(k for k in want if k == "session_date") if "session_date" in want else None
                row = by_date[exp["row_date"]] if "row_date" in exp else rows[0]
            else:
                row = rows[0]
            for key, value in want.items():
                assert json.loads(json.dumps(row[key], default=str)) == value, (fixture["fixture_id"], exp, key)
            assert out["receipt"]["temporal_mode"] == ctx.mode


def test_market_calendar_projection_and_receipt(settings):
    ctx = parse_context({"mode": "known_at", "known_at": "2027-03-10T08:00:00Z",
                         "effective_at": "2027-04-01T00:00:00Z"})
    out = project(settings, ctx, "MarketCalendar",
                  {"calendar_id": "cal_tadawul", "start_date": "2027-03-01", "end_date": "2027-04-30",
                   "include_closed": True}, "req")
    row = next(r for r in out["results"] if str(r["session_date"]) == "2027-03-11")
    assert row["market_effect"] == "closed" and row["evidence_status"] == "market_final"
    assert row["interruptions"] == []
    assert any(d["relation"] == "silver.calendar_exceptions" for d in out["receipt"]["dependencies"])


def test_market_calendar_needs_an_unambiguous_calendar(settings):
    with pytest.raises(DomainError) as exc:
        project(settings, parse_context(None), "MarketCalendar",
                {"start_date": "2027-03-01", "end_date": "2027-03-02"}, "req")
    assert exc.value.code == "IDENTITY_AMBIGUOUS"
    out = project(settings, parse_context(None), "MarketCalendar",
                  {"mic": "XSAU", "start_date": "2027-03-01", "end_date": "2027-03-31", "include_closed": True}, "req")
    assert out["results"]
```

For the calendar expectations in the Task 3 fixtures, add `"row_date"` next to each `MarketCalendar` expectation naming the session date the `expect` block describes (`"row_date": "2027-03-11"` and so on), and for the "no `2027-03-10` holiday row" assertion express it as `{"row_date": "2027-03-10", "expect": {"holiday_family": null}}` or omit it (the golden calendar tests already cover it).

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run --extra dev pytest -q tests/test_receipts.py tests/test_calendar_adapter.py tests/test_odp.py`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Write `store/receipts.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Immutable receipts: what a result depended on. Written before the response returns."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from security_master_api.config import Settings
from security_master_api.resolver.context import Context, _iso
from security_master_api.store.tables import append


def new_receipt(settings: Settings, kind: str, ctx: Context, manifest: dict[str, int],
                request_id: str, sql_fingerprint: str = "") -> dict:
    return {
        "execution_id": "qry_" + uuid.uuid4().hex[:16],
        "request_id": request_id,
        "kind": kind,
        "temporal_mode": ctx.mode,
        "effective_at": _iso(ctx.effective_at),
        "known_at": _iso(ctx.known_at),
        "availability_policy": ctx.availability_policy,
        "projection_version": settings.projection_version,
        "dependencies": [{"relation": r, "delta_version": v} for r, v in sorted(manifest.items())],
        "sql_fingerprint": sql_fingerprint,
        "created_at": _iso(datetime.now(UTC)),
    }


def record_receipt(settings: Settings, receipt: dict) -> None:
    row = dict(receipt)
    row["dependencies"] = json.dumps(receipt["dependencies"])
    append(settings, "ops.receipts", [row])
```

- [ ] **Step 5: Write `resolver/calendar.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The pinned pandas_market_calendars adapter: deterministic baseline sessions with provenance."""

from __future__ import annotations

from datetime import UTC, date, datetime
from importlib.metadata import version

import pandas_market_calendars as mcal


def adapter_version() -> str:
    return f"pandas_market_calendars/{version('pandas_market_calendars')}"


def generate_sessions(calendar_alias: str, start: date, end: date, calendar_id: str,
                      rule_id: str, generated_at: datetime | None = None) -> list[dict]:
    cal = mcal.get_calendar(calendar_alias)
    schedule = cal.schedule(start_date=str(start), end_date=str(end))
    now = generated_at or datetime.now(UTC)
    rows = []
    for session, row in schedule.iterrows():
        session_date = session.date()
        rows.append({
            "calendar_id": calendar_id,
            "session_date": session_date,
            "trade_date": session_date,
            "market_open": row["market_open"].to_pydatetime().astimezone(UTC),
            "market_close": row["market_close"].to_pydatetime().astimezone(UTC),
            "break_start": _opt(row.get("break_start")),
            "break_end": _opt(row.get("break_end")),
            "rule_id": rule_id,
            "source_version": adapter_version(),
            "effective_from": datetime.combine(session_date, datetime.min.time(), tzinfo=UTC),
            "effective_to": None,
            "observed_at": now,
            "available_at": now,
            "system_from": now,
            "system_to": None,
            "capture_id": None,
            "assertion_id": f"as_sess_{calendar_id}_{session_date.isoformat()}",
            "assertion_status": "current",
            "supersedes_assertion_id": None,
        })
    return rows


def _opt(value):
    if value is None or (hasattr(value, "__class__") and str(value) == "NaT"):
        return None
    return value.to_pydatetime().astimezone(UTC)
```

- [ ] **Step 6: Write `resolver/odp.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The ODP model registry and its projections. One resolver behind the browser and obb.*."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from functools import cache
from pathlib import Path

import security_master_api
from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.catalog import check_modes
from security_master_api.resolver.context import Context
from security_master_api.resolver.identity import resolve
from security_master_api.sql.session import open_session
from security_master_api.store.receipts import new_receipt, record_receipt

REGISTRY_PATH = Path(security_master_api.__file__).parent / "odp_registry.json"


@cache
def _load() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def registry() -> list[dict]:
    return _load()["models"]


def odp_version() -> str:
    return _load()["odp_version"]


def model(model_id: str) -> dict:
    for m in registry():
        if m["model_id"] == model_id:
            return m
    raise DomainError("QUERY_REJECTED", f"unknown ODP model {model_id}", {"model_id": model_id})


def _coerce(param: dict, value):
    kind = param["type"]
    try:
        if kind == "date":
            return date.fromisoformat(str(value))
        if kind == "boolean":
            if value in (True, False):
                return value
            raise ValueError(value)
        return str(value)
    except ValueError as exc:
        raise DomainError("QUERY_REJECTED", f"parameter {param['name']} is not a {kind}",
                          {"parameter": param["name"]}) from exc


def _bind(m: dict, params: dict) -> dict:
    bound = {}
    declared = {p["name"]: p for p in m["parameters"]}
    unknown = set(params) - set(declared)
    if unknown:
        raise DomainError("QUERY_REJECTED", f"unknown parameter(s): {sorted(unknown)}")
    for name, p in declared.items():
        if name in params and params[name] is not None:
            bound[name] = _coerce(p, params[name])
        elif p.get("required"):
            raise DomainError("QUERY_REJECTED", f"parameter {name} is required", {"parameter": name})
        else:
            bound[name] = False if p["type"] == "boolean" else None
    return bound


def _calendar_id(settings: Settings, ctx: Context, bound: dict) -> str:
    if bound.get("calendar_id"):
        return bound["calendar_id"]
    with open_session(settings, ctx, ["silver.exchanges"]) as s:
        clauses, values = [], []
        for key in ("mic", "exchange"):
            if bound.get(key):
                col = "mic" if key == "mic" else "coalesce(calendar_alias, name)"
                clauses.append(f"upper({col}) = upper(?)")
                values.append(bound[key])
        if not clauses:
            raise DomainError("IDENTITY_AMBIGUOUS", "calendar_id, mic or exchange is required",
                              {"parameter": "calendar_id"})
        rows = s.run(f"SELECT DISTINCT calendar_id FROM silver.exchanges WHERE {' OR '.join(clauses)}",
                     values).to_pylist()
    if len(rows) != 1:
        raise DomainError("IDENTITY_AMBIGUOUS" if rows else "IDENTITY_UNRESOLVED",
                          "exchange resolves to " + (f"{len(rows)} calendars" if rows else "no calendar"),
                          {"candidates": [r["calendar_id"] for r in rows]})
    return rows[0]["calendar_id"]


def project(settings: Settings, ctx: Context, model_id: str, params: dict, request_id: str) -> dict:
    m = model(model_id)
    if ctx.mode not in m["temporal_capabilities"]:
        raise DomainError("TEMPORAL_MODE_UNSUPPORTED", f"{ctx.mode} is not supported by {model_id}",
                          {"model_id": model_id, "supported_modes": m["temporal_capabilities"]})
    bound = _bind(m, params)
    if m.get("resolver"):
        out = resolve(settings, ctx, bound["identifier"], bound.get("identifier_type"))
        from security_master_api.resolver.manifest import resolve_manifest
        manifest = resolve_manifest(settings, ctx, ["gold.security_master", "silver.identifiers"])
        receipt = new_receipt(settings, "odp", ctx, manifest, request_id, _fp(model_id, bound))
        record_receipt(settings, receipt)
        cols = [{"name": f["name"], "dtype": f["type"]} for f in m["fields"]]
        return {"results": out["candidates"], "columns": cols, "receipt": receipt}
    sql = m["sql"]
    relation = m["backing_relation"]
    check_modes(settings, ctx, [relation])
    binds = dict(bound)
    if m.get("resolve"):
        candidates = resolve(settings, ctx, bound[m["resolve"]])["candidates"]
        binds["listing_id"] = candidates[0]["listing_id"]
    if model_id == "MarketCalendar":
        binds["calendar_id"] = _calendar_id(settings, ctx, bound)
    with open_session(settings, ctx, [relation] + (["silver.session_interruptions"] if model_id == "MarketCalendar" else [])) as s:
        table = s.run(sql, _named(sql, binds))
        rows = table.to_pylist()
        if model_id == "MarketCalendar":
            interruptions = s.run(
                "SELECT session_date, interruption_start, interruption_end "
                "FROM silver.session_interruptions WHERE calendar_id = ? "
                "AND system_to IS NULL", [binds["calendar_id"]]).to_pylist() if bound.get("include_interruptions") else []
            by_date: dict[str, list] = {}
            for i in interruptions:
                by_date.setdefault(str(i["session_date"]), []).append(
                    {"start": _j(i["interruption_start"]), "end": _j(i["interruption_end"])})
            for r in rows:
                r["interruptions"] = by_date.get(str(r["session_date"]), [])
                for k in ("candidate_branches", "selected_branch"):
                    if isinstance(r.get(k), str):
                        r[k] = json.loads(r[k])
        columns = [{"name": f.name, "dtype": str(f.type)} for f in table.schema]
        manifest = s.manifest
    receipt = new_receipt(settings, "odp", ctx, manifest, request_id, _fp(model_id, bound))
    record_receipt(settings, receipt)
    if not rows and ctx.mode == "known_at":
        raise DomainError("NO_LOCAL_EVIDENCE", f"{model_id} has no locally known facts at the cutoff",
                          {"model_id": model_id, "receipt": receipt})
    return {"results": [{k: _j(v) for k, v in r.items()} for r in rows], "columns": columns,
            "receipt": receipt}


def _named(sql: str, binds: dict) -> dict:
    """DuckDB binds `$name` placeholders from a dict; date values pass as ISO strings."""
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in binds.items()
            if f"${k}" in sql}


def _fp(model_id: str, bound: dict) -> str:
    return hashlib.sha256(json.dumps({"model": model_id, "params": bound}, sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]


def _j(v):
    return v.isoformat().replace("+00:00", "Z") if hasattr(v, "isoformat") else v
```

`QuerySession.run` must accept a dict for `params` (DuckDB's named-parameter form): change the signature to `params: list | dict | None = None` and pass it through unchanged. In `_named`, `$start_date`-style placeholders are left in the SQL for DuckDB; comparing a `DATE` column with a string bind works because DuckDB casts the string to `DATE`.

The `EquityInfo` fixture expectation for `known_at 2026-07-15` expects `NO_LOCAL_EVIDENCE`; with the listing known since 2026-08-05 the projection returns no rows at that cutoff and the `known_at` branch raises it. For the trade-correction expectation at `2026-09-14T20:04:59Z` the same branch applies.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check .`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add security-master-api
git commit -m "feat(security-master): ODP registry and projections, calendar adapter, receipts"
```

### Task 9: Executions, cursors, preview builder, budgets

**Files:**
- Create: `security_master_api/sql/executions.py`, `security_master_api/sql/preview.py`
- Test: `tests/test_executions.py`, `tests/test_preview.py`

**Interfaces:**
- Produces: `Executions(settings)` with `start(ctx, relations, sql, params, kind, request_id, principal, page_size=None, timeout_ms=None) -> dict` (the first page: `{execution_id, rows, columns, next_cursor, count: {value, quality}, receipt}`), `page(cursor: str) -> dict`, `cancel(execution_id) -> bool`, `plan(ctx, relations, sql) -> dict` (`{relations, functions, columns, manifest}`); `encode_cursor(execution_id, offset, fingerprint) -> str`, `decode_cursor(token) -> tuple[str, int, str]`; `build_preview_sql(relation, columns, filters, sort) -> tuple[str, list]`; `OPERATORS`.
- Budgets: `Executions` holds a `threading.BoundedSemaphore(settings.scan_slots)` and a per-principal counter; exceeding either is `QUERY_BUDGET_EXCEEDED`.

- [ ] **Step 1: Write the failing tests**

`tests/test_preview.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.errors import DomainError
from security_master_api.sql.preview import build_preview_sql


def test_structured_preview_is_bound_not_interpolated():
    sql, params = build_preview_sql(
        "gold.security_master", ["listing_id", "symbol"],
        [{"field": "status", "operator": "eq", "value": "active"},
         {"field": "symbol", "operator": "contains", "value": "AA"}],
        [{"field": "symbol", "direction": "asc"}])
    assert sql == ('SELECT "listing_id", "symbol" FROM gold."security_master" '
                   'WHERE "status" = ? AND "symbol" ILIKE ? ORDER BY "symbol" ASC')
    assert params == ["active", "%AA%"]


def test_all_columns_when_none_named():
    sql, params = build_preview_sql("silver.listings", None, [], [])
    assert sql == 'SELECT * FROM silver."listings"' and params == []


@pytest.mark.parametrize("bad", [
    {"field": "sym\"bol", "operator": "eq", "value": "x"},
    {"field": "symbol", "operator": "regex", "value": "x"},
    {"field": "symbol", "operator": "in", "value": "not-a-list"},
])
def test_bad_filters_are_rejected(bad):
    with pytest.raises(DomainError) as exc:
        build_preview_sql("silver.listings", None, [bad], [])
    assert exc.value.code == "QUERY_REJECTED"


def test_in_and_between_and_null():
    sql, params = build_preview_sql("silver.listings", None,
        [{"field": "symbol", "operator": "in", "value": ["A", "B"]},
         {"field": "effective_from", "operator": "between", "value": ["2020-01-01", "2021-01-01"]},
         {"field": "effective_to", "operator": "is_null"}], [])
    assert '"symbol" IN (?, ?)' in sql and '"effective_from" BETWEEN ? AND ?' in sql
    assert '"effective_to" IS NULL' in sql
    assert params == ["A", "B", "2020-01-01", "2021-01-01"]
```

`tests/test_executions.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import parse_context
from security_master_api.sql.executions import Executions, decode_cursor, encode_cursor
from security_master_api.store.seed import seed
from security_master_api.store.tables import open_table


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k", first_page=2, max_rows=5)
    seed(s)
    return s


def test_first_page_then_cursor_then_end(settings):
    ex = Executions(settings)
    first = ex.start(parse_context(None), ["silver.listings"],
                     "SELECT symbol FROM silver.listings ORDER BY symbol", [], "sql", "req", "user")
    assert len(first["rows"]) == 2 and first["next_cursor"]
    assert first["count"] == {"value": 5, "quality": "estimated"} or first["count"]["quality"] in ("exact", "estimated")
    second = ex.page(first["next_cursor"])
    assert second["receipt"]["execution_id"] == first["receipt"]["execution_id"]
    assert [r["symbol"] for r in second["rows"]] != [r["symbol"] for r in first["rows"]]
    rows = open_table(settings, "ops.receipts").to_pyarrow_table().to_pylist()
    assert any(r["execution_id"] == first["receipt"]["execution_id"] for r in rows)


def test_max_rows_marks_the_count_estimated_and_stops(settings):
    ex = Executions(settings)
    out = ex.start(parse_context(None), ["silver.listings"],
                   "SELECT * FROM silver.listings", [], "sql", "req", "user", page_size=100)
    assert len(out["rows"]) <= 5
    assert out["count"]["quality"] in ("exact", "estimated")


def test_cursor_binds_execution_and_offset(settings):
    token = encode_cursor("qry_x", 40, "fp")
    assert decode_cursor(token) == ("qry_x", 40, "fp")
    ex = Executions(settings)
    with pytest.raises(DomainError) as exc:
        ex.page(encode_cursor("qry_missing", 0, "fp"))
    assert exc.value.code == "QUERY_REJECTED"
    with pytest.raises(DomainError):
        ex.page("not-a-cursor")


def test_plan_reports_relations_columns_and_manifest(settings):
    ex = Executions(settings)
    plan = ex.plan(parse_context(None), ["gold.security_master"],
                   "SELECT symbol, name FROM gold.security_master")
    assert plan["relations"] == ["gold.security_master"]
    assert [c["name"] for c in plan["columns"]] == ["symbol", "name"]
    assert "silver.listings" in plan["manifest"]


def test_per_principal_budget(settings):
    ex = Executions(Settings(root=settings.root, preflight_secret="k", per_principal_queries=0))
    with pytest.raises(DomainError) as exc:
        ex.start(parse_context(None), ["silver.listings"], "SELECT 1", [], "sql", "req", "user")
    assert exc.value.code == "QUERY_BUDGET_EXCEEDED"


def test_cancel_unknown_is_false(settings):
    assert Executions(settings).cancel("qry_nope") is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --extra dev pytest -q tests/test_preview.py tests/test_executions.py`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write `sql/preview.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Structured, allowlisted filters and sorts become bound SQL. No user text reaches the SQL."""

from __future__ import annotations

import re

from security_master_api.errors import DomainError

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
OPERATORS = {
    "eq": '"{f}" = ?', "ne": '"{f}" <> ?', "lt": '"{f}" < ?', "lte": '"{f}" <= ?',
    "gt": '"{f}" > ?', "gte": '"{f}" >= ?', "contains": '"{f}" ILIKE ?',
    "starts_with": '"{f}" ILIKE ?', "in": '"{f}" IN ({ph})', "between": '"{f}" BETWEEN ? AND ?',
    "is_null": '"{f}" IS NULL', "not_null": '"{f}" IS NOT NULL',
}


def _ident(name: str, what: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise DomainError("QUERY_REJECTED", f"invalid {what} name", {what: str(name)[:40]})
    return name


def build_preview_sql(relation: str, columns: list[str] | None, filters: list[dict],
                      sort: list[dict]) -> tuple[str, list]:
    layer, _, name = relation.partition(".")
    _ident(layer, "layer")
    for part in name.split("."):
        _ident(part, "relation")
    select = ", ".join(f'"{_ident(c, "column")}"' for c in columns) if columns else "*"
    sql = f'SELECT {select} FROM {layer}."{name}"'
    params: list = []
    clauses = []
    for flt in filters or []:
        if not isinstance(flt, dict):
            raise DomainError("QUERY_REJECTED", "filter must be an object")
        field = _ident(flt.get("field"), "column")
        op = flt.get("operator")
        if op not in OPERATORS:
            raise DomainError("QUERY_REJECTED", f"unsupported operator {op!r}",
                              {"supported": sorted(OPERATORS)})
        value = flt.get("value")
        if op == "in":
            if not isinstance(value, list) or not value or len(value) > 100:
                raise DomainError("QUERY_REJECTED", "in needs a list of 1..100 values")
            clauses.append(OPERATORS[op].format(f=field, ph=", ".join("?" * len(value))))
            params.extend(value)
        elif op == "between":
            if not isinstance(value, list) or len(value) != 2:
                raise DomainError("QUERY_REJECTED", "between needs two values")
            clauses.append(OPERATORS[op].format(f=field))
            params.extend(value)
        elif op in ("is_null", "not_null"):
            clauses.append(OPERATORS[op].format(f=field))
        else:
            if isinstance(value, (list, dict)) or value is None:
                raise DomainError("QUERY_REJECTED", f"{op} needs a scalar value")
            clauses.append(OPERATORS[op].format(f=field))
            if op == "contains":
                params.append(f"%{value}%")
            elif op == "starts_with":
                params.append(f"{value}%")
            else:
                params.append(value)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    orders = []
    for s in sort or []:
        field = _ident((s or {}).get("field"), "column")
        direction = str((s or {}).get("direction", "asc")).upper()
        if direction not in ("ASC", "DESC"):
            raise DomainError("QUERY_REJECTED", "sort direction must be asc or desc")
        orders.append(f'"{field}" {direction}')
    if orders:
        sql += " ORDER BY " + ", ".join(orders)
    return sql, params
```

- [ ] **Step 4: Write `sql/executions.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Executions: run under a budget, hold the result, page it by opaque cursor, cancel it."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

import pyarrow as pa

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import Context
from security_master_api.resolver.manifest import resolve_manifest
from security_master_api.sql.session import QuerySession, open_session
from security_master_api.store.receipts import new_receipt, record_receipt

RESULT_TTL_S = 600


def encode_cursor(execution_id: str, offset: int, fingerprint: str) -> str:
    raw = json.dumps({"e": execution_id, "o": offset, "f": fingerprint}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str) -> tuple[str, int, str]:
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return str(raw["e"]), int(raw["o"]), str(raw["f"])
    except Exception as exc:  # noqa: BLE001 - any malformed token is one rejection
        raise DomainError("QUERY_REJECTED", "invalid cursor") from exc


@dataclass
class _Execution:
    receipt: dict
    fingerprint: str
    page_size: int
    table: pa.Table | None = None
    truncated: bool = False
    session: QuerySession | None = None
    created: float = field(default_factory=time.monotonic)
    principal: str = ""


def _fingerprint(ctx: Context, sql: str, params) -> str:
    return hashlib.sha256(json.dumps([ctx.as_dict(), sql, params], sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]


class Executions:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._by_id: dict[str, _Execution] = {}
        self._scan = threading.BoundedSemaphore(max(1, settings.scan_slots))
        self._per_principal: dict[str, int] = {}

    def _acquire(self, principal: str) -> None:
        with self._lock:
            active = self._per_principal.get(principal, 0)
            if active >= self.settings.per_principal_queries:
                raise DomainError("QUERY_BUDGET_EXCEEDED",
                                  f"{principal} already has {active} running queries",
                                  {"limit": self.settings.per_principal_queries})
            self._per_principal[principal] = active + 1
        if not self._scan.acquire(timeout=self.settings.timeout_ms / 1000):
            self._release(principal)
            raise DomainError("QUERY_BUDGET_EXCEEDED", "the service scan budget is exhausted")

    def _release(self, principal: str) -> None:
        with self._lock:
            self._per_principal[principal] = max(0, self._per_principal.get(principal, 1) - 1)
        try:
            self._scan.release()
        except ValueError:
            pass

    def _expire(self) -> None:
        cutoff = time.monotonic() - RESULT_TTL_S
        with self._lock:
            for key in [k for k, v in self._by_id.items() if v.created < cutoff]:
                del self._by_id[key]

    def plan(self, ctx: Context, relations: Iterable[str], sql: str) -> dict:
        with open_session(self.settings, ctx, list(relations)) as s:
            from security_master_api.sql.policy import validate_sql

            info = validate_sql(s.con, sql, s.allowed)
            return {"relations": info.relations, "functions": info.functions,
                    "columns": s.describe(sql), "manifest": s.manifest}

    def start(self, ctx: Context, relations: Iterable[str], sql: str, params, kind: str,
              request_id: str, principal: str, page_size: int | None = None,
              timeout_ms: int | None = None) -> dict:
        self._expire()
        page = min(page_size or self.settings.first_page, self.settings.max_rows)
        self._acquire(principal)
        session = None
        try:
            session = open_session(self.settings, ctx, list(relations))
            receipt = new_receipt(self.settings, kind, ctx, session.manifest, request_id,
                                  _fingerprint(ctx, sql, params))
            record_receipt(self.settings, receipt)
            ex = _Execution(receipt=receipt, fingerprint=receipt["sql_fingerprint"],
                            page_size=page, session=session, principal=principal)
            with self._lock:
                self._by_id[receipt["execution_id"]] = ex
            bounded = f"SELECT * FROM ({sql.strip().rstrip(';')}) AS q LIMIT {self.settings.max_rows + 1}"
            table = session.run(bounded, params, timeout_ms=timeout_ms)
            if table.num_rows > self.settings.max_rows:
                ex.truncated = True
                table = table.slice(0, self.settings.max_rows)
            ex.table = table
        finally:
            if session is not None:
                session.__exit__(None, None, None)
                if session.manifest and receipt["execution_id"] in self._by_id:
                    self._by_id[receipt["execution_id"]].session = None
            self._release(principal)
        return self._page(ex, 0)

    def page(self, cursor: str) -> dict:
        execution_id, offset, fingerprint = decode_cursor(cursor)
        with self._lock:
            ex = self._by_id.get(execution_id)
        if ex is None or ex.table is None:
            raise DomainError("QUERY_REJECTED", "unknown or expired execution",
                              {"execution_id": execution_id})
        if fingerprint != ex.fingerprint:
            raise DomainError("QUERY_REJECTED", "cursor does not match the execution's context")
        return self._page(ex, offset)

    def cancel(self, execution_id: str) -> bool:
        with self._lock:
            ex = self._by_id.get(execution_id)
        if ex is None:
            return False
        if ex.session is not None:
            ex.session.cancel()
        with self._lock:
            self._by_id.pop(execution_id, None)
        return True

    def _page(self, ex: _Execution, offset: int) -> dict:
        table = ex.table
        chunk = table.slice(offset, ex.page_size)
        rows = [{k: _j(v) for k, v in r.items()} for r in chunk.to_pylist()]
        next_offset = offset + chunk.num_rows
        has_more = next_offset < table.num_rows
        return {
            "execution_id": ex.receipt["execution_id"],
            "rows": rows,
            "columns": [{"name": f.name, "dtype": str(f.type)} for f in table.schema],
            "next_cursor": encode_cursor(ex.receipt["execution_id"], next_offset, ex.fingerprint) if has_more else None,
            "count": {"value": table.num_rows, "quality": "estimated" if ex.truncated else "exact"},
            "receipt": ex.receipt,
        }


def _j(v):
    return v.isoformat().replace("+00:00", "Z") if hasattr(v, "isoformat") else v
```

`start` runs the whole bounded statement before returning the first page so that the cursor pages a stable, already-materialized result under one receipt. The session is closed as soon as the table exists; `cancel` during the run reaches `interrupt()` through the still-open session.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check .`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add security-master-api
git commit -m "feat(security-master): executions with opaque cursors, budgets and the structured preview builder"
```

### Task 10: The FastAPI application — auth, error envelope, read-only routes, contract recording

**Files:**
- Create: `security_master_api/app/__init__.py`, `app/auth.py`, `app/errors.py`, `app/main.py`, `security-master-api/widgets.json`, `security-master-api/apps.json`
- Test: `tests/test_app_auth.py`, `tests/test_app_routes.py`, `tests/contract/` (recorded by the tests)

**Interfaces:**
- Produces: `create_app(settings: Settings | None = None) -> FastAPI`; module-level `app` built lazily from `settings_from_env()` for uvicorn (`security_master_api.app.main:app`). Every route lives under `/security-master/v1`. Every error is the envelope; every success from preview/sql/odp/compare/resolve/lineage carries `receipt`.
- Contract recording: a test helper `record(name: str, payload: dict)` writes `tests/contract/<name>.json` (sorted keys, two-space indent) so Part B's parser tests copy them verbatim.

- [ ] **Step 1: Write `app/auth.py`**

Copy `live-grid/app/auth.py` verbatim (header, docstring, `auth_enabled`, `credentials_ok`, `BasicAuthMiddleware`) with two changes: the middleware also reads the credential from the `authorization` query parameter (SSE cannot set headers) via `urllib.parse.parse_qs(scope.get("query_string", b"").decode()).get("authorization", [None])[0]`, and the path `/security-master/v1/health` bypasses the check.

- [ ] **Step 2: Write `app/errors.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""One envelope for every failure. FastAPI's own validation errors are re-wrapped."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from security_master_api.errors import DomainError


def request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if not rid:
        rid = "req_" + uuid.uuid4().hex[:12]
        request.state.request_id = rid
    return rid


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def _stamp(request: Request, call_next):
        request_id(request)
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        return response

    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status, content=exc.envelope(request_id(request)))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        err = DomainError("QUERY_REJECTED", "request body or parameters are invalid",
                          {"errors": [{"loc": list(e.get("loc", [])), "msg": e.get("msg")}
                                      for e in exc.errors()]})
        return JSONResponse(status_code=422, content=err.envelope(request_id(request)))
```

- [ ] **Step 3: Write the failing auth test**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import base64

import pytest
from fastapi.testclient import TestClient

from security_master_api.app.main import create_app
from security_master_api.config import Settings
from security_master_api.store.seed import seed


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "true")
    monkeypatch.setenv("OPENBB_API_USERNAME", "u")
    monkeypatch.setenv("OPENBB_API_PASSWORD", "p")
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return TestClient(create_app(s))


def basic(u="u", p="p"):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


def test_health_is_open_everything_else_is_not(client):
    assert client.get("/security-master/v1/health").status_code == 200
    r = client.get("/security-master/v1/catalog")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Basic"
    assert client.get("/widgets.json").status_code == 401


def test_header_and_query_credentials(client):
    assert client.get("/security-master/v1/catalog", headers=basic()).status_code == 200
    token = "Basic " + base64.b64encode(b"u:p").decode()
    assert client.get("/security-master/v1/catalog", params={"authorization": token}).status_code == 200
    assert client.get("/security-master/v1/catalog", headers=basic("u", "x")).status_code == 401


def test_unconfigured_credentials_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "true")
    monkeypatch.delenv("OPENBB_API_USERNAME", raising=False)
    monkeypatch.delenv("OPENBB_API_PASSWORD", raising=False)
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    c = TestClient(create_app(s))
    assert c.get("/security-master/v1/catalog", headers=basic("", "")).status_code == 401
```

- [ ] **Step 4: Write the failing routes test**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from security_master_api.app.main import create_app
from security_master_api.config import Settings
from security_master_api.store.seed import seed

CONTRACT = Path(__file__).parent / "contract"
V1 = "/security-master/v1"


def record(name: str, payload) -> None:
    CONTRACT.mkdir(exist_ok=True)
    (CONTRACT / f"{name}.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "false")
    s = Settings(root=str(tmp_path), preflight_secret="k", first_page=3)
    seed(s)
    return TestClient(create_app(s))


def test_catalog(client):
    r = client.get(f"{V1}/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["layers"] == ["gold", "silver", "bronze", "ops"]
    assert body["policies"] == {"sql": "enabled", "acquisition": "review"}
    assert body["odp_version"] == "openbb==4.7.2"
    assert body["newer_data_available"] is False
    names = {f"{x['layer']}.{x['name']}" for x in body["relations"]}
    assert {"gold.market_calendar", "silver.listings", "bronze.source_captures", "ops.receipts"} <= names
    record("catalog", body)


def test_relation_and_versions(client):
    r = client.get(f"{V1}/relations/silver/listings")
    assert r.status_code == 200
    body = r.json()
    assert body["relation"]["modes"] == ["current_corrected", "known_at", "effective_on", "delta_snapshot"]
    assert {"name", "dtype"} <= set(body["schema"][0])
    assert body["latest_version"] >= 1 and body["count"]["quality"] == "exact"
    record("relation", body)
    v = client.get(f"{V1}/relations/silver/listings/versions").json()
    assert v["versions"][0]["version"] >= 1 and "retained" in v["versions"][0]
    record("versions", v)
    assert client.get(f"{V1}/relations/gold/nope").json()["error"]["code"] == "QUERY_REJECTED"


def test_odp_models_and_query(client):
    models = client.get(f"{V1}/odp/models").json()
    assert [m["model_id"] for m in models["models"]][:2] == ["EquityInfo", "EquitySearch"]
    record("odp_models", models)
    one = client.get(f"{V1}/odp/models/MarketCalendar").json()
    assert one["model"]["primary_temporal_scenario"] == "lunar_holiday_correction"
    assert one["scenario"]["scenario"] == "lunar_holiday_correction"
    record("odp_model_market_calendar", one)
    q = client.post(f"{V1}/odp/models/EquityHistorical/query", json={
        "parameters": {"symbol": "AAPL", "start_date": "2026-09-14", "end_date": "2026-09-14"},
        "context": {"mode": "known_at", "effective_at": "2026-09-14T20:00:00Z",
                    "known_at": "2026-09-14T20:00:00Z"}})
    assert q.status_code == 404 and q.json()["error"]["code"] == "NO_LOCAL_EVIDENCE"
    record("error_no_local_evidence", q.json())
    q = client.post(f"{V1}/odp/models/EquityHistorical/query", json={
        "parameters": {"symbol": "AAPL", "start_date": "2026-09-14", "end_date": "2026-09-14"},
        "context": {"mode": "known_at", "effective_at": "2026-09-14T20:00:00Z",
                    "known_at": "2026-09-15T09:20:00Z"}})
    assert q.status_code == 200 and q.json()["results"][0]["close"] == 231.74
    assert q.json()["extra"]["security_master_receipt"]["temporal_mode"] == "known_at"
    record("odp_query", q.json())


def test_preview_pages_with_a_context_bound_cursor(client):
    body = {"relation": "gold.security_master", "context": {"mode": "current_corrected"},
            "columns": ["listing_id", "symbol"], "filters": [], "sort": [{"field": "symbol", "direction": "asc"}],
            "page": {"limit": 3, "cursor": None}}
    r = client.post(f"{V1}/preview", json=body)
    assert r.status_code == 200
    page = r.json()
    assert len(page["rows"]) == 3 and page["next_cursor"] and page["receipt"]["kind"] == "preview"
    record("preview", page)
    body["page"]["cursor"] = page["next_cursor"]
    nxt = client.post(f"{V1}/preview", json=body).json()
    assert nxt["receipt"]["execution_id"] == page["receipt"]["execution_id"]
    body["context"] = {"mode": "effective_on", "effective_at": "2022-06-07T00:00:00Z"}
    assert client.post(f"{V1}/preview", json=body).json()["error"]["code"] == "QUERY_REJECTED"


def test_preview_refuses_unsupported_mode(client):
    r = client.post(f"{V1}/preview", json={"relation": "bronze.source_captures",
                                          "context": {"mode": "known_at", "known_at": "2026-01-01T00:00:00Z"}})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["supported_modes"] == ["captured_by", "delta_snapshot"]
    record("error_mode_unsupported", r.json())


def test_sql_plan_execute_page_cancel(client):
    plan = client.post(f"{V1}/sql/plan", json={"sql": "SELECT symbol FROM gold.security_master ORDER BY symbol",
                                              "context": {"mode": "current_corrected"}}).json()
    assert plan["relations"] == ["gold.security_master"] and plan["columns"][0]["name"] == "symbol"
    record("sql_plan", plan)
    ex = client.post(f"{V1}/sql/execute", json={"sql": "SELECT symbol FROM gold.security_master ORDER BY symbol",
                                               "context": {"mode": "current_corrected"},
                                               "budgets": {"max_rows": 100, "timeout_ms": 5000}}).json()
    assert ex["rows"] and ex["next_cursor"] and ex["receipt"]["kind"] == "sql"
    record("sql_execute", ex)
    page = client.get(f"{V1}/sql/executions/{ex['execution_id']}/pages", params={"cursor": ex["next_cursor"]}).json()
    assert page["execution_id"] == ex["execution_id"]
    assert client.delete(f"{V1}/sql/executions/{ex['execution_id']}").status_code == 204
    assert client.delete(f"{V1}/sql/executions/qry_missing").status_code == 404
    bad = client.post(f"{V1}/sql/execute", json={"sql": "DROP TABLE silver.listings", "context": {}}).json()
    assert bad["error"]["code"] == "QUERY_REJECTED"
    record("error_query_rejected", bad)


def test_sql_disabled_by_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "false")
    s = Settings(root=str(tmp_path), preflight_secret="k", sql_policy="disabled")
    seed(s)
    c = TestClient(create_app(s))
    r = c.post(f"{V1}/sql/execute", json={"sql": "SELECT 1", "context": {}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "QUERY_REJECTED"
    assert c.get(f"{V1}/catalog").json()["policies"]["sql"] == "disabled"


def test_resolve_lineage_compare(client):
    r = client.post(f"{V1}/resolve", json={"identifier": "FB", "context": {"mode": "effective_on", "effective_at": "2022-06-07T00:00:00Z"}}).json()
    assert r["candidates"][0]["listing_id"] == "lst_000042" and r["receipt"]["kind"] == "resolve"
    record("resolve", r)
    amb = client.post(f"{V1}/resolve", json={"identifier": "ZZZZ", "context": {}}).json()
    assert amb["error"]["code"] == "IDENTITY_UNRESOLVED"
    lin = client.post(f"{V1}/lineage", json={"relation": "silver.prices_normalized",
                                             "assertion_id": "as_px_apple_20260914_2", "context": {}}).json()
    assert lin["chain"][0]["kind"] == "assertion" and lin["receipt"]["kind"] == "lineage"
    record("lineage", lin)
    cmp_ = client.post(f"{V1}/compare", json={
        "relation": "gold.price_daily", "selection": {"listing_id": "lst_apple", "market_date": "2026-09-14"},
        "left": {"mode": "known_at", "known_at": "2026-09-14T20:05:00Z"},
        "right": {"mode": "known_at", "known_at": "2026-09-15T09:20:00Z"}}).json()
    assert cmp_["differences"][0]["classification"] in ("corrected", "newly_known")
    assert cmp_["left_receipt"]["kind"] == "compare" and cmp_["right_receipt"]["kind"] == "compare"
    record("compare", cmp_)


def test_versions_compare_and_exchanges(client):
    vc = client.post(f"{V1}/versions/compare", json={"relation": "silver.issuers", "left": 0, "right": 1}).json()
    assert vc["schema"]["added"] == [] and vc["rows"]["left"] == 0 and vc["rows"]["right"] >= 1
    record("versions_compare", vc)
    ex = client.get(f"{V1}/exchanges").json()
    assert any(e["calendar_id"] == "cal_tadawul" for e in ex["exchanges"])
    record("exchanges", ex)


def test_widgets_and_apps(client):
    w = client.get("/widgets.json").json()
    assert w["security_master_browser"]["type"] == "security_master_browser"
    assert [p["paramName"] for p in w["security_master_browser"]["params"]] == [
        "listing_id", "effective_date", "known_at", "layer", "temporal_mode"]
    assert isinstance(client.get("/apps.json").json(), list)
```

- [ ] **Step 5: Run the tests to verify they fail**

Run: `uv run --extra dev pytest -q tests/test_app_auth.py tests/test_app_routes.py`
Expected: FAIL with `ModuleNotFoundError: security_master_api.app.main`.

- [ ] **Step 6: Write `widgets.json` and `apps.json`**

`widgets.json`:

```json
{
  "security_master_browser": {
    "name": "Security Master Browser",
    "description": "Explore what you have. Understand what you knew: ODP models, layered Delta relations, guarded SQL and temporal receipts.",
    "category": "Data",
    "type": "security_master_browser",
    "endpoint": "security-master/v1/catalog",
    "gridData": { "w": 40, "h": 24 },
    "params": [
      { "paramName": "listing_id", "type": "text", "label": "Listing", "value": "", "show": false },
      { "paramName": "effective_date", "type": "date", "label": "Effective date", "value": "", "show": false },
      { "paramName": "known_at", "type": "text", "label": "Known at", "value": "", "show": false },
      { "paramName": "layer", "type": "text", "label": "Layer", "value": "gold", "show": false,
        "options": [{ "label": "Gold", "value": "gold" }, { "label": "Silver", "value": "silver" }, { "label": "Bronze", "value": "bronze" }] },
      { "paramName": "temporal_mode", "type": "text", "label": "Temporal mode", "value": "current_corrected", "show": false,
        "options": [{ "label": "Current corrected", "value": "current_corrected" }, { "label": "Known at", "value": "known_at" }, { "label": "Effective on", "value": "effective_on" }, { "label": "Delta snapshot", "value": "delta_snapshot" }] }
    ],
    "source": ["Delta Lake", "EODHD"]
  }
}
```

`apps.json`: a one-tab app `"Security master"` with a single card `{"i": "security_master_browser", "x": 0, "y": 0, "w": 40, "h": 24, "state": {"params": {"layer": "gold", "temporal_mode": "current_corrected"}}}` following `stores-explorer/apps.json`'s shape (copy its `name`/`description`/`img`/`allowCustomization`/`tabs`/`groups`/`prompts` keys, one tab uuid).

- [ ] **Step 7: Write `app/main.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""security-master-api: the browser's service. Every read path is provider-silent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from security_master_api.app.auth import BasicAuthMiddleware
from security_master_api.app.errors import install, request_id
from security_master_api.config import Settings, settings_from_env
from security_master_api.errors import DomainError
from security_master_api.resolver import catalog as cat
from security_master_api.resolver.compare import compare as run_compare
from security_master_api.resolver.context import parse_context
from security_master_api.resolver.identity import resolve as run_resolve
from security_master_api.resolver.lineage import lineage as run_lineage
from security_master_api.resolver.manifest import resolve_manifest
from security_master_api.resolver.odp import model as odp_model, odp_version, project, registry
from security_master_api.sql.executions import Executions
from security_master_api.sql.preview import build_preview_sql
from security_master_api.store.receipts import new_receipt, record_receipt
from security_master_api.store.seed import golden_fixtures
from security_master_api.store.tables import history, latest_version, open_table
from security_master_api.sql.session import open_session

HERE = Path(__file__).resolve().parent.parent.parent
V1 = "/security-master/v1"


class Page(BaseModel):
    limit: int | None = None
    cursor: str | None = None


class PreviewBody(BaseModel):
    relation: str
    context: dict[str, Any] = Field(default_factory=dict)
    columns: list[str] | None = None
    filters: list[dict[str, Any]] = Field(default_factory=list)
    sort: list[dict[str, Any]] = Field(default_factory=list)
    page: Page = Field(default_factory=Page)


class SqlBody(BaseModel):
    sql: str = Field(max_length=20_000)
    context: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, int] = Field(default_factory=dict)


class OdpQueryBody(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class ResolveBody(BaseModel):
    identifier: str
    identifier_type: str | None = None
    include_historical: bool = True
    context: dict[str, Any] = Field(default_factory=dict)


class LineageBody(BaseModel):
    relation: str
    assertion_id: str
    context: dict[str, Any] = Field(default_factory=dict)


class CompareBody(BaseModel):
    relation: str
    selection: dict[str, Any]
    left: dict[str, Any]
    right: dict[str, Any]


class VersionsCompareBody(BaseModel):
    relation: str
    left: int
    right: int
    row_diff: bool = False


def _principal(request: Request) -> str:
    header = request.headers.get("authorization") or request.query_params.get("authorization") or ""
    return header[-16:] or "anonymous"


def _scenario_for(model_id: str) -> dict | None:
    m = odp_model(model_id)
    for fixture in golden_fixtures():
        if fixture.get("scenario") == m["primary_temporal_scenario"] or (
            m["primary_temporal_scenario"] == "lunar_holiday_correction"
            and fixture["fixture_id"] == "eid-lunar-correction"
        ):
            return {"scenario": m["primary_temporal_scenario"], "fixture_id": fixture["fixture_id"],
                    "expectations": fixture["expectations"],
                    "captures": [c["capture_id"] for c in fixture["captures"]]}
    return None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or settings_from_env()
    app = FastAPI(title="security-master-api", version="0.2.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                       expose_headers=["x-request-id"])
    app.add_middleware(BasicAuthMiddleware)
    install(app)
    executions = Executions(settings)
    app.state.settings = settings
    app.state.executions = executions
    seeded_versions: dict[str, int] = {}

    def newer_available() -> bool:
        current = {r.full: latest_version(settings, r.full) for r in cat.catalog(settings)
                   if r.kind == "delta_table"}
        if not seeded_versions:
            seeded_versions.update({k: v for k, v in current.items() if v is not None})
            return False
        return any(current.get(k, 0) != v for k, v in seeded_versions.items())

    @app.get(f"{V1}/health")
    def health() -> dict:
        return {"status": "ok", "version": settings.code_version}

    @app.get("/widgets.json")
    def widgets() -> JSONResponse:
        return JSONResponse(json.loads((HERE / "widgets.json").read_text()))

    @app.get("/apps.json")
    def apps() -> JSONResponse:
        return JSONResponse(json.loads((HERE / "apps.json").read_text()))

    @app.get(f"{V1}/catalog")
    def get_catalog() -> dict:
        return {
            "layers": ["gold", "silver", "bronze", "ops"],
            "relations": [r.as_dict() for r in cat.catalog(settings)],
            "policies": {"sql": settings.sql_policy, "acquisition": settings.acquisition_policy},
            "odp_version": odp_version(),
            "projection_version": settings.projection_version,
            "newer_data_available": newer_available(),
        }

    @app.get(f"{V1}/relations/{{layer}}/{{name:path}}")
    def get_relation(layer: str, name: str) -> dict:
        if name.endswith("/versions"):
            return get_versions(layer, name[: -len("/versions")])
        full = f"{layer}.{name}"
        rel = cat.relation(settings, full)
        out: dict = {"relation": rel.as_dict(), "schema": [], "latest_version": None,
                     "count": {"value": None, "quality": "unknown"}}
        if rel.kind == "view":
            ctx = parse_context(None)
            with open_session(settings, ctx, [full]) as s:
                out["schema"] = s.describe(f'SELECT * FROM gold."{rel.name}"')
                out["manifest"] = s.manifest
            return out
        table = open_table(settings, full)
        out["latest_version"] = table.version()
        out["schema"] = [{"name": f.name, "dtype": str(f.type)} for f in table.schema().to_pyarrow()]
        try:
            out["count"] = {"value": table.to_pyarrow_dataset().count_rows(), "quality": "exact"}
        except Exception:  # noqa: BLE001 - a count is optional; the relation is not
            pass
        return out

    def get_versions(layer: str, name: str) -> dict:
        full = f"{layer}.{name}"
        cat.relation(settings, full)
        return {"relation": full,
                "versions": [{**h, "retained": True} for h in history(settings, full)]}

    @app.get(f"{V1}/odp/models")
    def odp_models() -> dict:
        return {"odp_version": odp_version(), "models": registry()}

    @app.get(f"{V1}/odp/models/{{model_id}}")
    def odp_one(model_id: str) -> dict:
        return {"model": odp_model(model_id), "scenario": _scenario_for(model_id)}

    @app.post(f"{V1}/odp/models/{{model_id}}/query")
    def odp_query(model_id: str, body: OdpQueryBody, request: Request) -> dict:
        ctx = parse_context(body.context)
        out = project(settings, ctx, model_id, body.parameters, request_id(request))
        return {"results": out["results"], "columns": out["columns"],
                "extra": {"security_master_receipt": out["receipt"]}}

    @app.post(f"{V1}/preview")
    def preview(body: PreviewBody, request: Request) -> dict:
        if body.page.cursor:
            return executions.page(body.page.cursor)
        ctx = parse_context(body.context)
        sql, params = build_preview_sql(body.relation, body.columns, body.filters, body.sort)
        return executions.start(ctx, [body.relation], sql, params, "preview", request_id(request),
                                _principal(request), page_size=body.page.limit)

    def _sql_allowed() -> None:
        if settings.sql_policy == "disabled":
            raise DomainError("QUERY_REJECTED", "SQL is disabled by policy", {"policy": "sql"})

    def _relations_in(ctx, sql: str) -> list[str]:
        # Register every declared relation eligible for the mode; the policy then
        # rejects any BASE_TABLE outside that set. Views are cheap until scanned.
        return [r.full for r in cat.catalog(settings) if ctx.mode in r.modes and r.layer != "ops"]

    @app.post(f"{V1}/sql/plan")
    def sql_plan(body: SqlBody) -> dict:
        _sql_allowed()
        ctx = parse_context(body.context)
        return executions.plan(ctx, _relations_in(ctx, body.sql), body.sql)

    @app.post(f"{V1}/sql/execute")
    def sql_execute(body: SqlBody, request: Request) -> dict:
        _sql_allowed()
        ctx = parse_context(body.context)
        return executions.start(ctx, _relations_in(ctx, body.sql), body.sql, [], "sql",
                                request_id(request), _principal(request),
                                page_size=body.budgets.get("max_rows"),
                                timeout_ms=body.budgets.get("timeout_ms"))

    @app.get(f"{V1}/sql/executions/{{execution_id}}/pages")
    def sql_pages(execution_id: str, cursor: str) -> dict:
        page = executions.page(cursor)
        if page["execution_id"] != execution_id:
            raise DomainError("QUERY_REJECTED", "cursor belongs to another execution")
        return page

    @app.delete(f"{V1}/sql/executions/{{execution_id}}", status_code=204)
    def sql_cancel(execution_id: str) -> Response:
        if not executions.cancel(execution_id):
            raise DomainError("NO_MATCHING_FACTS", "unknown execution", {"execution_id": execution_id})
        return Response(status_code=204)

    @app.post(f"{V1}/resolve")
    def resolve_route(body: ResolveBody, request: Request) -> dict:
        ctx = parse_context(body.context)
        out = run_resolve(settings, ctx, body.identifier, body.identifier_type, body.include_historical)
        manifest = resolve_manifest(settings, ctx, ["gold.security_master", "silver.identifiers"])
        receipt = new_receipt(settings, "resolve", ctx, manifest, request_id(request))
        record_receipt(settings, receipt)
        return {**out, "receipt": receipt}

    @app.post(f"{V1}/lineage")
    def lineage_route(body: LineageBody, request: Request) -> dict:
        ctx = parse_context(body.context)
        out = run_lineage(settings, ctx, body.relation, body.assertion_id)
        snap = parse_context({"mode": "delta_snapshot", "versions": ctx.versions})
        manifest = resolve_manifest(settings, snap, [body.relation, "bronze.source_captures"])
        receipt = new_receipt(settings, "lineage", snap, manifest, request_id(request))
        record_receipt(settings, receipt)
        return {**out, "receipt": receipt}

    @app.post(f"{V1}/compare")
    def compare_route(body: CompareBody, request: Request) -> dict:
        left, right = parse_context(body.left), parse_context(body.right)
        out = run_compare(settings, body.relation, body.selection, left, right)
        lr = new_receipt(settings, "compare", left, out.pop("left_manifest"), request_id(request))
        rr = new_receipt(settings, "compare", right, out.pop("right_manifest"), request_id(request))
        record_receipt(settings, lr)
        record_receipt(settings, rr)
        return {**out, "left_receipt": lr, "right_receipt": rr}

    @app.post(f"{V1}/versions/compare")
    def versions_compare(body: VersionsCompareBody) -> dict:
        cat.relation(settings, body.relation)
        left = open_table(settings, body.relation, body.left)
        right = open_table(settings, body.relation, body.right)
        ls = {f.name: str(f.type) for f in left.schema().to_pyarrow()}
        rs = {f.name: str(f.type) for f in right.schema().to_pyarrow()}
        out = {
            "relation": body.relation, "left": body.left, "right": body.right,
            "schema": {"added": sorted(set(rs) - set(ls)), "removed": sorted(set(ls) - set(rs)),
                       "changed": sorted(k for k in ls.keys() & rs.keys() if ls[k] != rs[k])},
            "rows": {"left": left.to_pyarrow_dataset().count_rows(),
                     "right": right.to_pyarrow_dataset().count_rows()},
        }
        if body.row_diff:
            lt = left.to_pyarrow_table().to_pylist()
            rt = right.to_pyarrow_table().to_pylist()
            out["row_diff"] = {"added": rt[len(lt):][: settings.first_page]}
        return out

    @app.get(f"{V1}/exchanges")
    def exchanges() -> dict:
        ctx = parse_context(None)
        with open_session(settings, ctx, ["silver.exchanges"]) as s:
            rows = s.run("SELECT exchange_id, calendar_id, mic, name, timezone, calendar_alias, "
                         "eodhd_code, effective_from, effective_to FROM silver.exchanges "
                         "ORDER BY name").to_pylist()
        return {"exchanges": [{k: (v.isoformat() if hasattr(v, "isoformat") else v)
                               for k, v in r.items()} for r in rows]}

    return app


def _lazy():
    return create_app()


app = _lazy()
```

The `bronze.<library>.<symbol>` external relations contain a dot in `name`, which is why the relation route uses `{name:path}` and why `get_versions` is reached through the same handler.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check .`
Expected: PASS, and `tests/contract/` now holds sixteen JSON files. Commit them: they are Part B's parser fixtures.

- [ ] **Step 9: Commit**

```bash
git add security-master-api
git commit -m "feat(security-master): the FastAPI service with auth, error envelope and read-only routes"
```

### Task 11: Durable jobs, preflight, and the acquisition routes

**Files:**
- Create: `security_master_api/store/jobs.py`, `security_master_api/acquire/__init__.py`, `acquire/preflight.py`
- Modify: `security_master_api/app/main.py` (acquisition routes + SSE)
- Test: `tests/test_jobs.py`, `tests/test_preflight.py`, `tests/test_app_acquisitions.py`

**Interfaces:**
- Produces: `STAGES = ("queued", "fetching", "bronze_retained", "normalizing", "validating", "silver_ready", "materializing", "gold_ready")`, `TERMINAL = ("partial", "rate_limited", "entitlement_denied", "validation_failed", "cancel_pending", "cancelled", "failed", "gold_ready")`; `create_job(settings, kind, request: dict, fingerprint, token_hash) -> dict`; `append_event(settings, job_id, stage, detail: dict | None = None, worker_id="api") -> dict`; `claim(settings, job_id, worker_id) -> bool`; `job(settings, job_id) -> dict` (summary with `state`, `stage_history`); `events(settings, job_id) -> list[dict]`; `queued(settings) -> list[dict]`; `by_fingerprint(settings, fingerprint) -> dict | None`; `preflight(settings, ctx, request: dict) -> dict` → `{"token", "expires_at", "fingerprint", "identities", "missing_ranges", "expected_requests", "calendar", "warnings", "policy"}`; `verify_token(settings, token, request) -> dict` (the decoded claims) raising `QUERY_REJECTED` on expiry or mismatch; `fingerprint_of(settings, request) -> str`.
- Job summary state is derived from the latest event's stage. `cancel` appends `cancel_pending` unless the latest stage is `fetching` or later than `validating` (then `cancel_pending` still appends and the worker decides), and the API refuses cancellation of a terminal job with `409 MATERIALIZATION_NOT_BUILT`? No: use `QUERY_REJECTED` with `details.state`.

- [ ] **Step 1: Write the failing tests**

`tests/test_jobs.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.store.jobs import (
    STAGES, TERMINAL, append_event, by_fingerprint, claim, create_job, events, job, queued,
)
from security_master_api.store.seed import seed


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return s


def test_stage_tables():
    assert STAGES[0] == "queued" and STAGES[-1] == "gold_ready"
    assert "cancel_pending" in TERMINAL and "gold_ready" in TERMINAL


def test_create_is_idempotent_by_fingerprint(settings):
    j = create_job(settings, "price_daily", {"symbols": ["AAPL"]}, "fp1", "th")
    assert j["state"] == "queued" and j["job_id"].startswith("job_")
    again = create_job(settings, "price_daily", {"symbols": ["AAPL"]}, "fp1", "th")
    assert again["job_id"] == j["job_id"]
    assert by_fingerprint(settings, "fp1")["job_id"] == j["job_id"]
    assert [q["job_id"] for q in queued(settings)] == [j["job_id"]]


def test_events_are_monotonic_and_drive_state(settings):
    j = create_job(settings, "price_daily", {}, "fp2", "th")
    assert claim(settings, j["job_id"], "w1") is True
    assert claim(settings, j["job_id"], "w2") is False
    append_event(settings, j["job_id"], "bronze_retained", {"captures": 1}, "w1")
    summary = job(settings, j["job_id"])
    assert summary["state"] == "bronze_retained"
    assert [e["stage"] for e in events(settings, j["job_id"])] == ["queued", "fetching", "bronze_retained"]
    assert [e["seq"] for e in events(settings, j["job_id"])] == [0, 1, 2]
    assert queued(settings) == []


def test_terminal_then_new_job_for_same_fingerprint(settings):
    j = create_job(settings, "price_daily", {}, "fp3", "th")
    append_event(settings, j["job_id"], "failed", {"error": "x"})
    assert job(settings, j["job_id"])["state"] == "failed"
    j2 = create_job(settings, "price_daily", {}, "fp3", "th")
    assert j2["job_id"] != j["job_id"]
```

`tests/test_preflight.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import time

import pytest

from security_master_api.acquire.preflight import fingerprint_of, preflight, verify_token
from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import parse_context
from security_master_api.store.seed import seed


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k", preflight_ttl_s=2)
    seed(s)
    return s


REQ = {"dataset": "price_daily", "identifiers": ["AAPL"], "start_date": "2026-09-01",
       "end_date": "2026-09-16", "policy": "missing_only", "price_basis": "raw"}


def test_preflight_resolves_and_computes_missing_ranges(settings):
    out = preflight(settings, parse_context(None), REQ)
    assert out["identities"][0]["listing_id"] == "lst_apple"
    assert out["identities"][0]["provider_symbol"] == "AAPL.US"
    assert out["missing_ranges"] == [{"listing_id": "lst_apple", "start": "2026-09-01", "end": "2026-09-13"},
                                     {"listing_id": "lst_apple", "start": "2026-09-15", "end": "2026-09-16"}]
    assert out["expected_requests"] == 1
    assert out["policy"] == "review"
    assert out["token"] and out["fingerprint"] == fingerprint_of(settings, REQ)
    claims = verify_token(settings, out["token"], REQ)
    assert claims["fingerprint"] == out["fingerprint"]


def test_token_rejects_modified_request_and_expiry(settings):
    out = preflight(settings, parse_context(None), REQ)
    with pytest.raises(DomainError) as exc:
        verify_token(settings, out["token"], {**REQ, "end_date": "2026-09-17"})
    assert exc.value.code == "QUERY_REJECTED"
    time.sleep(2.1)
    with pytest.raises(DomainError):
        verify_token(settings, out["token"], REQ)


def test_unresolved_identifier_is_reported_not_guessed(settings):
    with pytest.raises(DomainError) as exc:
        preflight(settings, parse_context(None), {**REQ, "identifiers": ["ZZZZ"]})
    assert exc.value.code == "IDENTITY_UNRESOLVED"


def test_calendar_warning_when_session_evidence_is_not_final(settings):
    req = {"dataset": "price_daily", "identifiers": ["AAPL"], "start_date": "2027-03-01",
           "end_date": "2027-03-31", "policy": "missing_only", "price_basis": "raw",
           "calendar_id": "cal_tadawul"}
    out = preflight(settings, parse_context({"mode": "known_at", "known_at": "2027-03-09T19:00:00Z"}), req)
    assert any("pending" in w for w in out["warnings"])
    assert out["calendar"]["calendar_id"] == "cal_tadawul"
```

`tests/test_app_acquisitions.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest
from fastapi.testclient import TestClient

from security_master_api.app.main import create_app
from security_master_api.config import Settings
from security_master_api.store.jobs import append_event
from security_master_api.store.seed import seed
from tests.test_app_routes import V1, record

REQ = {"dataset": "price_daily", "identifiers": ["AAPL"], "start_date": "2026-09-01",
       "end_date": "2026-09-16", "policy": "missing_only", "price_basis": "raw"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "false")
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return TestClient(create_app(s))


def test_preflight_then_create_then_get_then_cancel(client):
    pf = client.post(f"{V1}/acquisitions/preflight", json={"request": REQ, "context": {}})
    assert pf.status_code == 200 and pf.json()["token"]
    record("acquisition_preflight", pf.json())
    created = client.post(f"{V1}/acquisitions", json={"request": REQ, "token": pf.json()["token"]})
    assert created.status_code == 201
    job = created.json()
    assert job["state"] == "queued" and job["kind"] == "price_daily"
    record("acquisition_job", job)
    got = client.get(f"{V1}/acquisitions/{job['job_id']}").json()
    assert got["job_id"] == job["job_id"] and got["stage_history"][0]["stage"] == "queued"
    dup = client.post(f"{V1}/acquisitions", json={"request": REQ, "token": pf.json()["token"]})
    assert dup.status_code == 200 and dup.json()["job_id"] == job["job_id"]
    c = client.post(f"{V1}/acquisitions/{job['job_id']}/cancel").json()
    assert c["state"] == "cancel_pending"
    record("acquisition_cancel", c)


def test_modified_or_missing_token_is_refused(client):
    pf = client.post(f"{V1}/acquisitions/preflight", json={"request": REQ, "context": {}}).json()
    r = client.post(f"{V1}/acquisitions", json={"request": {**REQ, "end_date": "2026-09-17"}, "token": pf["token"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "QUERY_REJECTED"
    r = client.post(f"{V1}/acquisitions", json={"request": REQ})
    assert r.status_code == 422


def test_acquisition_disabled_by_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "false")
    s = Settings(root=str(tmp_path), preflight_secret="k", acquisition_policy="disabled")
    seed(s)
    c = TestClient(create_app(s))
    r = c.post(f"{V1}/acquisitions/preflight", json={"request": REQ, "context": {}})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ACQUISITION_REVIEW_REQUIRED"


def test_events_stream_replays_history(client):
    pf = client.post(f"{V1}/acquisitions/preflight", json={"request": REQ, "context": {}}).json()
    job = client.post(f"{V1}/acquisitions", json={"request": REQ, "token": pf["token"]}).json()
    append_event(client.app.state.settings, job["job_id"], "failed", {"error": "stub"})
    with client.stream("GET", f"{V1}/acquisitions/{job['job_id']}/events", params={"replay": "1"}) as r:
        assert r.status_code == 200
        text = "".join(r.iter_text())
    assert "event: queued" in text and "event: failed" in text


def test_read_through_lookup_reports_review_required(client):
    r = client.post(f"{V1}/resolve", json={"identifier": "ZZZZ", "context": {}, "lookup_policy": "review"})
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "ACQUISITION_REVIEW_REQUIRED"
    assert body["error"]["details"]["preflight"]["request"]["dataset"] == "security_lookup"
    record("error_review_required", body)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --extra dev pytest -q tests/test_jobs.py tests/test_preflight.py tests/test_app_acquisitions.py`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write `store/jobs.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Durable job state as two append-only Delta tables. State is the latest event's stage."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from deltalake.exceptions import CommitFailedError

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import parse_context
from security_master_api.sql.session import open_session
from security_master_api.store.tables import append

STAGES = ("queued", "fetching", "bronze_retained", "normalizing", "validating", "silver_ready",
          "materializing", "gold_ready")
TERMINAL = ("partial", "rate_limited", "entitlement_denied", "validation_failed", "cancel_pending",
            "cancelled", "failed", "gold_ready")
ALL_STAGES = STAGES + tuple(s for s in TERMINAL if s not in STAGES)


def _now() -> datetime:
    return datetime.now(UTC)


def _j(v):
    return v.isoformat().replace("+00:00", "Z") if hasattr(v, "isoformat") else v


def _events_table(settings: Settings, where: str, params: list) -> list[dict]:
    ctx = parse_context({"mode": "delta_snapshot"})
    with open_session(settings, ctx, ["ops.job_events"]) as s:
        return s.run(f"SELECT * FROM ops.job_events WHERE {where} ORDER BY seq", params).to_pylist()


def _jobs_table(settings: Settings, where: str, params: list) -> list[dict]:
    ctx = parse_context({"mode": "delta_snapshot"})
    with open_session(settings, ctx, ["ops.jobs"]) as s:
        return s.run("SELECT * FROM ops.jobs WHERE " + where +
                     " QUALIFY row_number() OVER (PARTITION BY job_id ORDER BY seq DESC) = 1",
                     params).to_pylist()


def events(settings: Settings, job_id: str) -> list[dict]:
    rows = _events_table(settings, "job_id = ?", [job_id])
    return [{**{k: _j(v) for k, v in r.items()}, "detail": json.loads(r["detail"] or "{}")} for r in rows]


def job(settings: Settings, job_id: str) -> dict:
    rows = _jobs_table(settings, "job_id = ?", [job_id])
    if not rows:
        raise DomainError("NO_MATCHING_FACTS", f"unknown job {job_id}", {"job_id": job_id})
    evs = events(settings, job_id)
    latest = evs[-1] if evs else None
    row = rows[0]
    return {
        "job_id": row["job_id"], "kind": row["kind"], "fingerprint": row["fingerprint"],
        "state": latest["stage"] if latest else row["state"],
        "created_at": _j(row["created_at"]), "updated_at": latest["at"] if latest else _j(row["updated_at"]),
        "request": json.loads(row["request"] or "{}"), "summary": json.loads(row["summary"] or "{}"),
        "stage_history": [{"stage": e["stage"], "at": e["at"], "detail": e["detail"],
                           "worker_id": e["worker_id"]} for e in evs],
    }


def by_fingerprint(settings: Settings, fingerprint: str) -> dict | None:
    for row in _jobs_table(settings, "fingerprint = ?", [fingerprint]):
        summary = job(settings, row["job_id"])
        if summary["state"] not in TERMINAL:
            return summary
    return None


def queued(settings: Settings) -> list[dict]:
    out = []
    for row in _jobs_table(settings, "TRUE", []):
        summary = job(settings, row["job_id"])
        if summary["state"] == "queued":
            out.append(summary)
    return sorted(out, key=lambda j: j["created_at"])


def create_job(settings: Settings, kind: str, request: dict, fingerprint: str,
               token_hash: str) -> dict:
    existing = by_fingerprint(settings, fingerprint)
    if existing is not None:
        return existing
    job_id = "job_" + uuid.uuid4().hex[:12]
    now = _now()
    append(settings, "ops.jobs", [{
        "job_id": job_id, "kind": kind, "fingerprint": fingerprint, "state": "queued",
        "created_at": now, "updated_at": now, "request": json.dumps(request, sort_keys=True),
        "summary": "{}", "preflight_token_hash": token_hash, "seq": 0,
    }])
    append_event(settings, job_id, "queued", {"kind": kind}, "api")
    return job(settings, job_id)


def append_event(settings: Settings, job_id: str, stage: str, detail: dict | None = None,
                 worker_id: str = "api") -> dict:
    if stage not in ALL_STAGES:
        raise DomainError("QUERY_REJECTED", f"unknown stage {stage}", {"stage": stage})
    seq = len(_events_table(settings, "job_id = ?", [job_id]))
    row = {"event_id": "ev_" + uuid.uuid4().hex[:12], "job_id": job_id, "seq": seq,
           "stage": stage, "at": _now(), "detail": json.dumps(detail or {}, sort_keys=True,
                                                             default=str), "worker_id": worker_id}
    append(settings, "ops.job_events", [row])
    return {**{k: _j(v) for k, v in row.items()}, "detail": detail or {}}


def claim(settings: Settings, job_id: str, worker_id: str) -> bool:
    """Append `fetching`; the first fetching event for the job names the winner."""
    evs = _events_table(settings, "job_id = ?", [job_id])
    if any(e["stage"] == "fetching" for e in evs) or evs[-1]["stage"] != "queued":
        return False
    try:
        append_event(settings, job_id, "fetching", {}, worker_id)
    except CommitFailedError:
        return False
    first = next(e for e in _events_table(settings, "job_id = ? AND stage = 'fetching'", [job_id]))
    return first["worker_id"] == worker_id
```

- [ ] **Step 4: Write `acquire/preflight.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Preflight: what an acquisition would touch, signed so the job cannot drift from it."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import date, timedelta

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import Context
from security_master_api.resolver.identity import resolve
from security_master_api.sql.session import open_session

DATASETS = ("price_daily", "exchange_calendar", "shares_outstanding", "security_lookup")
POLICIES = ("missing_only", "refresh", "force")


def _canonical(request: dict) -> bytes:
    return json.dumps(request, sort_keys=True, separators=(",", ":"), default=str).encode()


def fingerprint_of(settings: Settings, request: dict) -> str:
    return hashlib.sha256(_canonical(request) + settings.code_version.encode()).hexdigest()[:24]


def _sign(settings: Settings, claims: dict) -> str:
    body = base64.urlsafe_b64encode(_canonical(claims)).decode().rstrip("=")
    mac = hmac.new(settings.preflight_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def verify_token(settings: Settings, token: str, request: dict) -> dict:
    try:
        body, mac = token.split(".", 1)
    except ValueError as exc:
        raise DomainError("QUERY_REJECTED", "malformed preflight token") from exc
    expected = hmac.new(settings.preflight_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise DomainError("QUERY_REJECTED", "preflight token signature does not match")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if claims["exp"] < time.time():
        raise DomainError("QUERY_REJECTED", "preflight token has expired", {"expired_at": claims["exp"]})
    if claims["fingerprint"] != fingerprint_of(settings, request):
        raise DomainError("QUERY_REJECTED", "the request differs from its preflight")
    return claims


def _missing_ranges(settings: Settings, ctx: Context, listing_id: str, start: date, end: date,
                    policy: str) -> list[dict]:
    if policy == "force":
        return [{"listing_id": listing_id, "start": start.isoformat(), "end": end.isoformat()}]
    with open_session(settings, ctx, ["gold.price_daily"]) as s:
        have = {r["market_date"] for r in s.run(
            "SELECT DISTINCT market_date FROM gold.price_daily WHERE listing_id = ? "
            "AND market_date BETWEEN ? AND ?", [listing_id, start.isoformat(), end.isoformat()]).to_pylist()}
    ranges: list[dict] = []
    cursor = None
    day = start
    while day <= end:
        if day not in have and day.weekday() < 5:
            cursor = cursor or day
        elif cursor is not None:
            ranges.append({"listing_id": listing_id, "start": cursor.isoformat(),
                           "end": (day - timedelta(days=1)).isoformat()})
            cursor = None
        day += timedelta(days=1)
    if cursor is not None:
        ranges.append({"listing_id": listing_id, "start": cursor.isoformat(), "end": end.isoformat()})
    return ranges


def _calendar(settings: Settings, ctx: Context, calendar_id: str | None, start: date, end: date) -> tuple[dict | None, list[str]]:
    if not calendar_id:
        return None, []
    with open_session(settings, ctx, ["gold.market_calendar"]) as s:
        rows = s.run("SELECT session_date, evidence_status, market_effect FROM gold.market_calendar "
                     "WHERE calendar_id = ? AND session_date BETWEEN ? AND ?",
                     [calendar_id, start.isoformat(), end.isoformat()]).to_pylist()
        manifest = s.manifest
    warnings = [f"{r['session_date']}: market effect {r['market_effect']} ({r['evidence_status']}) is not final"
                for r in rows if r["market_effect"] == "pending" or r["evidence_status"] not in ("market_final", "observed")]
    return {"calendar_id": calendar_id, "sessions": len(rows), "manifest": manifest}, warnings


def preflight(settings: Settings, ctx: Context, request: dict) -> dict:
    if settings.acquisition_policy == "disabled":
        raise DomainError("ACQUISITION_REVIEW_REQUIRED", "acquisition is disabled by policy",
                          {"policy": "disabled"})
    dataset = request.get("dataset")
    if dataset not in DATASETS:
        raise DomainError("QUERY_REJECTED", f"unknown dataset {dataset!r}", {"supported": list(DATASETS)})
    policy = request.get("policy", "missing_only")
    if policy not in POLICIES:
        raise DomainError("QUERY_REJECTED", f"unknown policy {policy!r}", {"supported": list(POLICIES)})
    identities = []
    for ident in request.get("identifiers", []):
        cands = resolve(settings, ctx, ident)["candidates"]
        c = cands[0]
        identities.append({"identifier": ident, "listing_id": c["listing_id"],
                           "instrument_id": c["instrument_id"], "security_id": c.get("security_id"),
                           "provider_symbol": _provider_symbol(settings, ctx, c["listing_id"]),
                           "reason": c["reason"]})
    missing: list[dict] = []
    calendar, warnings = None, []
    if dataset == "price_daily":
        start, end = date.fromisoformat(request["start_date"]), date.fromisoformat(request["end_date"])
        if end < start:
            raise DomainError("QUERY_REJECTED", "end_date precedes start_date")
        for ident in identities:
            missing += _missing_ranges(settings, ctx, ident["listing_id"], start, end, policy)
        calendar, warnings = _calendar(settings, ctx, request.get("calendar_id"), start, end)
    expected = len(missing) if dataset == "price_daily" else max(1, len(identities))
    fingerprint = fingerprint_of(settings, request)
    claims = {"fingerprint": fingerprint, "exp": int(time.time()) + settings.preflight_ttl_s,
              "dataset": dataset, "expected_requests": expected}
    return {
        "token": _sign(settings, claims), "expires_at": claims["exp"], "fingerprint": fingerprint,
        "dataset": dataset, "identities": identities, "missing_ranges": missing,
        "expected_requests": expected, "calendar": calendar, "warnings": warnings,
        "policy": settings.acquisition_policy, "code_version": settings.code_version,
        "context": ctx.as_dict(),
    }


def _provider_symbol(settings: Settings, ctx: Context, listing_id: str) -> str | None:
    with open_session(settings, ctx, ["gold.security_master"]) as s:
        rows = s.run("SELECT provider_symbol FROM gold.security_master WHERE listing_id = ?",
                     [listing_id]).to_pylist()
    return rows[0]["provider_symbol"] if rows else None
```

- [ ] **Step 5: Add the acquisition routes and read-through review to `app/main.py`**

Add the models and routes:

```python
class PreflightBody(BaseModel):
    request: dict[str, Any]
    context: dict[str, Any] = Field(default_factory=dict)


class CreateJobBody(BaseModel):
    request: dict[str, Any]
    token: str


@app.post(f"{V1}/acquisitions/preflight")
def acquisitions_preflight(body: PreflightBody) -> dict:
    return preflight(settings, parse_context(body.context), body.request)


@app.post(f"{V1}/acquisitions", status_code=201)
def acquisitions_create(body: CreateJobBody, response: Response) -> dict:
    if settings.acquisition_policy == "disabled":
        raise DomainError("ACQUISITION_REVIEW_REQUIRED", "acquisition is disabled by policy")
    claims = verify_token(settings, body.token, body.request)
    existing = by_fingerprint(settings, claims["fingerprint"])
    if existing is not None:
        response.status_code = 200
        return existing
    token_hash = hashlib.sha256(body.token.encode()).hexdigest()[:16]
    return create_job(settings, claims["dataset"], body.request, claims["fingerprint"], token_hash)


@app.get(f"{V1}/acquisitions/{{job_id}}")
def acquisitions_get(job_id: str) -> dict:
    return job(settings, job_id)


@app.post(f"{V1}/acquisitions/{{job_id}}/cancel")
def acquisitions_cancel(job_id: str) -> dict:
    current = job(settings, job_id)
    if current["state"] in TERMINAL:
        raise DomainError("QUERY_REJECTED", f"job is already {current['state']}",
                          {"state": current["state"]})
    append_event(settings, job_id, "cancel_pending", {"requested_by": "api"})
    return job(settings, job_id)


@app.get(f"{V1}/acquisitions/{{job_id}}/events")
def acquisitions_events(job_id: str, replay: str = "0") -> StreamingResponse:
    job(settings, job_id)

    def stream():
        seen = 0
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            evs = events(settings, job_id)
            for e in evs[seen:]:
                yield f"event: {e['stage']}\ndata: {json.dumps(e)}\n\n"
            seen = len(evs)
            if evs and evs[-1]["stage"] in TERMINAL:
                return
            if replay == "1" and evs and evs[-1]["stage"] in TERMINAL:
                return
            time.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream")
```

with `import hashlib, time`, `from fastapi.responses import StreamingResponse`, `from security_master_api.acquire.preflight import preflight, verify_token`, `from security_master_api.store.jobs import TERMINAL, append_event, by_fingerprint, create_job, events, job`. In `resolve_route`, accept an optional `lookup_policy: str | None = None` on `ResolveBody`; when `run_resolve` raises `IDENTITY_UNRESOLVED` and `lookup_policy == "review"` and `settings.acquisition_policy != "disabled"`, raise `DomainError("ACQUISITION_REVIEW_REQUIRED", "no local match; a bounded lookup is available for review", {"preflight": preflight(settings, ctx, {"dataset": "security_lookup", "identifiers": [], "query": body.identifier})})`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check .`
Expected: PASS; `tests/contract/` gains `acquisition_preflight`, `acquisition_job`, `acquisition_cancel`, `error_review_required`.

- [ ] **Step 7: Commit**

```bash
git add security-master-api
git commit -m "feat(security-master): durable Delta-backed jobs, signed preflight and the acquisition routes"
```

### Task 12: openbb-api client, normalizers, and the worker

**Files:**
- Create: `security_master_api/acquire/client.py`, `acquire/normalize.py`, `acquire/worker.py`, `acquire/__main__.py`
- Test: `tests/test_normalize.py`, `tests/test_worker.py`, `tests/stub_openbb.py`

**Interfaces:**
- Produces: `OpenbbClient(base_url, username, password, timeout_s=60)` with `get(path, params) -> Response` (`Response(status: int, body: str, json: object | None, retry_after_s: int | None)`) and `classify(status) -> str` (`"ok" | "auth" | "entitlement" | "not_covered" | "rate_limited" | "transport"`); `normalize_price_daily(payload, listing_id, capture_id, observed_at, price_basis) -> list[dict]`; `normalize_exchange_calendar(payload, calendar_id, capture_id, observed_at) -> list[dict]` (rows for `silver.calendar_exceptions`, `assertion_domain="market_session"`, `source_kind="eodhd"`, `evidence_status="estimated"`); `normalize_shares_outstanding(payload, issuer_id, capture_id, observed_at) -> list[dict]`; `validate(relation, rows) -> list[str]` (problems; empty = valid); `run_once(settings, client, worker_id) -> int`; `main()`.
- Worker stages per job: `fetching` (claimed) → for each request: `bronze.request_log` + `bronze.source_captures` (payload text) → `bronze_retained` → `normalizing` (rows) → `validating` (`validate`) → `silver_ready` (append with close rows for superseded assertions under `refresh`/`force`) → `materializing` (record a receipt of the pinned versions) → `gold_ready`. A `cancel_pending` event seen between requests ends the job at `cancelled`; a 429 ends at `rate_limited` with `retry_after_s` in the detail; a 403 at `entitlement_denied`; validation problems at `validation_failed` with Bronze kept; other exceptions at `failed`.

- [ ] **Step 1: Write `tests/stub_openbb.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""A scriptable stand-in for openbb-api: status, body and delay per path."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


class StubOpenbb:
    def __init__(self):
        self.routes: dict[str, tuple[int, object, dict]] = {}
        self.requests: list[tuple[str, dict]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                from urllib.parse import parse_qs

                stub.requests.append((parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}))
                if self.headers.get("Authorization", "")[:6] != "Basic ":
                    self.send_response(401); self.end_headers(); return
                status, body, headers = stub.routes.get(parsed.path, (404, {"detail": "no route"}, {}))
                payload = body if isinstance(body, (bytes, str)) else json.dumps(body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(payload.encode() if isinstance(payload, str) else payload)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
```

- [ ] **Step 2: Write the failing normalizer test**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime

from security_master_api.acquire.normalize import (
    normalize_exchange_calendar, normalize_price_daily, normalize_shares_outstanding, validate,
)

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


def test_price_rows_carry_provenance_and_intervals():
    payload = {"results": [{"date": "2026-09-15", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}]}
    rows = normalize_price_daily(payload, "lst_apple", "cap_x", NOW, "raw")
    r = rows[0]
    assert (r["listing_id"], str(r["market_date"]), r["close"]) == ("lst_apple", "2026-09-15", 1.5)
    assert r["capture_id"] == "cap_x" and r["available_at"] == NOW and r["system_to"] is None
    assert r["assertion_id"] == "as_px_lst_apple_2026-09-15_cap_x"
    assert validate("silver.prices_normalized", rows) == []


def test_price_validation_catches_bad_ranges():
    rows = normalize_price_daily({"results": [{"date": "2026-09-15", "open": 1, "high": 0.4, "low": 0.5,
                                               "close": 1.5, "volume": -1}]}, "lst_apple", "cap_x", NOW, "raw")
    problems = validate("silver.prices_normalized", rows)
    assert any("high" in p for p in problems) and any("volume" in p for p in problems)


def test_exchange_calendar_rows_never_claim_authority():
    payload = {"results": {"Code": "US", "Timezone": "America/New_York", "ExchangeHolidays": {
        "0": {"Holiday": "Christmas", "Date": "2026-12-25", "Type": "official"},
        "1": {"Holiday": "Early close", "Date": "2026-12-24", "Type": "early_close"}}}}
    rows = normalize_exchange_calendar(payload, "cal_xnys", "cap_c", NOW)
    by = {str(r["session_date"]): r for r in rows}
    assert by["2026-12-25"]["market_effect"] == "closed" and by["2026-12-24"]["market_effect"] == "early_close"
    assert all(r["assertion_domain"] == "market_session" and r["source_kind"] == "eodhd"
               and r["evidence_status"] == "estimated" and r["authority_type"] == "calendar_package"
               for r in rows)


def test_shares_outstanding_row():
    payload = {"results": [{"shares_outstanding": 1480000000, "period_end": "2026-06-30"}]}
    rows = normalize_shares_outstanding(payload, "iss_example", "cap_s", NOW)
    assert rows[0]["fact"] == "shares_outstanding" and rows[0]["value"] == 1.48e9
    assert str(rows[0]["period_end"]) == "2026-06-30"


def test_malformed_payload_is_a_problem_not_a_crash():
    assert normalize_price_daily({"nope": 1}, "l", "c", NOW, "raw") == []
    assert validate("silver.prices_normalized", []) == ["no rows"]
```

- [ ] **Step 3: Write the failing worker test**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.acquire.client import OpenbbClient
from security_master_api.acquire.preflight import fingerprint_of
from security_master_api.acquire.worker import run_once
from security_master_api.config import Settings
from security_master_api.resolver.context import parse_context
from security_master_api.sql.session import open_session
from security_master_api.store.jobs import append_event, create_job, job
from security_master_api.store.seed import seed
from security_master_api.store.tables import open_table
from tests.stub_openbb import StubOpenbb

REQ = {"dataset": "price_daily", "identifiers": ["AAPL"], "start_date": "2026-09-15",
       "end_date": "2026-09-16", "policy": "missing_only", "price_basis": "raw"}
PRICES = {"results": [{"date": "2026-09-15", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
                      {"date": "2026-09-16", "open": 1, "high": 2, "low": 0.5, "close": 1.6, "volume": 11}]}


@pytest.fixture
def stub():
    s = StubOpenbb()
    yield s
    s.close()


@pytest.fixture
def settings(tmp_path, stub):
    s = Settings(root=str(tmp_path), preflight_secret="k", openbb_url=stub.url,
                 openbb_username="u", openbb_password="p")
    seed(s)
    return s


def make_job(settings):
    return create_job(settings, "price_daily", REQ, fingerprint_of(settings, REQ), "th")


def test_happy_path_reaches_gold_ready(settings, stub):
    stub.routes["/api/v1/equity/price/historical"] = (200, PRICES, {})
    j = make_job(settings)
    assert run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1") == 1
    done = job(settings, j["job_id"])
    assert [s["stage"] for s in done["stage_history"]] == [
        "queued", "fetching", "bronze_retained", "normalizing", "validating", "silver_ready",
        "materializing", "gold_ready"]
    assert stub.requests[0][1]["provider"] == "eodhd" and stub.requests[0][1]["symbol"] == "AAPL.US"
    caps = open_table(settings, "bronze.source_captures").to_pyarrow_table().to_pylist()
    assert any(c["job_id"] == j["job_id"] and c["status"] == 200 for c in caps)
    with open_session(settings, parse_context(None), ["gold.price_daily"]) as s:
        rows = s.run("SELECT market_date, close FROM gold.price_daily WHERE listing_id = 'lst_apple' "
                     "AND market_date >= DATE '2026-09-15' ORDER BY market_date").to_pylist()
    assert [r["close"] for r in rows] == [1.5, 1.6]
    assert done["summary"]["dependencies"]
    assert run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1") == 0


def test_rate_limit_and_entitlement_are_terminal_with_bronze_kept(settings, stub):
    stub.routes["/api/v1/equity/price/historical"] = (429, {"detail": "slow down"}, {"Retry-After": "30"})
    j = make_job(settings)
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    done = job(settings, j["job_id"])
    assert done["state"] == "rate_limited" and done["stage_history"][-1]["detail"]["retry_after_s"] == 30
    caps = open_table(settings, "bronze.source_captures").to_pyarrow_table().to_pylist()
    assert any(c["job_id"] == j["job_id"] and c["status"] == 429 for c in caps)
    stub.routes["/api/v1/equity/price/historical"] = (403, {"detail": "plan"}, {})
    j2 = create_job(settings, "price_daily", {**REQ, "end_date": "2026-09-17"},
                    fingerprint_of(settings, {**REQ, "end_date": "2026-09-17"}), "th")
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    assert job(settings, j2["job_id"])["state"] == "entitlement_denied"


def test_validation_failure_keeps_bronze(settings, stub):
    stub.routes["/api/v1/equity/price/historical"] = (200, {"results": [{"date": "2026-09-15", "open": 1,
                                                                        "high": 0.1, "low": 0.5, "close": 1, "volume": 1}]}, {})
    j = make_job(settings)
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    done = job(settings, j["job_id"])
    assert done["state"] == "validation_failed" and done["stage_history"][-1]["detail"]["problems"]
    caps = open_table(settings, "bronze.source_captures").to_pyarrow_table().to_pylist()
    assert any(c["job_id"] == j["job_id"] for c in caps)


def test_malformed_payload_fails_cleanly(settings, stub):
    stub.routes["/api/v1/equity/price/historical"] = (200, "not json{", {})
    j = make_job(settings)
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    assert job(settings, j["job_id"])["state"] == "validation_failed"


def test_cancel_pending_is_honoured_between_requests(settings, stub):
    stub.routes["/api/v1/equity/price/historical"] = (200, PRICES, {})
    two = {**REQ, "identifiers": ["AAPL", "META"], "start_date": "2022-06-01", "end_date": "2026-09-16"}
    j = create_job(settings, "price_daily", two, fingerprint_of(settings, two), "th")
    append_event(settings, j["job_id"], "cancel_pending", {})
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    assert job(settings, j["job_id"])["state"] == "cancelled"


def test_restart_safety_a_claimed_job_is_not_reclaimed(settings, stub):
    stub.routes["/api/v1/equity/price/historical"] = (200, PRICES, {})
    j = make_job(settings)
    append_event(settings, j["job_id"], "fetching", {}, "w-dead")
    assert run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1") == 0
    assert job(settings, j["job_id"])["state"] == "fetching"
```

- [ ] **Step 4: Run them to verify they fail**

Run: `uv run --extra dev pytest -q tests/test_normalize.py tests/test_worker.py`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 5: Write `acquire/client.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The worker's only network client: this stack's openbb-api, with Basic auth. No provider key."""

from __future__ import annotations

from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class Response:
    status: int
    body: str
    json: object | None
    retry_after_s: int | None


def classify(status: int) -> str:
    if 200 <= status < 300:
        return "ok"
    if status == 401:
        return "auth"
    if status == 403:
        return "entitlement"
    if status == 404:
        return "not_covered"
    if status == 429:
        return "rate_limited"
    return "transport"


class OpenbbClient:
    def __init__(self, base_url: str, username: str, password: str, timeout_s: int = 60):
        self.base_url = base_url.rstrip("/")
        self._auth = (username, password)
        self.timeout_s = timeout_s

    def get(self, path: str, params: dict) -> Response:
        try:
            r = requests.get(f"{self.base_url}{path}", params=params, auth=self._auth,
                             timeout=self.timeout_s)
        except requests.RequestException as exc:
            return Response(0, str(exc)[:500], None, None)
        retry = r.headers.get("Retry-After")
        try:
            parsed = r.json()
        except ValueError:
            parsed = None
        return Response(r.status_code, r.text, parsed, int(retry) if retry and retry.isdigit() else None)
```

- [ ] **Step 6: Write `acquire/normalize.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""OpenBB responses become Silver assertion rows. Nothing here claims more than the source did."""

from __future__ import annotations

from datetime import UTC, date, datetime


def _interval(capture_id: str, observed_at: datetime, assertion_id: str,
              effective_from: datetime) -> dict:
    return {"effective_from": effective_from, "effective_to": None, "observed_at": observed_at,
            "available_at": observed_at, "system_from": observed_at, "system_to": None,
            "capture_id": capture_id, "assertion_id": assertion_id, "assertion_status": "current",
            "supersedes_assertion_id": None}


def _results(payload) -> list[dict]:
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return [r for r in payload["results"] if isinstance(r, dict)]
    return []


def _num(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def normalize_price_daily(payload, listing_id: str, capture_id: str, observed_at: datetime,
                          price_basis: str) -> list[dict]:
    rows = []
    for r in _results(payload):
        try:
            day = date.fromisoformat(str(r.get("date"))[:10])
        except ValueError:
            continue
        rows.append({
            "listing_id": listing_id, "market_date": day, "open": _num(r.get("open")),
            "high": _num(r.get("high")), "low": _num(r.get("low")), "close": _num(r.get("close")),
            "volume": _num(r.get("volume")), "price_basis": price_basis,
            **_interval(capture_id, observed_at, f"as_px_{listing_id}_{day.isoformat()}_{capture_id}",
                        datetime.combine(day, datetime.min.time(), tzinfo=UTC)),
        })
    return rows


_EFFECTS = {"official": "closed", "closed": "closed", "early_close": "early_close",
            "half_day": "early_close", "late_open": "late_open"}


def normalize_exchange_calendar(payload, calendar_id: str, capture_id: str,
                                observed_at: datetime) -> list[dict]:
    results = payload.get("results") if isinstance(payload, dict) else None
    holidays = (results or {}).get("ExchangeHolidays") if isinstance(results, dict) else None
    rows = []
    for entry in (holidays or {}).values() if isinstance(holidays, dict) else []:
        if not isinstance(entry, dict):
            continue
        try:
            day = date.fromisoformat(str(entry.get("Date"))[:10])
        except ValueError:
            continue
        effect = _EFFECTS.get(str(entry.get("Type", "official")).lower(), "closed")
        rows.append({
            "calendar_id": calendar_id, "session_date": day, "assertion_domain": "market_session",
            "holiday_family": None, "calendar_system": "gregorian", "evidence_status": "estimated",
            "authority_type": "calendar_package", "authority_name": "EODHD exchange details",
            "authority_verified_at": None, "source_kind": "eodhd", "source_version": capture_id,
            "rule_id": None, "market_effect": effect, "holiday_name": entry.get("Holiday"),
            "special_open": effect == "late_open", "special_close": effect == "early_close",
            "market_open": None, "market_close": None, "candidate_branches": None,
            "selected_branch": None, "expiry_date": None, "settlement_status": None,
            **_interval(capture_id, observed_at, f"as_cal_{calendar_id}_{day.isoformat()}_{capture_id}",
                        datetime.combine(day, datetime.min.time(), tzinfo=UTC)),
        })
    return rows


def normalize_shares_outstanding(payload, issuer_id: str, capture_id: str,
                                 observed_at: datetime) -> list[dict]:
    rows = []
    for r in _results(payload):
        value = _num(r.get("shares_outstanding"))
        if value is None:
            continue
        period = r.get("period_end") or r.get("date") or observed_at.date().isoformat()
        try:
            period_end = date.fromisoformat(str(period)[:10])
        except ValueError:
            continue
        rows.append({
            "issuer_id": issuer_id, "fact": "shares_outstanding", "period_end": period_end,
            "value": value, "unit": "shares", "filing_kind": r.get("filing_kind") or "provider",
            **_interval(capture_id, observed_at,
                        f"as_so_{issuer_id}_{period_end.isoformat()}_{capture_id}",
                        datetime.combine(period_end, datetime.min.time(), tzinfo=UTC)),
        })
    return rows


def validate(relation: str, rows: list[dict]) -> list[str]:
    if not rows:
        return ["no rows"]
    problems = []
    if relation == "silver.prices_normalized":
        for r in rows:
            d = r["market_date"]
            if r["close"] is None:
                problems.append(f"{d}: close is missing")
            if r["high"] is not None and r["low"] is not None and r["high"] < r["low"]:
                problems.append(f"{d}: high {r['high']} below low {r['low']}")
            if r["volume"] is not None and r["volume"] < 0:
                problems.append(f"{d}: negative volume")
    if relation == "silver.fundamental_facts":
        problems += [f"{r['period_end']}: non-positive value" for r in rows if r["value"] <= 0]
    return problems
```

- [ ] **Step 7: Write `acquire/worker.py` and `acquire/__main__.py`**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The acquisition worker: claim a job, retain Bronze, normalize, validate, promote. Restart-safe."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import time
import uuid
from datetime import UTC, datetime

from security_master_api.acquire.client import OpenbbClient, classify
from security_master_api.acquire.normalize import (
    normalize_exchange_calendar, normalize_price_daily, normalize_shares_outstanding, validate,
)
from security_master_api.acquire.preflight import preflight
from security_master_api.config import Settings, settings_from_env
from security_master_api.errors import DomainError
from security_master_api.resolver.context import parse_context
from security_master_api.resolver.manifest import resolve_manifest
from security_master_api.sql.session import open_session
from security_master_api.store.jobs import append_event, claim, events, job, queued
from security_master_api.store.receipts import new_receipt, record_receipt
from security_master_api.store.tables import append

log = logging.getLogger("security-master-worker")

_ROUTES = {
    "price_daily": "/api/v1/equity/price/historical",
    "exchange_calendar": "/api/v1/reference/exchange_details",
    "shares_outstanding": "/api/v1/equity/profile",
    "security_lookup": "/api/v1/equity/search",
}


def _cancelled(settings: Settings, job_id: str) -> bool:
    return any(e["stage"] == "cancel_pending" for e in events(settings, job_id))


def _capture(settings: Settings, job_id: str, endpoint: str, params: dict, resp, attempt: int) -> str:
    now = datetime.now(UTC)
    capture_id = "cap_" + uuid.uuid4().hex[:12]
    fingerprint = hashlib.sha256(json.dumps([endpoint, params], sort_keys=True).encode()).hexdigest()[:16]
    append(settings, "bronze.source_captures", [{
        "capture_id": capture_id, "provider": "openbb-api/eodhd", "endpoint": endpoint,
        "request_fingerprint": fingerprint, "captured_at": now,
        "content_hash": hashlib.sha256(resp.body.encode()).hexdigest(), "status": resp.status,
        "payload": resp.body[:2_000_000], "job_id": job_id,
    }])
    append(settings, "bronze.request_log", [{
        "request_id": "rq_" + uuid.uuid4().hex[:12], "job_id": job_id, "capture_id": capture_id,
        "requested_at": now, "response_status": resp.status, "attempt": attempt,
        "retry_after_s": resp.retry_after_s, "error": None if resp.status else resp.body[:200],
    }])
    return capture_id


def _requests_for(settings: Settings, job_row: dict) -> list[tuple[str, dict, dict]]:
    """(endpoint, params, identity) per HTTP call the job will make."""
    req = job_row["request"]
    ctx = parse_context(None)
    pf = preflight(settings, ctx, req)
    out = []
    kind = job_row["kind"]
    if kind == "price_daily":
        by_listing = {i["listing_id"]: i for i in pf["identities"]}
        for rng in pf["missing_ranges"]:
            ident = by_listing[rng["listing_id"]]
            out.append((_ROUTES[kind], {"symbol": ident["provider_symbol"], "provider": "eodhd",
                                        "interval": "1d", "start_date": rng["start"],
                                        "end_date": rng["end"]}, ident))
    elif kind == "shares_outstanding":
        for ident in pf["identities"]:
            out.append((_ROUTES[kind], {"symbol": ident["provider_symbol"], "provider": "eodhd"}, ident))
    elif kind == "exchange_calendar":
        out.append((_ROUTES[kind], {"code": req["exchange_code"]}, {"calendar_id": req["calendar_id"]}))
    elif kind == "security_lookup":
        out.append((_ROUTES[kind], {"query": req["query"], "provider": "eodhd"}, {}))
    return out


def _normalize(kind: str, payload, ident: dict, capture_id: str, now: datetime, req: dict):
    if kind == "price_daily":
        return "silver.prices_normalized", normalize_price_daily(
            payload, ident["listing_id"], capture_id, now, req.get("price_basis", "raw"))
    if kind == "shares_outstanding":
        with_issuer = ident.get("issuer_id") or ident["listing_id"]
        return "silver.fundamental_facts", normalize_shares_outstanding(payload, with_issuer, capture_id, now)
    if kind == "exchange_calendar":
        return "silver.calendar_exceptions", normalize_exchange_calendar(
            payload, ident["calendar_id"], capture_id, now)
    return None, []


def _supersede(settings: Settings, relation: str, rows: list[dict], now: datetime) -> list[dict]:
    """Under refresh/force, close the assertions the new rows replace."""
    keys = {"silver.prices_normalized": ("listing_id", "market_date"),
            "silver.fundamental_facts": ("issuer_id", "fact", "period_end"),
            "silver.calendar_exceptions": ("calendar_id", "session_date", "assertion_domain")}[relation]
    layer, name = relation.split(".", 1)
    ctx = parse_context(None)
    with open_session(settings, ctx, [relation]) as s:
        current = s.run(f'SELECT * FROM {layer}."{name}" WHERE system_to IS NULL '
                        "QUALIFY row_number() OVER (PARTITION BY assertion_id ORDER BY system_from DESC) = 1").to_pylist()
    by_key = {tuple(str(c[k]) for k in keys): c for c in current}
    closes = []
    for r in rows:
        old = by_key.get(tuple(str(r[k]) for k in keys))
        if old is not None and old["assertion_id"] != r["assertion_id"]:
            closed = dict(old)
            closed.update({"system_from": now, "system_to": now, "assertion_status": "superseded"})
            closes.append(closed)
            r["supersedes_assertion_id"] = old["assertion_id"]
    return closes


def process(settings: Settings, client: OpenbbClient, job_id: str, worker_id: str) -> None:
    row = job(settings, job_id)
    kind, req = row["kind"], row["request"]
    now = datetime.now(UTC)
    run_id = "run_" + uuid.uuid4().hex[:12]
    append(settings, "bronze.ingestion_runs", [{"run_id": run_id, "job_id": job_id,
                                                "code_version": settings.code_version,
                                                "started_at": now, "ended_at": None, "outcome": None}])
    captures: list[tuple[str, object, dict]] = []
    try:
        for attempt, (endpoint, params, ident) in enumerate(_requests_for(settings, row), start=1):
            if _cancelled(settings, job_id):
                append_event(settings, job_id, "cancelled", {"after_requests": attempt - 1}, worker_id)
                return
            resp = client.get(endpoint, params)
            capture_id = _capture(settings, job_id, endpoint, params, resp, attempt)
            outcome = classify(resp.status)
            if outcome == "rate_limited":
                append_event(settings, job_id, "rate_limited",
                             {"retry_after_s": resp.retry_after_s, "capture_id": capture_id}, worker_id)
                return
            if outcome == "entitlement":
                append_event(settings, job_id, "entitlement_denied", {"capture_id": capture_id}, worker_id)
                return
            if outcome != "ok":
                append_event(settings, job_id, "failed", {"status": resp.status, "capture_id": capture_id,
                                                          "outcome": outcome}, worker_id)
                return
            captures.append((capture_id, resp.json, ident))
        append_event(settings, job_id, "bronze_retained", {"captures": len(captures)}, worker_id)
        append_event(settings, job_id, "normalizing", {}, worker_id)
        relation, rows = None, []
        for capture_id, payload, ident in captures:
            relation, part = _normalize(kind, payload, ident, capture_id, now, req)
            rows += part
        append_event(settings, job_id, "validating", {"rows": len(rows)}, worker_id)
        problems = validate(relation, rows) if relation else ["nothing to promote"]
        if problems:
            append_event(settings, job_id, "validation_failed", {"problems": problems[:50]}, worker_id)
            return
        closes = _supersede(settings, relation, rows, now) if req.get("policy") in ("refresh", "force") else []
        if closes:
            append(settings, relation, closes)
        append(settings, relation, rows)
        append_event(settings, job_id, "silver_ready", {"relation": relation, "rows": len(rows)}, worker_id)
        append_event(settings, job_id, "materializing", {}, worker_id)
        ctx = parse_context(None)
        manifest = resolve_manifest(settings, ctx, [relation])
        receipt = new_receipt(settings, "acquisition", ctx, manifest, job_id)
        record_receipt(settings, receipt)
        append_event(settings, job_id, "gold_ready", {"dependencies": receipt["dependencies"],
                                                      "execution_id": receipt["execution_id"]}, worker_id)
    except DomainError as exc:
        append_event(settings, job_id, "failed", {"code": exc.code, "message": exc.message}, worker_id)
    except Exception as exc:  # noqa: BLE001 - a job must end in a durable state
        log.exception("job %s failed", job_id)
        append_event(settings, job_id, "failed", {"error": str(exc)[:300]}, worker_id)


def run_once(settings: Settings, client: OpenbbClient, worker_id: str) -> int:
    done = 0
    for candidate in queued(settings):
        if claim(settings, candidate["job_id"], worker_id):
            process(settings, client, candidate["job_id"], worker_id)
            done += 1
    return done


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = settings_from_env()
    client = OpenbbClient(settings.openbb_url, settings.openbb_username, settings.openbb_password)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    interval = float(os.environ.get("SECURITY_MASTER_WORKER_POLL_S", "2"))
    log.info("worker %s polling every %ss", worker_id, interval)
    while True:
        try:
            run_once(settings, client, worker_id)
        except Exception:  # noqa: BLE001
            log.exception("poll failed")
        time.sleep(interval)
```

`acquire/__main__.py` is the header plus `from security_master_api.acquire.worker import main; main()` under `if __name__ == "__main__":`.

Also update `job()` in `store/jobs.py` so `summary` reflects the terminal `gold_ready` event's detail (`dependencies`, `execution_id`) when present: after computing `evs`, `summary = {**json.loads(row["summary"] or "{}"), **(evs[-1]["detail"] if evs and evs[-1]["stage"] == "gold_ready" else {})}`.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q && uv run --extra dev ruff check .`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add security-master-api
git commit -m "feat(security-master): the acquisition worker over openbb-api with Bronze retention and promotion"
```

### Task 13: Image, compose, Serve, CI, README

**Files:**
- Create: `security-master-api/Dockerfile`
- Modify: `docker-compose.yml`, `ts-config/serve.json`, `ts-config/serve-funnel.json`, `.github/workflows/ci.yml`, `security-master-api/README.md`, root `README.md`, `api-auth.env.example` (comment only), `credentials.env.example` (no change), new `security-master.env.example`
- Test: `tests/test_compose_contract.py` (repo-root `tests/`), `security-master-api/tests/test_docker_smoke.py` (skipped without docker)

- [ ] **Step 1: Write the failing compose contract test (repo root `tests/test_compose_contract.py`)**

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_security_master_services_are_declared():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    api = compose["services"]["security-master-api"]
    worker = compose["services"]["security-master-worker"]
    assert api["image"] == worker["image"] == "openbb-security-master:11.4.0"
    assert api["build"] == {"context": ".", "dockerfile": "security-master-api/Dockerfile"}
    assert api["networks"] == ["openbb-internal"] and worker["networks"] == ["openbb-internal"]
    assert api["command"][:4] == ["uvicorn", "security_master_api.app.main:app", "--host", "0.0.0.0"]
    assert worker["command"] == ["python", "-m", "security_master_api.acquire"]
    for svc in (api, worker):
        files = {e["path"]: e["required"] for e in svc["env_file"]}
        assert files == {"./api-auth.env": True, "./minio.env": True, "./security-master.env": True}
        assert "tailscale" in svc["depends_on"] and "minio" in svc["depends_on"]
        assert svc["restart"] == "unless-stopped"
    assert "OPENBB_URL=http://openbb-api:6900" in api["environment"]
    assert "ports" not in api and "ports" not in worker


def test_serve_routes_publish_the_service():
    for name in ("serve.json", "serve-funnel.json"):
        cfg = json.loads((ROOT / "ts-config" / name).read_text())
        assert cfg["TCP"]["6905"] == {"HTTPS": True}
        assert cfg["Web"]["${TS_CERT_DOMAIN}:6905"]["Handlers"]["/"]["Proxy"] == "http://security-master-api:6905"
        svc = cfg["Services"]["svc:openbb-security-master"]
        assert svc["Web"]["openbb-security-master.<your-tailnet>.ts.net:443"]["Handlers"]["/"]["Proxy"] == "http://security-master-api:6905"


def test_ci_has_the_two_jobs():
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    for job in ("security-master-api", "openbb-security-master"):
        steps = ci["jobs"][job]["steps"]
        runs = [s.get("run", "") for s in steps]
        assert any("pip install -e ./openbb-deltalake" in r for r in runs)
        assert any("timeout -s ABRT 600 pytest -q --capture=sys" in r for r in runs)
        assert ci["jobs"][job]["timeout-minutes"] == 20
```

The root `tests/` already runs `api-auth-guard`; add `pip install pyyaml` to that CI job's install line and run this file alongside `tests/test_api_app_auth.py`.

- [ ] **Step 2: Run it to verify it fails**

Run (repo root): `python -m pip install -q pyyaml pytest && python -m pytest -q tests/test_compose_contract.py`
Expected: FAIL with `KeyError: 'security-master-api'`.

- [ ] **Step 3: Write the Dockerfile**

```dockerfile
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
# security-master-api: the Security Master Browser's service (API) and its
# acquisition worker, one image. Built with the REPO ROOT as build context
# (see docker-compose.yml) because openbb-deltalake/ is a sibling, not a
# descendant, exactly like stores-explorer. Loopback-only by default; compose
# overrides the bind, Tailscale Serve is the ingress.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY openbb-deltalake/ /srv/openbb-deltalake/
RUN pip install /srv/openbb-deltalake && rm -rf /srv/openbb-deltalake

COPY security-master-api/pyproject.toml ./
COPY security-master-api/security_master_api/ security_master_api/
COPY security-master-api/widgets.json security-master-api/apps.json ./
RUN pip install .

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "security_master_api.app.main:app", "--host", "127.0.0.1", "--port", "6905"]
```

`app/main.py` reads `widgets.json` and `apps.json` from `HERE`, which resolves to `/srv` in the image and to `security-master-api/` in a checkout; both hold the two files.

- [ ] **Step 4: Add the compose services, env example, and Serve routes**

After `stores-explorer` in `docker-compose.yml`:

```yaml
  # security-master-api: the Security Master Browser's service (Ep. 11 line).
  # Owns MinIO access, DuckDB execution, temporal resolution, ODP projection,
  # receipts and job state. Read paths never call a provider. Basic auth from
  # api-auth.env (the stack's one credential), published by Serve on :6905 and
  # as the Tailscale Service svc:openbb-security-master.
  security-master-api:
    build:
      context: .
      dockerfile: security-master-api/Dockerfile
    image: openbb-security-master:11.4.0
    container_name: openbb-security-master-api
    restart: unless-stopped
    networks:
      - openbb-internal
    depends_on:
      - tailscale
      - minio
    command: ["uvicorn", "security_master_api.app.main:app", "--host", "0.0.0.0", "--port", "6905"]
    environment:
      - OPENBB_URL=http://openbb-api:6900
    env_file:
      - path: ./api-auth.env
        required: true
      - path: ./minio.env
        required: true
      - path: ./security-master.env
        required: true

  # security-master-worker: the acquisition worker from the same image. It
  # holds no EODHD key: every acquisition goes through openbb-api with the
  # stack's Basic credential, so entitlement and rate limits stay in one place.
  security-master-worker:
    image: openbb-security-master:11.4.0
    container_name: openbb-security-master-worker
    restart: unless-stopped
    networks:
      - openbb-internal
    depends_on:
      - tailscale
      - minio
      - openbb-api
      - security-master-api
    command: ["python", "-m", "security_master_api.acquire"]
    environment:
      - OPENBB_URL=http://openbb-api:6900
    env_file:
      - path: ./api-auth.env
        required: true
      - path: ./minio.env
        required: true
      - path: ./security-master.env
        required: true
```

`security-master.env.example`:

```sh
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
# Signs preflight tokens; any long random string. Rotate to invalidate outstanding preflights.
SECURITY_MASTER_PREFLIGHT_SECRET=change-me
# enabled | review | disabled
SECURITY_MASTER_SQL=enabled
SECURITY_MASTER_ACQUISITION=review
# Budgets (spec defaults)
SECURITY_MASTER_FIRST_PAGE=100
SECURITY_MASTER_MAX_ROWS=10000
SECURITY_MASTER_TIMEOUT_MS=15000
SECURITY_MASTER_PER_PRINCIPAL_QUERIES=2
SECURITY_MASTER_SCAN_SLOTS=4
SECURITY_MASTER_MEMORY_LIMIT=1GB
SECURITY_MASTER_THREADS=2
```

Add `security-master.env` to `.gitignore` next to the other `*.env` entries, and add a `scrub-check.sh` pattern for `SECURITY_MASTER_PREFLIGHT_SECRET=` values other than `change-me` if the script keys on names (read it first; if it scans for known secret shapes only, leave it).

Serve: in both `ts-config/serve.json` and `ts-config/serve-funnel.json` add `"6905": {"HTTPS": true}` to `TCP`, a `"${TS_CERT_DOMAIN}:6905"` Web handler proxying `http://security-master-api:6905`, and a `"svc:openbb-security-master"` service with `TCP 443` and Web `"openbb-security-master.<your-tailnet>.ts.net:443"` → the same proxy, mirroring `svc:openbb-explorer`.

- [ ] **Step 5: Add the CI jobs**

In `.github/workflows/ci.yml`, after `stores-explorer`:

```yaml
  security-master-api:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with: { python-version: "3.12" }
      - name: Install openbb-deltalake (sibling dependency, not on PyPI)
        run: pip install -e ./openbb-deltalake
      - name: Test security-master-api (tmp-path Delta, stub openbb-api, no network)
        working-directory: security-master-api
        run: |
          pip install -e .[dev]
          PYTHONFAULTHANDLER=1 timeout -s ABRT 600 pytest -q --capture=sys

  openbb-security-master:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with: { python-version: "3.12" }
      - name: Install openbb-deltalake (sibling dependency, not on PyPI)
        run: pip install -e ./openbb-deltalake
      - name: Test openbb-security-master (stubbed service, no network)
        working-directory: openbb-security-master
        run: |
          pip install -e .[dev]
          PYTHONFAULTHANDLER=1 timeout -s ABRT 600 pytest -q --capture=sys
```

And in the `api-auth-guard` job change `pip install fastapi httpx pytest` to `pip install fastapi httpx pytest pyyaml` and its pytest line to run `tests/test_api_app_auth.py tests/test_compose_contract.py`.

- [ ] **Step 6: Rewrite `security-master-api/README.md` and add the root README note**

README sections: purpose (one paragraph), *Temporal contract* (the five modes, the interval columns, the receipt), *API* (the route list with one-line descriptions), *Policies and budgets* (the env table from `security-master.env.example`), *Seeding* (`docker compose run --rm security-master-api python -m security_master_api.store`), *Development* (`uv run --extra dev pytest -q`, `uv run --extra dev ruff check .`), *Service boundary* (the renderer never sees MinIO/EODHD; the worker never holds a provider key; SQL is cache-only). Root `README.md`: under "What you get", add a `**New in v11.4.0 (Ep. 11 line):**` paragraph naming the two services, the Serve port and the Tailscale Service, linking `security-master-api/README.md` and the design doc.

- [ ] **Step 7: Run the contract test and a local image build**

Run (repo root): `python -m pytest -q tests/test_compose_contract.py && bash scripts/check-serve-config.sh && docker build -f security-master-api/Dockerfile -t openbb-security-master:11.4.0 . && docker run --rm -e SECURITY_MASTER_ROOT=/tmp/sm -e SECURITY_MASTER_PREFLIGHT_SECRET=k -e OPENBB_API_AUTH=false openbb-security-master:11.4.0 python -c "from security_master_api.app.main import create_app; print('import OK')"`
Expected: tests pass, serve configs valid, image builds, `import OK`.

- [ ] **Step 8: Commit**

```bash
git add -A security-master-api docker-compose.yml ts-config .github/workflows/ci.yml README.md security-master.env.example .gitignore tests/test_compose_contract.py
git commit -m "feat(security-master): image, compose services, Serve routes, CI jobs and README"
```

### Task 14: The OpenBB router extension `openbb-security-master`

**Files:**
- Create: `openbb-security-master/pyproject.toml`, `openbb-security-master/README.md`, `openbb_security_master/__init__.py`, `openbb_security_master/client.py`, `openbb_security_master/models.py`, `openbb_security_master/router.py`, `openbb_security_master/odp_registry.json` (a copy, checked equal by a test), `tests/test_router.py`, `tests/test_registry_parity.py`
- Modify: root `Dockerfile` (install the extension after openbb-deltalake; assert the router registers in the build-time check)

**Interfaces:**
- Produces routes `GET /api/v1/reference/market_calendar`, `GET /api/v1/reference/exchange_details`, `GET /api/v1/reference/security_master_resolve`; `SecurityMasterClient(base_url, username, password).odp_query(model_id, parameters, context) -> dict`, `.resolve(identifier, context) -> dict`; env `SECURITY_MASTER_URL` (default `http://security-master-api:6905`), `OPENBB_API_USERNAME`/`OPENBB_API_PASSWORD`.

- [ ] **Step 1: Write the failing tests**

`tests/test_registry_parity.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def test_registry_copy_matches_the_service():
    ours = json.loads((HERE / "openbb_security_master" / "odp_registry.json").read_text())
    theirs = json.loads((HERE.parent / "security-master-api" / "security_master_api" / "odp_registry.json").read_text())
    assert ours == theirs
```

`tests/test_router.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from openbb_security_master.client import SecurityMasterClient
from openbb_security_master.models import MarketCalendarData, MarketCalendarQueryParams
from openbb_security_master.router import exchange_details, market_calendar, router


class Stub:
    def __init__(self, status=200, body=None):
        self.calls = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                stub.calls.append((self.path, json.loads(self.rfile.read(length) or b"{}")))
                self.send_response(status); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


ROW = {"calendar_id": "cal_tadawul", "exchange_id": "exch_xsau", "exchange_name": "Tadawul", "mic": "XSAU",
       "calendar_alias": None, "session_date": "2027-03-11", "trade_date": "2027-03-11", "timezone": "Asia/Riyadh",
       "session_status": "closed", "calendar_system": "hijri", "holiday_family": "eid_al_fitr",
       "assertion_domain": "market_session", "evidence_status": "market_final", "market_open": None,
       "market_close": None, "break_start": None, "break_end": None, "interruptions": [], "holiday_name": None,
       "special_open": False, "special_close": False, "market_effect": "closed", "authority_type": "exchange",
       "authority_name": "Tadawul", "authority_verified_at": None, "source_kind": "exchange_notice",
       "source_version": "cap_eid_t2", "rule_id": None, "supersedes_assertion_id": None,
       "effective_from": "2027-03-11T00:00:00Z", "effective_to": None, "known_from": "2027-03-10T08:00:00Z",
       "known_to": None, "capture_id": "cap_eid_t2"}
RECEIPT = {"execution_id": "qry_1", "temporal_mode": "known_at", "dependencies": []}


def test_router_registers_three_commands():
    paths = {r.path for r in router.api_router.routes}
    assert {"/market_calendar", "/exchange_details", "/security_master_resolve"} <= paths


def test_market_calendar_delegates_and_keeps_the_receipt_in_extra(monkeypatch):
    stub = Stub(200, {"results": [ROW], "columns": [], "extra": {"security_master_receipt": RECEIPT}})
    monkeypatch.setenv("SECURITY_MASTER_URL", stub.url)
    monkeypatch.setenv("OPENBB_API_USERNAME", "u")
    monkeypatch.setenv("OPENBB_API_PASSWORD", "p")
    params = MarketCalendarQueryParams(calendar_id="cal_tadawul", start_date="2027-03-01",
                                       end_date="2027-03-31", include_closed=True,
                                       knowledge_at="2027-03-10T08:00:00Z")
    obb = market_calendar(**params.model_dump())
    assert isinstance(obb.results[0], MarketCalendarData)
    assert obb.results[0].market_effect == "closed"
    assert obb.extra["security_master_receipt"] == RECEIPT
    path, body = stub.calls[0]
    assert path == "/security-master/v1/odp/models/MarketCalendar/query"
    assert body["context"] == {"mode": "known_at", "known_at": "2027-03-10T08:00:00Z",
                               "effective_at": "2027-03-01T00:00:00Z"}
    assert body["parameters"]["calendar_id"] == "cal_tadawul"


def test_service_error_surfaces_as_openbb_error(monkeypatch):
    stub = Stub(422, {"error": {"code": "TEMPORAL_MODE_UNSUPPORTED", "message": "no", "details": {}, "request_id": "r"}})
    monkeypatch.setenv("SECURITY_MASTER_URL", stub.url)
    from openbb_core.app.model.abstract.error import OpenBBError

    with pytest.raises(OpenBBError, match="TEMPORAL_MODE_UNSUPPORTED"):
        market_calendar(calendar_id="x", start_date="2027-03-01", end_date="2027-03-02")


def test_exchange_details_uses_the_eodhd_client(monkeypatch):
    seen = {}

    def fake_rest_json(client, endpoint, params):
        seen["endpoint"], seen["params"] = endpoint, params
        return {"Code": "US", "ExchangeHolidays": {}}

    monkeypatch.setattr("openbb_security_master.router._eodhd_rest_json", fake_rest_json)
    monkeypatch.setattr("openbb_security_master.router._eodhd_client", lambda: object())
    obb = exchange_details(code="US")
    assert seen["endpoint"] == "exchange-details/US" and obb.results["Code"] == "US"


def test_client_builds_context_from_odp_parameters():
    c = SecurityMasterClient("http://x", "u", "p")
    assert c.context_for(as_of="2027-03-01", knowledge_at=None) == {"mode": "effective_on", "effective_at": "2027-03-01T00:00:00Z"}
    assert c.context_for(as_of=None, knowledge_at=None) == {"mode": "current_corrected"}
```

- [ ] **Step 2: Run them to verify they fail**

Run (from `openbb-security-master/`): `pip install -e ../openbb-deltalake -e ../openbb-eodhd -e .[dev] && pytest -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write `pyproject.toml`, `client.py`, `models.py`, `router.py`**

`pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "openbb-security-master"
version = "11.4.0"
description = "OpenBB router extension: MarketCalendar, exchange details and identity resolution backed by security-master-api"
requires-python = ">=3.10"
license = { text = "Apache-2.0" }
dependencies = ["openbb-core>=1.5.8,<2", "requests>=2.32", "openbb-eodhd"]

[project.optional-dependencies]
dev = ["pytest", "ruff==0.15.22"]

[project.entry-points."openbb_core_extension"]
security_master = "openbb_security_master.router:router"

[tool.setuptools.packages.find]
include = ["openbb_security_master*"]

[tool.setuptools.package-data]
openbb_security_master = ["odp_registry.json"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]

[tool.ruff]
target-version = "py310"
line-length = 100
```

`client.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""HTTP client for security-master-api. The extension never opens Delta or MinIO itself."""

from __future__ import annotations

import os

import requests
from openbb_core.app.model.abstract.error import OpenBBError

V1 = "/security-master/v1"


class SecurityMasterClient:
    def __init__(self, base_url: str, username: str, password: str, timeout_s: int = 30):
        self.base_url = base_url.rstrip("/")
        self._auth = (username, password)
        self.timeout_s = timeout_s

    @classmethod
    def from_env(cls) -> "SecurityMasterClient":
        return cls(os.environ.get("SECURITY_MASTER_URL", "http://security-master-api:6905"),
                   os.environ.get("OPENBB_API_USERNAME", ""), os.environ.get("OPENBB_API_PASSWORD", ""))

    @staticmethod
    def context_for(as_of: str | None, knowledge_at: str | None) -> dict:
        if knowledge_at:
            ctx = {"mode": "known_at", "known_at": knowledge_at}
            if as_of:
                ctx["effective_at"] = f"{as_of}T00:00:00Z"
            return ctx
        if as_of:
            return {"mode": "effective_on", "effective_at": f"{as_of}T00:00:00Z"}
        return {"mode": "current_corrected"}

    def _post(self, path: str, body: dict) -> dict:
        try:
            r = requests.post(f"{self.base_url}{V1}{path}", json=body, auth=self._auth,
                              timeout=self.timeout_s)
        except requests.RequestException as exc:
            raise OpenBBError(f"security-master-api unreachable: {exc}") from exc
        try:
            payload = r.json()
        except ValueError as exc:
            raise OpenBBError(f"security-master-api answered {r.status_code} without JSON") from exc
        if r.status_code >= 400:
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            raise OpenBBError(f"{err.get('code', 'ERROR')}: {err.get('message', r.text[:200])}")
        return payload

    def odp_query(self, model_id: str, parameters: dict, context: dict) -> dict:
        return self._post(f"/odp/models/{model_id}/query", {"parameters": parameters, "context": context})

    def resolve(self, identifier: str, context: dict, identifier_type: str | None = None) -> dict:
        return self._post("/resolve", {"identifier": identifier, "identifier_type": identifier_type,
                                       "context": context})
```

`models.py`: `MarketCalendarQueryParams(QueryParams)` with the thirteen spec parameters (`calendar_id: str | None`, `exchange: str | None`, `mic: str | None`, `start_date: dateType`, `end_date: dateType`, `include_closed: bool = False`, `include_breaks: bool = False`, `include_interruptions: bool = False`, `session_label: Literal["calendar_date", "trade_date"] = "calendar_date"`, `timezone: str | None = None`, `as_of: dateType | None = None`, `knowledge_at: datetime | None = None`, `provider: str = "local_security_master"`) and `MarketCalendarData(Data)` with the thirty-four result fields typed per the registry (`session_date: dateType`, `interruptions: list[dict] = []`, timestamps `datetime | None`, booleans, strings `str | None`). Add `ExchangeDetailsData(Data)` with `code: str`, `timezone: str | None`, `holidays: list[dict]`, `raw: dict`, and `ResolveData(Data)` with `listing_id, instrument_id, security_id, symbol, reason: str | None`.

`router.py`:

```python
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The /reference router: three commands, all delegating to security-master-api or openbb-eodhd."""

from __future__ import annotations

from typing import Any

from openbb_core.app.model.obbject import OBBject
from openbb_core.app.router import Router

from openbb_security_master.client import SecurityMasterClient
from openbb_security_master.models import (
    ExchangeDetailsData, MarketCalendarData, MarketCalendarQueryParams, ResolveData,
)

router = Router(prefix="/reference", description="Security-master reference data")


def _eodhd_client():
    from openbb_core.app.service.user_service import UserService
    from openbb_eodhd.models._client import get_client

    creds = UserService.read_from_file().credentials.model_dump()
    return get_client({"eodhd_api_key": creds.get("eodhd_api_key")})


def _eodhd_rest_json(client, endpoint: str, params: dict):
    from openbb_eodhd.models._client import rest_json, sdk_call

    return sdk_call(None, lambda: rest_json(client, endpoint, params), endpoint)


@router.command(methods=["GET"])
def market_calendar(**kwargs: Any) -> OBBject[list[MarketCalendarData]]:
    """Exchange sessions with holiday, authority and market-effect evidence under a temporal context."""
    params = MarketCalendarQueryParams(**kwargs)
    client = SecurityMasterClient.from_env()
    context = client.context_for(
        params.as_of.isoformat() if params.as_of else None,
        params.knowledge_at.isoformat().replace("+00:00", "Z") if params.knowledge_at else None)
    if context["mode"] != "current_corrected" and "effective_at" not in context:
        context["effective_at"] = f"{params.start_date.isoformat()}T00:00:00Z"
    body = params.model_dump(exclude={"as_of", "knowledge_at", "provider"}, exclude_none=True)
    for key in ("start_date", "end_date"):
        body[key] = body[key].isoformat()
    out = client.odp_query("MarketCalendar", body, context)
    return OBBject(results=[MarketCalendarData(**row) for row in out["results"]],
                   extra=out.get("extra", {}))


@router.command(methods=["GET"])
def exchange_details(code: str) -> OBBject[ExchangeDetailsData]:
    """EODHD exchange details (trading hours, holidays) as maintained calendar input."""
    raw = _eodhd_rest_json(_eodhd_client(), f"exchange-details/{code}", {})
    holidays = list((raw.get("ExchangeHolidays") or {}).values()) if isinstance(raw, dict) else []
    return OBBject(results=ExchangeDetailsData(code=code, timezone=(raw or {}).get("Timezone"),
                                               holidays=holidays, raw=raw or {}))


@router.command(methods=["GET"])
def security_master_resolve(identifier: str, identifier_type: str | None = None,
                            as_of: str | None = None, knowledge_at: str | None = None) -> OBBject[list[ResolveData]]:
    """Resolve a ticker, CUSIP, ISIN, FIGI or internal id under an explicit temporal context."""
    client = SecurityMasterClient.from_env()
    out = client.resolve(identifier, client.context_for(as_of, knowledge_at), identifier_type)
    return OBBject(results=[ResolveData(**{k: c.get(k) for k in ("listing_id", "instrument_id",
                                                                  "security_id", "symbol", "reason")})
                            for c in out["candidates"]],
                   extra={"security_master_receipt": out.get("receipt", {})})
```

If OpenBB's `Router.command` rejects `**kwargs` signatures on the pinned version, spell the thirteen parameters out in `market_calendar`'s signature with the same defaults and build `MarketCalendarQueryParams` from `locals()`.

- [ ] **Step 4: Wire the Dockerfile**

After the `openbb-deltalake` install block in the root `Dockerfile`:

```dockerfile
# Security-master reference router (Ep. 11 line): MarketCalendar, exchange
# details and identity resolution, delegating to security-master-api.
COPY openbb-security-master /tmp/openbb-security-master
RUN pip install --no-cache-dir /tmp/openbb-security-master && rm -rf /tmp/openbb-security-master
```

and extend the build-time assertion with `from openbb import obb; assert hasattr(obb.reference, 'market_calendar'), 'security-master router not registered'` inside the existing `openbb.build()` check. Add `- SECURITY_MASTER_URL=http://security-master-api:6905` to `openbb-api`'s `environment` in compose.

- [ ] **Step 5: Run the tests**

Run (from `openbb-security-master/`): `pytest -q && ruff check .`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add openbb-security-master Dockerfile docker-compose.yml
git commit -m "feat(openbb-security-master): the reference router extension over security-master-api"
```

---

## Part B — bdobb-v2 (`release/v11` line, branch `claude/security-master-browser-v11`)

All commands run from the bdobb-v2 worktree root. Copy `security-master-api/tests/contract/*.json` from the openbb-docker branch into `src/test/contract/security-master/` before Task 15; a checked-in `MANIFEST.sha256` (produced by `shasum -a 256 *.json > MANIFEST.sha256` in that directory) is asserted by a test so the two repositories cannot drift silently.

### Task 15: Transport additions, service types, parsers and client

**Files:**
- Modify: `src/lib/dataClient.ts` (`request` method union gains `"DELETE"`; new `ServiceHttpError`, `postEndpointJson`, `deleteEndpoint`, `fetchServiceJson`)
- Create: `src/lib/securityMaster.ts`, `src/test/contract/security-master/*.json` (copies), `src/test/contract/security-master/MANIFEST.sha256`
- Test: `src/lib/securityMaster.test.ts`, additions to `src/lib/dataClient.test.ts`

**Interfaces:**
- Produces (dataClient): `class ServiceHttpError extends Error { status: number; body: unknown }`; `postEndpointJson(backend, endpoint, body: unknown, signal?) -> Promise<unknown>`; `deleteEndpoint(backend, endpoint, signal?) -> Promise<void>`; `fetchServiceJson(backend, endpoint, params, signal?) -> Promise<unknown>` (like `fetchEndpointJson` but non-2xx throws `ServiceHttpError` carrying the parsed JSON body).
- Produces (securityMaster): the types listed in the code below; `MALFORMED = "The security-master service answered with a malformed payload."`; `class SecurityMasterError extends Error { code: string; details: Record<string, unknown>; requestId: string }`; parsers `parseCatalog`, `parseOdpModels`, `parseOdpModelDetail`, `parseRelationDetail`, `parseVersions`, `parsePage`, `parseOdpQuery`, `parseResolve`, `parseLineage`, `parseCompare`, `parseSqlPlan`, `parsePreflight`, `parseJob`, `parseServiceError`, each `(value: unknown) => T | null`; client `smClient(backend)` returning an object with `catalog(signal)`, `odpModels(signal)`, `odpModel(id, signal)`, `odpQuery(id, parameters, context, signal)`, `relation(layer, name, signal)`, `versions(layer, name, signal)`, `preview(body, signal)`, `sqlPlan(sql, context, signal)`, `sqlExecute(sql, context, budgets, signal)`, `sqlPage(executionId, cursor, signal)`, `sqlCancel(executionId)`, `resolve(body, signal)`, `lineage(body, signal)`, `compare(body, signal)`, `versionsCompare(body, signal)`, `exchanges(signal)`, `preflight(request, context, signal)`, `createJob(request, token, signal)`, `job(id, signal)`, `cancelJob(id, signal)`.

- [ ] **Step 1: Copy the contract fixtures and write the manifest**

```bash
mkdir -p src/test/contract/security-master
cp <openbb-docker worktree>/security-master-api/tests/contract/*.json src/test/contract/security-master/
(cd src/test/contract/security-master && shasum -a 256 *.json > MANIFEST.sha256)
```

- [ ] **Step 2: Write the failing dataClient tests**

Append to `src/lib/dataClient.test.ts` (it already has `startMockServer` and `backend()` in scope; if not, import them as the file's other tests do):

```ts
describe("service transport", () => {
  it("postEndpointJson sends a JSON body with the credential and returns the parsed answer", async () => {
    const server = await startMockServer({ "/security-master/v1/preview": { rows: [], columns: [] } });
    try {
      const b = backend({ baseUrl: server.url, headerName: "Authorization", headerValue: "Basic dTpw" });
      const out = await postEndpointJson(b, "security-master/v1/preview", { relation: "gold.x" });
      expect(out).toEqual({ rows: [], columns: [] });
      expect(server.requests[0]).toMatchObject({ method: "POST", path: "/security-master/v1/preview" });
      expect(JSON.parse(server.requests[0]!.body)).toEqual({ relation: "gold.x" });
      expect(server.requests[0]!.headers["authorization"]).toBe("Basic dTpw");
    } finally {
      await server.close();
    }
  });

  it("a non-2xx answer becomes a ServiceHttpError carrying the JSON body", async () => {
    const envelope = { error: { code: "QUERY_REJECTED", message: "no", details: {}, request_id: "r" } };
    const server = await startMockServer({ "/security-master/v1/sql/execute": { __status: 422, __body: envelope } });
    try {
      const b = backend({ baseUrl: server.url });
      await expect(postEndpointJson(b, "security-master/v1/sql/execute", { sql: "x" })).rejects.toMatchObject({
        name: "ServiceHttpError", status: 422, body: envelope,
      });
    } finally {
      await server.close();
    }
  });

  it("deleteEndpoint resolves on 204 and rejects otherwise", async () => {
    const server = await startMockServer({
      "/security-master/v1/sql/executions/qry_1": { __status: 204 },
      "/security-master/v1/sql/executions/qry_2": { __status: 404, __body: { error: { code: "NO_MATCHING_FACTS", message: "", details: {}, request_id: "r" } } },
    });
    try {
      const b = backend({ baseUrl: server.url });
      await expect(deleteEndpoint(b, "security-master/v1/sql/executions/qry_1")).resolves.toBeUndefined();
      await expect(deleteEndpoint(b, "security-master/v1/sql/executions/qry_2")).rejects.toMatchObject({ status: 404 });
      expect(server.requests.map((r) => r.method)).toEqual(["DELETE", "DELETE"]);
    } finally {
      await server.close();
    }
  });
});
```

- [ ] **Step 3: Write the failing securityMaster tests**

`src/lib/securityMaster.test.ts`:

```ts
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { startMockServer } from "../test/mockServer";
import { backend } from "../test/fixtures";
import {
  MALFORMED, SecurityMasterError, parseCatalog, parseCompare, parseJob, parseLineage, parseOdpModelDetail,
  parseOdpModels, parseOdpQuery, parsePage, parsePreflight, parseRelationDetail, parseResolve,
  parseServiceError, parseSqlPlan, parseVersions, smClient,
} from "./securityMaster";

const DIR = join(__dirname, "../test/contract/security-master");
const load = (name: string) => JSON.parse(readFileSync(join(DIR, `${name}.json`), "utf8")) as unknown;

it("the contract fixtures match their manifest", () => {
  const manifest = readFileSync(join(DIR, "MANIFEST.sha256"), "utf8").trim().split("\n");
  const files = readdirSync(DIR).filter((f) => f.endsWith(".json")).sort();
  expect(manifest.map((l) => l.split(/\s+/)[1])).toEqual(files);
  for (const line of manifest) {
    const [hash, file] = line.split(/\s+/) as [string, string];
    expect(createHash("sha256").update(readFileSync(join(DIR, file))).digest("hex")).toBe(hash);
  }
});

describe("parsers accept every recorded response", () => {
  it.each([
    ["catalog", parseCatalog], ["odp_models", parseOdpModels], ["odp_model_market_calendar", parseOdpModelDetail],
    ["relation", parseRelationDetail], ["versions", parseVersions], ["preview", parsePage],
    ["sql_execute", parsePage], ["odp_query", parseOdpQuery], ["resolve", parseResolve],
    ["lineage", parseLineage], ["compare", parseCompare], ["sql_plan", parseSqlPlan],
    ["acquisition_preflight", parsePreflight], ["acquisition_job", parseJob], ["acquisition_cancel", parseJob],
  ] as const)("%s", (name, parse) => {
    expect((parse as (v: unknown) => unknown)(load(name))).not.toBeNull();
  });

  it.each(["error_no_local_evidence", "error_mode_unsupported", "error_query_rejected", "error_review_required"])(
    "%s is an error envelope", (name) => {
      const err = parseServiceError(load(name));
      expect(err?.code).toMatch(/^[A-Z_]+$/);
      expect(typeof err?.request_id).toBe("string");
    });
});

describe("parsers refuse malformed bodies all-or-nothing", () => {
  it("catalog without policies is null, and a coerced boolean is refused", () => {
    const good = load("catalog") as Record<string, unknown>;
    expect(parseCatalog({ ...good, policies: undefined })).toBeNull();
    expect(parseCatalog({ ...good, newer_data_available: "false" })).toBeNull();
    expect(parseCatalog([])).toBeNull();
    expect(parseCatalog(null)).toBeNull();
  });
  it("a page with a non-array rows is null; a bad receipt is null", () => {
    const good = load("preview") as Record<string, unknown>;
    expect(parsePage({ ...good, rows: {} })).toBeNull();
    expect(parsePage({ ...good, receipt: { execution_id: 1 } })).toBeNull();
    expect(parsePage({ ...good, count: { value: 3, quality: "guess" } })).toBeNull();
  });
  it("a job with an unknown stage is refused", () => {
    const good = load("acquisition_job") as Record<string, unknown>;
    expect(parseJob({ ...good, state: "done" })).toBeNull();
  });
  it("an odp model with a non-list of fields is refused", () => {
    const good = load("odp_model_market_calendar") as { model: Record<string, unknown> };
    expect(parseOdpModelDetail({ ...good, model: { ...good.model, fields: "x" } })).toBeNull();
  });
});

describe("smClient", () => {
  it("turns an error envelope into SecurityMasterError and a malformed body into the fixed sentence", async () => {
    const server = await startMockServer({
      "/security-master/v1/catalog": { nope: true },
      "/security-master/v1/preview": { __status: 422, __body: load("error_mode_unsupported") },
    });
    try {
      const c = smClient(backend({ baseUrl: server.url }));
      await expect(c.catalog()).rejects.toThrow(MALFORMED);
      const err = await c.preview({ relation: "bronze.source_captures", context: { mode: "known_at", known_at: "x" } }).catch((e: unknown) => e);
      expect(err).toBeInstanceOf(SecurityMasterError);
      expect((err as SecurityMasterError).code).toBe("TEMPORAL_MODE_UNSUPPORTED");
      expect((err as SecurityMasterError).details.supported_modes).toEqual(["captured_by", "delta_snapshot"]);
    } finally {
      await server.close();
    }
  });

  it("issues the documented paths and methods", async () => {
    const server = await startMockServer({
      "/security-master/v1/odp/models/EquityInfo/query": load("odp_query"),
      "/security-master/v1/sql/executions/qry_1/pages": load("sql_execute"),
      "/security-master/v1/sql/executions/qry_1": { __status: 204 },
      "/security-master/v1/acquisitions/job_1/cancel": load("acquisition_cancel"),
    });
    try {
      const c = smClient(backend({ baseUrl: server.url }));
      await c.odpQuery("EquityInfo", { symbol: "AAPL" }, { mode: "current_corrected" });
      await c.sqlPage("qry_1", "abc");
      await c.sqlCancel("qry_1");
      await c.cancelJob("job_1");
      expect(server.requests.map((r) => [r.method, r.path, r.search])).toEqual([
        ["POST", "/security-master/v1/odp/models/EquityInfo/query", ""],
        ["GET", "/security-master/v1/sql/executions/qry_1/pages", "?cursor=abc"],
        ["DELETE", "/security-master/v1/sql/executions/qry_1", ""],
        ["POST", "/security-master/v1/acquisitions/job_1/cancel", ""],
      ]);
    } finally {
      await server.close();
    }
  });
});
```

- [ ] **Step 4: Run both test files to verify they fail**

Run: `pnpm vitest run src/lib/dataClient.test.ts src/lib/securityMaster.test.ts`
Expected: FAIL (`postEndpointJson` is not exported; `./securityMaster` not found).

- [ ] **Step 5: Extend `dataClient.ts`**

Change `request`'s `method` union to `"GET" | "PUT" | "POST" | "DELETE"`, `const hasBody = method === "PUT" || method === "POST";`, add an option `rawErrors?: boolean` to its options object, and replace the `if (!res.ok) throw ...` line with:

```ts
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    if (rawErrors) {
      let body: unknown = text;
      try { body = JSON.parse(text); } catch { /* keep the text */ }
      throw new ServiceHttpError(res.status, body);
    }
    throw new Error(describeHttpError(res.status, text));
  }
```

Add, near `fetchEndpointJson`:

```ts
/**
 * A non-2xx answer from a service that speaks a structured error envelope.
 * The body is kept whole so the caller can read a domain code; the generic
 * describeHttpError sentence is for backends that say nothing structured.
 */
export class ServiceHttpError extends Error {
  readonly status: number;
  readonly body: unknown;
  constructor(status: number, body: unknown) {
    super(`HTTP ${status}`);
    this.name = "ServiceHttpError";
    this.status = status;
    this.body = body;
  }
}

function serviceUrl(backend: BackendConfig, endpoint: string, params: Record<string, string> = {}): string {
  const url = new URL(joinUrl(backend.baseUrl, endpoint));
  for (const [key, value] of Object.entries(params)) {
    if (value !== "") url.searchParams.set(key, value);
  }
  return applyAuthQuery(url, backend).toString();
}

export async function fetchServiceJson(
  backend: BackendConfig, endpoint: string, params: Record<string, string>, signal?: AbortSignal,
): Promise<unknown> {
  return request("GET", serviceUrl(backend, endpoint, params), authHeaders(backend), undefined, { signal, rawErrors: true });
}

export async function postEndpointJson(
  backend: BackendConfig, endpoint: string, body: unknown, signal?: AbortSignal,
): Promise<unknown> {
  return request("POST", serviceUrl(backend, endpoint), authHeaders(backend), body, { signal, rawErrors: true });
}

export async function deleteEndpoint(backend: BackendConfig, endpoint: string, signal?: AbortSignal): Promise<void> {
  await request("DELETE", serviceUrl(backend, endpoint), authHeaders(backend), undefined, { signal, rawErrors: true, parse: false });
}
```

- [ ] **Step 6: Write `src/lib/securityMaster.ts`**

```ts
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import { deleteEndpoint, fetchServiceJson, postEndpointJson, ServiceHttpError } from "./dataClient";
import { isRecord } from "./isRecord";
import type { BackendConfig } from "./types";

export const MALFORMED = "The security-master service answered with a malformed payload.";
export const V1 = "security-master/v1";

export const TEMPORAL_MODES = ["current_corrected", "known_at", "effective_on", "delta_snapshot", "captured_by"] as const;
export type TemporalMode = (typeof TEMPORAL_MODES)[number];
export type Policy = "enabled" | "review" | "disabled";
export const JOB_STATES = [
  "queued", "fetching", "bronze_retained", "normalizing", "validating", "silver_ready", "materializing",
  "gold_ready", "partial", "rate_limited", "entitlement_denied", "validation_failed", "cancel_pending",
  "cancelled", "failed",
] as const;
export type JobState = (typeof JOB_STATES)[number];

export type QueryContext = {
  mode: TemporalMode;
  effective_at?: string;
  known_at?: string;
  availability_policy?: string;
  timezone?: string;
  versions?: Record<string, number>;
};
export type Column = { name: string; dtype: string };
export type CatalogRelation = {
  layer: string; name: string; description: string; kind: string; modes: string[]; keys: string[]; depends_on: string[];
};
export type Catalog = {
  layers: string[]; relations: CatalogRelation[]; policies: { sql: Policy; acquisition: Policy };
  odp_version: string; projection_version: string; newer_data_available: boolean;
};
export type OdpParam = { name: string; type: string; required: boolean; description?: string };
export type OdpField = { name: string; type: string; source: string };
export type OdpModel = {
  model_id: string; route: string; kind: string; backing_relation: string; parameters: OdpParam[];
  fields: OdpField[]; temporal_capabilities: string[]; primary_temporal_scenario: string; consumers: string[];
};
export type Scenario = { scenario: string; fixture_id: string; expectations: unknown[]; captures: string[] };
export type OdpModelDetail = { model: OdpModel; scenario: Scenario | null };
export type Receipt = {
  execution_id: string; request_id: string; kind: string; temporal_mode: string; effective_at: string | null;
  known_at: string | null; availability_policy: string; projection_version: string;
  dependencies: Array<{ relation: string; delta_version: number }>; sql_fingerprint: string; created_at: string;
};
export type Count = { value: number | null; quality: "exact" | "estimated" | "unknown" };
export type ResultPage = {
  execution_id: string; rows: Record<string, unknown>[]; columns: Column[]; next_cursor: string | null;
  count: Count; receipt: Receipt;
};
export type OdpQueryResult = { results: Record<string, unknown>[]; columns: Column[]; receipt: Receipt };
export type RelationDetail = {
  relation: CatalogRelation; schema: Column[]; latest_version: number | null; count: Count;
  manifest?: Record<string, number>;
};
export type VersionList = { relation: string; versions: Array<{ version: number; timestamp: string; retained: boolean }> };
export type Candidate = Record<string, unknown> & { listing_id: string | null; reason: string };
export type ResolveResult = { candidates: Candidate[]; receipt: Receipt };
export type LineageResult = { chain: Array<Record<string, unknown> & { kind: string }>; receipt: Receipt };
export type Difference = {
  key: Record<string, unknown>; field: string; left: unknown; right: unknown; classification: string;
};
export type CompareResult = { differences: Difference[]; left_receipt: Receipt; right_receipt: Receipt };
export type SqlPlan = { relations: string[]; functions: string[]; columns: Column[]; manifest: Record<string, number> };
export type Preflight = {
  token: string; expires_at: number; fingerprint: string; dataset: string; identities: Record<string, unknown>[];
  missing_ranges: Array<{ listing_id: string; start: string; end: string }>; expected_requests: number;
  calendar: unknown; warnings: string[]; policy: Policy;
};
export type JobEvent = { stage: JobState; at: string; detail: Record<string, unknown>; worker_id: string };
export type Job = {
  job_id: string; kind: string; state: JobState; created_at: string; updated_at: string;
  request: Record<string, unknown>; summary: Record<string, unknown>; stage_history: JobEvent[];
};
export type ServiceError = { code: string; message: string; details: Record<string, unknown>; request_id: string };

export class SecurityMasterError extends Error {
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly requestId: string;
  constructor(err: ServiceError) {
    super(err.message);
    this.name = "SecurityMasterError";
    this.code = err.code;
    this.details = err.details;
    this.requestId = err.request_id;
  }
}

// ---- parsers: all-or-nothing, literal booleans only, never coerce ----------

const str = (v: unknown): v is string => typeof v === "string";
const num = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const bool = (v: unknown): v is boolean => v === true || v === false;
const strList = (v: unknown): v is string[] => Array.isArray(v) && v.every(str);
const nullableStr = (v: unknown): v is string | null => v === null || str(v);
const recordList = (v: unknown): v is Record<string, unknown>[] => Array.isArray(v) && v.every(isRecord);

function column(v: unknown): Column | null {
  return isRecord(v) && str(v.name) && str(v.dtype) ? { name: v.name, dtype: v.dtype } : null;
}
function columns(v: unknown): Column[] | null {
  if (!Array.isArray(v)) return null;
  const out: Column[] = [];
  for (const c of v) { const p = column(c); if (!p) return null; out.push(p); }
  return out;
}
function count(v: unknown): Count | null {
  if (!isRecord(v) || !(v.value === null || num(v.value))) return null;
  if (v.quality !== "exact" && v.quality !== "estimated" && v.quality !== "unknown") return null;
  return { value: v.value, quality: v.quality };
}
function policy(v: unknown): Policy | null {
  return v === "enabled" || v === "review" || v === "disabled" ? v : null;
}

export function parseReceipt(v: unknown): Receipt | null {
  if (!isRecord(v)) return null;
  const { execution_id, request_id, kind, temporal_mode, effective_at, known_at, availability_policy,
    projection_version, dependencies, sql_fingerprint, created_at } = v;
  if (!str(execution_id) || !str(request_id) || !str(kind) || !str(temporal_mode)) return null;
  if (!nullableStr(effective_at) || !nullableStr(known_at) || !str(availability_policy)) return null;
  if (!str(projection_version) || !str(sql_fingerprint) || !str(created_at) || !Array.isArray(dependencies)) return null;
  const deps: Receipt["dependencies"] = [];
  for (const d of dependencies) {
    if (!isRecord(d) || !str(d.relation) || !num(d.delta_version)) return null;
    deps.push({ relation: d.relation, delta_version: d.delta_version });
  }
  return { execution_id, request_id, kind, temporal_mode, effective_at, known_at, availability_policy,
    projection_version, dependencies: deps, sql_fingerprint, created_at };
}

function relation(v: unknown): CatalogRelation | null {
  if (!isRecord(v) || !str(v.layer) || !str(v.name) || !str(v.description) || !str(v.kind)) return null;
  if (!strList(v.modes) || !strList(v.keys) || !strList(v.depends_on)) return null;
  return { layer: v.layer, name: v.name, description: v.description, kind: v.kind, modes: v.modes,
    keys: v.keys, depends_on: v.depends_on };
}

export function parseCatalog(v: unknown): Catalog | null {
  if (!isRecord(v) || !strList(v.layers) || !Array.isArray(v.relations) || !isRecord(v.policies)) return null;
  if (!str(v.odp_version) || !str(v.projection_version) || !bool(v.newer_data_available)) return null;
  const sql = policy(v.policies.sql), acquisition = policy(v.policies.acquisition);
  if (!sql || !acquisition) return null;
  const relations: CatalogRelation[] = [];
  for (const r of v.relations) { const p = relation(r); if (!p) return null; relations.push(p); }
  return { layers: v.layers, relations, policies: { sql, acquisition }, odp_version: v.odp_version,
    projection_version: v.projection_version, newer_data_available: v.newer_data_available };
}

function odpModel(v: unknown): OdpModel | null {
  if (!isRecord(v) || !str(v.model_id) || !str(v.route) || !str(v.kind) || !str(v.backing_relation)) return null;
  if (!Array.isArray(v.parameters) || !Array.isArray(v.fields) || !strList(v.temporal_capabilities)) return null;
  if (!str(v.primary_temporal_scenario) || !strList(v.consumers)) return null;
  const parameters: OdpParam[] = [];
  for (const p of v.parameters) {
    if (!isRecord(p) || !str(p.name) || !str(p.type)) return null;
    const required = p.required === undefined ? false : p.required;
    if (!bool(required)) return null;
    parameters.push({ name: p.name, type: p.type, required, ...(str(p.description) ? { description: p.description } : {}) });
  }
  const fields: OdpField[] = [];
  for (const f of v.fields) {
    if (!isRecord(f) || !str(f.name) || !str(f.type) || !str(f.source)) return null;
    fields.push({ name: f.name, type: f.type, source: f.source });
  }
  return { model_id: v.model_id, route: v.route, kind: v.kind, backing_relation: v.backing_relation, parameters,
    fields, temporal_capabilities: v.temporal_capabilities, primary_temporal_scenario: v.primary_temporal_scenario,
    consumers: v.consumers };
}

export function parseOdpModels(v: unknown): { odp_version: string; models: OdpModel[] } | null {
  if (!isRecord(v) || !str(v.odp_version) || !Array.isArray(v.models)) return null;
  const models: OdpModel[] = [];
  for (const m of v.models) { const p = odpModel(m); if (!p) return null; models.push(p); }
  return { odp_version: v.odp_version, models };
}

export function parseOdpModelDetail(v: unknown): OdpModelDetail | null {
  if (!isRecord(v)) return null;
  const model = odpModel(v.model);
  if (!model) return null;
  if (v.scenario === null || v.scenario === undefined) return { model, scenario: null };
  const s = v.scenario;
  if (!isRecord(s) || !str(s.scenario) || !str(s.fixture_id) || !Array.isArray(s.expectations) || !strList(s.captures)) return null;
  return { model, scenario: { scenario: s.scenario, fixture_id: s.fixture_id, expectations: s.expectations, captures: s.captures } };
}

export function parseRelationDetail(v: unknown): RelationDetail | null {
  if (!isRecord(v)) return null;
  const rel = relation(v.relation), schema = columns(v.schema), c = count(v.count);
  if (!rel || !schema || !c || !(v.latest_version === null || num(v.latest_version))) return null;
  const out: RelationDetail = { relation: rel, schema, latest_version: v.latest_version, count: c };
  if (isRecord(v.manifest) && Object.values(v.manifest).every(num)) out.manifest = v.manifest as Record<string, number>;
  return out;
}

export function parseVersions(v: unknown): VersionList | null {
  if (!isRecord(v) || !str(v.relation) || !Array.isArray(v.versions)) return null;
  const versions: VersionList["versions"] = [];
  for (const x of v.versions) {
    if (!isRecord(x) || !num(x.version) || !str(x.timestamp) || !bool(x.retained)) return null;
    versions.push({ version: x.version, timestamp: x.timestamp, retained: x.retained });
  }
  return { relation: v.relation, versions };
}

export function parsePage(v: unknown): ResultPage | null {
  if (!isRecord(v) || !str(v.execution_id) || !recordList(v.rows) || !nullableStr(v.next_cursor)) return null;
  const cols = columns(v.columns), c = count(v.count), receipt = parseReceipt(v.receipt);
  if (!cols || !c || !receipt) return null;
  return { execution_id: v.execution_id, rows: v.rows, columns: cols, next_cursor: v.next_cursor, count: c, receipt };
}

export function parseOdpQuery(v: unknown): OdpQueryResult | null {
  if (!isRecord(v) || !recordList(v.results) || !isRecord(v.extra)) return null;
  const cols = columns(v.columns), receipt = parseReceipt(v.extra.security_master_receipt);
  if (!cols || !receipt) return null;
  return { results: v.results, columns: cols, receipt };
}

export function parseResolve(v: unknown): ResolveResult | null {
  if (!isRecord(v) || !recordList(v.candidates)) return null;
  const receipt = parseReceipt(v.receipt);
  if (!receipt) return null;
  const candidates: Candidate[] = [];
  for (const c of v.candidates) {
    if (!nullableStr(c.listing_id ?? null) || !str(c.reason)) return null;
    candidates.push({ ...c, listing_id: (c.listing_id as string | null) ?? null, reason: c.reason });
  }
  return { candidates, receipt };
}

export function parseLineage(v: unknown): LineageResult | null {
  if (!isRecord(v) || !recordList(v.chain)) return null;
  const receipt = parseReceipt(v.receipt);
  if (!receipt) return null;
  const chain: LineageResult["chain"] = [];
  for (const c of v.chain) { if (!str(c.kind)) return null; chain.push({ ...c, kind: c.kind }); }
  return { chain, receipt };
}

export function parseCompare(v: unknown): CompareResult | null {
  if (!isRecord(v) || !Array.isArray(v.differences)) return null;
  const left = parseReceipt(v.left_receipt), right = parseReceipt(v.right_receipt);
  if (!left || !right) return null;
  const differences: Difference[] = [];
  for (const d of v.differences) {
    if (!isRecord(d) || !isRecord(d.key) || !str(d.field) || !str(d.classification)) return null;
    differences.push({ key: d.key, field: d.field, left: d.left, right: d.right, classification: d.classification });
  }
  return { differences, left_receipt: left, right_receipt: right };
}

export function parseSqlPlan(v: unknown): SqlPlan | null {
  if (!isRecord(v) || !strList(v.relations) || !strList(v.functions) || !isRecord(v.manifest)) return null;
  const cols = columns(v.columns);
  if (!cols || !Object.values(v.manifest).every(num)) return null;
  return { relations: v.relations, functions: v.functions, columns: cols, manifest: v.manifest as Record<string, number> };
}

export function parsePreflight(v: unknown): Preflight | null {
  if (!isRecord(v) || !str(v.token) || !num(v.expires_at) || !str(v.fingerprint) || !str(v.dataset)) return null;
  if (!recordList(v.identities) || !Array.isArray(v.missing_ranges) || !num(v.expected_requests) || !strList(v.warnings)) return null;
  const p = policy(v.policy);
  if (!p) return null;
  const missing: Preflight["missing_ranges"] = [];
  for (const r of v.missing_ranges) {
    if (!isRecord(r) || !str(r.listing_id) || !str(r.start) || !str(r.end)) return null;
    missing.push({ listing_id: r.listing_id, start: r.start, end: r.end });
  }
  return { token: v.token, expires_at: v.expires_at, fingerprint: v.fingerprint, dataset: v.dataset,
    identities: v.identities, missing_ranges: missing, expected_requests: v.expected_requests,
    calendar: v.calendar ?? null, warnings: v.warnings, policy: p };
}

const isState = (v: unknown): v is JobState => str(v) && (JOB_STATES as readonly string[]).includes(v);

export function parseJob(v: unknown): Job | null {
  if (!isRecord(v) || !str(v.job_id) || !str(v.kind) || !isState(v.state) || !str(v.created_at) || !str(v.updated_at)) return null;
  if (!isRecord(v.request) || !isRecord(v.summary) || !Array.isArray(v.stage_history)) return null;
  const stage_history: JobEvent[] = [];
  for (const e of v.stage_history) {
    if (!isRecord(e) || !isState(e.stage) || !str(e.at) || !isRecord(e.detail) || !str(e.worker_id)) return null;
    stage_history.push({ stage: e.stage, at: e.at, detail: e.detail, worker_id: e.worker_id });
  }
  return { job_id: v.job_id, kind: v.kind, state: v.state, created_at: v.created_at, updated_at: v.updated_at,
    request: v.request, summary: v.summary, stage_history };
}

export function parseServiceError(v: unknown): ServiceError | null {
  if (!isRecord(v) || !isRecord(v.error)) return null;
  const e = v.error;
  if (!str(e.code) || !str(e.message) || !str(e.request_id)) return null;
  return { code: e.code, message: e.message, details: isRecord(e.details) ? e.details : {}, request_id: e.request_id };
}

// ---- client -------------------------------------------------------------------

function unwrap<T>(parse: (v: unknown) => T | null) {
  return (v: unknown): T => {
    const out = parse(v);
    if (out === null) throw new Error(MALFORMED);
    return out;
  };
}

function rethrow(err: unknown): never {
  if (err instanceof ServiceHttpError) {
    const parsed = parseServiceError(err.body);
    if (parsed) throw new SecurityMasterError(parsed);
    throw new Error(`The security-master service answered HTTP ${err.status}.`);
  }
  throw err;
}

export function smClient(backend: BackendConfig) {
  const get = (path: string, params: Record<string, string> = {}, signal?: AbortSignal) =>
    fetchServiceJson(backend, `${V1}/${path}`, params, signal).catch(rethrow);
  const post = (path: string, body: unknown, signal?: AbortSignal) =>
    postEndpointJson(backend, `${V1}/${path}`, body, signal).catch(rethrow);
  return {
    catalog: (signal?: AbortSignal) => get("catalog", {}, signal).then(unwrap(parseCatalog)),
    odpModels: (signal?: AbortSignal) => get("odp/models", {}, signal).then(unwrap(parseOdpModels)),
    odpModel: (id: string, signal?: AbortSignal) => get(`odp/models/${encodeURIComponent(id)}`, {}, signal).then(unwrap(parseOdpModelDetail)),
    odpQuery: (id: string, parameters: Record<string, unknown>, context: QueryContext, signal?: AbortSignal) =>
      post(`odp/models/${encodeURIComponent(id)}/query`, { parameters, context }, signal).then(unwrap(parseOdpQuery)),
    relation: (layer: string, name: string, signal?: AbortSignal) => get(`relations/${layer}/${name}`, {}, signal).then(unwrap(parseRelationDetail)),
    versions: (layer: string, name: string, signal?: AbortSignal) => get(`relations/${layer}/${name}/versions`, {}, signal).then(unwrap(parseVersions)),
    preview: (body: { relation: string; context: QueryContext; columns?: string[]; filters?: unknown[]; sort?: unknown[]; page?: { limit?: number; cursor?: string | null } }, signal?: AbortSignal) =>
      post("preview", body, signal).then(unwrap(parsePage)),
    sqlPlan: (sql: string, context: QueryContext, signal?: AbortSignal) => post("sql/plan", { sql, context }, signal).then(unwrap(parseSqlPlan)),
    sqlExecute: (sql: string, context: QueryContext, budgets: { max_rows?: number; timeout_ms?: number }, signal?: AbortSignal) =>
      post("sql/execute", { sql, context, budgets }, signal).then(unwrap(parsePage)),
    sqlPage: (executionId: string, cursor: string, signal?: AbortSignal) =>
      get(`sql/executions/${encodeURIComponent(executionId)}/pages`, { cursor }, signal).then(unwrap(parsePage)),
    sqlCancel: (executionId: string) => deleteEndpoint(backend, `${V1}/sql/executions/${encodeURIComponent(executionId)}`).catch(rethrow),
    resolve: (body: { identifier: string; identifier_type?: string; include_historical?: boolean; context: QueryContext; lookup_policy?: string }, signal?: AbortSignal) =>
      post("resolve", body, signal).then(unwrap(parseResolve)),
    lineage: (body: { relation: string; assertion_id: string; context: QueryContext }, signal?: AbortSignal) =>
      post("lineage", body, signal).then(unwrap(parseLineage)),
    compare: (body: { relation: string; selection: Record<string, unknown>; left: QueryContext; right: QueryContext }, signal?: AbortSignal) =>
      post("compare", body, signal).then(unwrap(parseCompare)),
    versionsCompare: (body: { relation: string; left: number; right: number }, signal?: AbortSignal) => post("versions/compare", body, signal),
    exchanges: (signal?: AbortSignal) => get("exchanges", {}, signal),
    preflight: (request: Record<string, unknown>, context: QueryContext, signal?: AbortSignal) =>
      post("acquisitions/preflight", { request, context }, signal).then(unwrap(parsePreflight)),
    createJob: (request: Record<string, unknown>, token: string, signal?: AbortSignal) =>
      post("acquisitions", { request, token }, signal).then(unwrap(parseJob)),
    job: (id: string, signal?: AbortSignal) => get(`acquisitions/${encodeURIComponent(id)}`, {}, signal).then(unwrap(parseJob)),
    cancelJob: (id: string, signal?: AbortSignal) => post(`acquisitions/${encodeURIComponent(id)}/cancel`, {}, signal).then(unwrap(parseJob)),
  };
}
export type SmClient = ReturnType<typeof smClient>;
```


- [ ] **Step 7: Run the tests to verify they pass**

Run: `pnpm vitest run src/lib/dataClient.test.ts src/lib/securityMaster.test.ts && pnpm typecheck`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/lib/dataClient.ts src/lib/dataClient.test.ts src/lib/securityMaster.ts src/lib/securityMaster.test.ts src/test/contract
git commit -m "feat(security-master): service transport, types, all-or-nothing parsers and the client"
```

### Task 16: Re-type the temporal view-model to the canonical fixture shape

**Files:**
- Modify: `src/lib/securityMasterTemporal.ts`, `src/test/fixtures/securityMasterTemporal.ts`, `src/lib/securityMasterTemporal.test.ts`

**Interfaces:**
- `TemporalStage` becomes `"T0" | "T1" | "T2"`; `TemporalScenarioFixture.schema` replaces `schema_version` (value `"security-master.temporal-fixture.v1"`); `fixture_id`s become `"india-bakri-eid-2023"` and `"dubai-eid-al-fitr-2024"`; India T1 `market_dates` become `[{"2023-06-28","closed"},{"2023-06-29","pending"}]` and `expiry_date: null`; `STAGE_LABELS` keyed `T0`/`T1`/`T2` with the same label text; `temporalScenarioView` unchanged otherwise. Add `parseTemporalFixture(value: unknown): TemporalScenarioFixture | null` (all-or-nothing) so the renderer can accept the fixture from the service's `scenario` payload later.

- [ ] **Step 1: Update the fixture file to the backend JSON byte-for-byte**

Replace the two exported objects with the content of the openbb-docker files `security_master_api/fixtures/india_bakri_eid_2023.json` and `dubai_eid_al_fitr_2024.json` (their `events` and `expected_states`; drop the `assertions` block), keeping the TypeScript export names. `capture_time_note` is not in the backend files: remove it from the type.

- [ ] **Step 2: Update the tests**

In `securityMasterTemporal.test.ts` change `schema_version` to `schema`, the fixture ids, the India T1 expectations (`marketDates` `[{date:"2023-06-28", sessionLabel:"Closed"},{date:"2023-06-29", sessionLabel:"Pending"}]`, no `expiryLabel`, `settlementLabel: "Pending"`), `stageLabel` still `"T1 · Authority confirmed"`, and add:

```ts
it("Dubai T1 keeps both literal branches unselected", () => {
  const view = temporalScenarioView(dubaiEidAlFitr2024, new Date("2024-04-08T20:00:00Z"));
  expect(view?.candidateBranches).toEqual([
    { condition: "eid_first_day=2024-04-09", reopenDate: "2024-04-12", selected: false },
    { condition: "eid_first_day=2024-04-10", reopenDate: "2024-04-15", selected: false },
  ]);
});

it("parseTemporalFixture accepts the fixtures and refuses a wrong schema", () => {
  expect(parseTemporalFixture(JSON.parse(JSON.stringify(indiaBakriEid2023)))).not.toBeNull();
  expect(parseTemporalFixture({ ...indiaBakriEid2023, schema: "v0" })).toBeNull();
  expect(parseTemporalFixture({ ...indiaBakriEid2023, expected_states: "x" })).toBeNull();
});
```

- [ ] **Step 3: Run to verify failures, then update the module**

Run: `pnpm vitest run src/lib/securityMasterTemporal.test.ts` → FAIL. Then in `securityMasterTemporal.ts`: rename the stage union, `STAGE_LABELS` keys, `schema_version` → `schema`, drop `capture_time_note`, and add:

```ts
import { isRecord } from "./isRecord";

export function parseTemporalFixture(value: unknown): TemporalScenarioFixture | null {
  if (!isRecord(value) || value.schema !== "security-master.temporal-fixture.v1") return null;
  if (value.capture_time_semantics !== "synthetic_test_capture") return null;
  const s = (v: unknown): v is string => typeof v === "string";
  if (!s(value.fixture_id) || !s(value.exchange_mic) || !s(value.exchange_name) || !s(value.holiday_family)) return null;
  if (!Array.isArray(value.events) || !Array.isArray(value.expected_states)) return null;
  for (const e of value.events) {
    if (!isRecord(e) || !s(e.event_id) || !s(e.known_from) || !s(e.published_on) || !s(e.source_kind)
      || !s(e.authority) || !s(e.source_url) || !s(e.assertion)) return null;
  }
  for (const st of value.expected_states) {
    if (!isRecord(st) || !s(st.known_from) || !(st.stage === "T0" || st.stage === "T1" || st.stage === "T2")) return null;
    if (!Array.isArray(st.evidence_event_ids) || !Array.isArray(st.market_dates)) return null;
  }
  return value as unknown as TemporalScenarioFixture;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pnpm vitest run src/lib/securityMasterTemporal.test.ts && pnpm typecheck`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lib/securityMasterTemporal.ts src/lib/securityMasterTemporal.test.ts src/test/fixtures/securityMasterTemporal.ts
git commit -m "refactor(security-master): the temporal view-model adopts the service's canonical fixture shape"
```

### Task 17: The session hook

**Files:**
- Create: `src/hooks/useSecurityMaster.ts`
- Test: `src/hooks/useSecurityMaster.test.ts`

**Interfaces:**
- Produces:

```ts
export type CatalogMode = "models" | "tables";
export type Selection = { kind: "model"; modelId: string } | { kind: "relation"; layer: string; name: string } | null;
export type SmSession = {           // persisted per card (the spec's list)
  catalogMode: CatalogMode; selection: Selection; tab: string; context: QueryContext;
  lookupPolicy: "cache_only" | "review"; sqlDraft: string; inspectorOpen: boolean;
};
export type Async<T> = { status: "idle" } | { status: "loading" } | { status: "ready"; value: T } | { status: "error"; error: string; code?: string; details?: Record<string, unknown> };
export function useSecurityMaster(backend: BackendConfig | undefined, session: SmSession, refreshKey: number): {
  client: SmClient | null;
  catalog: Async<Catalog>;
  models: Async<OdpModel[]>;
  detail: Async<OdpModelDetail | RelationDetail>;   // follows session.selection
  run: <T>(key: string, fn: (signal: AbortSignal) => Promise<T>) => Promise<T | undefined>;  // stale-guarded
  results: Record<string, Async<unknown>>;          // keyed by `key`
  cancelAll: () => void;
}
export const DEFAULT_SESSION: SmSession;
```
- `run(key, fn)` aborts any in-flight request under the same key, tags the call with a request id, and applies its answer only if that id is still current for the key. A `SecurityMasterError` becomes `{status: "error", error: message, code, details}`; any other error `{status: "error", error: message}`. A context change by the caller re-keys nothing by itself: callers include the context fingerprint in `key` when a result must not be relabeled.

- [ ] **Step 1: Write the failing test**

```ts
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import { act, renderHook, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { backend } from "../test/fixtures";
import { startMockServer, type MockServer } from "../test/mockServer";
import { DEFAULT_SESSION, useSecurityMaster } from "./useSecurityMaster";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: globalThis.fetch }));

const DIR = join(__dirname, "../test/contract/security-master");
const load = (n: string) => JSON.parse(readFileSync(join(DIR, `${n}.json`), "utf8")) as unknown;
let server: MockServer | undefined;
afterEach(async () => { await server?.close(); server = undefined; });

describe("useSecurityMaster", () => {
  it("loads the catalog and models, then the selected model's detail", async () => {
    server = await startMockServer({
      "/security-master/v1/catalog": load("catalog"),
      "/security-master/v1/odp/models": load("odp_models"),
      "/security-master/v1/odp/models/MarketCalendar": load("odp_model_market_calendar"),
    });
    const b = backend({ baseUrl: server.url });
    const session = { ...DEFAULT_SESSION, selection: { kind: "model" as const, modelId: "MarketCalendar" } };
    const { result } = renderHook(() => useSecurityMaster(b, session, 0));
    await waitFor(() => expect(result.current.catalog.status).toBe("ready"));
    await waitFor(() => expect(result.current.detail.status).toBe("ready"));
    expect(result.current.models.status).toBe("ready");
    expect((result.current.detail as { value: { model: { model_id: string } } }).value.model.model_id).toBe("MarketCalendar");
  });

  it("a malformed catalog is a bounded error, not a throw", async () => {
    server = await startMockServer({ "/security-master/v1/catalog": { layers: 1 }, "/security-master/v1/odp/models": load("odp_models") });
    const { result } = renderHook(() => useSecurityMaster(backend({ baseUrl: server!.url }), DEFAULT_SESSION, 0));
    await waitFor(() => expect(result.current.catalog.status).toBe("error"));
    expect((result.current.catalog as { error: string }).error).toBe("The security-master service answered with a malformed payload.");
  });

  it("run() applies only the latest answer for a key and exposes domain errors", async () => {
    server = await startMockServer({
      "/security-master/v1/catalog": load("catalog"),
      "/security-master/v1/odp/models": load("odp_models"),
      "/security-master/v1/preview": (url) => (url.searchParams.get("slow") ? { __delayMs: 300, __body: load("preview") } : load("preview")),
      "/security-master/v1/sql/execute": { __status: 422, __body: load("error_query_rejected") },
    });
    const b = backend({ baseUrl: server.url });
    const { result } = renderHook(() => useSecurityMaster(b, DEFAULT_SESSION, 0));
    await waitFor(() => expect(result.current.client).not.toBeNull());
    const client = result.current.client!;
    let first: Promise<unknown> | undefined;
    act(() => {
      first = result.current.run("preview", (signal) => client.preview({ relation: "gold.security_master", context: { mode: "current_corrected" } }, signal).then(() => "first"));
      void result.current.run("preview", () => Promise.resolve("second"));
    });
    await waitFor(() => expect(result.current.results.preview).toEqual({ status: "ready", value: "second" }));
    await expect(first).resolves.toBeUndefined();
    act(() => { void result.current.run("sql", (signal) => client.sqlExecute("DROP", { mode: "current_corrected" }, {}, signal)); });
    await waitFor(() => expect(result.current.results.sql?.status).toBe("error"));
    expect(result.current.results.sql).toMatchObject({ code: "QUERY_REJECTED" });
  });

  it("no backend means idle everywhere and a null client", () => {
    const { result } = renderHook(() => useSecurityMaster(undefined, DEFAULT_SESSION, 0));
    expect(result.current.client).toBeNull();
    expect(result.current.catalog).toEqual({ status: "idle" });
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm vitest run src/hooks/useSecurityMaster.test.ts`
Expected: FAIL (module not found).

- [ ] **Step 3: Write the hook**

```ts
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  SecurityMasterError, smClient, type Catalog, type OdpModel, type OdpModelDetail, type QueryContext,
  type RelationDetail, type SmClient,
} from "../lib/securityMaster";
import type { BackendConfig } from "../lib/types";

export type CatalogMode = "models" | "tables";
export type Selection =
  | { kind: "model"; modelId: string }
  | { kind: "relation"; layer: string; name: string }
  | null;
export type SmSession = {
  catalogMode: CatalogMode;
  selection: Selection;
  tab: string;
  context: QueryContext;
  lookupPolicy: "cache_only" | "review";
  sqlDraft: string;
  inspectorOpen: boolean;
};
export type Async<T> =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; value: T }
  | { status: "error"; error: string; code?: string; details?: Record<string, unknown> };

export const DEFAULT_SESSION: SmSession = {
  catalogMode: "models",
  selection: null,
  tab: "contract",
  context: { mode: "current_corrected", availability_policy: "local_as_ingested", timezone: "UTC" },
  lookupPolicy: "cache_only",
  sqlDraft: "",
  inspectorOpen: true,
};

function failed(err: unknown): Async<never> {
  if (err instanceof SecurityMasterError) return { status: "error", error: err.message, code: err.code, details: err.details };
  return { status: "error", error: err instanceof Error ? err.message : String(err) };
}

/**
 * The browser's session: catalog, models, the selected thing's detail, and a
 * stale-guarded runner for everything the workbench asks for. A response is
 * applied only when its request is still the newest for its key; the older
 * one is aborted, and its promise resolves to undefined so no caller can
 * relabel it with newer controls.
 */
export function useSecurityMaster(backend: BackendConfig | undefined, session: SmSession, refreshKey: number) {
  const client = useMemo(() => (backend ? smClient(backend) : null), [backend]);
  const [catalog, setCatalog] = useState<Async<Catalog>>({ status: "idle" });
  const [models, setModels] = useState<Async<OdpModel[]>>({ status: "idle" });
  const [detail, setDetail] = useState<Async<OdpModelDetail | RelationDetail>>({ status: "idle" });
  const [results, setResults] = useState<Record<string, Async<unknown>>>({});
  const inflight = useRef(new Map<string, { id: number; controller: AbortController }>());
  const seq = useRef(0);

  const run = useCallback(async <T,>(key: string, fn: (signal: AbortSignal) => Promise<T>): Promise<T | undefined> => {
    inflight.current.get(key)?.controller.abort();
    const id = ++seq.current;
    const controller = new AbortController();
    inflight.current.set(key, { id, controller });
    setResults((r) => ({ ...r, [key]: { status: "loading" } }));
    try {
      const value = await fn(controller.signal);
      if (inflight.current.get(key)?.id !== id) return undefined;
      setResults((r) => ({ ...r, [key]: { status: "ready", value } }));
      return value;
    } catch (err) {
      if (inflight.current.get(key)?.id !== id) return undefined;
      setResults((r) => ({ ...r, [key]: failed(err) }));
      return undefined;
    }
  }, []);

  const cancelAll = useCallback(() => {
    for (const v of inflight.current.values()) v.controller.abort();
    inflight.current.clear();
  }, []);

  useEffect(() => {
    if (!client) { setCatalog({ status: "idle" }); setModels({ status: "idle" }); return; }
    const controller = new AbortController();
    setCatalog({ status: "loading" });
    setModels({ status: "loading" });
    client.catalog(controller.signal).then((v) => setCatalog({ status: "ready", value: v }), (e: unknown) => { if (!controller.signal.aborted) setCatalog(failed(e)); });
    client.odpModels(controller.signal).then((v) => setModels({ status: "ready", value: v.models }), (e: unknown) => { if (!controller.signal.aborted) setModels(failed(e)); });
    return () => controller.abort();
  }, [client, refreshKey]);

  const sel = session.selection;
  const selKey = sel === null ? "" : sel.kind === "model" ? `m:${sel.modelId}` : `r:${sel.layer}.${sel.name}`;
  useEffect(() => {
    if (!client || !sel) { setDetail({ status: "idle" }); return; }
    const controller = new AbortController();
    setDetail({ status: "loading" });
    const p = sel.kind === "model" ? client.odpModel(sel.modelId, controller.signal) : client.relation(sel.layer, sel.name, controller.signal);
    p.then((v) => setDetail({ status: "ready", value: v }), (e: unknown) => { if (!controller.signal.aborted) setDetail(failed(e)); });
    return () => controller.abort();
    // selKey stands in for `sel`, whose object identity changes on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, selKey, refreshKey]);

  useEffect(() => cancelAll, [cancelAll]);

  return { client, catalog, models, detail, run, results, cancelAll };
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pnpm vitest run src/hooks/useSecurityMaster.test.ts && pnpm typecheck`
Expected: PASS. (`@testing-library/react` 16 is already a dev dependency on this line and provides `renderHook`.)

- [ ] **Step 5: Commit**

```bash
git add src/hooks/useSecurityMaster.ts src/hooks/useSecurityMaster.test.ts
git commit -m "feat(security-master): the session hook with stale-response guards and bounded errors"
```

### Task 18: Renderer shell, catalog pane, context bar, model workbench, inspector

**Files:**
- Create: `src/components/renderers/SecurityMasterBrowserRenderer.tsx`, `src/components/renderers/securityMaster/CatalogPane.tsx`, `securityMaster/ContextBar.tsx`, `securityMaster/ModelWorkbench.tsx`, `securityMaster/Inspector.tsx`, `securityMaster/format.ts`
- Modify: `src/lib/types.ts` (`DashboardCard.securityMaster?: Partial<SmSession>`)
- Test: `src/components/renderers/SecurityMasterBrowserRenderer.test.tsx`

**Interfaces:**
- Produces:

```ts
export type SecurityMasterBrowserProps = {
  backend: BackendConfig | undefined;
  session: Partial<SmSession> | undefined;      // card.securityMaster
  onSessionChange: (patch: Partial<SmSession>) => void;
  params: Record<string, string>;               // effective card params
  onParamChange: (edits: Record<string, string>) => void;
  refreshKey: number;
};
export function SecurityMasterBrowserRenderer(props: SecurityMasterBrowserProps): JSX.Element;
```
- `format.ts`: `contextSummary(ctx: QueryContext): string` (e.g. `"Known at 2026-09-14 20:00 UTC · effective 2026-09-14"`, `"Current corrected · versions pinned when a query runs"`), `contextKey(ctx): string` (stable JSON), `toUtcIso(local: string): string | undefined`, `fromUtcIso(iso: string | undefined): string` (for `datetime-local` inputs), `MODE_LABELS: Record<TemporalMode, string>`.
- `CatalogPane` props: `{ mode, onMode, models: Async<OdpModel[]>, catalog: Async<Catalog>, selection, onSelect }`.
- `ContextBar` props: `{ context, onContext, allowedModes: string[], lookupPolicy, onLookupPolicy, acquisitionPolicy: Policy | null, layerLabel: string }`.
- `ModelWorkbench` props: `{ detail: OdpModelDetail, tab, onTab, context, onOpenScenario: (relation: string, earlier: QueryContext, later: QueryContext) => void }`.
- `Inspector` props: `{ title, subtitle, sections: Array<{ heading: string; rows: Array<[string, string]> }>, actions: Array<{ label: string; onClick: () => void }>, onClose }`.
- Session merging: `const session = { ...DEFAULT_SESSION, ...props.session }`; card params seed the context on mount when present (`temporal_mode`, `effective_date` → `effective_at = date + "T00:00:00Z"`, `known_at`), and a context change publishes `temporal_mode`, `effective_date`, `known_at` through `onParamChange` so parameter groups can bind them.

- [ ] **Step 1: Write the failing renderer test**

```tsx
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { backend } from "../../test/fixtures";
import { startMockServer, type MockServer } from "../../test/mockServer";
import { SecurityMasterBrowserRenderer } from "./SecurityMasterBrowserRenderer";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: globalThis.fetch }));

const DIR = join(__dirname, "../../test/contract/security-master");
const load = (n: string) => JSON.parse(readFileSync(join(DIR, `${n}.json`), "utf8")) as unknown;
let server: MockServer | undefined;
afterEach(async () => { await server?.close(); server = undefined; });

const ROUTES = {
  "/security-master/v1/catalog": load("catalog"),
  "/security-master/v1/odp/models": load("odp_models"),
  "/security-master/v1/odp/models/MarketCalendar": load("odp_model_market_calendar"),
  "/security-master/v1/odp/models/EquityInfo": { model: (load("odp_models") as { models: unknown[] }).models[0], scenario: { scenario: "shares_outstanding", fixture_id: "shares-outstanding", expectations: [], captures: [] } },
};

function mount(over: Partial<Parameters<typeof SecurityMasterBrowserRenderer>[0]> = {}) {
  const onSessionChange = vi.fn();
  const onParamChange = vi.fn();
  render(<SecurityMasterBrowserRenderer backend={backend({ baseUrl: server!.url })} session={undefined}
    onSessionChange={onSessionChange} params={{}} onParamChange={onParamChange} refreshKey={0} {...over} />);
  return { onSessionChange, onParamChange };
}

describe("SecurityMasterBrowserRenderer", () => {
  it("opens on ODP Models with the six models grouped, and selects one", async () => {
    server = await startMockServer(ROUTES);
    const { onSessionChange } = mount();
    expect(screen.getByRole("tab", { name: "ODP Models" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(screen.getByRole("button", { name: /Market Calendar/ })).toBeInTheDocument());
    expect(screen.getAllByRole("button", { name: /model$/ })).toHaveLength(6);
    await userEvent.click(screen.getByRole("button", { name: /Market Calendar/ }));
    expect(onSessionChange).toHaveBeenCalledWith({ selection: { kind: "model", modelId: "MarketCalendar" }, tab: "contract" });
  });

  it("shows the contract, fields, mapping, consumers and temporal example tabs for a selected model", async () => {
    server = await startMockServer(ROUTES);
    mount({ session: { selection: { kind: "model", modelId: "MarketCalendar" } } });
    await waitFor(() => expect(screen.getByRole("heading", { name: "Market Calendar" })).toBeInTheDocument());
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(
      expect.arrayContaining(["Contract", "Fields", "Mapping", "Consumers", "Temporal example"]));
    expect(screen.getByText("/api/v1/reference/market-calendar")).toBeInTheDocument();
    expect(screen.getByText("calendar_id")).toBeInTheDocument();
    const inspector = screen.getByRole("complementary", { name: "ODP model" });
    expect(within(inspector).getByText("gold.market_calendar")).toBeInTheDocument();
  });

  it("the temporal example tab names the scenario and offers to open the comparison", async () => {
    server = await startMockServer(ROUTES);
    const { onSessionChange } = mount({ session: { selection: { kind: "model", modelId: "MarketCalendar" }, tab: "example" } });
    await waitFor(() => expect(screen.getByText("Lunar holiday correction")).toBeInTheDocument());
    expect(screen.getByText("T1 · Authority confirmed")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Open Lunar holiday correction comparison/ }));
    expect(onSessionChange).toHaveBeenLastCalledWith(expect.objectContaining({
      selection: { kind: "relation", layer: "gold", name: "market_calendar" }, tab: "compare", catalogMode: "tables",
    }));
  });

  it("the context bar disables modes the model does not support and publishes the chosen context", async () => {
    server = await startMockServer(ROUTES);
    const { onSessionChange, onParamChange } = mount({ session: { selection: { kind: "model", modelId: "EquityInfo" } } });
    await waitFor(() => expect(screen.getByRole("heading", { name: "Equity Info" })).toBeInTheDocument());
    const mode = screen.getByRole("combobox", { name: "Temporal mode" });
    expect(within(mode).getByRole("option", { name: "Delta snapshot" })).toBeDisabled();
    await userEvent.selectOptions(mode, "known_at");
    await userEvent.type(screen.getByLabelText("Known at (UTC)"), "2026-08-05T12:00");
    expect(onSessionChange).toHaveBeenLastCalledWith({ context: expect.objectContaining({ mode: "known_at", known_at: "2026-08-05T12:00:00Z" }) });
    expect(onParamChange).toHaveBeenLastCalledWith({ temporal_mode: "known_at", known_at: "2026-08-05T12:00:00Z", effective_date: "" });
  });

  it("card params seed the context on mount", async () => {
    server = await startMockServer(ROUTES);
    mount({ params: { temporal_mode: "effective_on", effective_date: "2022-06-07" } });
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Temporal mode" })).toHaveValue("effective_on"));
    expect(screen.getByText(/Effective on 2022-06-07/)).toBeInTheDocument();
  });

  it("a service error is a bounded state with the domain code", async () => {
    server = await startMockServer({ ...ROUTES, "/security-master/v1/catalog": { __status: 500, __body: "boom" } });
    mount();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("The security-master service answered HTTP 500."));
  });
});
```

`@testing-library/user-event` is on this line's dev dependencies (used by other renderer tests); confirm with `grep user-event package.json`.

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm vitest run src/components/renderers/SecurityMasterBrowserRenderer.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 3: Write `securityMaster/format.ts`**

```ts
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import type { QueryContext, TemporalMode } from "../../../lib/securityMaster";

export const MODE_LABELS: Record<TemporalMode, string> = {
  current_corrected: "Current corrected",
  known_at: "Known at",
  effective_on: "Effective on",
  delta_snapshot: "Delta snapshot",
  captured_by: "Captured by",
};

/** "2026-08-05T12:00" (a datetime-local value, read as UTC) → "2026-08-05T12:00:00Z". */
export function toUtcIso(local: string): string | undefined {
  if (!local) return undefined;
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::(\d{2}))?$/.exec(local);
  if (!m) return undefined;
  return `${m[1]}T${m[2]}:${m[3] ?? "00"}Z`;
}

export function fromUtcIso(iso: string | undefined): string {
  if (!iso) return "";
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(iso);
  return m ? `${m[1]}T${m[2]}` : "";
}

export function contextKey(ctx: QueryContext): string {
  return JSON.stringify([ctx.mode, ctx.effective_at ?? "", ctx.known_at ?? "", ctx.availability_policy ?? "", ctx.versions ?? null]);
}

export function contextSummary(ctx: QueryContext): string {
  const eff = ctx.effective_at ? ctx.effective_at.slice(0, 10) : "";
  switch (ctx.mode) {
    case "known_at":
      return `Known at ${(ctx.known_at ?? "").replace("T", " ").replace(/:00Z$/, " UTC")}${eff ? ` · effective ${eff}` : ""}`;
    case "effective_on":
      return `Effective on ${eff} · latest corrected knowledge`;
    case "delta_snapshot":
      return "Physical table versions · no business-time claim";
    case "captured_by":
      return `Captures observed by ${ctx.known_at ?? ""} · no effective truth`;
    default:
      return "Current corrected · versions pinned when a query runs";
  }
}

export function titleCase(id: string): string {
  return id.replace(/([a-z])([A-Z])/g, "$1 $2").replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
}
```

- [ ] **Step 4: Write `CatalogPane.tsx`**

```tsx
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import type { Async, CatalogMode, Selection } from "../../../hooks/useSecurityMaster";
import type { Catalog, OdpModel } from "../../../lib/securityMaster";
import { titleCase } from "./format";

type Props = {
  mode: CatalogMode;
  onMode: (mode: CatalogMode) => void;
  models: Async<OdpModel[]>;
  catalog: Async<Catalog>;
  selection: Selection;
  onSelect: (selection: Selection) => void;
};

const LAYERS: Array<[string, string]> = [["gold", "Curated"], ["silver", "Normalized"], ["bronze", "Raw evidence"]];

function groupOf(model: OdpModel): string {
  if (model.route.startsWith("/api/v1/equity/price")) return "Market data";
  if (model.route.startsWith("/api/v1/equity")) return "Equity";
  return "Reference";
}

export function CatalogPane({ mode, onMode, models, catalog, selection, onSelect }: Props) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const isSelected = (s: Selection) => JSON.stringify(s) === JSON.stringify(selection);
  return (
    <aside className="sm-catalog" aria-label="Catalog">
      <div className="sm-catalog-head">
        <span className="sm-eyebrow">Catalog</span>
        <span className="sm-muted">
          {mode === "models"
            ? models.status === "ready" ? `${models.value.length} models` : ""
            : catalog.status === "ready" ? `${catalog.value.relations.filter((r) => r.layer !== "ops").length} tables` : ""}
        </span>
      </div>
      <div className="sm-segment" role="tablist" aria-label="Catalog mode">
        {(["models", "tables"] as const).map((m) => (
          <button key={m} type="button" role="tab" aria-selected={mode === m} className="sm-segment-btn" onClick={() => onMode(m)}>
            {m === "models" ? "ODP Models" : "Tables"}
          </button>
        ))}
      </div>
      <input className="sm-search" type="search" aria-label={mode === "models" ? "Find an ODP model" : "Find a table"}
        placeholder={mode === "models" ? "Find an ODP model…" : "Find a table…"} value={query} onChange={(e) => setQuery(e.target.value)} />
      {mode === "models" && models.status === "error" && <p className="sm-error" role="alert">{models.error}</p>}
      {mode === "tables" && catalog.status === "error" && <p className="sm-error" role="alert">{catalog.error}</p>}
      {mode === "models" && models.status === "ready" && (
        <div className="sm-tree">
          {["Equity", "Market data", "Reference"].map((group) => {
            const items = models.value.filter((m) => groupOf(m) === group && (!q || m.model_id.toLowerCase().includes(q)));
            if (!items.length) return null;
            return (
              <section key={group} className="sm-tree-group">
                <h4 className="sm-tree-title">{group} <span className="sm-muted">{items.length}</span></h4>
                {items.map((m) => {
                  const sel: Selection = { kind: "model", modelId: m.model_id };
                  return (
                    <button key={m.model_id} type="button" className="sm-tree-item" aria-current={isSelected(sel) ? "true" : undefined}
                      onClick={() => onSelect(sel)}>
                      <span className="sm-tree-label">{titleCase(m.model_id)}</span>
                      <span className="sm-muted">{m.kind === "custom" ? "Custom model" : "Standard model"}</span>
                    </button>
                  );
                })}
              </section>
            );
          })}
        </div>
      )}
      {mode === "tables" && catalog.status === "ready" && (
        <div className="sm-tree">
          {LAYERS.map(([layer, sub]) => {
            const items = catalog.value.relations.filter((r) => r.layer === layer && (!q || r.name.toLowerCase().includes(q)));
            if (!items.length) return null;
            return (
              <section key={layer} className="sm-tree-group">
                <h4 className="sm-tree-title"><span className={`sm-dot sm-dot-${layer}`} />{titleCase(layer)} <span className="sm-muted">{sub}</span></h4>
                {items.map((r) => {
                  const sel: Selection = { kind: "relation", layer: r.layer, name: r.name };
                  return (
                    <button key={r.name} type="button" className="sm-tree-item" aria-current={isSelected(sel) ? "true" : undefined}
                      onClick={() => onSelect(sel)} title={r.description}>
                      <span className="sm-tree-label">{titleCase(r.name)}</span>
                    </button>
                  );
                })}
              </section>
            );
          })}
        </div>
      )}
      {(models.status === "loading" || catalog.status === "loading") && <p className="sm-muted">Reading the catalog…</p>}
    </aside>
  );
}
```

- [ ] **Step 5: Write `ContextBar.tsx`**

```tsx
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import { TEMPORAL_MODES, type Policy, type QueryContext, type TemporalMode } from "../../../lib/securityMaster";
import { MODE_LABELS, contextSummary, fromUtcIso, toUtcIso } from "./format";

type Props = {
  context: QueryContext;
  onContext: (context: QueryContext) => void;
  allowedModes: string[];
  lookupPolicy: "cache_only" | "review";
  onLookupPolicy: (p: "cache_only" | "review") => void;
  acquisitionPolicy: Policy | null;
  layerLabel: string;
};

export function ContextBar({ context, onContext, allowedModes, lookupPolicy, onLookupPolicy, acquisitionPolicy, layerLabel }: Props) {
  const set = (patch: Partial<QueryContext>) => onContext({ ...context, ...patch });
  const needsEffective = context.mode === "effective_on" || context.mode === "known_at";
  const needsKnown = context.mode === "known_at" || context.mode === "captured_by";
  return (
    <div className="sm-context" role="group" aria-label="Query context">
      <span className="sm-eyebrow">Layer</span>
      <span className="sm-chip">{layerLabel}</span>
      <label className="sm-field">
        <span className="sm-visually-hidden">Temporal mode</span>
        <select aria-label="Temporal mode" value={context.mode} onChange={(e) => {
          const mode = e.target.value as TemporalMode;
          set({ mode, ...(mode === "current_corrected" || mode === "delta_snapshot" ? { known_at: undefined } : {}) });
        }}>
          {TEMPORAL_MODES.map((m) => (
            <option key={m} value={m} disabled={allowedModes.length > 0 && !allowedModes.includes(m)}>{MODE_LABELS[m]}</option>
          ))}
        </select>
      </label>
      {needsEffective && (
        <label className="sm-field">Effective
          <input type="date" aria-label="Effective date" value={context.effective_at?.slice(0, 10) ?? ""}
            onChange={(e) => set({ effective_at: e.target.value ? `${e.target.value}T00:00:00Z` : undefined })} />
        </label>
      )}
      {needsKnown && (
        <label className="sm-field">Known at (UTC)
          <input type="datetime-local" aria-label="Known at (UTC)" value={fromUtcIso(context.known_at)}
            onChange={(e) => set({ known_at: toUtcIso(e.target.value) })} />
        </label>
      )}
      <span className="sm-context-summary">{contextSummary(context)}</span>
      <label className="sm-field sm-context-lookup">Lookup
        <select aria-label="Lookup policy" value={lookupPolicy} disabled={acquisitionPolicy === "disabled"}
          onChange={(e) => onLookupPolicy(e.target.value as "cache_only" | "review")}>
          <option value="cache_only">Cache only</option>
          <option value="review">Review before acquiring</option>
        </select>
      </label>
    </div>
  );
}
```

- [ ] **Step 6: Write `ModelWorkbench.tsx`**

```tsx
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import type { OdpModelDetail, QueryContext } from "../../../lib/securityMaster";
import { temporalScenarioView } from "../../../lib/securityMasterTemporal";
import { dubaiEidAlFitr2024, indiaBakriEid2023 } from "../../../test/fixtures/securityMasterTemporal";
import { MODE_LABELS, titleCase } from "./format";

export const MODEL_TABS: Array<[string, string]> = [
  ["contract", "Contract"], ["fields", "Fields"], ["mapping", "Mapping"], ["consumers", "Consumers"], ["example", "Temporal example"],
];

const SCENARIO_TITLES: Record<string, string> = {
  trade_correction: "Trade correction",
  shares_outstanding: "Shares outstanding",
  cusip_change: "CUSIP change",
  ticker_change: "Ticker change",
  lunar_holiday_correction: "Lunar holiday correction",
};

const SCENARIO_WHY: Record<string, [string, string]> = {
  trade_correction: ["Knowledge time changes when the provider revises the close", "A market date and a Delta version are not enough to reproduce a decision made before the correction arrived."],
  shares_outstanding: ["Knowledge time changes when a filing is published, then amended", "The period end never moves; only what was known by the cutoff does."],
  cusip_change: ["Effective time changes at the reorganization", "The issuer continues while the legal security identity changes; the old CUSIP is a historical alias, never current."],
  ticker_change: ["Effective time changes at the symbol change", "One stable listing carries both symbols; prices stay one series."],
  lunar_holiday_correction: ["Knowledge time changes at religious verification, then at the exchange notice", "The religious authority corrects the lunar date at its captured knowledge time; the exchange separately publishes the trading-session effect."],
};

type Props = {
  detail: OdpModelDetail;
  tab: string;
  onTab: (tab: string) => void;
  context: QueryContext;
  onOpenScenario: (relation: string, earlier: QueryContext, later: QueryContext) => void;
};

type Expectation = { context?: QueryContext; params?: Record<string, unknown> };

function bounds(expectations: unknown[]): [QueryContext, QueryContext] {
  const ctxs = (expectations as Expectation[]).map((e) => e.context).filter((c): c is QueryContext => !!c && c.mode === "known_at");
  const earlier = ctxs[0] ?? { mode: "current_corrected" };
  const later = ctxs[ctxs.length - 1] ?? { mode: "current_corrected" };
  return [earlier, later];
}

export function ModelWorkbench({ detail, tab, onTab, context, onOpenScenario }: Props) {
  const { model, scenario } = detail;
  const title = SCENARIO_TITLES[model.primary_temporal_scenario] ?? titleCase(model.primary_temporal_scenario);
  const [why, whyBody] = SCENARIO_WHY[model.primary_temporal_scenario] ?? [title, ""];
  const example = (scenario ? (scenario.expectations as Expectation[]).find((e) => e.params) : undefined);
  return (
    <section className="sm-workbench">
      <div className="sm-crumbs">odp / {model.kind === "custom" ? "custom_models" : "standard_models"} / {model.model_id}</div>
      <header className="sm-workbench-head">
        <h3>{titleCase(model.model_id)}</h3>
        <span className="sm-chip">{model.kind === "custom" ? "Custom model" : "Standard model"}</span>
      </header>
      <div className="sm-tabs" role="tablist">
        {MODEL_TABS.map(([id, label]) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} className="sm-tab" onClick={() => onTab(id)}>{label}</button>
        ))}
      </div>
      {tab === "contract" && (
        <div className="sm-panel">
          <div className="sm-route"><code>GET</code> <code>{model.route}</code></div>
          <div className="sm-two">
            <div>
              <h4 className="sm-eyebrow">Query parameters <span className="sm-muted">{model.parameters.length}</span></h4>
              <table className="sm-kv"><tbody>
                {model.parameters.map((p) => (
                  <tr key={p.name}><td><code>{p.name}</code>{p.description ? <div className="sm-muted">{p.description}</div> : null}</td>
                    <td className="sm-right"><code>{p.type}</code><div className="sm-muted">{p.required ? "Required" : "Optional"}</div></td></tr>
                ))}
              </tbody></table>
            </div>
            <div>
              <h4 className="sm-eyebrow">Example request <span className="sm-muted">Python</span></h4>
              <pre className="sm-code">{`from openbb import obb\n\nobb${model.route.replace("/api/v1", "").replaceAll("/", ".").replaceAll("-", "_")}(\n${
                Object.entries(example?.params ?? {}).map(([k, v]) => `    ${k}=${JSON.stringify(v)},`).join("\n")}\n    provider="local_security_master",\n)`}</pre>
              <p className="sm-muted">Standard rows remain portable. Cache status, temporal context, capture IDs and Delta versions travel in response metadata.</p>
            </div>
          </div>
        </div>
      )}
      {tab === "fields" && (
        <div className="sm-panel">
          <table className="sm-kv"><thead><tr><th>Field</th><th>Type</th><th>Source</th></tr></thead><tbody>
            {model.fields.map((f) => <tr key={f.name}><td><code>{f.name}</code></td><td><code>{f.type}</code></td><td className="sm-muted">{f.source}</td></tr>)}
          </tbody></table>
        </div>
      )}
      {tab === "mapping" && (
        <div className="sm-panel">
          <p>Backing relation <code>{model.backing_relation}</code> · temporal capabilities {model.temporal_capabilities.map((m) => MODE_LABELS[m as keyof typeof MODE_LABELS] ?? m).join(", ")}</p>
          {[...new Set(model.fields.map((f) => f.source.split(".").slice(0, 2).join(".")))].map((rel) => (
            <div key={rel}><h4 className="sm-eyebrow">{rel}</h4>
              <p>{model.fields.filter((f) => f.source.startsWith(rel)).map((f) => <code key={f.name} className="sm-pill">{f.name}</code>)}</p></div>
          ))}
        </div>
      )}
      {tab === "consumers" && (
        <div className="sm-panel"><ul>{model.consumers.map((c) => <li key={c}>{c}</li>)}</ul></div>
      )}
      {tab === "example" && (
        <div className="sm-panel">
          <div className="sm-two">
            <div className="sm-box"><span className="sm-eyebrow">ODP model</span><strong>{titleCase(model.model_id)}</strong><code>{model.route}</code></div>
            <div className="sm-box"><span className="sm-eyebrow">Relevant temporal case</span><strong>{title}</strong><code>{model.backing_relation}</code></div>
          </div>
          <div className="sm-callout">
            <span className="sm-eyebrow">Why this model is affected</span>
            <strong>{why}</strong>
            <p className="sm-muted">{whyBody}</p>
          </div>
          {scenario && (
            <div className="sm-two">
              <div className="sm-box"><span className="sm-eyebrow">Projected</span><strong>Known before the correction</strong><span className="sm-muted">Earlier context · {bounds(scenario.expectations)[0].known_at ?? "current"}</span></div>
              <div className="sm-box sm-box-active"><span className="sm-eyebrow">Corrected</span><strong>Known after the correction</strong><span className="sm-muted">Later context · {bounds(scenario.expectations)[1].known_at ?? "current"}</span></div>
            </div>
          )}
          {model.model_id === "MarketCalendar" && (
            <div className="sm-timeline">
              <span className="sm-eyebrow">Observation timeline · historical cases</span>
              {[indiaBakriEid2023, dubaiEidAlFitr2024].map((fixture) => (
                <div key={fixture.fixture_id} className="sm-timeline-row">
                  <strong>{fixture.exchange_mic} · {fixture.holiday_family}</strong>
                  {fixture.expected_states.map((state) => {
                    const view = temporalScenarioView(fixture, new Date(state.known_from));
                    return view ? (
                      <div key={state.stage} className="sm-timeline-stage">
                        <span className="sm-chip">{view.stageLabel}</span>
                        <span>{view.authorityLabel} · market {view.marketLabel.toLowerCase()}</span>
                        <span className="sm-muted">{view.summary}</span>
                      </div>
                    ) : null;
                  })}
                </div>
              ))}
            </div>
          )}
          <div className="sm-actions">
            <p className="sm-muted">Open the scenario to inspect its observation timeline, bound context, backing relation and temporal receipt.</p>
            <button type="button" className="sm-primary" disabled={!scenario}
              onClick={() => scenario && onOpenScenario(model.backing_relation, ...bounds(scenario.expectations))}>
              Open {title} comparison ↗
            </button>
          </div>
          <p className="sm-muted">Current context: {MODE_LABELS[context.mode]}. Opening the comparison pins the scenario's two contexts instead.</p>
        </div>
      )}
    </section>
  );
}
```

The fixtures import from `src/test/fixtures/securityMasterTemporal.ts` is deliberate for this release: the two historical cases are static, hand-checked data with no network path, and the design keeps them as the "observation timeline" until the service serves them (a follow-up). Vite bundles the module like any other.

- [ ] **Step 7: Write `Inspector.tsx`**

```tsx
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

type Props = {
  label: string;
  title: string;
  subtitle?: string;
  sections: Array<{ heading: string; rows: Array<[string, string]> }>;
  actions?: Array<{ label: string; onClick: () => void; disabled?: boolean }>;
  onClose: () => void;
};

export function Inspector({ label, title, subtitle, sections, actions = [], onClose }: Props) {
  return (
    <aside className="sm-inspector" aria-label={label}>
      <div className="sm-inspector-head">
        <span className="sm-eyebrow">{label}</span>
        <button type="button" className="sm-icon" aria-label="Close inspector" onClick={onClose}>×</button>
      </div>
      <h3>{title}</h3>
      {subtitle ? <p className="sm-muted">{subtitle}</p> : null}
      {sections.map((s) => (
        <section key={s.heading}>
          <h4 className="sm-eyebrow">{s.heading}</h4>
          <table className="sm-kv"><tbody>
            {s.rows.map(([k, v]) => <tr key={k}><td className="sm-muted">{k}</td><td className="sm-right"><code>{v}</code></td></tr>)}
          </tbody></table>
        </section>
      ))}
      {actions.map((a) => (
        <button key={a.label} type="button" className="sm-secondary" onClick={a.onClick} disabled={a.disabled}>{a.label}</button>
      ))}
    </aside>
  );
}
```

- [ ] **Step 8: Write the renderer shell**

```tsx
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from "react";
import { DEFAULT_SESSION, useSecurityMaster, type Selection, type SmSession } from "../../hooks/useSecurityMaster";
import type { OdpModelDetail, QueryContext, RelationDetail } from "../../lib/securityMaster";
import type { BackendConfig } from "../../lib/types";
import { ErrorState } from "./ErrorState";
import { CatalogPane } from "./securityMaster/CatalogPane";
import { ContextBar } from "./securityMaster/ContextBar";
import { Inspector } from "./securityMaster/Inspector";
import { ModelWorkbench } from "./securityMaster/ModelWorkbench";
import { RelationWorkbench } from "./securityMaster/RelationWorkbench";
import { MODE_LABELS, titleCase } from "./securityMaster/format";

export type SecurityMasterBrowserProps = {
  backend: BackendConfig | undefined;
  session: Partial<SmSession> | undefined;
  onSessionChange: (patch: Partial<SmSession>) => void;
  params: Record<string, string>;
  onParamChange: (edits: Record<string, string>) => void;
  refreshKey: number;
};

function seeded(params: Record<string, string>, base: QueryContext): QueryContext {
  const mode = params.temporal_mode;
  const ctx: QueryContext = { ...base };
  if (mode === "current_corrected" || mode === "known_at" || mode === "effective_on" || mode === "delta_snapshot" || mode === "captured_by") ctx.mode = mode;
  if (params.effective_date) ctx.effective_at = `${params.effective_date}T00:00:00Z`;
  if (params.known_at) ctx.known_at = params.known_at;
  return ctx;
}

const isModel = (d: OdpModelDetail | RelationDetail): d is OdpModelDetail => "model" in d;

/**
 * The Security Master Browser: a catalog, a context bar, a workbench and an
 * inspector. Presentation state lives on the card; every result lives in the
 * session hook and is never persisted. See docs/superpowers/specs/2026-09-17-security-master-browser-design.md.
 */
export function SecurityMasterBrowserRenderer({ backend, session: stored, onSessionChange, params, onParamChange, refreshKey }: SecurityMasterBrowserProps) {
  const session = useMemo<SmSession>(() => ({ ...DEFAULT_SESSION, ...stored }), [stored]);
  // Card params (possibly a group's) seed the context once; after that the bar owns it.
  const [seededOnce, setSeededOnce] = useState(false);
  useEffect(() => {
    if (seededOnce) return;
    setSeededOnce(true);
    if (params.temporal_mode || params.effective_date || params.known_at) {
      onSessionChange({ context: seeded(params, session.context) });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const context = seededOnce ? session.context : seeded(params, session.context);
  const sm = useSecurityMaster(backend, session, refreshKey);
  const [inspected, setInspected] = useState<Record<string, unknown> | null>(null);

  const select = (selection: Selection) => {
    setInspected(null);
    onSessionChange({ selection, tab: selection?.kind === "model" ? "contract" : "data" });
  };
  const setContext = (ctx: QueryContext) => {
    onSessionChange({ context: ctx });
    onParamChange({ temporal_mode: ctx.mode, known_at: ctx.known_at ?? "", effective_date: ctx.effective_at?.slice(0, 10) ?? "" });
  };
  const openScenario = (relation: string, earlier: QueryContext, later: QueryContext) => {
    const [layer, name] = relation.split(".", 2) as [string, string];
    onSessionChange({ selection: { kind: "relation", layer, name }, tab: "compare", catalogMode: "tables", context: earlier, compareRight: later } as Partial<SmSession>);
  };

  if (!backend) return <p className="empty-state">Waiting for backend…</p>;
  if (sm.catalog.status === "error") return <ErrorState message={sm.catalog.error} onRetry={() => onSessionChange({})} />;

  const detail = sm.detail;
  const allowedModes = detail.status === "ready" ? (isModel(detail.value) ? detail.value.model.temporal_capabilities : detail.value.relation.modes) : [];
  const layerLabel = session.selection?.kind === "relation" ? titleCase(session.selection.layer) : "ODP contract";
  const acquisitionPolicy = sm.catalog.status === "ready" ? sm.catalog.value.policies.acquisition : null;

  return (
    <div className="sm-browser">
      <CatalogPane mode={session.catalogMode} onMode={(m) => onSessionChange({ catalogMode: m })} models={sm.models} catalog={sm.catalog}
        selection={session.selection} onSelect={select} />
      <div className="sm-main">
        <ContextBar context={context} onContext={setContext} allowedModes={allowedModes} lookupPolicy={session.lookupPolicy}
          onLookupPolicy={(p) => onSessionChange({ lookupPolicy: p })} acquisitionPolicy={acquisitionPolicy} layerLabel={layerLabel} />
        {sm.catalog.status === "ready" && sm.catalog.value.newer_data_available && (
          <p className="sm-notice" role="status">Newer data is available. Refresh the card to query it; pinned results are unchanged.</p>
        )}
        {session.selection === null && <p className="empty-state">Pick an ODP model or a table to begin.</p>}
        {detail.status === "loading" && <p className="sm-muted">Loading…</p>}
        {detail.status === "error" && <p className="sm-error" role="alert">{detail.error}{detail.code ? ` (${detail.code})` : ""}</p>}
        {detail.status === "ready" && isModel(detail.value) && (
          <ModelWorkbench detail={detail.value} tab={session.tab} onTab={(tab) => onSessionChange({ tab })} context={context} onOpenScenario={openScenario} />
        )}
        {detail.status === "ready" && !isModel(detail.value) && sm.client && (
          <RelationWorkbench detail={detail.value} session={session} context={context} onSession={onSessionChange} client={sm.client}
            run={sm.run} results={sm.results} onInspect={setInspected} onPublish={onParamChange} acquisitionPolicy={acquisitionPolicy} />
        )}
        <footer className="sm-status">
          <span>{session.selection?.kind === "relation" ? "Cache only · no provider requests" : "Contract projection · no provider request"}</span>
          <span className="sm-muted">{MODE_LABELS[context.mode]}</span>
        </footer>
      </div>
      {session.inspectorOpen && detail.status === "ready" && isModel(detail.value) && (
        <Inspector label="ODP model" title={titleCase(detail.value.model.model_id)} subtitle={detail.value.model.kind === "custom" ? "Custom model" : "Standard model"}
          sections={[
            { heading: "Contract", rows: [["Route", detail.value.model.route], ["Parameters", String(detail.value.model.parameters.length)], ["Fields mapped", `${detail.value.model.fields.length} / ${detail.value.model.fields.length}`]] },
            { heading: "Backing relation", rows: [["Relation", detail.value.model.backing_relation], ["Temporal", detail.value.model.temporal_capabilities.join(", ")]] },
            { heading: "Response envelope", rows: [["Rows", "ODP Data[]"], ["Receipt", "OBBject.extra"], ["Temporal mode", MODE_LABELS[context.mode]]] },
          ]}
          actions={[{ label: "Open backing relation ↗", onClick: () => { const [layer, name] = detail.value.model.backing_relation.split(".", 2) as [string, string]; select({ kind: "relation", layer, name }); onSessionChange({ catalogMode: "tables" }); } }]}
          onClose={() => onSessionChange({ inspectorOpen: false })} />
      )}
      {session.inspectorOpen && detail.status === "ready" && !isModel(detail.value) && inspected && (
        <Inspector label="Record" title={String(inspected.symbol ?? inspected.listing_id ?? inspected.calendar_id ?? inspected.assertion_id ?? "Record")}
          subtitle={typeof inspected.name === "string" ? inspected.name : undefined}
          sections={[
            { heading: "Identity", rows: (["listing_id", "instrument_id", "security_id", "issuer_id", "calendar_id"] as const).filter((k) => inspected[k] != null).map((k) => [k, String(inspected[k])]) },
            { heading: "Provenance", rows: (["assertion_id", "capture_id", "available_at", "system_from", "supersedes_assertion_id"] as const).filter((k) => inspected[k] != null).map((k) => [k, String(inspected[k])]) },
          ]}
          actions={[
            { label: "Inspect lineage ↗", disabled: typeof inspected.assertion_id !== "string", onClick: () => onSessionChange({ tab: "lineage", lineageAssertion: String(inspected.assertion_id) } as Partial<SmSession>) },
            { label: "Review price download ↗", disabled: acquisitionPolicy === "disabled" || typeof inspected.listing_id !== "string", onClick: () => onSessionChange({ tab: "data", reviewListing: String(inspected.listing_id) } as Partial<SmSession>) },
          ]}
          onClose={() => setInspected(null)} />
      )}
    </div>
  );
}
```

`SmSession` gains three optional fields used above: `compareRight?: QueryContext`, `lineageAssertion?: string`, `reviewListing?: string` (add them to the type in the hook, all optional, none in `DEFAULT_SESSION`). `RelationWorkbench` is Task 19; for this task's tests to run, create it as a stub returning `null` with the props signature from Task 19's Interfaces block.

- [ ] **Step 9: Add `securityMaster` to `DashboardCard`**

In `src/lib/types.ts`, next to `table?: TableCardState`, add `securityMaster?: Record<string, unknown>;` with a comment that the renderer owns its shape (`Partial<SmSession>`), persisted per card and never holding results.

- [ ] **Step 10: Run the tests to verify they pass**

Run: `pnpm vitest run src/components/renderers/SecurityMasterBrowserRenderer.test.tsx && pnpm typecheck`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add src/components/renderers/SecurityMasterBrowserRenderer.tsx src/components/renderers/SecurityMasterBrowserRenderer.test.tsx src/components/renderers/securityMaster src/lib/types.ts src/hooks/useSecurityMaster.ts
git commit -m "feat(security-master): the browser renderer with catalog, context bar, model workbench and inspector"
```

### Task 19: Relation workbench — data, schema, SQL, temporal compare, versions, lineage

**Files:**
- Create: `src/components/renderers/securityMaster/RelationWorkbench.tsx` (replacing the stub)
- Test: `src/components/renderers/securityMaster/RelationWorkbench.test.tsx`

**Interfaces:**
- Props:

```ts
type Props = {
  detail: RelationDetail;
  session: SmSession;
  context: QueryContext;
  onSession: (patch: Partial<SmSession>) => void;
  client: SmClient;
  run: <T>(key: string, fn: (signal: AbortSignal) => Promise<T>) => Promise<T | undefined>;
  results: Record<string, Async<unknown>>;
  onInspect: (row: Record<string, unknown>) => void;
  onPublish: (edits: Record<string, string>) => void;
  acquisitionPolicy: Policy | null;
};
export const RELATION_TABS: Array<[string, string]> = [["data","Data"],["schema","Schema"],["sql","SQL query"],["compare","Temporal compare"],["versions","Versions"],["lineage","Lineage"]];
```
- Result keys: `preview:<relation>:<contextKey>`, `sql:<relation>:<contextKey>`, `compare:<relation>:<leftKey>:<rightKey>`, `versions:<relation>`, `lineage:<relation>:<assertion>`. Pages accumulate in component state keyed by execution id; a new execution replaces them. Clicking a row calls `onInspect(row)` and, when the row has `listing_id`, `onPublish({ listing_id })`.

- [ ] **Step 1: Write the failing test**

```tsx
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_SESSION } from "../../../hooks/useSecurityMaster";
import { backend } from "../../../test/fixtures";
import { startMockServer, type MockServer } from "../../../test/mockServer";
import { SecurityMasterBrowserRenderer } from "../SecurityMasterBrowserRenderer";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: globalThis.fetch }));

const DIR = join(__dirname, "../../../test/contract/security-master");
const load = (n: string) => JSON.parse(readFileSync(join(DIR, `${n}.json`), "utf8")) as unknown;
let server: MockServer | undefined;
afterEach(async () => { await server?.close(); server = undefined; });

const ROUTES = {
  "/security-master/v1/catalog": load("catalog"),
  "/security-master/v1/odp/models": load("odp_models"),
  "/security-master/v1/relations/gold/security_master": load("relation"),
  "/security-master/v1/relations/gold/security_master/versions": load("versions"),
  "/security-master/v1/relations/gold/price_daily": load("relation"),
  "/security-master/v1/preview": load("preview"),
  "/security-master/v1/sql/plan": load("sql_plan"),
  "/security-master/v1/sql/execute": load("sql_execute"),
  "/security-master/v1/sql/executions/qry_x/pages": load("sql_execute"),
  "/security-master/v1/compare": load("compare"),
  "/security-master/v1/lineage": load("lineage"),
};

function mount(session: Record<string, unknown>, routes: Record<string, unknown> = ROUTES) {
  const onSessionChange = vi.fn();
  const onParamChange = vi.fn();
  return startMockServer(routes).then((s) => {
    server = s;
    render(<SecurityMasterBrowserRenderer backend={backend({ baseUrl: s.url })} session={{ ...DEFAULT_SESSION, catalogMode: "tables", ...session }}
      onSessionChange={onSessionChange} params={{}} onParamChange={onParamChange} refreshKey={0} />);
    return { onSessionChange, onParamChange, server: s };
  });
}

const REL = { selection: { kind: "relation", layer: "gold", name: "security_master" } };

describe("RelationWorkbench", () => {
  it("Data tab previews rows under the context, shows the receipt, and inspects a row", async () => {
    const { onParamChange } = await mount({ ...REL, tab: "data" });
    await waitFor(() => expect(screen.getByRole("heading", { name: "Security master" })).toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    const rows = within(screen.getByRole("table")).getAllByRole("row");
    expect(rows.length).toBeGreaterThan(1);
    expect(screen.getByText(/Receipt qry_/)).toBeInTheDocument();
    expect(screen.getByText(/rows/)).toBeInTheDocument();
    await userEvent.click(rows[1]!);
    expect(onParamChange).toHaveBeenCalledWith({ listing_id: expect.any(String) });
    expect(screen.getByRole("complementary", { name: "Record" })).toBeInTheDocument();
  });

  it("a preview request carries the context and never a provider call", async () => {
    const { server: s } = await mount({ ...REL, tab: "data", context: { mode: "effective_on", effective_at: "2022-06-07T00:00:00Z" } });
    await waitFor(() => expect(s.requests.some((r) => r.path === "/security-master/v1/preview")).toBe(true));
    const body = JSON.parse(s.requests.find((r) => r.path === "/security-master/v1/preview")!.body) as { context: unknown; relation: string };
    expect(body).toMatchObject({ relation: "gold.security_master", context: { mode: "effective_on", effective_at: "2022-06-07T00:00:00Z" } });
    expect(s.requests.every((r) => r.path.startsWith("/security-master/v1/"))).toBe(true);
  });

  it("Schema tab lists the columns", async () => {
    await mount({ ...REL, tab: "schema" });
    await waitFor(() => expect(screen.getByText("listing_id")).toBeInTheDocument());
  });

  it("SQL tab plans, runs, pages and cancels, keeping the draft in the session", async () => {
    const { onSessionChange, server: s } = await mount({ ...REL, tab: "sql", sqlDraft: "SELECT symbol FROM gold.security_master" });
    await waitFor(() => expect(screen.getByRole("textbox", { name: "SQL" })).toHaveValue("SELECT symbol FROM gold.security_master"));
    await userEvent.type(screen.getByRole("textbox", { name: "SQL" }), " ORDER BY symbol");
    expect(onSessionChange).toHaveBeenLastCalledWith({ sqlDraft: expect.stringContaining("ORDER BY symbol") });
    await userEvent.click(screen.getByRole("button", { name: "Plan" }));
    await waitFor(() => expect(screen.getByText(/gold.security_master/)).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    expect(s.requests.some((r) => r.path === "/security-master/v1/sql/execute" && r.method === "POST")).toBe(true);
    expect(screen.getByText(/Receipt qry_/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(s.requests.some((r) => r.path.endsWith("/pages"))).toBe(true));
  });

  it("SQL is disabled when the catalog policy says so", async () => {
    const catalog = { ...(load("catalog") as Record<string, unknown>), policies: { sql: "disabled", acquisition: "review" } };
    await mount({ ...REL, tab: "sql" }, { ...ROUTES, "/security-master/v1/catalog": catalog });
    await waitFor(() => expect(screen.getByRole("button", { name: "Run" })).toBeDisabled());
    expect(screen.getByText(/SQL is disabled by policy/)).toBeInTheDocument();
  });

  it("Temporal compare shows both contexts, the differences and both receipts", async () => {
    await mount({ ...REL, tab: "compare", context: { mode: "known_at", known_at: "2026-09-14T20:05:00Z" }, compareRight: { mode: "known_at", known_at: "2026-09-15T09:20:00Z" } });
    await userEvent.type(screen.getByLabelText("listing_id"), "lst_apple");
    await userEvent.click(screen.getByRole("button", { name: "Compare" }));
    await waitFor(() => expect(screen.getByText("corrected")).toBeInTheDocument());
    expect(screen.getAllByText(/Receipt qry_/)).toHaveLength(2);
  });

  it("Versions tab lists retained versions", async () => {
    await mount({ ...REL, tab: "versions" });
    await waitFor(() => expect(screen.getAllByText(/^v\d+/).length).toBeGreaterThan(0));
  });

  it("Lineage tab walks a chain", async () => {
    await mount({ ...REL, tab: "lineage", lineageAssertion: "as_px_apple_20260914_2" });
    await waitFor(() => expect(screen.getByText("assertion")).toBeInTheDocument());
    expect(screen.getByText("capture")).toBeInTheDocument();
  });

  it("a mode the relation does not support renders the domain error, not the previous rows", async () => {
    const routes = { ...ROUTES, "/security-master/v1/preview": { __status: 422, __body: load("error_mode_unsupported") } };
    await mount({ ...REL, tab: "data", context: { mode: "known_at", known_at: "2026-01-01T00:00:00Z" } }, routes);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("TEMPORAL_MODE_UNSUPPORTED"));
    expect(screen.queryByRole("table")).toBeNull();
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm vitest run src/components/renderers/securityMaster/RelationWorkbench.test.tsx`
Expected: FAIL (the stub renders nothing).

- [ ] **Step 3: Write `RelationWorkbench.tsx`**

```tsx
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react";
import type { Async, SmSession } from "../../../hooks/useSecurityMaster";
import type {
  CompareResult, LineageResult, Policy, QueryContext, Receipt, RelationDetail, ResultPage, SmClient, SqlPlan, VersionList,
} from "../../../lib/securityMaster";
import { TableRenderer } from "../TableRenderer";
import { ContextBar } from "./ContextBar";
import { MODE_LABELS, contextKey, contextSummary, titleCase } from "./format";

export const RELATION_TABS: Array<[string, string]> = [
  ["data", "Data"], ["schema", "Schema"], ["sql", "SQL query"], ["compare", "Temporal compare"], ["versions", "Versions"], ["lineage", "Lineage"],
];

type Props = {
  detail: RelationDetail;
  session: SmSession;
  context: QueryContext;
  onSession: (patch: Partial<SmSession>) => void;
  client: SmClient;
  run: <T>(key: string, fn: (signal: AbortSignal) => Promise<T>) => Promise<T | undefined>;
  results: Record<string, Async<unknown>>;
  onInspect: (row: Record<string, unknown>) => void;
  onPublish: (edits: Record<string, string>) => void;
  acquisitionPolicy: Policy | null;
};

function ReceiptLine({ receipt }: { receipt: Receipt }) {
  return (
    <span className="sm-receipt" title={receipt.dependencies.map((d) => `${d.relation}@v${d.delta_version}`).join("\n")}>
      Receipt {receipt.execution_id} · {MODE_LABELS[receipt.temporal_mode as keyof typeof MODE_LABELS] ?? receipt.temporal_mode} · {receipt.dependencies.length} pinned versions
    </span>
  );
}

function Failure({ state }: { state: Async<unknown> }) {
  if (state.status !== "error") return null;
  return <p className="sm-error" role="alert">{state.code ? `${state.code}: ` : ""}{state.error}</p>;
}

/** Pages of one execution, accumulated in order. A new execution replaces the set. */
function usePages(key: string, results: Record<string, Async<unknown>>) {
  const [pages, setPages] = useState<{ execution: string; list: ResultPage[] }>({ execution: "", list: [] });
  const state = results[key];
  useEffect(() => {
    if (state?.status !== "ready") return;
    const page = state.value as ResultPage;
    setPages((p) => (p.execution === page.execution_id ? { ...p, list: [...p.list.filter((x) => x.next_cursor !== page.next_cursor), page] } : { execution: page.execution_id, list: [page] }));
  }, [state]);
  return pages;
}

export function RelationWorkbench({ detail, session, context, onSession, client, run, results, onInspect, onPublish, acquisitionPolicy }: Props) {
  const rel = detail.relation;
  const full = `${rel.layer}.${rel.name}`;
  const tab = session.tab;
  const ckey = contextKey(context);
  const previewKey = `preview:${full}:${ckey}`;
  const sqlKey = `sql:${full}:${ckey}`;
  const preview = usePages(previewKey, results);
  const sqlPages = usePages(sqlKey, results);
  const [compareRight, setCompareRight] = useState<QueryContext>(session.compareRight ?? { mode: "current_corrected" });
  const [selection, setSelection] = useState<Record<string, string>>({});
  const [assertion, setAssertion] = useState(session.lineageAssertion ?? "");

  useEffect(() => {
    if (tab === "data" && !results[previewKey]) {
      void run(previewKey, (signal) => client.preview({ relation: full, context, page: { limit: 100 } }, signal));
    }
    if (tab === "versions" && !results[`versions:${full}`]) {
      void run(`versions:${full}`, (signal) => client.versions(rel.layer, rel.name, signal));
    }
    if (tab === "lineage" && assertion && !results[`lineage:${full}:${assertion}`]) {
      void run(`lineage:${full}:${assertion}`, (signal) => client.lineage({ relation: full, assertion_id: assertion, context }, signal));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, previewKey, full, assertion]);

  const onRow = (row: Record<string, unknown>) => {
    onInspect(row);
    if (typeof row.listing_id === "string") onPublish({ listing_id: row.listing_id });
  };

  const table = (pages: ResultPage[], moreKey: string) => {
    const rows = pages.flatMap((p) => p.rows);
    const last = pages[pages.length - 1];
    return (
      <>
        <div className="sm-result-strip">
          <span>{last?.count.value ?? rows.length} rows{last?.count.quality === "estimated" ? " (estimated)" : ""}</span>
          {last && <ReceiptLine receipt={last.receipt} />}
          {last?.next_cursor && (
            <button type="button" className="sm-secondary" onClick={() => void run(moreKey, (signal) => client.sqlPage(last.execution_id, last.next_cursor!, signal))}>Load more</button>
          )}
        </div>
        <div className="sm-table" onClickCapture={(e) => {
          const tr = (e.target as HTMLElement).closest("tr[data-index]");
          const i = tr ? Number(tr.getAttribute("data-index")) : NaN;
          if (Number.isInteger(i) && rows[i]) onRow(rows[i]!);
        }}>
          <TableRenderer data={rows} plain />
        </div>
      </>
    );
  };

  return (
    <section className="sm-workbench">
      <div className="sm-crumbs">research / {rel.layer} / {full}</div>
      <header className="sm-workbench-head">
        <h3>{titleCase(rel.name)}</h3>
        <span className="sm-chip">{rel.kind === "view" ? "Curated view" : rel.kind === "external_delta" ? "External Delta" : "Delta table"}</span>
        <span className="sm-muted">{rel.description}</span>
      </header>
      <div className="sm-tabs" role="tablist">
        {RELATION_TABS.map(([id, label]) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} className="sm-tab" onClick={() => onSession({ tab: id })}>{label}</button>
        ))}
      </div>

      {tab === "data" && (
        <div className="sm-panel">
          {results[previewKey]?.status === "loading" && <p className="sm-muted">Reading {full}…</p>}
          <Failure state={results[previewKey] ?? { status: "idle" }} />
          {results[previewKey]?.status !== "error" && preview.list.length > 0 && table(preview.list, previewKey)}
        </div>
      )}

      {tab === "schema" && (
        <div className="sm-panel">
          <table className="sm-kv"><thead><tr><th>Column</th><th>Type</th></tr></thead><tbody>
            {detail.schema.map((c) => <tr key={c.name}><td><code>{c.name}</code></td><td><code>{c.dtype}</code></td></tr>)}
          </tbody></table>
          <p className="sm-muted">Keys {rel.keys.join(", ")} · modes {rel.modes.map((m) => MODE_LABELS[m as keyof typeof MODE_LABELS] ?? m).join(", ")}{detail.latest_version !== null ? ` · latest v${detail.latest_version}` : ""}</p>
        </div>
      )}

      {tab === "sql" && (
        <div className="sm-panel">
          <div className="sm-sql-bar">
            <select aria-label="Query examples" onChange={(e) => e.target.value && onSession({ sqlDraft: e.target.value })} value="">
              <option value="">Query examples</option>
              <option value={`SELECT * FROM ${full} LIMIT 100`}>All columns, 100 rows</option>
              <option value={`SELECT count(*) AS n FROM ${full}`}>Row count</option>
            </select>
            <button type="button" className="sm-secondary" onClick={() => void run(`plan:${full}:${ckey}`, (signal) => client.sqlPlan(session.sqlDraft, context, signal))}>Plan</button>
            <button type="button" className="sm-primary" disabled={session.sqlDraft.trim() === "" || results[sqlKey]?.status === "loading" || sqlDisabled(results)}
              onClick={() => void run(sqlKey, (signal) => client.sqlExecute(session.sqlDraft, context, { max_rows: 1000, timeout_ms: 15000 }, signal))}>Run</button>
            {sqlPages.execution && <button type="button" className="sm-secondary" onClick={() => void client.sqlCancel(sqlPages.execution)}>Cancel</button>}
          </div>
          <textarea className="sm-sql" aria-label="SQL" rows={6} value={session.sqlDraft} onChange={(e) => onSession({ sqlDraft: e.target.value })} spellCheck={false} />
          <p className="sm-muted">Read-only runner: one SELECT over registered relations. Context is bound outside SQL. No writes, files or network.</p>
          {results[`plan:${full}:${ckey}`]?.status === "ready" && (
            <p className="sm-plan">Plan: {(results[`plan:${full}:${ckey}`] as { value: SqlPlan }).value.relations.join(", ")} · columns {(results[`plan:${full}:${ckey}`] as { value: SqlPlan }).value.columns.map((c) => c.name).join(", ")}</p>
          )}
          <Failure state={results[`plan:${full}:${ckey}`] ?? { status: "idle" }} />
          <Failure state={results[sqlKey] ?? { status: "idle" }} />
          {results[sqlKey]?.status === "loading" && <p className="sm-muted">Running…</p>}
          {results[sqlKey]?.status !== "error" && sqlPages.list.length > 0 && table(sqlPages.list, sqlKey)}
          {!sqlPages.list.length && results[sqlKey]?.status !== "loading" && results[sqlKey]?.status !== "error" && (
            <p className="empty-state">Ready when you are. Run a query to see results and a pinned context receipt.</p>
          )}
        </div>
      )}

      {tab === "compare" && (
        <div className="sm-panel">
          <div className="sm-two">
            <div className="sm-box"><span className="sm-eyebrow">Earlier context</span><strong>{contextSummary(context)}</strong></div>
            <div className="sm-box sm-box-active"><span className="sm-eyebrow">Later context</span>
              <ContextBar context={compareRight} onContext={setCompareRight} allowedModes={rel.modes} lookupPolicy="cache_only" onLookupPolicy={() => {}} acquisitionPolicy={acquisitionPolicy} layerLabel={titleCase(rel.layer)} />
            </div>
          </div>
          <div className="sm-sql-bar">
            {rel.keys.map((k) => (
              <label key={k} className="sm-field">{k}<input aria-label={k} value={selection[k] ?? ""} onChange={(e) => setSelection({ ...selection, [k]: e.target.value })} /></label>
            ))}
            <button type="button" className="sm-primary" onClick={() => {
              const sel = Object.fromEntries(Object.entries(selection).filter(([, v]) => v !== ""));
              void run(`compare:${full}:${ckey}:${contextKey(compareRight)}`, (signal) => client.compare({ relation: full, selection: sel, left: context, right: compareRight }, signal));
            }}>Compare</button>
          </div>
          {Object.entries(results).filter(([k]) => k.startsWith(`compare:${full}:`)).slice(-1).map(([k, state]) => (
            <div key={k}>
              <Failure state={state} />
              {state.status === "ready" && (() => {
                const out = state.value as CompareResult;
                return (
                  <>
                    <table className="sm-kv"><thead><tr><th>Field</th><th>Earlier</th><th>Later</th><th>Temporal effect</th></tr></thead><tbody>
                      {out.differences.map((d, i) => <tr key={i}><td><code>{d.field}</code></td><td>{String(d.left ?? "—")}</td><td>{String(d.right ?? "—")}</td><td><span className="sm-pill">{d.classification}</span></td></tr>)}
                      {out.differences.length === 0 && <tr><td colSpan={4} className="sm-muted">No field differs between the two contexts.</td></tr>}
                    </tbody></table>
                    <div className="sm-result-strip"><ReceiptLine receipt={out.left_receipt} /><ReceiptLine receipt={out.right_receipt} /></div>
                  </>
                );
              })()}
            </div>
          ))}
        </div>
      )}

      {tab === "versions" && (
        <div className="sm-panel">
          <Failure state={results[`versions:${full}`] ?? { status: "idle" }} />
          {results[`versions:${full}`]?.status === "ready" && (
            <ul className="sm-versions">
              {(results[`versions:${full}`] as { value: VersionList }).value.versions.map((v) => (
                <li key={v.version}><code>v{v.version}</code> <span className="sm-muted">{v.timestamp}</span>{v.retained ? "" : <span className="sm-pill">not retained</span>}</li>
              ))}
            </ul>
          )}
          <p className="sm-muted">A Delta version is the physical table after a commit. It is not what a strategy knew at a historical cutoff; use Known at for that.</p>
        </div>
      )}

      {tab === "lineage" && (
        <div className="sm-panel">
          <div className="sm-sql-bar">
            <label className="sm-field">Assertion<input aria-label="Assertion id" value={assertion} onChange={(e) => setAssertion(e.target.value)} /></label>
            <button type="button" className="sm-primary" disabled={!assertion} onClick={() => void run(`lineage:${full}:${assertion}`, (signal) => client.lineage({ relation: full.startsWith("gold.") ? rel.depends_on[0] ?? full : full, assertion_id: assertion, context }, signal))}>Trace</button>
          </div>
          <Failure state={results[`lineage:${full}:${assertion}`] ?? { status: "idle" }} />
          {results[`lineage:${full}:${assertion}`]?.status === "ready" && (
            <ol className="sm-chain">
              {(results[`lineage:${full}:${assertion}`] as { value: LineageResult }).value.chain.map((link, i) => (
                <li key={i}><span className="sm-pill">{link.kind}</span> <code>{String(link.assertion_id ?? link.capture_id ?? link.request_id ?? link.run_id ?? "")}</code>
                  <span className="sm-muted"> {String(link.supersedes_assertion_id ? `supersedes ${link.supersedes_assertion_id}` : link.endpoint ?? link.outcome ?? "")}</span></li>
              ))}
            </ol>
          )}
        </div>
      )}
    </section>
  );
}

function sqlDisabled(results: Record<string, Async<unknown>>): boolean {
  return results.__sqlDisabled?.status === "ready";
}
```

Replace the `sqlDisabled` helper with a prop: add `sqlPolicy: Policy | null` to `Props`, pass `sm.catalog.value.policies.sql` from the renderer, disable Run when it is `"disabled"`, and render `<p className="sm-muted">SQL is disabled by policy on this backend.</p>` under the bar in that case. Delete the helper.

For the row click: `TableRenderer`'s `PlainTable` renders `<tr>` per row; add `data-index={i}` to its row element (one attribute, in `TableRenderer.tsx`'s `PlainTable`), which is the only change to the shared table.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pnpm vitest run src/components/renderers/securityMaster/RelationWorkbench.test.tsx src/components/renderers/SecurityMasterBrowserRenderer.test.tsx src/components/renderers/TableRenderer.test.tsx && pnpm typecheck`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/components/renderers/securityMaster/RelationWorkbench.tsx src/components/renderers/securityMaster/RelationWorkbench.test.tsx src/components/renderers/SecurityMasterBrowserRenderer.tsx src/components/renderers/TableRenderer.tsx src/hooks/useSecurityMaster.ts
git commit -m "feat(security-master): relation workbench with paged data, guarded SQL, temporal compare, versions and lineage"
```

### Task 20: Acquisition review and download activity dialogs

**Files:**
- Create: `src/components/renderers/securityMaster/AcquisitionDialogs.tsx`
- Modify: `src/components/renderers/SecurityMasterBrowserRenderer.tsx` (mount the dialogs; `session.reviewListing` opens the review; a header button opens activity), `src/hooks/useSecurityMaster.ts` (`SmSession.jobIds?: string[]`)
- Test: `src/components/renderers/securityMaster/AcquisitionDialogs.test.tsx`

**Interfaces:**
- `AcquisitionReviewDialog` props: `{ client: SmClient; context: QueryContext; listingId: string; onClose: () => void; onCreated: (job: Job) => void }`. It runs a preflight for `{dataset: "price_daily", identifiers: [listingId], start_date, end_date, policy, price_basis}` with dates from two inputs (default: last 30 days) and a policy select, shows identities, missing ranges, expected requests, warnings and the policy, then `Submit` creates the job with the token. Never submits without a preflight.
- `DownloadActivityDialog` props: `{ client: SmClient; jobIds: string[]; onClose: () => void }`. Polls each job every 2 s while non-terminal, lists the stage history, offers `Cancel` on non-terminal jobs.
- The renderer keeps `jobIds` in the session (persisted, ids only) and shows `Download activity <n>` in the workbench header.

- [ ] **Step 1: Write the failing test**

```tsx
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { smClient } from "../../../lib/securityMaster";
import { backend } from "../../../test/fixtures";
import { startMockServer, type MockServer } from "../../../test/mockServer";
import { AcquisitionReviewDialog, DownloadActivityDialog } from "./AcquisitionDialogs";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: globalThis.fetch }));

const DIR = join(__dirname, "../../../test/contract/security-master");
const load = (n: string) => JSON.parse(readFileSync(join(DIR, `${n}.json`), "utf8")) as unknown;
let server: MockServer | undefined;
afterEach(async () => { await server?.close(); server = undefined; });

describe("AcquisitionReviewDialog", () => {
  it("preflights first, shows the review, then submits with the token", async () => {
    server = await startMockServer({
      "/security-master/v1/acquisitions/preflight": load("acquisition_preflight"),
      "/security-master/v1/acquisitions": load("acquisition_job"),
    });
    const onCreated = vi.fn();
    render(<AcquisitionReviewDialog client={smClient(backend({ baseUrl: server.url }))} context={{ mode: "current_corrected" }} listingId="lst_apple" onClose={() => {}} onCreated={onCreated} />);
    await userEvent.click(screen.getByRole("button", { name: "Preflight" }));
    await waitFor(() => expect(screen.getByText(/Expected requests/)).toBeInTheDocument());
    expect(screen.getByText(/lst_apple/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit download" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Submit download" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ job_id: expect.stringMatching(/^job_/) })));
    const create = server.requests.find((r) => r.path === "/security-master/v1/acquisitions")!;
    expect(JSON.parse(create.body)).toMatchObject({ token: expect.any(String), request: { dataset: "price_daily", identifiers: ["lst_apple"] } });
  });

  it("submit is disabled until a preflight succeeds, and a review-required error is shown", async () => {
    server = await startMockServer({ "/security-master/v1/acquisitions/preflight": { __status: 409, __body: load("error_review_required") } });
    render(<AcquisitionReviewDialog client={smClient(backend({ baseUrl: server.url }))} context={{ mode: "current_corrected" }} listingId="lst_apple" onClose={() => {}} onCreated={() => {}} />);
    expect(screen.getByRole("button", { name: "Submit download" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Preflight" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("ACQUISITION_REVIEW_REQUIRED"));
  });
});

describe("DownloadActivityDialog", () => {
  it("polls jobs, lists every stage, and cancels", async () => {
    const job = load("acquisition_job") as { job_id: string };
    server = await startMockServer({
      [`/security-master/v1/acquisitions/${job.job_id}`]: job,
      [`/security-master/v1/acquisitions/${job.job_id}/cancel`]: load("acquisition_cancel"),
    });
    render(<DownloadActivityDialog client={smClient(backend({ baseUrl: server.url }))} jobIds={[job.job_id]} onClose={() => {}} pollMs={50} />);
    await waitFor(() => expect(screen.getByText("queued")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Cancel job" }));
    await waitFor(() => expect(screen.getByText("cancel_pending")).toBeInTheDocument());
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm vitest run src/components/renderers/securityMaster/AcquisitionDialogs.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 3: Write `AcquisitionDialogs.tsx`**

```tsx
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react";
import { SecurityMasterError, type Job, type Preflight, type QueryContext, type SmClient } from "../../../lib/securityMaster";

const TERMINAL = new Set(["partial", "rate_limited", "entitlement_denied", "validation_failed", "cancelled", "failed", "gold_ready"]);

function isoDaysAgo(n: number): string {
  const d = new Date(Date.now() - n * 86_400_000);
  return d.toISOString().slice(0, 10);
}

function errorText(err: unknown): string {
  if (err instanceof SecurityMasterError) return `${err.code}: ${err.message}`;
  return err instanceof Error ? err.message : String(err);
}

export function AcquisitionReviewDialog({ client, context, listingId, onClose, onCreated }: {
  client: SmClient; context: QueryContext; listingId: string; onClose: () => void; onCreated: (job: Job) => void;
}) {
  const [start, setStart] = useState(isoDaysAgo(30));
  const [end, setEnd] = useState(isoDaysAgo(0));
  const [policy, setPolicy] = useState<"missing_only" | "refresh" | "force">("missing_only");
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const request = { dataset: "price_daily", identifiers: [listingId], start_date: start, end_date: end, policy, price_basis: "raw" };

  return (
    <div className="sm-dialog" role="dialog" aria-label="Acquisition review">
      <header className="sm-inspector-head"><span className="sm-eyebrow">Review price download</span>
        <button type="button" className="sm-icon" aria-label="Close" onClick={onClose}>×</button></header>
      <div className="sm-sql-bar">
        <label className="sm-field">Start<input type="date" aria-label="Start date" value={start} onChange={(e) => { setStart(e.target.value); setPreflight(null); }} /></label>
        <label className="sm-field">End<input type="date" aria-label="End date" value={end} onChange={(e) => { setEnd(e.target.value); setPreflight(null); }} /></label>
        <label className="sm-field">Policy
          <select aria-label="Policy" value={policy} onChange={(e) => { setPolicy(e.target.value as typeof policy); setPreflight(null); }}>
            <option value="missing_only">Missing only</option><option value="refresh">Refresh</option><option value="force">Force</option>
          </select>
        </label>
        <button type="button" className="sm-secondary" disabled={busy} onClick={() => {
          setBusy(true); setError(null);
          client.preflight(request, context).then(setPreflight, (e: unknown) => setError(errorText(e))).finally(() => setBusy(false));
        }}>Preflight</button>
      </div>
      {error && <p className="sm-error" role="alert">{error}</p>}
      {preflight && (
        <div className="sm-panel">
          <table className="sm-kv"><tbody>
            <tr><td>Identities</td><td>{preflight.identities.map((i) => `${String(i.identifier)} → ${String(i.listing_id)} (${String(i.provider_symbol)})`).join(", ")}</td></tr>
            <tr><td>Missing ranges</td><td>{preflight.missing_ranges.length ? preflight.missing_ranges.map((r) => `${r.start}…${r.end}`).join(", ") : "none"}</td></tr>
            <tr><td>Expected requests</td><td>{preflight.expected_requests}</td></tr>
            <tr><td>Policy</td><td>{preflight.policy}</td></tr>
          </tbody></table>
          {preflight.warnings.length > 0 && <ul className="sm-warnings">{preflight.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
          <p className="sm-muted">A provider response is Bronze evidence, not validated data. Silver and Gold states are reported separately by the job.</p>
        </div>
      )}
      <div className="sm-actions">
        <button type="button" className="sm-primary" disabled={!preflight || busy || preflight.expected_requests === 0} onClick={() => {
          if (!preflight) return;
          setBusy(true); setError(null);
          client.createJob(request, preflight.token).then((job) => { onCreated(job); onClose(); }, (e: unknown) => setError(errorText(e))).finally(() => setBusy(false));
        }}>Submit download</button>
      </div>
    </div>
  );
}

export function DownloadActivityDialog({ client, jobIds, onClose, pollMs = 2000 }: {
  client: SmClient; jobIds: string[]; onClose: () => void; pollMs?: number;
}) {
  const [jobs, setJobs] = useState<Record<string, Job | { error: string }>>({});
  useEffect(() => {
    let stopped = false;
    const tick = async () => {
      for (const id of jobIds) {
        const current = jobs[id];
        if (current && "state" in current && TERMINAL.has(current.state)) continue;
        try {
          const job = await client.job(id);
          if (!stopped) setJobs((j) => ({ ...j, [id]: job }));
        } catch (e) {
          if (!stopped) setJobs((j) => ({ ...j, [id]: { error: errorText(e) } }));
        }
      }
    };
    void tick();
    const timer = setInterval(() => void tick(), pollMs);
    return () => { stopped = true; clearInterval(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, jobIds.join(","), pollMs]);

  return (
    <div className="sm-dialog" role="dialog" aria-label="Download activity">
      <header className="sm-inspector-head"><span className="sm-eyebrow">Download activity</span>
        <button type="button" className="sm-icon" aria-label="Close" onClick={onClose}>×</button></header>
      {jobIds.length === 0 && <p className="empty-state">No downloads yet.</p>}
      {jobIds.map((id) => {
        const job = jobs[id];
        return (
          <section key={id} className="sm-job">
            <h4><code>{id}</code> {job && "state" in job ? <span className="sm-pill">{job.state}</span> : null}</h4>
            {job && "error" in job && <p className="sm-error" role="alert">{job.error}</p>}
            {job && "state" in job && (
              <>
                <ol className="sm-chain">{job.stage_history.map((s, i) => <li key={i}><span className="sm-pill">{s.stage}</span> <span className="sm-muted">{s.at}</span></li>)}</ol>
                {!TERMINAL.has(job.state) && (
                  <button type="button" className="sm-secondary" onClick={() => client.cancelJob(id).then((j) => setJobs((all) => ({ ...all, [id]: j })), (e: unknown) => setJobs((all) => ({ ...all, [id]: { error: errorText(e) } })))}>Cancel job</button>
                )}
              </>
            )}
          </section>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 4: Mount the dialogs in the renderer**

In `SecurityMasterBrowserRenderer.tsx`: add `jobIds?: string[]` to `SmSession`; render `<AcquisitionReviewDialog>` when `session.reviewListing` is set (`onClose` clears it via `onSessionChange({ reviewListing: undefined })`, `onCreated` appends the id to `jobIds`); add a header button `Download activity {n}` in `.sm-main` above the context bar that toggles a local `activityOpen` state rendering `<DownloadActivityDialog>`; both dialogs require `sm.client`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pnpm vitest run src/components/renderers && pnpm typecheck`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/components/renderers/securityMaster/AcquisitionDialogs.tsx src/components/renderers/securityMaster/AcquisitionDialogs.test.tsx src/components/renderers/SecurityMasterBrowserRenderer.tsx src/hooks/useSecurityMaster.ts
git commit -m "feat(security-master): acquisition review and download activity dialogs"
```

### Task 21: WidgetCard wiring, rendered-types list, CSS, help topic

**Files:**
- Modify: `src/components/WidgetCard.tsx` (early return for the type; `TYPE_LABELS`; `saveSecurityMaster`), `src/lib/widgetTypes.ts`, `src/lib/builtins.ts` (`fetchesData` excludes the type), `src/styles.css`, `src/help/index.ts` (`TOPIC_ORDER`), `src/help/index.test.ts` if it pins a count
- Create: `src/help/topics/security-master.md`
- Test: `src/components/WidgetCard.securityMaster.test.tsx`

- [ ] **Step 1: Write the failing card-level test**

```tsx
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: globalThis.fetch }));
vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl: vi.fn() }));
vi.mock("../lib/plotly", () => ({
  renderPlot: () => Promise.resolve(),
  purgePlot: () => {},
  REGISTERED_TRACE_TYPES: new Set(["scatter", "candlestick"]),
}));
vi.mock("react-grid-layout", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div data-testid="grid">{children}</div>,
  WidthProvider: (Comp: React.ComponentType) => Comp,
}));

import { __reset } from "../test/memfs";
import { __resetParamOptionsCacheForTests } from "../hooks/useParamOptions";
import { __resetJsonStoreForTests } from "../lib/jsonStore";
import { __resetLoggerForTests } from "../lib/logger";
import { fetchesData } from "../lib/builtins";
import type { Dashboard, DashboardCard, WidgetDef } from "../lib/types";
import { RENDERED_TYPES } from "../lib/widgetTypes";
import { useBackendsStore } from "../stores/backendsStore";
import { useDashboardStore } from "../stores/dashboardStore";
import { useWidgetRegistryStore } from "../stores/widgetRegistryStore";
import { backend, card as makeCard, widget as makeWidget } from "../test/fixtures";
import { startMockServer, type MockServer } from "../test/mockServer";
import { DashboardGrid } from "./DashboardGrid";

const DIR = join(__dirname, "../test/contract/security-master");
const load = (n: string) => JSON.parse(readFileSync(join(DIR, `${n}.json`), "utf8")) as unknown;

/** The same end-to-end shape as WidgetCard.deltaExplorer.test.tsx: a real card, the real hook, a real server. */
const browser: WidgetDef = makeWidget({
  id: "security_master_browser",
  name: "Security Master Browser",
  type: "security_master_browser",
  endpoint: "security-master/v1/catalog",
  params: [
    { paramName: "listing_id", label: "Listing", type: "text" },
    { paramName: "effective_date", label: "Effective date", type: "date" },
    { paramName: "known_at", label: "Known at", type: "text" },
    { paramName: "layer", label: "Layer", type: "text" },
    { paramName: "temporal_mode", label: "Temporal mode", type: "text" },
  ],
});

const servers: MockServer[] = [];

async function serve() {
  const s = await startMockServer({
    "/security-master/v1/catalog": load("catalog"),
    "/security-master/v1/odp/models": load("odp_models"),
    "/security-master/v1/relations/gold/security_master": load("relation"),
    "/security-master/v1/preview": load("preview"),
  });
  servers.push(s);
  useBackendsStore.setState({ backends: [backend({ baseUrl: s.url })], statuses: { b1: "online" } });
  return s;
}

function mount(card: DashboardCard) {
  useWidgetRegistryStore.setState({ widgets: [browser] });
  const dashboard: Dashboard = { id: "d1", name: "D", tabs: [{ id: "t1", name: "Main", cards: [card] }], groups: [] };
  useDashboardStore.setState({ dashboards: [dashboard], activeId: "d1", activeTabId: "t1", previousId: null });
  return render(<DashboardGrid onOpenLibrary={() => {}} />);
}

const findCard = (id: string) =>
  useDashboardStore.getState().dashboards[0]!.tabs[0]!.cards.find((c) => c.id === id);

beforeEach(() => {
  __reset();
  __resetJsonStoreForTests();
  __resetLoggerForTests();
  __resetParamOptionsCacheForTests();
});

afterEach(async () => {
  await Promise.all(servers.splice(0).map((s) => s.close()));
});

describe("a security_master_browser card, end to end", () => {
  it("is a rendered type that fetches nothing through useWidgetData", () => {
    expect(RENDERED_TYPES.has("security_master_browser")).toBe(true);
    expect(fetchesData(browser)).toBe(false);
  });

  it("renders the browser, persists session state on the card, and publishes listing_id through the card's params", async () => {
    const s = await serve();
    mount(makeCard({
      id: "c1", widgetId: browser.id, backendId: "b1",
      securityMaster: { catalogMode: "tables", selection: { kind: "relation", layer: "gold", name: "security_master" }, tab: "data" },
    }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Security master" })).toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    await userEvent.click(screen.getAllByRole("row")[1]!);
    await waitFor(() => expect(findCard("c1")?.params.listing_id).toMatch(/^lst_/));
    await userEvent.click(screen.getByRole("tab", { name: "Schema" }));
    await waitFor(() => expect(findCard("c1")?.securityMaster).toMatchObject({ tab: "schema" }));
    expect(JSON.stringify(findCard("c1")?.securityMaster)).not.toContain("qry_");
    expect(s.requests.every((r) => r.path.startsWith("/security-master/v1/"))).toBe(true);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pnpm vitest run src/components/WidgetCard.securityMaster.test.tsx`
Expected: FAIL (type not rendered; `RENDERED_TYPES` lacks it).

- [ ] **Step 3: Wire WidgetCard**

In `WidgetCard.tsx`:
- `TYPE_LABELS`: add `security_master_browser: "Browser"`.
- Next to `saveTable`, add `saveSecurityMaster(patch)` that merges `patch` into `card.securityMaster` through the same store call `saveTable` uses for `card.table` (dropping keys whose value is `undefined`).
- Add an early return immediately after the `live_grid` block, in the same shape:

```tsx
    // Like live_grid: this renderer issues its own requests (fetchesData is
    // false), so the `data === null` gate below must never claim it.
    if (widget?.type === "security_master_browser") {
      if (status === "unresolved") return <p className="empty-state">Waiting for widget…</p>;
      return (
        <SecurityMasterBrowserRenderer
          backend={backend}
          session={card.securityMaster as Partial<SmSession> | undefined}
          onSessionChange={(patch) => void saveSecurityMaster(patch)}
          params={params}
          onParamChange={(edits) => void apply(splitParamEdit(edits, card, groups, widget, groupsDisabled))}
          refreshKey={refreshKey}
        />
      );
    }
```

`apply`, `splitParamEdit`, `groups`, `groupsDisabled` and `refreshKey` are the names the `handleAsOfChange` and `multi_file_viewer` paths already use in this file; reuse them, do not add a second path.

In `src/lib/builtins.ts` add `widget.type !== "security_master_browser" &&` to `fetchesData` with a one-line comment (its catalog call is the renderer's own). In `src/lib/widgetTypes.ts` add `"security_master_browser"` to `RENDERED_TYPES`.

- [ ] **Step 4: CSS**

Append to `src/styles.css`, using only existing tokens and the font-size scale:

```css
/* Security Master Browser: catalog | main | inspector. Every font-size is a
   token so the Font size setting reaches it. */
.sm-browser { display: grid; grid-template-columns: 220px minmax(0, 1fr) 260px; height: 100%; min-height: 0; font-size: var(--font-size-13); }
.sm-browser:has(.sm-inspector:empty), .sm-browser.sm-no-inspector { grid-template-columns: 220px minmax(0, 1fr); }
.sm-catalog, .sm-inspector { display: flex; flex-direction: column; gap: 0.5rem; padding: 0.6rem; overflow: auto; min-height: 0; }
.sm-catalog { border-right: 1px solid var(--border); }
.sm-inspector { border-left: 1px solid var(--border); }
.sm-main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.sm-catalog-head, .sm-inspector-head { display: flex; justify-content: space-between; align-items: baseline; }
.sm-eyebrow { font-size: var(--font-size-10); letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-muted); }
.sm-muted { color: var(--text-muted); }
.sm-error { color: var(--error); }
.sm-segment { display: flex; border: 1px solid var(--border); border-radius: 4px; overflow: hidden; }
.sm-segment-btn { flex: 1; padding: 0.3rem; background: transparent; border: 0; color: var(--text); cursor: pointer; font-size: var(--font-size-12); }
.sm-segment-btn[aria-selected="true"] { background: var(--bg-panel); font-weight: 600; }
.sm-search { width: 100%; box-sizing: border-box; padding: 0.3rem 0.5rem; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); color: var(--text); font-size: var(--font-size-12); }
.sm-tree { display: flex; flex-direction: column; gap: 0.6rem; }
.sm-tree-title { margin: 0 0 0.2rem; font-size: var(--font-size-12); display: flex; gap: 0.4rem; align-items: center; }
.sm-tree-item { display: flex; flex-direction: column; align-items: flex-start; width: 100%; padding: 0.3rem 0.5rem; border: 0; border-radius: 4px; background: transparent; color: var(--text); text-align: left; cursor: pointer; font-size: var(--font-size-12); }
.sm-tree-item[aria-current="true"] { background: var(--bg-panel); outline: 1px solid var(--accent); }
.sm-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; background: var(--accent); }
.sm-dot-silver { background: var(--text-muted); } .sm-dot-bronze { background: var(--error); }
.sm-context { display: flex; flex-wrap: wrap; gap: 0.6rem; align-items: center; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--border); font-size: var(--font-size-12); }
.sm-context-summary { color: var(--text-muted); } .sm-context-lookup { margin-left: auto; }
.sm-field { display: inline-flex; gap: 0.3rem; align-items: center; }
.sm-field input, .sm-field select, .sm-sql-bar select { font-size: var(--font-size-12); background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: 4px; padding: 0.15rem 0.3rem; }
.sm-chip, .sm-pill { display: inline-block; padding: 0.05rem 0.4rem; border: 1px solid var(--border); border-radius: 999px; font-size: var(--font-size-10); margin-right: 0.2rem; }
.sm-notice { margin: 0; padding: 0.3rem 0.6rem; background: var(--bg-panel); border-bottom: 1px solid var(--border); font-size: var(--font-size-12); }
.sm-workbench { display: flex; flex-direction: column; flex: 1; min-height: 0; overflow: auto; padding: 0.6rem; gap: 0.5rem; }
.sm-crumbs { font-family: monospace; font-size: var(--font-size-11); color: var(--text-muted); }
.sm-workbench-head { display: flex; gap: 0.6rem; align-items: baseline; } .sm-workbench-head h3 { margin: 0; font-size: var(--font-size-16); }
.sm-tabs { display: flex; gap: 0.2rem; border-bottom: 1px solid var(--border); }
.sm-tab { background: transparent; border: 0; border-bottom: 2px solid transparent; padding: 0.3rem 0.6rem; color: var(--text-muted); cursor: pointer; font-size: var(--font-size-12); }
.sm-tab[aria-selected="true"] { color: var(--text); border-bottom-color: var(--accent); }
.sm-panel { display: flex; flex-direction: column; gap: 0.6rem; }
.sm-two { display: grid; grid-template-columns: 1fr 1fr; gap: 0.6rem; }
.sm-box, .sm-callout { display: flex; flex-direction: column; gap: 0.2rem; padding: 0.5rem 0.7rem; border: 1px solid var(--border); border-radius: 6px; }
.sm-box-active { border-color: var(--accent); } .sm-callout { border-left: 3px solid var(--accent); }
.sm-kv { width: 100%; border-collapse: collapse; font-size: var(--font-size-12); } .sm-kv td, .sm-kv th { padding: 0.25rem 0.4rem; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; } .sm-right { text-align: right; }
.sm-code, .sm-sql { font-family: monospace; font-size: var(--font-size-12); background: var(--bg-panel); border: 1px solid var(--border); border-radius: 4px; padding: 0.5rem; margin: 0; white-space: pre-wrap; color: var(--text); width: 100%; box-sizing: border-box; }
.sm-route code { font-size: var(--font-size-12); }
.sm-sql-bar, .sm-result-strip, .sm-actions { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
.sm-primary, .sm-secondary, .sm-icon { font-size: var(--font-size-12); border-radius: 4px; padding: 0.3rem 0.7rem; cursor: pointer; border: 1px solid var(--border); background: var(--bg-panel); color: var(--text); }
.sm-primary { background: var(--accent); border-color: var(--accent); color: var(--bg); } .sm-primary:disabled, .sm-secondary:disabled { opacity: 0.5; cursor: default; }
.sm-icon { padding: 0 0.4rem; }
.sm-table { flex: 1; min-height: 0; overflow: auto; } .sm-table tr[data-index] { cursor: pointer; }
.sm-receipt { font-family: monospace; font-size: var(--font-size-11); color: var(--text-muted); }
.sm-status { display: flex; justify-content: space-between; padding: 0.3rem 0.6rem; border-top: 1px solid var(--border); font-size: var(--font-size-11); }
.sm-chain, .sm-versions { margin: 0; padding-left: 1.2rem; font-size: var(--font-size-12); }
.sm-timeline { display: flex; flex-direction: column; gap: 0.4rem; } .sm-timeline-row { display: flex; flex-direction: column; gap: 0.2rem; } .sm-timeline-stage { display: flex; gap: 0.5rem; align-items: baseline; }
.sm-dialog { position: absolute; inset: 2rem; z-index: 5; overflow: auto; padding: 0.8rem; background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; display: flex; flex-direction: column; gap: 0.6rem; }
.sm-warnings { color: var(--error); margin: 0; padding-left: 1.2rem; }
.sm-visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
/* Landscape tablet: the inspector stacks under the workbench. */
@media (max-width: 1100px) {
  .sm-browser { grid-template-columns: 200px minmax(0, 1fr); grid-template-rows: minmax(0, 1fr) auto; }
  .sm-inspector { grid-column: 2; border-left: 0; border-top: 1px solid var(--border); max-height: 40%; }
}
```

The renderer's root gets `className={"sm-browser" + (inspectorVisible ? "" : " sm-no-inspector")}` so the grid collapses without an inspector. Remove the `:has(...)` selector if `src/lib/themeFlash.test.ts` or the build rejects it; the class covers the case.

- [ ] **Step 5: Help topic**

Create `src/help/topics/security-master.md` (title `# Security Master Browser`, four short sections: what the catalog shows, the temporal modes in one sentence each, what a receipt is and why results never change under your feet, and how acquisition is reviewed). Add `"security-master"` to `TOPIC_ORDER` after `"tables"`. Run `pnpm vitest run src/help` and fix any pinned count.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pnpm vitest run src/components/WidgetCard.securityMaster.test.tsx src/help src/lib/themeFlash.test.ts && pnpm typecheck && pnpm test:run`
Expected: PASS, full unit suite green.

- [ ] **Step 7: Commit**

```bash
git add src/components/WidgetCard.tsx src/components/WidgetCard.securityMaster.test.tsx src/lib/widgetTypes.ts src/lib/builtins.ts src/styles.css src/help
git commit -m "feat(security-master): wire the renderer into WidgetCard with persisted session state, styles and help"
```

### Task 22: Browser fixtures and the Playwright spec

**Files:**
- Modify: `scripts/browser-fixtures.mjs` (widgets.json entry + `/security-master/v1/*` routes from the contract fixtures)
- Create: `e2e/security-master.spec.ts`

- [ ] **Step 1: Add the fixture routes**

In `scripts/browser-fixtures.mjs`, import the contract files with `readFileSync` (the file already imports it) from `src/test/contract/security-master/`, add the `security_master_browser` widget entry (copy `security-master-api/widgets.json`'s entry) to the `/widgets.json` object, and add routes:

```js
  "/security-master/v1/catalog": contract("catalog"),
  "/security-master/v1/odp/models": contract("odp_models"),
  "/security-master/v1/odp/models/EquityInfo": modelDetail("EquityInfo", "shares_outstanding"),
  "/security-master/v1/odp/models/EquitySearch": modelDetail("EquitySearch", "ticker_change"),
  "/security-master/v1/odp/models/EquityHistorical": modelDetail("EquityHistorical", "trade_correction"),
  "/security-master/v1/odp/models/ReferenceSecurity": modelDetail("ReferenceSecurity", "cusip_change"),
  "/security-master/v1/odp/models/ReferenceResolve": modelDetail("ReferenceResolve", "ticker_change"),
  "/security-master/v1/odp/models/MarketCalendar": contract("odp_model_market_calendar"),
  "/security-master/v1/relations/gold/market_calendar": contract("relation"),
  "/security-master/v1/relations/gold/security_master": contract("relation"),
  "/security-master/v1/relations/bronze/source_captures": { ...contract("relation"), relation: { ...contract("relation").relation, layer: "bronze", name: "source_captures", modes: ["captured_by", "delta_snapshot"] } },
  "/security-master/v1/preview": (url) => url.searchParams.get("mode") === "known_at" ? { __status: 422, __body: contract("error_mode_unsupported") } : contract("preview"),
  "/security-master/v1/compare": contract("compare"),
  "/security-master/v1/sql/plan": contract("sql_plan"),
  "/security-master/v1/sql/execute": contract("sql_execute"),
  "/security-master/v1/lineage": contract("lineage"),
  "/security-master/v1/relations/gold/security_master/versions": contract("versions"),
  "/security-master/v1/acquisitions/preflight": contract("acquisition_preflight"),
  "/security-master/v1/acquisitions": contract("acquisition_job"),
```

with helpers `const contract = (n) => JSON.parse(readFileSync(new URL(`../src/test/contract/security-master/${n}.json`, import.meta.url), "utf8"));` and `const modelDetail = (id, scenario) => ({ model: contract("odp_models").models.find((m) => m.model_id === id), scenario: { scenario, fixture_id: scenario.replaceAll("_", "-"), expectations: [{ context: { mode: "known_at", known_at: "2026-01-01T00:00:00Z" } }, { context: { mode: "known_at", known_at: "2026-02-01T00:00:00Z" } }], captures: [] } })`. Because mockServer routes by pathname only, the preview route cannot see the POST body; the e2e "unsupported mode" check instead selects the Bronze relation, whose fixture detail declares only `captured_by`/`delta_snapshot`, and asserts the mode select disables `Known at`.

- [ ] **Step 2: Write the spec**

```ts
// Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
// SPDX-License-Identifier: Apache-2.0

import { expect, test, type Page } from "@playwright/test";
import { DEFAULT_CARDS, seedCards } from "./fixtures";

const CARD = { id: "c-sm", widgetId: "security_master_browser", backendId: "b1", params: {}, layout: { x: 0, y: 12, w: 40, h: 24 } };

async function open(page: Page, securityMaster: Record<string, unknown> = {}) {
  await seedCards(page, [...DEFAULT_CARDS, { ...CARD, securityMaster }]);
  return page.locator("article", { hasText: "Security Master Browser" });
}

test("ODP Models is the initial catalog view and each model opens its temporal example", async ({ page }) => {
  const card = await open(page);
  await expect(card.getByRole("tab", { name: "ODP Models" })).toHaveAttribute("aria-selected", "true");
  const expected: Array<[string, string]> = [
    ["Equity Info", "Shares outstanding"], ["Equity Search", "Ticker change"], ["Equity Historical", "Trade correction"],
    ["Reference Security", "CUSIP change"], ["Identifier Resolve", "Ticker change"], ["Market Calendar", "Lunar holiday correction"],
  ];
  for (const [model, scenario] of expected) {
    await card.getByRole("button", { name: new RegExp(model.replace("Identifier Resolve", "Reference Resolve")) }).click();
    await card.getByRole("tab", { name: "Temporal example" }).click();
    await expect(card.getByText(scenario, { exact: true }).first()).toBeVisible();
  }
});

test("the MarketCalendar model exposes the calendar fields and the two-stage pending state", async ({ page }) => {
  const card = await open(page, { selection: { kind: "model", modelId: "MarketCalendar" }, tab: "fields" });
  for (const field of ["exchange_id", "session_date", "holiday_family", "calendar_system", "authority_type", "evidence_status", "market_effect", "source_version", "known_from"]) {
    await expect(card.getByText(field, { exact: true })).toBeVisible();
  }
  await card.getByRole("tab", { name: "Temporal example" }).click();
  await expect(card.getByText("T1 · Authority confirmed").first()).toBeVisible();
  await expect(card.getByText(/Confirmed by religious authority · market pending resolution/)).toBeVisible();
  await expect(card.getByText(/Confirmed by government · market pending/)).toBeVisible();
});

test("moving between models preserves the Temporal example tab, and Open scenario reaches the comparison", async ({ page }) => {
  const card = await open(page, { selection: { kind: "model", modelId: "EquityInfo" }, tab: "example" });
  await card.getByRole("button", { name: /Equity Historical/ }).click();
  await expect(card.getByRole("tab", { name: "Temporal example" })).toHaveAttribute("aria-selected", "true");
  await card.getByRole("button", { name: /Open Trade correction comparison/ }).click();
  await expect(card.getByRole("tab", { name: "Temporal compare" })).toHaveAttribute("aria-selected", "true");
  await expect(card.getByRole("tab", { name: "Tables" })).toHaveAttribute("aria-selected", "true");
});

test("table preview, schema, SQL, versions and lineage share one visible context, and changing it never relabels an old result", async ({ page }) => {
  const requests: string[] = [];
  page.on("request", (r) => requests.push(new URL(r.url()).pathname));
  const card = await open(page, { catalogMode: "tables", selection: { kind: "relation", layer: "gold", name: "security_master" }, tab: "data" });
  await expect(card.getByRole("table")).toBeVisible();
  const receipt = await card.getByText(/Receipt qry_/).textContent();
  await card.getByRole("combobox", { name: "Temporal mode" }).selectOption("effective_on");
  await card.getByLabel("Effective date").fill("2022-06-07");
  await expect(card.getByText(/Effective on 2022-06-07/)).toBeVisible();
  await expect(card.getByText(/Receipt qry_/)).not.toHaveText(receipt ?? "__");
  for (const tab of ["Schema", "SQL query", "Versions", "Lineage"]) {
    await card.getByRole("tab", { name: tab }).click();
    await expect(card.getByText(/Effective on 2022-06-07/)).toBeVisible();
  }
  expect(requests.filter((p) => p.startsWith("/api/") || p.includes("eodhd"))).toEqual([]);
});

test("an unsupported mode is disabled for a Bronze relation rather than silently substituted", async ({ page }) => {
  const card = await open(page, { catalogMode: "tables", selection: { kind: "relation", layer: "bronze", name: "source_captures" }, tab: "data" });
  const mode = card.getByRole("combobox", { name: "Temporal mode" });
  await expect(mode.getByRole("option", { name: "Known at" })).toBeDisabled();
  await expect(mode.getByRole("option", { name: "Captured by" })).toBeEnabled();
});

test("acquisition requires a preflight before submit", async ({ page }) => {
  const card = await open(page, { catalogMode: "tables", selection: { kind: "relation", layer: "gold", name: "security_master" }, tab: "data" });
  await card.getByRole("row").nth(1).click();
  await card.getByRole("button", { name: /Review price download/ }).click();
  const dialog = card.getByRole("dialog", { name: "Acquisition review" });
  await expect(dialog.getByRole("button", { name: "Submit download" })).toBeDisabled();
  await dialog.getByRole("button", { name: "Preflight" }).click();
  await expect(dialog.getByText(/Expected requests/)).toBeVisible();
  await dialog.getByRole("button", { name: "Submit download" }).click();
  await card.getByRole("button", { name: /Download activity/ }).click();
  await expect(card.getByRole("dialog", { name: "Download activity" }).getByText("queued")).toBeVisible();
});

test("landscape tablet stacks the inspector under the workbench", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 });
  const card = await open(page, { selection: { kind: "model", modelId: "MarketCalendar" } });
  const inspector = card.getByRole("complementary", { name: "ODP model" });
  const wb = card.locator(".sm-main");
  const [a, b] = await Promise.all([inspector.boundingBox(), wb.boundingBox()]);
  expect(a && b && a.y >= b.y + b.height - 1).toBe(true);
});
```

- [ ] **Step 3: Run the spec**

Run: `pnpm exec playwright install --with-deps chromium` (once) then `pnpm e2e -- e2e/security-master.spec.ts`, then the full `pnpm e2e`.
Expected: PASS. If the tablet assertion is off by the card's own header, compare against the workbench's bottom edge as written and adjust the media query width, not the test.

- [ ] **Step 4: Commit**

```bash
git add scripts/browser-fixtures.mjs e2e/security-master.spec.ts
git commit -m "test(security-master): browser fixtures and the end-to-end spec"
```

### Task 23: Gates, verification record, review, and the deployment procedure

**Files:**
- Create: openbb-docker `docs/superpowers/verification/2026-09-17-security-master-browser-execution.md`; bdobb-v2 the same path
- Modify: this plan's checkboxes as tasks complete

- [ ] **Step 1: openbb-docker gates**

From the openbb-docker worktree root:

```bash
(cd security-master-api && uv run --extra dev pytest -q && uv run --extra dev ruff check .)
(cd openbb-security-master && pip install -e ../openbb-deltalake -e ../openbb-eodhd -e .[dev] -q && pytest -q && ruff check .)
python -m pytest -q tests/test_compose_contract.py tests/test_api_app_auth.py
bash scripts/scrub-check.sh && bash scripts/check-serve-config.sh
docker build -f security-master-api/Dockerfile -t openbb-security-master:11.4.0 .
docker build -t openbb-local:ci .
```

Record every command and its result (counts, pass/fail) in the verification file. Then push the branch and open a PR against `release/v11.2.x` with the summary structure of PR #52 (Summary, Scope, Verification, Deployment boundary); hosted CI must be green before merge.

- [ ] **Step 2: bdobb-v2 gates**

From the bdobb-v2 worktree root:

```bash
pnpm install --frozen-lockfile
pnpm typecheck
pnpm test:run
pnpm build && git diff --exit-code
bash scripts/scrub-check.sh
pnpm e2e
pnpm reference-backend & sleep 60; pnpm test:reference
```

Record results. Push and open a PR against `release/v11` with PR #123's structure; note explicitly that Vercel is untouched and that the legacy `bdobb-v2-recut` preview is assessed in its own context.

- [ ] **Step 3: Code review**

Invoke `superpowers:requesting-code-review` on each branch's whole diff (spec, plan and verification file as inputs). Address Critical and Important findings with focused commits and re-run the affected gates; record the outcome in the verification file.

- [ ] **Step 4: Deployment procedure (documented here; executed only on the user's explicit go, after 4 PM ET)**

```bash
# on the Mac, from the merged release/v11.2.x checkout
docker build --platform linux/amd64 -f security-master-api/Dockerfile -t openbb-security-master:11.4.0 .
docker build --platform linux/amd64 --build-arg OPENBB_VERSION=4.7.2 -t openbb-local:11.4.0 .
docker save openbb-security-master:11.4.0 | gzip -1 | ssh nas 'gunzip | /share/ZFS530_DATA/.qpkg/container-station/bin/docker load'
docker save openbb-local:11.4.0 | gzip -1 | ssh nas 'gunzip | /share/ZFS530_DATA/.qpkg/container-station/bin/docker load'
# on the NAS, in /share/Container/openbb
cp docker-compose.yml docker-compose.yml.pre-security-master
# copy the two new service blocks and the openbb-api SECURITY_MASTER_URL line into docker-compose.yml;
# write security-master.env from security-master.env.example with a real secret;
# add the Serve routes to ts-config/serve.json and serve-funnel.json
docker compose up -d --no-deps security-master-api security-master-worker
docker compose run --rm security-master-api python -m security_master_api.store    # seed once
docker compose up -d --no-deps openbb-api tailscale                                 # the router extension and the Serve routes
curl -u "$USER:$PASS" https://openbb-security-master.<tailnet>.ts.net/security-master/v1/health
```

Rollback: `cp docker-compose.yml.pre-security-master docker-compose.yml && docker compose up -d --no-deps openbb-api tailscale && docker compose rm -sf security-master-api security-master-worker`.

- [ ] **Step 5: Commit the verification records**

```bash
git add docs/superpowers/verification/2026-09-17-security-master-browser-execution.md
git commit -m "docs(verification): security master browser execution record"
```

(in each repository.)

## Self-review notes

- **Spec coverage.** §1 → Tasks 1, 13, 14, 23. §2 → Tasks 2, 3, 4 (external libraries). §3 → Tasks 4, 6, 7, 8 (receipts). §4 → Tasks 5, 9, 10. §5 → Task 14. §6 → Tasks 11, 12. §7 → Tasks 15–21. §8 → every task's tests, Task 10 contract recording, Task 22 e2e, Task 23 gates. Out-of-scope items are not planned.
- **Interfaces.** `QuerySession.run(sql, params: list | dict | None, timeout_ms=None, allow_explain=False)`; `open_session` returns an entered session; `Executions.start(ctx, relations, sql, params, kind, request_id, principal, page_size=None, timeout_ms=None)`; `SmSession` optional fields `compareRight`, `lineageAssertion`, `reviewListing`, `jobIds`; `RelationWorkbench` receives `sqlPolicy`. Task 4's manifest note about external relations is resolved by Task 5's `physical_path`.
- **Known judgement calls** (state them in the PR): Gold relations are DuckDB views, never materialized; job state is two Delta tables read through DuckDB, claimed by a conditional-put append; the SQL first page is served from a fully materialized bounded result (max 10,000 rows) rather than a streaming cursor; SSE exists on the service while the app polls every 2 s; the India and Dubai observation timeline in the renderer reads the bundled TypeScript fixtures until the service serves them.

# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""security-master-api: the browser's service. Every read path is provider-silent."""

from __future__ import annotations

import functools
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import anyio
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

import security_master_api
from security_master_api.acquire.preflight import preflight, verify_token
from security_master_api.app.auth import BasicAuthMiddleware, credential_of
from security_master_api.app.errors import install, request_id
from security_master_api.config import Settings, settings_from_env
from security_master_api.errors import DomainError
from security_master_api.resolver import catalog as cat
from security_master_api.resolver.compare import compare as run_compare
from security_master_api.resolver.context import Context, iso_utc, parse_context
from security_master_api.resolver.identity import resolve as run_resolve
from security_master_api.resolver.lineage import lineage as run_lineage
from security_master_api.resolver.manifest import resolve_manifest
from security_master_api.resolver.odp import model as odp_model
from security_master_api.resolver.odp import odp_version, project, registry
from security_master_api.sql.executions import Executions, decode_cursor, encode_cursor, fingerprint
from security_master_api.sql.preview import build_preview_sql
from security_master_api.sql.session import open_session
from security_master_api.store.jobs import (
    TERMINAL,
    append_event,
    by_fingerprint,
    create_job,
    events,
    job,
)
from security_master_api.store.receipts import new_receipt, record_receipt
from security_master_api.store.seed import golden_fixtures
from security_master_api.store.tables import history, latest_version, open_table

# widgets.json and apps.json are PACKAGE DATA, not repository files: a non-editable install
# copies the package and nothing above it, so a path out of the package resolves into
# site-packages and the routes 500.
HERE = Path(security_master_api.__file__).resolve().parent
V1 = "/security-master/v1"
# What /resolve may be asked to do when the store has no match: answer from the store alone,
# or offer the bounded lookup for review. Anything else is a typo, not a policy.
LOOKUP_POLICIES = ("cache_only", "review")


class Page(BaseModel):
    # A page of zero or fewer rows is not a smaller request, it is one the pager cannot serve:
    # `start()` reads `page_size or first_page`, so 0 silently became the default and a
    # negative limit reached `min()` as a ceiling below every row.
    limit: int | None = Field(default=None, ge=1)
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

    @field_validator("budgets")
    @classmethod
    def _budgets_are_positive(cls, value: dict[str, int]) -> dict[str, int]:
        # `max_rows=0` falls back to the default page instead of returning nothing, and a
        # negative `timeout_ms` is a deadline already past. Both are rejected rather than
        # reinterpreted.
        for key in ("max_rows", "timeout_ms"):
            if key in value and value[key] < 1:
                raise ValueError(f"{key} must be at least 1")
        return value


class OdpQueryBody(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class ResolveBody(BaseModel):
    identifier: str
    identifier_type: str | None = None
    include_historical: bool = True
    context: dict[str, Any] = Field(default_factory=dict)
    lookup_policy: str | None = None


class PreflightBody(BaseModel):
    request: dict[str, Any]
    context: dict[str, Any] = Field(default_factory=dict)


class CreateJobBody(BaseModel):
    request: dict[str, Any]
    token: str


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
    """Who the per-principal query budget counts against.

    A DIGEST of the credential, never the credential itself: this string reaches the client
    inside the QUERY_BUDGET_EXCEEDED message, and a slice of the Basic token would be a
    credential leaking through an error body and into every log that keeps one.
    """
    credential = credential_of(request.scope)
    return hashlib.sha256(credential.encode()).hexdigest()[:16] if credential else "anonymous"


def _scenario_for(model_id: str) -> dict | None:
    """The golden fixture that demonstrates a model's primary temporal scenario.

    Fixture ids are the scenario in kebab-case; MarketCalendar's `lunar_holiday_correction` is
    the one scenario with no fixture of its own name - the Eid/lunar correction fixture is it.
    """
    scenario = odp_model(model_id)["primary_temporal_scenario"]
    wanted = "eid-lunar-correction" if scenario == "lunar_holiday_correction" \
        else scenario.replace("_", "-")
    for fixture in golden_fixtures():
        if fixture["fixture_id"] == wanted:
            return {"scenario": scenario, "fixture_id": fixture["fixture_id"],
                    "expectations": fixture["expectations"],
                    "captures": [c["capture_id"] for c in fixture["captures"]]}
    return None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or settings_from_env()
    app = FastAPI(title="security-master-api", version="0.2.0")
    # ORDER MATTERS, and add_middleware inserts at index 0, where index 0 is OUTERMOST -- so
    # these three lines read backwards: auth first makes it INNERMOST, and the stack ends up
    # [stamp, CORS, auth]. Auth must be inside CORS because a browser preflight carries no
    # credentials by definition: outside, every cross-origin caller -- OpenBB Workspace
    # included -- would get a bare 401 with no Access-Control-Allow-* headers. curl never sends
    # a preflight, so no amount of curl testing catches it; tests/test_app_auth.py does.
    app.add_middleware(BasicAuthMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"], expose_headers=["x-request-id"])
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

    def _columns_of(relation: str, ctx: Context) -> set[str]:
        layer, name = relation.split(".", 1)
        with open_session(settings, ctx, [relation]) as s:
            return {c["name"] for c in s.describe(f'SELECT * FROM {layer}."{name}"')}

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
        # `bronze.<library>.<symbol>` carries a dot in its own name, so the path segment is
        # greedy - which is also how `/versions` arrives here rather than at its own route.
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
        # An external Delta table opens exactly like a declared one; only its path differs.
        table = open_table(settings, full)
        out["latest_version"] = table.version()
        dataset = table.to_pyarrow_dataset()
        out["schema"] = [{"name": f.name, "dtype": str(f.type)} for f in dataset.schema]
        out["count"] = {"value": dataset.count_rows(), "quality": "exact"}
        return out

    def get_versions(layer: str, name: str) -> dict:
        full = f"{layer}.{name}"
        if cat.relation(settings, full).kind == "view":
            raise DomainError("QUERY_REJECTED", "views have no versions; see their dependencies",
                              {"relation": full})
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
        ctx = parse_context(body.context)
        sql, params = build_preview_sql(body.relation, body.columns, body.filters, body.sort)
        if body.page.cursor:
            # The cursor is bound to the context it was issued under: re-stamp it with the
            # fingerprint THIS request implies and let the execution refuse a mismatch. A page
            # two of a different question is a new question.
            execution_id, offset, _ = decode_cursor(body.page.cursor)
            return executions.page(
                encode_cursor(execution_id, offset, fingerprint(ctx, sql, params)),
                _principal(request))
        return executions.start(ctx, [body.relation], sql, params, "preview", request_id(request),
                                _principal(request), page_size=body.page.limit,
                                index=cat.catalog_index(settings))

    def _sql_allowed() -> None:
        if settings.sql_policy == "disabled":
            raise DomainError("QUERY_REJECTED", "SQL is disabled by policy", {"policy": "sql"})

    def _relations_in(ctx: Context, index: dict[str, cat.Relation]) -> list[str]:
        # Register every DECLARED relation eligible for the mode; the policy then rejects any
        # BASE_TABLE outside that set. Views are cheap until scanned. External Delta tables are
        # excluded deliberately: there can be thousands of `bronze.<library>.<symbol>`, each one
        # a table open, and nothing registers them into a session for free. A caller who wants
        # one names it through /preview.
        #
        # The index is BUILT BY THE CALLER and handed down to the session too: building it
        # lists the object store once, and a request that built its own here and let
        # `resolve_manifest` build a second one paid for that listing twice.
        return [r.full for r in index.values()
                if r.kind != "external_delta" and ctx.mode in r.modes and r.layer != "ops"]

    @app.post(f"{V1}/sql/plan")
    def sql_plan(body: SqlBody) -> dict:
        _sql_allowed()
        ctx = parse_context(body.context)
        index = cat.catalog_index(settings)
        return executions.plan(ctx, _relations_in(ctx, index), body.sql, index=index)

    @app.post(f"{V1}/sql/execute")
    def sql_execute(body: SqlBody, request: Request) -> dict:
        _sql_allowed()
        ctx = parse_context(body.context)
        # `max_rows` bounds one response, never widens it: the service's own first page is the
        # ceiling, and the whole result is still capped by settings.max_rows inside start().
        budget = body.budgets.get("max_rows")
        page_size = min(budget, settings.first_page) if budget else None
        index = cat.catalog_index(settings)
        return executions.start(ctx, _relations_in(ctx, index), body.sql, [], "sql",
                                request_id(request), _principal(request), page_size=page_size,
                                timeout_ms=body.budgets.get("timeout_ms"), index=index)

    @app.get(f"{V1}/sql/executions/{{execution_id}}/pages")
    def sql_pages(execution_id: str, cursor: str, request: Request) -> dict:
        page = executions.page(cursor, _principal(request))
        if page["execution_id"] != execution_id:
            raise DomainError("QUERY_REJECTED", "cursor belongs to another execution")
        return page

    @app.delete(f"{V1}/sql/executions/{{execution_id}}", status_code=204)
    def sql_cancel(execution_id: str, request: Request) -> Response:
        if not executions.cancel(execution_id, _principal(request)):
            raise DomainError("NO_MATCHING_FACTS", "unknown execution",
                              {"execution_id": execution_id})
        return Response(status_code=204)

    @app.post(f"{V1}/resolve")
    def resolve_route(body: ResolveBody, request: Request) -> dict:
        if body.lookup_policy is not None and body.lookup_policy not in LOOKUP_POLICIES:
            raise DomainError("QUERY_REJECTED", f"unknown lookup_policy {body.lookup_policy!r}",
                              {"supported": list(LOOKUP_POLICIES)})
        ctx = parse_context(body.context)
        try:
            out = run_resolve(settings, ctx, body.identifier, body.identifier_type,
                              body.include_historical)
        except DomainError as exc:
            # A read-through lookup is never performed on the caller's behalf here: the answer
            # is the BOUNDED request a person can approve, priced and signed, so that nothing
            # reaches a provider because a typo happened to miss the store.
            if (exc.code != "IDENTITY_UNRESOLVED" or body.lookup_policy != "review"
                    or settings.acquisition_policy == "disabled"):
                raise
            raise DomainError(
                "ACQUISITION_REVIEW_REQUIRED",
                "no local match; a bounded lookup is available for review",
                {"preflight": preflight(settings, ctx, {"dataset": "security_lookup",
                                                        "identifiers": [],
                                                        "query": body.identifier})}) from exc
        receipt = new_receipt(settings, "resolve", ctx, out.pop("manifest"), request_id(request))
        record_receipt(settings, receipt)
        return {**out, "receipt": receipt}

    @app.post(f"{V1}/lineage")
    def lineage_route(body: LineageBody, request: Request) -> dict:
        ctx = parse_context(body.context)
        out = run_lineage(settings, ctx, body.relation, body.assertion_id)
        # The chain was read under a snapshot, so the receipt records a snapshot: a receipt
        # that named the caller's knowledge time would not describe what was actually read.
        snap = parse_context({"mode": "delta_snapshot", "versions": ctx.versions})
        manifest = resolve_manifest(settings, snap, [body.relation, "bronze.source_captures"])
        receipt = new_receipt(settings, "lineage", snap, manifest, request_id(request))
        record_receipt(settings, receipt)
        return {**out, "receipt": receipt}

    @app.post(f"{V1}/compare")
    def compare_route(body: CompareBody, request: Request) -> dict:
        left, right = parse_context(body.left), parse_context(body.right)
        # The selection's KEYS are wire input and reach SQL as identifiers. Check them against
        # the relation before comparing anything.
        unknown = sorted(set(body.selection) - _columns_of(body.relation, left))
        if unknown:
            raise DomainError("QUERY_REJECTED", f"{body.relation} has no column(s) {unknown}",
                              {"relation": body.relation, "columns": unknown})
        out = run_compare(settings, body.relation, body.selection, left, right)
        lr = new_receipt(settings, "compare", left, out.pop("left_manifest"), request_id(request))
        rr = new_receipt(settings, "compare", right, out.pop("right_manifest"),
                         request_id(request))
        record_receipt(settings, lr)
        record_receipt(settings, rr)
        return {**out, "left_receipt": lr, "right_receipt": rr}

    @app.post(f"{V1}/versions/compare")
    def versions_compare(body: VersionsCompareBody) -> dict:
        cat.relation(settings, body.relation)
        left = open_table(settings, body.relation, body.left).to_pyarrow_dataset()
        right = open_table(settings, body.relation, body.right).to_pyarrow_dataset()
        ls = {f.name: str(f.type) for f in left.schema}
        rs = {f.name: str(f.type) for f in right.schema}
        out = {
            "relation": body.relation, "left": body.left, "right": body.right,
            "schema": {"added": sorted(set(rs) - set(ls)), "removed": sorted(set(ls) - set(rs)),
                       "changed": sorted(k for k in ls.keys() & rs.keys() if ls[k] != rs[k])},
            "rows": {"left": left.count_rows(), "right": right.count_rows()},
        }
        if body.row_diff:
            lt = left.to_table().to_pylist()
            rt = right.to_table().to_pylist()
            out["row_diff"] = {"added": [{k: iso_utc(v) for k, v in row.items()}
                                         for row in rt[len(lt):][: settings.first_page]]}
        return out

    @app.post(f"{V1}/acquisitions/preflight")
    def acquisitions_preflight(body: PreflightBody) -> dict:
        return preflight(settings, parse_context(body.context), body.request)

    @app.post(f"{V1}/acquisitions", status_code=201)
    def acquisitions_create(body: CreateJobBody, response: Response) -> dict:
        if settings.acquisition_policy == "disabled":
            raise DomainError("ACQUISITION_REVIEW_REQUIRED", "acquisition is disabled by policy",
                              {"policy": "disabled"})
        claims = verify_token(settings, body.token, body.request)
        if claims["dataset"] == "security_lookup":
            # security_lookup exists so a caller can PREVIEW a bounded provider lookup before
            # deciding anything; running it as a job would spend the one call it costs and
            # still have nothing durable to promote - normalize() has no writer for it.
            raise DomainError("QUERY_REJECTED",
                              "security_lookup is preflight-only in this release",
                              {"dataset": "security_lookup"})
        existing = by_fingerprint(settings, claims["fingerprint"])
        if existing is not None:
            # Not a new job and not an error: the same request arriving twice gets the job it
            # already has, and 200 rather than 201 says nothing was created.
            response.status_code = 200
            return existing
        token_hash = hashlib.sha256(body.token.encode()).hexdigest()[:16]
        return create_job(settings, claims["dataset"], body.request, claims["fingerprint"],
                          token_hash)

    @app.get(f"{V1}/acquisitions/{{job_id}}")
    def acquisitions_get(job_id: str) -> dict:
        return job(settings, job_id)

    @app.post(f"{V1}/acquisitions/{{job_id}}/cancel")
    def acquisitions_cancel(job_id: str) -> dict:
        current = job(settings, job_id)
        if current["state"] in TERMINAL:
            raise DomainError("QUERY_REJECTED", f"job is already {current['state']}",
                              {"state": current["state"]})
        # `cancel_pending`, never `cancelled`: the API cannot stop work already in flight. The
        # worker sees this stage and decides what its own stage allows.
        append_event(settings, job_id, "cancel_pending", {"requested_by": "api"})
        return job(settings, job_id)

    @app.get(f"{V1}/acquisitions/{{job_id}}/events")
    async def acquisitions_events(job_id: str, replay: str = "0") -> StreamingResponse:
        # The path MUST end in /events: that suffix is what lets auth accept the credential
        # from the query string, and an EventSource cannot send a header.
        #
        # ASYNC on purpose. A sync generator runs on a threadpool worker and holds it for the
        # whole stream - up to 300 s of mostly sleeping - so a handful of open streams would
        # starve every other request of threads. The blocking reads go to the pool one at a
        # time; the sleeping happens on the event loop, which costs nothing.
        await run_in_threadpool(job, settings, job_id)
        # `replay=1` starts from the first event, so a client that connects late still sees
        # the whole history; without it the stream carries only what happens from now on.
        # Either way a terminal stage ends the stream - there is nothing further to send.
        emitted: set[str] = set()
        if replay != "1":
            evs = await run_in_threadpool(events, settings, job_id)
            emitted = {e["event_id"] for e in evs}

        async def stream():
            # Sent events are tracked by ID, never by position: a late-arriving event can sort
            # BEFORE one already sent (same seq, earlier clock), and an index would then skip
            # it and re-send its neighbour.
            deadline = time.monotonic() + 300
            while True:
                evs = await run_in_threadpool(events, settings, job_id)
                for event in evs:
                    if event["event_id"] not in emitted:
                        emitted.add(event["event_id"])
                        yield f"event: {event['stage']}\ndata: {json.dumps(event)}\n\n"
                if (evs and evs[-1]["stage"] in TERMINAL) or time.monotonic() >= deadline:
                    return
                await anyio.sleep(2)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get(f"{V1}/exchanges")
    def exchanges() -> dict:
        ctx = parse_context(None)
        with open_session(settings, ctx, ["silver.exchanges"]) as s:
            rows = s.run("SELECT exchange_id, calendar_id, mic, name, timezone, calendar_alias, "
                         "eodhd_code, effective_from, effective_to FROM silver.exchanges "
                         "ORDER BY name").to_pylist()
        return {"exchanges": [{k: iso_utc(v) for k, v in r.items()} for r in rows]}

    return app


@functools.cache
def _app() -> FastAPI:
    return create_app()


def __getattr__(name: str):
    """`security_master_api.app.main:app` builds the app on first access, not on import.

    Module-level construction would read the environment when a test merely imports
    `create_app`, and settings_from_env raises without the service's own variables set.
    Cached, because `__getattr__` runs on EVERY attribute access: without it each mention of
    `main.app` would build a second application with its own Executions and its own budget.
    """
    if name == "app":
        return _app()
    raise AttributeError(name)

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""security-master-api: the browser's service. Every read path is provider-silent."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware import Middleware

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
from security_master_api.store.receipts import new_receipt, record_receipt
from security_master_api.store.seed import golden_fixtures
from security_master_api.store.tables import history, latest_version, open_table

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
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"], expose_headers=["x-request-id"])
    install(app)
    # APPEND, do not add_middleware. Starlette's add_middleware inserts at index 0 and index 0
    # is OUTERMOST, so calling it here would put auth outside CORS. A browser preflight carries
    # no credentials by definition, so it would get a bare 401 with no Access-Control-Allow-*
    # headers and every cross-origin caller -- OpenBB Workspace included -- would be locked out.
    # curl never sends a preflight, so no amount of curl testing catches it. Appending makes
    # auth INNERMOST, which still covers every path: middleware wraps the whole app.
    app.user_middleware.append(Middleware(BasicAuthMiddleware))
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
        ctx = parse_context(body.context)
        sql, params = build_preview_sql(body.relation, body.columns, body.filters, body.sort)
        if body.page.cursor:
            # The cursor is bound to the context it was issued under: re-stamp it with the
            # fingerprint THIS request implies and let the execution refuse a mismatch. A page
            # two of a different question is a new question.
            execution_id, offset, _ = decode_cursor(body.page.cursor)
            return executions.page(encode_cursor(execution_id, offset,
                                                 fingerprint(ctx, sql, params)))
        return executions.start(ctx, [body.relation], sql, params, "preview", request_id(request),
                                _principal(request), page_size=body.page.limit)

    def _sql_allowed() -> None:
        if settings.sql_policy == "disabled":
            raise DomainError("QUERY_REJECTED", "SQL is disabled by policy", {"policy": "sql"})

    def _relations_in(ctx: Context) -> list[str]:
        # Register every declared relation eligible for the mode; the policy then
        # rejects any BASE_TABLE outside that set. Views are cheap until scanned.
        return [r.full for r in cat.catalog(settings) if ctx.mode in r.modes and r.layer != "ops"]

    @app.post(f"{V1}/sql/plan")
    def sql_plan(body: SqlBody) -> dict:
        _sql_allowed()
        ctx = parse_context(body.context)
        return executions.plan(ctx, _relations_in(ctx), body.sql)

    @app.post(f"{V1}/sql/execute")
    def sql_execute(body: SqlBody, request: Request) -> dict:
        _sql_allowed()
        ctx = parse_context(body.context)
        # `max_rows` bounds one response, never widens it: the service's own first page is the
        # ceiling, and the whole result is still capped by settings.max_rows inside start().
        budget = body.budgets.get("max_rows")
        page_size = min(budget, settings.first_page) if budget else None
        return executions.start(ctx, _relations_in(ctx), body.sql, [], "sql",
                                request_id(request), _principal(request), page_size=page_size,
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
            raise DomainError("NO_MATCHING_FACTS", "unknown execution",
                              {"execution_id": execution_id})
        return Response(status_code=204)

    @app.post(f"{V1}/resolve")
    def resolve_route(body: ResolveBody, request: Request) -> dict:
        ctx = parse_context(body.context)
        out = run_resolve(settings, ctx, body.identifier, body.identifier_type,
                          body.include_historical)
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

    @app.get(f"{V1}/exchanges")
    def exchanges() -> dict:
        ctx = parse_context(None)
        with open_session(settings, ctx, ["silver.exchanges"]) as s:
            rows = s.run("SELECT exchange_id, calendar_id, mic, name, timezone, calendar_alias, "
                         "eodhd_code, effective_from, effective_to FROM silver.exchanges "
                         "ORDER BY name").to_pylist()
        return {"exchanges": [{k: iso_utc(v) for k, v in r.items()} for r in rows]}

    return app


def __getattr__(name: str):
    """`security_master_api.app.main:app` builds the app on first access, not on import.

    Module-level construction would read the environment when a test merely imports
    `create_app`, and settings_from_env raises without the service's own variables set.
    """
    if name == "app":
        return create_app()
    raise AttributeError(name)

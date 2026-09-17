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
from security_master_api.resolver.context import Context, iso_utc
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
            if isinstance(value, bool):
                return value
            raise ValueError(value)
        return str(value)
    except ValueError as exc:
        raise DomainError("QUERY_REJECTED", f"parameter {param['name']} is not a {kind}",
                          {"parameter": param["name"]}) from exc


def _bind(m: dict, params: dict) -> dict:
    """Every declared parameter, coerced. Unknown or missing ones fail before DuckDB sees them."""
    bound = {}
    declared = {p["name"]: p for p in m["parameters"]}
    unknown = set(params) - set(declared)
    if unknown:
        raise DomainError("QUERY_REJECTED", f"unknown parameter(s): {sorted(unknown)}")
    for name, p in declared.items():
        if name in params and params[name] is not None:
            bound[name] = _coerce(p, params[name])
        elif p.get("required"):
            raise DomainError("QUERY_REJECTED", f"parameter {name} is required",
                              {"parameter": name})
        else:
            bound[name] = False if p["type"] == "boolean" else None
    return bound


def _calendar_id(settings: Settings, ctx: Context, bound: dict) -> str:
    """The one calendar the request names. Never guesses between two."""
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
        where = " OR ".join(clauses)
        rows = s.run(f"SELECT DISTINCT calendar_id FROM silver.exchanges WHERE {where}",
                     values).to_pylist()
    if len(rows) != 1:
        found = f"{len(rows)} calendars" if rows else "no calendar"
        raise DomainError("IDENTITY_AMBIGUOUS" if rows else "IDENTITY_UNRESOLVED",
                          f"exchange resolves to {found}",
                          {"candidates": [r["calendar_id"] for r in rows]})
    return rows[0]["calendar_id"]


def _identity_binds(settings: Settings, ctx: Context, m: dict, bound: dict) -> dict:
    """Stable ids for the identifier the model resolves on.

    Both `listing_id` and `security_id` are bound because a security the store holds may carry
    no listing at all (a reorganization seen only in `silver.securities`); the model's SQL picks
    whichever of the two it can filter on, and the unavailable one binds as NULL.

    Under `known_at`, an identifier the store had not yet heard of is not an unresolvable
    identifier - it is the absence of local evidence at that cutoff, which is what the caller
    asked about, so the failure is reported in those terms.
    """
    try:
        candidates = resolve(settings, ctx, bound[m["resolve"]])["candidates"]
    except DomainError as exc:
        if exc.code == "IDENTITY_UNRESOLVED" and ctx.mode == "known_at":
            raise DomainError("NO_LOCAL_EVIDENCE",
                              f"{bound[m['resolve']]} was not locally known at the cutoff",
                              exc.details) from exc
        raise
    return {"listing_id": candidates[0]["listing_id"],
            "security_id": candidates[0]["security_id"]}


def project(settings: Settings, ctx: Context, model_id: str, params: dict,
            request_id: str) -> dict:
    m = model(model_id)
    if ctx.mode not in m["temporal_capabilities"]:
        raise DomainError("TEMPORAL_MODE_UNSUPPORTED", f"{ctx.mode} is not supported by {model_id}",
                          {"model_id": model_id, "supported_modes": m["temporal_capabilities"]})
    bound = _bind(m, params)
    fingerprint = _fp(model_id, bound)
    if m.get("resolver"):
        out = resolve(settings, ctx, bound["identifier"], bound.get("identifier_type"))
        receipt = new_receipt(settings, "odp", ctx, out["manifest"], request_id, fingerprint)
        record_receipt(settings, receipt)
        cols = [{"name": f["name"], "dtype": f["type"]} for f in m["fields"]]
        return {"results": out["candidates"], "columns": cols, "receipt": receipt}
    sql = m["sql"]
    relation = m["backing_relation"]
    check_modes(settings, ctx, [relation])
    binds = dict(bound)
    calendar = model_id == "MarketCalendar"
    if m.get("resolve"):
        binds |= _identity_binds(settings, ctx, m, bound)
    if calendar:
        binds["calendar_id"] = _calendar_id(settings, ctx, bound)
    wanted = [relation] + (["silver.session_interruptions"] if calendar else [])
    with open_session(settings, ctx, wanted) as s:
        table = s.run(sql, _named(sql, binds))
        rows = table.to_pylist()
        columns = [{"name": f.name, "dtype": str(f.type)} for f in table.schema]
        if calendar:
            _decorate_calendar(s, rows, binds["calendar_id"],
                               bool(bound.get("include_interruptions")))
            columns.append({"name": "interruptions", "dtype": "array"})
        manifest = s.manifest
    receipt = new_receipt(settings, "odp", ctx, manifest, request_id, fingerprint)
    record_receipt(settings, receipt)
    if not rows and ctx.mode == "known_at":
        raise DomainError("NO_LOCAL_EVIDENCE",
                          f"{model_id} has no locally known facts at the cutoff",
                          {"model_id": model_id, "receipt": receipt})
    return {"results": [{k: iso_utc(v) for k, v in r.items()} for r in rows], "columns": columns,
            "receipt": receipt}


def _decorate_calendar(session, rows: list[dict], calendar_id: str, include: bool) -> None:
    """Give every session row its interruptions list and parsed branch JSON.

    `interruptions` is always a list so the shape does not change with the flag: asking for
    them and there being none reads the same as not asking, which is the honest answer either
    way - the rows that have them are the only ones that differ.
    """
    found = session.run(
        "SELECT session_date, interruption_start, interruption_end "
        "FROM silver.session_interruptions WHERE calendar_id = ? AND system_to IS NULL",
        [calendar_id]).to_pylist() if include else []
    by_date: dict[str, list] = {}
    for i in found:
        by_date.setdefault(str(i["session_date"]), []).append(
            {"start": iso_utc(i["interruption_start"]), "end": iso_utc(i["interruption_end"])})
    for r in rows:
        r["interruptions"] = by_date.get(str(r["session_date"]), [])
        for k in ("candidate_branches", "selected_branch"):
            if isinstance(r.get(k), str):
                r[k] = json.loads(r[k])


def _named(sql: str, binds: dict) -> dict:
    """DuckDB binds `$name` placeholders from a dict; date values pass as ISO strings."""
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in binds.items()
            if f"${k}" in sql}


def _fp(model_id: str, bound: dict) -> str:
    return hashlib.sha256(json.dumps({"model": model_id, "params": bound}, sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The ODP model registry and its projections. One resolver behind the browser and obb.*."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime, time
from functools import cache
from pathlib import Path

import security_master_api
from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.catalog import check_modes
from security_master_api.resolver.context import Context, iso_utc, parse_context
from security_master_api.resolver.identity import resolve
from security_master_api.sql.session import QuerySession, open_session
from security_master_api.store.receipts import new_receipt, record_receipt
from security_master_api.temporal_fixture import _instant

CALENDAR_PROVIDER = "local_security_master"
SESSION_LABELS = ("session_date", "trade_date")

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
        if kind == "datetime":
            return _instant(str(value))
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


def _calendar_options(bound: dict) -> None:
    """Refuse the calendar options this release cannot honour, rather than ignoring them."""
    provider = bound.get("provider")
    if provider not in (None, CALENDAR_PROVIDER):
        raise DomainError("QUERY_REJECTED", f"provider {provider!r} is not served locally",
                          {"parameter": "provider", "supported": [CALENDAR_PROVIDER]})
    if bound.get("timezone") not in (None, "UTC"):
        raise DomainError("QUERY_REJECTED",
                          "output timezone conversion is not available in this release",
                          {"parameter": "timezone"})
    if bound.get("session_label") not in (None, *SESSION_LABELS):
        raise DomainError("QUERY_REJECTED",
                          f"session_label must be one of {list(SESSION_LABELS)}",
                          {"parameter": "session_label"})


def _calendar_context(ctx: Context, bound: dict) -> Context:
    """`as_of` and `knowledge_at` are this model's own spelling of a temporal context.

    They are honoured when the request carried no context of its own, and refused - never
    quietly reinterpreted - when it carried one that says something else. `knowledge_at`
    answers "what did we know then", which needs an effective date to ask it about: `as_of`
    when given, otherwise the first day the request asks for.
    """
    as_of, knowledge_at = bound.get("as_of"), bound.get("knowledge_at")
    if as_of is None and knowledge_at is None:
        return ctx
    effective = datetime.combine(as_of or bound["start_date"], time.min, tzinfo=UTC)
    if ctx.mode != "current_corrected" or ctx.effective_at or ctx.known_at:
        if (as_of is not None and ctx.effective_at != effective) or (
                knowledge_at is not None and ctx.known_at != knowledge_at):
            raise DomainError("QUERY_REJECTED",
                              "as_of/knowledge_at disagree with the request's temporal context",
                              {"parameters": ["as_of", "knowledge_at"], "context": ctx.as_dict()})
        return ctx
    if knowledge_at is not None:
        return replace(ctx, mode="known_at", effective_at=effective, known_at=knowledge_at)
    return replace(ctx, mode="effective_on", effective_at=effective)


def _identity_binds(settings: Settings, ctx: Context, m: dict,
                    bound: dict) -> tuple[dict, dict[str, int]]:
    """Stable ids for the identifier the model resolves on, and what resolving them read.

    Both `listing_id` and `security_id` are bound because a security the store holds may carry
    no listing at all (a reorganization seen only in `silver.securities`); the model's SQL picks
    whichever of the two it can filter on, and the unavailable one binds as NULL.

    The manifest comes back with them: the identity relations are as much a dependency of the
    answer as the model's own backing relation, and the receipt has to name them.
    """
    identifier = bound[m["resolve"]]
    try:
        out = resolve(settings, ctx, identifier)
    except DomainError as exc:
        raise _unresolved(settings, identifier, ctx, exc) from exc
    candidates = out["candidates"]
    stable = {(c["listing_id"], c["security_id"]) for c in candidates}
    if len(stable) > 1:
        # `resolve` allows several candidates when an `effective_at` might have told them
        # apart; binding one of them here would be a guess, so the request is refused instead.
        raise DomainError("IDENTITY_AMBIGUOUS",
                          f"{identifier} matches {len(stable)} securities under this context",
                          {"identifier": identifier, "candidates": candidates})
    return ({"listing_id": candidates[0]["listing_id"],
             "security_id": candidates[0]["security_id"]}, out["manifest"])


def _unresolved(settings: Settings, identifier: str, ctx: Context,
                exc: DomainError) -> DomainError:
    """Which refusal an unresolvable identifier earns under `known_at`.

    An identifier the store knows today but had not heard of at the cutoff is the absence of
    local evidence, which is exactly what the caller asked about. One the store does not know
    at all is an unresolved identifier whatever the cutoff, and saying otherwise would tell a
    caller who fat-fingered a ticker that their clock was the problem.
    """
    if exc.code != "IDENTITY_UNRESOLVED" or ctx.mode != "known_at":
        return exc
    try:
        resolve(settings, parse_context(None), identifier)
    except DomainError:
        return exc
    return DomainError("NO_LOCAL_EVIDENCE", f"{identifier} was not locally known at the cutoff",
                       exc.details)


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
    calendar = model_id == "MarketCalendar"
    if calendar:
        _calendar_options(bound)
        ctx = _calendar_context(ctx, bound)
    check_modes(settings, ctx, [relation])
    binds, identity_manifest = dict(bound), {}
    if m.get("resolve"):
        identity, identity_manifest = _identity_binds(settings, ctx, m, bound)
        binds |= identity
    if calendar:
        binds["calendar_id"] = _calendar_id(settings, ctx, bound)
    wanted = [relation] + (["silver.session_interruptions"] if calendar else [])
    with open_session(settings, ctx, wanted) as s:
        table = s.run(sql, _named(sql, binds))
        rows = table.to_pylist()
        columns = [{"name": f.name, "dtype": str(f.type)} for f in table.schema]
        if calendar:
            _decorate_calendar(s, rows, binds["calendar_id"], bound)
            columns.append({"name": "interruptions", "dtype": "array"})
        empty = not rows and ctx.mode == "known_at" and not _any_evidence(s, m, model_id, binds)
        manifest = s.manifest | identity_manifest
    receipt = new_receipt(settings, "odp", ctx, manifest, request_id, fingerprint)
    record_receipt(settings, receipt)
    if empty:
        raise DomainError("NO_LOCAL_EVIDENCE",
                          f"{model_id} has no locally known facts at the cutoff",
                          {"model_id": model_id, "receipt": receipt})
    return {"results": [{k: iso_utc(v) for k, v in r.items()} for r in rows], "columns": columns,
            "receipt": receipt}


def _any_evidence(session: QuerySession, m: dict, model_id: str, binds: dict) -> bool:
    """Does the backing relation hold anything at all for this identity under the context?

    An empty `known_at` result means one of two different things: the store knew nothing about
    this identity yet, or it knew plenty and the request's own filters excluded all of it.
    Only the first is an absence of local evidence; the second is an honest empty list. The
    probe drops the user's filters and keeps the identity, which is the line between them.
    A model that filters on no identity at all has nothing to probe, so it never 404s.
    """
    if model_id == "MarketCalendar":
        key = "calendar_id"
    elif m.get("resolve"):
        key = "listing_id"
    else:
        return True
    sql = f"SELECT count(*) AS n FROM {m['backing_relation']} WHERE {key} = ${key}"
    return session.run(sql, _named(sql, binds)).to_pylist()[0]["n"] > 0


def _decorate_calendar(session: QuerySession, rows: list[dict], calendar_id: str,
                       bound: dict) -> None:
    """Apply the projection-side calendar options and parse the branch JSON.

    `interruptions` is always a list so the shape does not change with the flag: asking for
    them and there being none reads the same as not asking, which is the honest answer either
    way - the rows that have them are the only ones that differ. The relabel runs last, so
    interruptions are still matched on the session's own date.
    """
    found = session.run(
        "SELECT session_date, interruption_start, interruption_end "
        "FROM silver.session_interruptions WHERE calendar_id = ? AND system_to IS NULL",
        [calendar_id]).to_pylist() if bound.get("include_interruptions") else []
    by_date: dict[str, list] = {}
    for i in found:
        by_date.setdefault(str(i["session_date"]), []).append(
            {"start": iso_utc(i["interruption_start"]), "end": iso_utc(i["interruption_end"])})
    for r in rows:
        r["interruptions"] = by_date.get(str(r["session_date"]), [])
        if not bound.get("include_breaks"):
            r["break_start"] = r["break_end"] = None
        for k in ("candidate_branches", "selected_branch"):
            if isinstance(r.get(k), str):
                r[k] = _branch(r, k)
        if bound.get("session_label") == "trade_date":
            r["session_date"] = r["trade_date"]


def _branch(row: dict, column: str):
    """A stored branch column that is not JSON is a promotion that should never have landed."""
    try:
        return json.loads(row[column])
    except json.JSONDecodeError as exc:
        raise DomainError("PROMOTION_VALIDATION_FAILED", f"{column} is not valid JSON",
                          {"column": column, "calendar_id": row.get("calendar_id"),
                           "session_date": str(row.get("session_date"))}) from exc


def _named(sql: str, binds: dict) -> dict:
    """DuckDB binds `$name` placeholders from a dict; date values pass as ISO strings."""
    used = set(re.findall(r"\$(\w+)", sql))
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in binds.items()
            if k in used}


def _fp(model_id: str, bound: dict) -> str:
    return hashlib.sha256(json.dumps({"model": model_id, "params": bound}, sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]

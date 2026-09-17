# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The same selection under two contexts, diffed field by field and classified."""

from __future__ import annotations

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.catalog import relation as catalog_relation
from security_master_api.resolver.context import Context, iso_utc
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


def _classify(field: str, left: dict | None, right: dict | None, lv, rv) -> str:
    if left is None and right is not None:
        return "newly_known" if rv is not None else "unresolved"
    if left is not None and right is None:
        return "expired"
    if field in _IDENTITY_FIELDS and lv is not None and rv is not None and lv != rv:
        return "identity_successor"
    if lv is None and rv is not None:
        return "newly_known"
    if lv is not None and rv is None:
        return "expired"
    left, right = left or {}, right or {}
    if right.get("supersedes_assertion_id") and \
            right["supersedes_assertion_id"] == left.get("assertion_id"):
        return "corrected"
    if left.get("assertion_id") != right.get("assertion_id"):
        return "corrected" if right.get("available_at") != left.get("available_at") \
            else "superseded"
    return "became_effective"


def compare(settings: Settings, relation: str, selection: dict, left: Context,
            right: Context) -> dict:
    rel = catalog_relation(settings, relation)
    keys = list(rel.keys)
    lrows = {tuple(str(r.get(k)) for k in keys): r
             for r in _select(settings, relation, selection, left)}
    rrows = {tuple(str(r.get(k)) for k in keys): r
             for r in _select(settings, relation, selection, right)}
    differences = []
    for key in sorted(set(lrows) | set(rrows)):
        lrow, rrow = lrows.get(key), rrows.get(key)
        for field in sorted((set(lrow or {}) | set(rrow or {})) - _PROVENANCE):
            lv, rv = (lrow or {}).get(field), (rrow or {}).get(field)
            if lv == rv:
                continue
            differences.append({
                "key": dict(zip(keys, key, strict=True)), "field": field,
                "left": iso_utc(lv), "right": iso_utc(rv),
                "classification": _classify(field, lrow, rrow, lv, rv),
            })
    return {
        "differences": differences,
        "left_manifest": resolve_manifest(settings, left, [relation]),
        "right_manifest": resolve_manifest(settings, right, [relation]),
    }

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
_ORDER_TIEBREAK = (("effective_from", "DESC NULLS LAST"), ("system_from", "DESC"),
                   ("assertion_id", "DESC"))
_PROVENANCE = {"assertion_id", "capture_id", "available_at", "system_from", "known_from",
               "known_to", "holiday_assertion_id", "market_assertion_id",
               "supersedes_assertion_id"}


def _select(settings: Settings, relation: str, selection: dict, ctx: Context,
            keys: list[str]) -> dict[tuple, dict]:
    """One row per declared key, chosen deterministically.

    An interval relation answers with several rows per key whenever the context does not pin
    an effective instant, and `ORDER BY 1` leaves which one lands last to DuckDB: the same
    comparison could report six differences or none from one run to the next. The order below
    is total over the columns the view actually exposes - a Gold view without `effective_from`
    or `system_from` simply orders by what it has - and the first row per key is the one that
    speaks for it.
    """
    if not selection:
        raise DomainError("QUERY_REJECTED", "compare needs a non-empty selection")
    layer, name = relation.split(".", 1)
    where = " AND ".join(f'"{k}" = ?' for k in selection)
    with open_session(settings, ctx, [relation]) as s:
        have = {c["name"] for c in s.describe(f'SELECT * FROM {layer}."{name}"')}
        order = [f'"{k}"' for k in keys if k in have]
        order += [f'"{c}" {d}' for c, d in _ORDER_TIEBREAK if c in have]
        rows = s.run(f'SELECT * FROM {layer}."{name}" WHERE {where} '
                     f"ORDER BY {', '.join(order) or '1'}",
                     list(selection.values())).to_pylist()
    out: dict[tuple, dict] = {}
    for row in rows:
        out.setdefault(tuple(str(row.get(k)) for k in keys), row)
    return out


def _effective_time_only(left: Context, right: Context) -> bool:
    """Do the two contexts differ only in effective time?

    Then a field that changed did so because a new effective interval took over, not because
    anyone corrected a value or learned a new one - the knowledge-time classes would be a lie.
    """
    if left.mode != right.mode or left.known_at != right.known_at:
        return False
    return left.mode == "effective_on" or left.effective_at != right.effective_at


def _classify(field: str, left: dict | None, right: dict | None, lv, rv,
              left_ctx: Context, right_ctx: Context) -> str:
    # A stable id that changed is a reorganization whichever axis moved.
    if field in _IDENTITY_FIELDS and lv is not None and rv is not None and lv != rv:
        return "identity_successor"
    if _effective_time_only(left_ctx, right_ctx):
        return "expired" if right is None or rv is None else "became_effective"
    if left is None and right is not None:
        return "newly_known" if rv is not None else "unresolved"
    if left is not None and right is None:
        return "expired"
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
    lrows = _select(settings, relation, selection, left, keys)
    rrows = _select(settings, relation, selection, right, keys)
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
                "classification": _classify(field, lrow, rrow, lv, rv, left, right),
            })
    return {
        "differences": differences,
        "left_manifest": resolve_manifest(settings, left, [relation]),
        "right_manifest": resolve_manifest(settings, right, [relation]),
    }

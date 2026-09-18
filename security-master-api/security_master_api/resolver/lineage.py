# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""From an assertion to its capture, request and run: the provenance chain."""

from __future__ import annotations

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import Context, iso_utc, parse_context
from security_master_api.sql.session import open_session


def _plain(row: dict, kind: str) -> dict:
    return {"kind": kind, **{k: iso_utc(v) for k, v in row.items()}}


def lineage(settings: Settings, ctx: Context, relation: str, assertion_id: str) -> dict:
    if not relation.startswith("silver."):
        raise DomainError("QUERY_REJECTED", "lineage starts from a Silver assertion",
                          {"relation": relation})
    # A snapshot context, not the caller's: close rows and superseded assertions are exactly
    # what a provenance chain is for, and every knowledge-time filter would hide them.
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
            for cap in s.run("SELECT * FROM bronze.source_captures WHERE capture_id = ?",
                             [capture_id]).to_pylist():
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

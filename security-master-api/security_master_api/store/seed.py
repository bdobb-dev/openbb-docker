# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Seed the golden and historical fixtures into Delta. Idempotent by assertion_id."""

from __future__ import annotations

import json
from pathlib import Path

import security_master_api
from security_master_api.config import Settings
from security_master_api.store.schemas import RELATIONS
from security_master_api.store.tables import append, ensure, latest_version, open_table
from security_master_api.temporal_fixture import _instant, load_temporal_fixture

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


def _existing_rows(settings: Settings, relation: str) -> set[tuple[str, str]]:
    """The (assertion_id, system_from) keys already stored. A close row repeats the
    assertion_id of the row it closes, so the instant is part of the identity."""
    if latest_version(settings, relation) is None:
        return set()
    table = open_table(settings, relation).to_pyarrow_table(
        columns=["assertion_id", "system_from"])
    return {
        (assertion_id, system_from.isoformat())
        for assertion_id, system_from in zip(
            table.column("assertion_id").to_pylist(),
            table.column("system_from").to_pylist(),
            strict=True,
        )
        if assertion_id is not None and system_from is not None
    }


def seed(settings: Settings) -> dict[str, int]:
    for relation in RELATIONS:
        ensure(settings, relation)
    captures = set()
    if latest_version(settings, "bronze.source_captures") is not None:
        table = open_table(settings, "bronze.source_captures").to_pyarrow_table(
            columns=["capture_id"])
        captures = {v for v in table.column("capture_id").to_pylist() if v is not None}
    for fixture in golden_fixtures():
        new_caps = [c for c in fixture["captures"] if c["capture_id"] not in captures]
        if new_caps:
            append(settings, "bronze.source_captures", new_caps)
            captures |= {c["capture_id"] for c in new_caps}
        for relation, rows in fixture["assertions"].items():
            existing = _existing_rows(settings, relation)
            fresh = [
                r for r in rows
                if (r["assertion_id"], _instant(r["system_from"]).isoformat()) not in existing
            ]
            if fresh:
                append(settings, relation, fresh)
    return {relation: latest_version(settings, relation) or 0 for relation in RELATIONS}

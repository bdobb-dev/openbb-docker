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

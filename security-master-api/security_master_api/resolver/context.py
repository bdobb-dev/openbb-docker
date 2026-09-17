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

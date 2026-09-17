# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Service settings from the environment. Nothing here reads a file."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from openbb_deltalake.utils import s3_options_from_env

_POLICIES = ("enabled", "review", "disabled")


@dataclass(frozen=True)
class Settings:
    root: str
    storage_options: dict[str, str] = field(default_factory=dict)
    delta_base: str | None = None
    external_libraries: tuple[str, ...] = ("openbb", "ticks", "ticks_live")
    first_page: int = 100
    max_rows: int = 10_000
    timeout_ms: int = 15_000
    per_principal_queries: int = 8
    scan_slots: int = 4
    memory_limit: str = "1GB"
    threads: int = 2
    sql_policy: str = "enabled"
    acquisition_policy: str = "review"
    openbb_url: str = "http://openbb-api:6900"
    openbb_username: str = ""
    openbb_password: str = ""
    preflight_secret: str = ""
    preflight_ttl_s: int = 900
    code_version: str = "security-master-api/0.2.0"
    projection_version: str = "security-master-projection/1"


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = str(env.get(key, "")).strip()
    return int(raw) if raw else default


def _policy(env: Mapping[str, str], key: str, default: str) -> str:
    raw = str(env.get(key, "")).strip().lower() or default
    if raw not in _POLICIES:
        raise ValueError(f"{key} must be one of {_POLICIES}, got {raw!r}")
    return raw


def settings_from_env(env: Mapping[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    secret = str(e.get("SECURITY_MASTER_PREFLIGHT_SECRET", "")).strip()
    if not secret:
        raise ValueError("SECURITY_MASTER_PREFLIGHT_SECRET must be set")
    local = str(e.get("SECURITY_MASTER_ROOT", "")).strip()
    if local:
        root, options, base = local, {}, None
    else:
        s3 = s3_options_from_env(e)
        if s3 is None:
            raise ValueError("set SECURITY_MASTER_ROOT (local) or the DELTA_S3_* variables")
        base, options = s3
        root = f"{base}/security_master"
    return Settings(
        root=root,
        storage_options=dict(options),
        delta_base=base,
        first_page=_int(e, "SECURITY_MASTER_FIRST_PAGE", 100),
        max_rows=_int(e, "SECURITY_MASTER_MAX_ROWS", 10_000),
        timeout_ms=_int(e, "SECURITY_MASTER_TIMEOUT_MS", 15_000),
        per_principal_queries=_int(e, "SECURITY_MASTER_PER_PRINCIPAL_QUERIES", 8),
        scan_slots=_int(e, "SECURITY_MASTER_SCAN_SLOTS", 4),
        memory_limit=str(e.get("SECURITY_MASTER_MEMORY_LIMIT", "1GB")).strip() or "1GB",
        threads=_int(e, "SECURITY_MASTER_THREADS", 2),
        sql_policy=_policy(e, "SECURITY_MASTER_SQL", "enabled"),
        acquisition_policy=_policy(e, "SECURITY_MASTER_ACQUISITION", "review"),
        openbb_url=str(e.get("OPENBB_URL", "http://openbb-api:6900")).strip().rstrip("/"),
        openbb_username=str(e.get("OPENBB_API_USERNAME", "")).strip(),
        openbb_password=str(e.get("OPENBB_API_PASSWORD", "")).strip(),
        preflight_secret=secret,
    )

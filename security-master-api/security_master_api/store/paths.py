# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Where a logical relation lives. Physical paths never leave this module's callers."""

from __future__ import annotations

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.store.schemas import RELATIONS


def split(relation: str) -> tuple[str, str]:
    layer, _, name = relation.partition(".")
    if relation not in RELATIONS:
        raise DomainError("QUERY_REJECTED", f"unknown relation: {relation}", {"relation": relation})
    return layer, name


def relation_path(settings: Settings, relation: str) -> str:
    layer, name = split(relation)
    return f"{settings.root}/{layer}__{name}"

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Pin every physical table a request opens. The manifest is what the receipt records."""

from __future__ import annotations

from collections.abc import Iterable

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.catalog import (
    Relation,
    catalog_index,
    check_modes,
    expand,
    relation,
)
from security_master_api.resolver.context import Context
from security_master_api.store.tables import latest_version


def resolve_manifest(settings: Settings, ctx: Context, relations: Iterable[str],
                     index: dict[str, Relation] | None = None) -> dict[str, int]:
    # One catalog for the whole resolution: expand, check_modes and the loop below each look
    # relations up, and without this they would each rebuild it -- including the object-store
    # listing that `external_relations` does.
    index = catalog_index(settings) if index is None else index
    wanted = expand(settings, relations, index)
    check_modes(settings, ctx, wanted, index)
    manifest: dict[str, int] = {}
    for full in sorted(wanted):
        if relation(settings, full, index).kind == "view":
            continue
        latest = latest_version(settings, full)
        if latest is None:
            raise DomainError("NO_LOCAL_EVIDENCE", f"{full} has no local table", {"relation": full})
        requested = (ctx.versions or {}).get(full)
        if requested is None:
            manifest[full] = latest
        elif 0 <= requested <= latest:
            manifest[full] = requested
        else:
            raise DomainError("VERSION_NOT_RETAINED", f"{full} has no retained version {requested}",
                              {"relation": full, "latest": latest})
    return manifest

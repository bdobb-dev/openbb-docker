# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Append-only Delta tables, one per relation, opened at exact versions."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pyarrow as pa
import pyarrow.dataset as pads
from deltalake import DeltaTable, write_deltalake
from deltalake.exceptions import TableNotFoundError

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.store.paths import physical_path, relation_path
from security_master_api.store.schemas import RELATIONS
from security_master_api.temporal_fixture import _instant


def _coerce(rows: list[dict], schema: pa.Schema) -> pa.Table:
    names = set(schema.names)
    out: list[dict] = []
    for row in rows:
        unknown = set(row) - names
        if unknown:
            raise ValueError(f"unknown column(s) for relation: {sorted(unknown)}")
        clean: dict = {}
        for field in schema:
            value = row.get(field.name)
            if value is None:
                clean[field.name] = None
            elif pa.types.is_timestamp(field.type):
                clean[field.name] = _instant(value) if isinstance(value, str) else value
            elif pa.types.is_date(field.type):
                clean[field.name] = date.fromisoformat(value) if isinstance(value, str) else value
            else:
                clean[field.name] = value
        out.append(clean)
    return pa.Table.from_pylist(out, schema=schema)


def _open(settings: Settings, relation: str, version: int | None) -> DeltaTable:
    path = physical_path(settings, relation)
    try:
        table = DeltaTable(path, storage_options=settings.storage_options or None)
    except TableNotFoundError as exc:
        raise DomainError("NO_LOCAL_EVIDENCE", f"{relation} has no local table",
                          {"relation": relation}) from exc
    if version is not None:
        if version < 0 or version > table.version():
            raise DomainError("VERSION_NOT_RETAINED",
                              f"{relation} has no retained version {version}",
                              {"relation": relation, "latest": table.version()})
        table.load_as_version(version)
    return table


def latest_version(settings: Settings, relation: str) -> int | None:
    try:
        return _open(settings, relation, None).version()
    except DomainError as exc:
        if exc.code == "NO_LOCAL_EVIDENCE":
            return None
        raise


def ensure(settings: Settings, relation: str) -> None:
    schema = RELATIONS.get(relation)
    if schema is None:
        raise DomainError("QUERY_REJECTED", f"unknown relation: {relation}", {"relation": relation})
    if latest_version(settings, relation) is None:
        write_deltalake(relation_path(settings, relation), schema.empty_table(), mode="error",
                        storage_options=settings.storage_options or None)


def append(settings: Settings, relation: str, rows: list[dict]) -> int:
    ensure(settings, relation)
    schema = RELATIONS[relation]
    write_deltalake(relation_path(settings, relation), _coerce(rows, schema), mode="append",
                    storage_options=settings.storage_options or None)
    return _open(settings, relation, None).version()


def open_table(settings: Settings, relation: str, version: int | None = None) -> DeltaTable:
    return _open(settings, relation, version)


def dataset(settings: Settings, relation: str, version: int | None = None) -> pads.Dataset:
    return _open(settings, relation, version).to_pyarrow_dataset()


def history(settings: Settings, relation: str) -> list[dict]:
    out = []
    for entry in _open(settings, relation, None).history():
        ts = entry.get("timestamp")
        if isinstance(ts, int):
            ts = datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({"version": int(entry["version"]), "timestamp": str(ts)})
    out.sort(key=lambda h: h["version"], reverse=True)
    return out

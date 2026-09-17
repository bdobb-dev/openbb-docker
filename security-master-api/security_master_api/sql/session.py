# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""One isolated DuckDB connection per request, holding only the manifest's relations."""

from __future__ import annotations

import threading
from collections.abc import Mapping

import duckdb
import pyarrow as pa

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import Context
from security_master_api.sql.policy import validate_sql
from security_master_api.store.tables import dataset


class QuerySession:
    def __init__(self, settings: Settings, ctx: Context, manifest: Mapping[str, int],
                 view_sql: Mapping[str, str] | None = None):
        self.settings = settings
        self.ctx = ctx
        self.manifest = dict(manifest)
        self.view_sql = dict(view_sql or {})
        self.allowed: set[str] = set(self.manifest) | {f"gold.{n}" for n in self.view_sql}
        self.con: duckdb.DuckDBPyConnection | None = None
        self._cancelled = threading.Event()

    def __enter__(self) -> QuerySession:
        # Idempotent: a session handed out already entered is still used inside `with`.
        if self.con is not None:
            return self
        con = duckdb.connect(":memory:", config={
            "enable_external_access": "false",
            "memory_limit": self.settings.memory_limit,
            "threads": str(self.settings.threads),
            "lock_configuration": "true",
        })
        self.con = con
        for schema in ("bronze", "silver", "gold", "ops"):
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        for full, version in self.manifest.items():
            layer, _, name = full.partition(".")
            alias = f"{layer}__{name.replace('.', '__')}"
            con.register(alias, dataset(self.settings, full, version))
            con.execute(f'CREATE VIEW {layer}."{name}" AS SELECT * FROM "{alias}"')
        for name, sql in self.view_sql.items():
            con.execute(f'CREATE VIEW gold."{name}" AS {sql}')
        return self

    def __exit__(self, *exc) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None

    def cancel(self) -> None:
        self._cancelled.set()
        if self.con is not None:
            self.con.interrupt()

    def describe(self, sql: str) -> list[dict]:
        validate_sql(self.con, sql, self.allowed)
        rows = self.con.execute(f"DESCRIBE {sql}").fetchall()
        return [{"name": r[0], "dtype": r[1]} for r in rows]

    def run(self, sql: str, params: list | dict | None = None, timeout_ms: int | None = None,
            allow_explain: bool = False) -> pa.Table:
        assert self.con is not None, "session not entered"
        validate_sql(self.con, sql, self.allowed, allow_explain=allow_explain)
        budget = (timeout_ms or self.settings.timeout_ms) / 1000
        timer = threading.Timer(budget, self.con.interrupt)
        timer.start()
        try:
            return self.con.execute(sql, params if params is not None else []).to_arrow_table()
        except duckdb.InterruptException as exc:
            if self._cancelled.is_set():
                raise DomainError("QUERY_CANCELLED", "the query was cancelled") from exc
            raise DomainError("QUERY_BUDGET_EXCEEDED", f"the query exceeded {budget:.1f}s",
                              {"timeout_ms": int(budget * 1000)}) from exc
        except duckdb.OutOfMemoryException as exc:
            raise DomainError("QUERY_BUDGET_EXCEEDED", "the query exceeded the memory budget") from exc
        finally:
            timer.cancel()

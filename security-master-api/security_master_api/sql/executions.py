# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Executions: run under a budget, hold the result, page it by opaque cursor, cancel it."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

import pyarrow as pa

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import Context, iso_utc
from security_master_api.sql.session import QuerySession, open_session
from security_master_api.store.receipts import new_receipt, record_receipt

RESULT_TTL_S = 600


def encode_cursor(execution_id: str, offset: int, fingerprint: str) -> str:
    raw = json.dumps({"e": execution_id, "o": offset, "f": fingerprint}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str) -> tuple[str, int, str]:
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return str(raw["e"]), int(raw["o"]), str(raw["f"])
    except Exception as exc:  # noqa: BLE001 - any malformed token is one rejection
        raise DomainError("QUERY_REJECTED", "invalid cursor") from exc


@dataclass
class _Execution:
    receipt: dict
    fingerprint: str
    page_size: int
    table: pa.Table | None = None
    truncated: bool = False
    session: QuerySession | None = None
    created: float = field(default_factory=time.monotonic)
    principal: str = ""


def _fingerprint(ctx: Context, sql: str, params) -> str:
    return hashlib.sha256(json.dumps([ctx.as_dict(), sql, params], sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]


class Executions:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._by_id: dict[str, _Execution] = {}
        self._scan = threading.BoundedSemaphore(max(1, settings.scan_slots))
        self._per_principal: dict[str, int] = {}

    def _acquire(self, principal: str) -> None:
        with self._lock:
            active = self._per_principal.get(principal, 0)
            if active >= self.settings.per_principal_queries:
                raise DomainError("QUERY_BUDGET_EXCEEDED",
                                  f"{principal} already has {active} running queries",
                                  {"limit": self.settings.per_principal_queries})
            self._per_principal[principal] = active + 1
        if not self._scan.acquire(timeout=self.settings.timeout_ms / 1000):
            self._release(principal)
            raise DomainError("QUERY_BUDGET_EXCEEDED", "the service scan budget is exhausted")

    def _release(self, principal: str) -> None:
        with self._lock:
            self._per_principal[principal] = max(0, self._per_principal.get(principal, 1) - 1)
        try:
            self._scan.release()
        except ValueError:
            pass

    def _expire(self) -> None:
        cutoff = time.monotonic() - RESULT_TTL_S
        with self._lock:
            for key in [k for k, v in self._by_id.items() if v.created < cutoff]:
                del self._by_id[key]

    def plan(self, ctx: Context, relations: Iterable[str], sql: str) -> dict:
        with open_session(self.settings, ctx, list(relations)) as s:
            from security_master_api.sql.policy import validate_sql

            info = validate_sql(s.con, sql, s.allowed)
            return {"relations": info.relations, "functions": info.functions,
                    "columns": s.describe(sql), "manifest": s.manifest}

    def start(self, ctx: Context, relations: Iterable[str], sql: str, params, kind: str,
              request_id: str, principal: str, page_size: int | None = None,
              timeout_ms: int | None = None) -> dict:
        self._expire()
        page = min(page_size or self.settings.first_page, self.settings.max_rows)
        self._acquire(principal)
        # `session` and `receipt` start unset so a failure at any point below - before the
        # session opens, or between opening it and recording the receipt - leaves `finally`
        # with well-defined state: it always closes whatever session got opened and always
        # releases the principal's budget slot, but only touches `_by_id` once a receipt (and
        # therefore an execution_id) actually exists.
        session = None
        receipt = None
        ex = None
        try:
            session = open_session(self.settings, ctx, list(relations))
            receipt = new_receipt(self.settings, kind, ctx, session.manifest, request_id,
                                  _fingerprint(ctx, sql, params))
            record_receipt(self.settings, receipt)
            ex = _Execution(receipt=receipt, fingerprint=receipt["sql_fingerprint"],
                            page_size=page, session=session, principal=principal)
            with self._lock:
                self._by_id[receipt["execution_id"]] = ex
            bounded = f"SELECT * FROM ({sql.strip().rstrip(';')}) AS q LIMIT {self.settings.max_rows + 1}"
            table = session.run(bounded, params, timeout_ms=timeout_ms)
            if table.num_rows > self.settings.max_rows:
                ex.truncated = True
                table = table.slice(0, self.settings.max_rows)
            ex.table = table
        finally:
            if session is not None:
                session.__exit__(None, None, None)
            if receipt is not None:
                with self._lock:
                    entry = self._by_id.get(receipt["execution_id"])
                    if entry is not None:
                        entry.session = None
            self._release(principal)
        return self._page(ex, 0)

    def page(self, cursor: str) -> dict:
        execution_id, offset, fingerprint = decode_cursor(cursor)
        with self._lock:
            ex = self._by_id.get(execution_id)
        if ex is None or ex.table is None:
            raise DomainError("QUERY_REJECTED", "unknown or expired execution",
                              {"execution_id": execution_id})
        if fingerprint != ex.fingerprint:
            raise DomainError("QUERY_REJECTED", "cursor does not match the execution's context")
        return self._page(ex, offset)

    def cancel(self, execution_id: str) -> bool:
        with self._lock:
            ex = self._by_id.get(execution_id)
        if ex is None:
            return False
        if ex.session is not None:
            ex.session.cancel()
        with self._lock:
            self._by_id.pop(execution_id, None)
        return True

    def _page(self, ex: _Execution, offset: int) -> dict:
        table = ex.table
        chunk = table.slice(offset, ex.page_size)
        rows = [{k: iso_utc(v) for k, v in r.items()} for r in chunk.to_pylist()]
        next_offset = offset + chunk.num_rows
        has_more = next_offset < table.num_rows
        return {
            "execution_id": ex.receipt["execution_id"],
            "rows": rows,
            "columns": [{"name": f.name, "dtype": str(f.type)} for f in table.schema],
            "next_cursor": encode_cursor(ex.receipt["execution_id"], next_offset, ex.fingerprint) if has_more else None,
            "count": {"value": table.num_rows, "quality": "estimated" if ex.truncated else "exact"},
            "receipt": ex.receipt,
        }

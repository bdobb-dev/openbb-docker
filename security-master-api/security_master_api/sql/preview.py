# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Structured, allowlisted filters and sorts become bound SQL. No user text reaches the SQL."""

from __future__ import annotations

import re

from security_master_api.errors import DomainError

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
OPERATORS = {
    "eq": '"{f}" = ?', "ne": '"{f}" <> ?', "lt": '"{f}" < ?', "lte": '"{f}" <= ?',
    "gt": '"{f}" > ?', "gte": '"{f}" >= ?', "contains": '"{f}" ILIKE ?',
    "starts_with": '"{f}" ILIKE ?', "in": '"{f}" IN ({ph})', "between": '"{f}" BETWEEN ? AND ?',
    "is_null": '"{f}" IS NULL', "not_null": '"{f}" IS NOT NULL',
}


def _ident(name: str, what: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise DomainError("QUERY_REJECTED", f"invalid {what} name", {what: str(name)[:40]})
    return name


def build_preview_sql(relation: str, columns: list[str] | None, filters: list[dict],
                      sort: list[dict]) -> tuple[str, list]:
    layer, _, name = relation.partition(".")
    _ident(layer, "layer")
    # `name` may itself carry a dot for an external relation (bronze.openbb.AAPL) - each
    # dot-separated part is validated on its own, but the whole thing is quoted as one
    # identifier below, matching how the session registers these views.
    for part in name.split("."):
        _ident(part, "relation")
    select = ", ".join(f'"{_ident(c, "column")}"' for c in columns) if columns else "*"
    sql = f'SELECT {select} FROM {layer}."{name}"'
    params: list = []
    clauses = []
    for flt in filters or []:
        if not isinstance(flt, dict):
            raise DomainError("QUERY_REJECTED", "filter must be an object")
        field = _ident(flt.get("field"), "column")
        op = flt.get("operator")
        if op not in OPERATORS:
            raise DomainError("QUERY_REJECTED", f"unsupported operator {op!r}",
                              {"supported": sorted(OPERATORS)})
        value = flt.get("value")
        if op == "in":
            if not isinstance(value, list) or not value or len(value) > 100:
                raise DomainError("QUERY_REJECTED", "in needs a list of 1..100 values")
            clauses.append(OPERATORS[op].format(f=field, ph=", ".join("?" * len(value))))
            params.extend(value)
        elif op == "between":
            if not isinstance(value, list) or len(value) != 2:
                raise DomainError("QUERY_REJECTED", "between needs two values")
            clauses.append(OPERATORS[op].format(f=field))
            params.extend(value)
        elif op in ("is_null", "not_null"):
            clauses.append(OPERATORS[op].format(f=field))
        else:
            if isinstance(value, (list, dict)) or value is None:
                raise DomainError("QUERY_REJECTED", f"{op} needs a scalar value")
            clauses.append(OPERATORS[op].format(f=field))
            if op == "contains":
                params.append(f"%{value}%")
            elif op == "starts_with":
                params.append(f"{value}%")
            else:
                params.append(value)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    orders = []
    for s in sort or []:
        field = _ident((s or {}).get("field"), "column")
        direction = str((s or {}).get("direction", "asc")).upper()
        if direction not in ("ASC", "DESC"):
            raise DomainError("QUERY_REJECTED", "sort direction must be asc or desc")
        orders.append(f'"{field}" {direction}')
    if orders:
        sql += " ORDER BY " + ", ".join(orders)
    return sql, params

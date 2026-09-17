# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Read-only SQL policy, enforced on DuckDB's own AST rather than on the text."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import duckdb

from security_master_api.errors import DomainError

ALLOWED_FUNCTIONS = frozenset({
    # arithmetic / comparison / logic
    "+", "-", "*", "/", "//", "%", "**", "^", "=", "==", "<>", "!=", "<", "<=", ">", ">=",
    "and", "or", "not", "between", "in", "is", "coalesce", "nullif", "greatest", "least",
    "abs", "round", "floor", "ceil", "ceiling", "sign", "sqrt", "power", "pow", "ln", "log",
    "log10", "log2", "exp", "mod", "random",
    # strings
    "upper", "lower", "length", "len", "trim", "ltrim", "rtrim", "substr", "substring",
    "left", "right", "concat", "concat_ws", "||", "replace", "contains", "starts_with",
    "ends_with", "prefix", "suffix", "strip_accents", "regexp_matches", "regexp_replace",
    "regexp_extract", "like", "~~", "!~~", "ilike", "~~*", "split_part", "string_split",
    "format", "printf", "repeat", "reverse", "lpad", "rpad", "instr", "strpos", "position",
    # dates and times
    "date_trunc", "date_part", "datepart", "date_diff", "datediff", "date_add", "date_sub",
    "epoch", "epoch_ms", "strftime", "strptime", "year", "month", "day", "hour", "minute",
    "second", "dayofweek", "weekday", "dayofyear", "week", "quarter", "isodow", "make_date",
    "make_timestamp", "to_timestamp", "age", "now", "current_date", "current_timestamp",
    "today", "timezone", "interval",
    # casting / structs / lists / json
    "cast", "try_cast", "typeof", "list_value", "list_contains", "list_extract",
    "array_extract", "unnest", "struct_pack", "struct_extract", "json_extract",
    "json_extract_string", "->", "->>", "json", "from_json", "json_valid", "json_type",
    # aggregates and windows
    "count", "count_star", "sum", "avg", "mean", "min", "max", "median", "quantile",
    "quantile_cont", "quantile_disc", "stddev", "stddev_samp", "stddev_pop", "variance",
    "var_samp", "var_pop", "first", "last", "any_value", "arg_min", "arg_max", "string_agg",
    "list", "array_agg", "bool_and", "bool_or", "approx_count_distinct",
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile", "lag", "lead",
    "first_value", "last_value", "nth_value",
    # misc
    "hash", "md5", "sha256", "case", "if", "ifnull",
})

SELECT_NODES = {"SELECT_NODE", "SET_OPERATION_NODE", "RECURSIVE_CTE_NODE", "CTE_NODE"}


@dataclass
class PlanInfo:
    relations: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)


def _reject(message: str, **details) -> DomainError:
    return DomainError("QUERY_REJECTED", message, details or None)


def _walk(node, info: PlanInfo, cte_names: set[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, info, cte_names)
        return
    if not isinstance(node, dict):
        return
    node_type = node.get("type")
    if node_type == "TABLE_FUNCTION":
        raise _reject("table functions are not allowed",
                      function=node.get("function", {}).get("function_name"))
    # A CTE name is in scope for the whole node it is declared on, so the map is read
    # before any BASE_TABLE below it is classified.
    for entry in (node.get("cte_map") or {}).get("map", []) or []:
        if isinstance(entry, dict) and "key" in entry:
            cte_names.add(str(entry["key"]))
    if node_type == "BASE_TABLE":
        schema = node.get("schema_name") or ""
        table = node.get("table_name") or ""
        if schema or table not in cte_names:
            info.relations.append(f"{schema}.{table}" if schema else table)
    if node.get("class") == "FUNCTION":
        info.functions.append(str(node.get("function_name", "")).lower())
    for value in node.values():
        _walk(value, info, cte_names)


def validate_sql(con: duckdb.DuckDBPyConnection, sql: str, allowed: set[str],
                 allow_explain: bool = False) -> PlanInfo:
    text = sql.strip().rstrip(";").strip()
    body = text
    if text[:7].lower() == "explain":
        if not allow_explain:
            raise _reject("EXPLAIN is not allowed by policy")
        body = text[7:].strip()
    raw = con.execute("SELECT json_serialize_sql(?)", [body]).fetchone()[0]
    parsed = json.loads(raw)
    if parsed.get("error"):
        raise _reject(f"parser: {parsed.get('error_message', 'invalid SQL')}")
    # Multi-statement text reaches here as several serialized statements, so the count -
    # not a scan for ';' - is what forbids it. A ';' inside a string literal stays legal.
    statements = parsed.get("statements") or []
    if len(statements) != 1:
        raise _reject("exactly one statement is allowed")
    node = statements[0].get("node") or {}
    if node.get("type") not in SELECT_NODES:
        raise _reject("only SELECT statements are allowed", statement=node.get("type"))
    info = PlanInfo()
    _walk(node, info, set())
    for full in info.relations:
        if full not in allowed:
            raise _reject(f"relation is not registered for this context: {full}", relation=full)
    for fn in info.functions:
        if fn not in ALLOWED_FUNCTIONS:
            raise _reject(f"function is not allowed: {fn}", function=fn)
    info.relations = sorted(set(info.relations))
    info.functions = sorted(set(info.functions))
    return info

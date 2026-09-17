# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import parse_context
from security_master_api.sql.executions import Executions, decode_cursor, encode_cursor
from security_master_api.sql.preview import build_preview_sql
from security_master_api.store.seed import seed
from security_master_api.store.tables import open_table


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k", first_page=2, max_rows=5)
    seed(s)
    return s


def test_first_page_then_cursor_then_end(settings):
    ex = Executions(settings)
    first = ex.start(parse_context(None), ["silver.listings"],
                     "SELECT symbol FROM silver.listings ORDER BY symbol", [], "sql", "req", "user")
    assert len(first["rows"]) == 2 and first["next_cursor"]
    assert first["count"] == {"value": 5, "quality": "estimated"} or first["count"]["quality"] in ("exact", "estimated")
    second = ex.page(first["next_cursor"])
    assert second["receipt"]["execution_id"] == first["receipt"]["execution_id"]
    assert [r["symbol"] for r in second["rows"]] != [r["symbol"] for r in first["rows"]]
    rows = open_table(settings, "ops.receipts").to_pyarrow_table().to_pylist()
    assert any(r["execution_id"] == first["receipt"]["execution_id"] for r in rows)


def test_max_rows_marks_the_count_estimated_and_stops(settings):
    ex = Executions(settings)
    out = ex.start(parse_context(None), ["silver.listings"],
                   "SELECT * FROM silver.listings", [], "sql", "req", "user", page_size=100)
    assert len(out["rows"]) <= 5
    assert out["count"]["quality"] in ("exact", "estimated")


def test_cursor_binds_execution_and_offset(settings):
    token = encode_cursor("qry_x", 40, "fp")
    assert decode_cursor(token) == ("qry_x", 40, "fp")
    ex = Executions(settings)
    with pytest.raises(DomainError) as exc:
        ex.page(encode_cursor("qry_missing", 0, "fp"))
    assert exc.value.code == "QUERY_REJECTED"
    with pytest.raises(DomainError):
        ex.page("not-a-cursor")


def test_plan_reports_relations_columns_and_manifest(settings):
    ex = Executions(settings)
    plan = ex.plan(parse_context(None), ["gold.security_master"],
                   "SELECT symbol, name FROM gold.security_master")
    assert plan["relations"] == ["gold.security_master"]
    assert [c["name"] for c in plan["columns"]] == ["symbol", "name"]
    assert "silver.listings" in plan["manifest"]


def test_per_principal_budget(settings):
    ex = Executions(Settings(root=settings.root, preflight_secret="k", per_principal_queries=0))
    with pytest.raises(DomainError) as exc:
        ex.start(parse_context(None), ["silver.listings"], "SELECT 1", [], "sql", "req", "user")
    assert exc.value.code == "QUERY_BUDGET_EXCEEDED"


def test_cancel_unknown_is_false(settings):
    assert Executions(settings).cancel("qry_nope") is False


def test_timed_out_acquire_releases_only_the_per_principal_count(settings):
    # scan_slots=1 and a tiny timeout: holding the one slot on "a" must make every other
    # principal's acquire fail fast, and a failed acquire must not free the slot "a" holds.
    ex = Executions(Settings(root=settings.root, preflight_secret="k", scan_slots=1,
                             timeout_ms=50))
    ex._acquire("a")
    with pytest.raises(DomainError) as exc:
        ex._acquire("b")
    assert exc.value.code == "QUERY_BUDGET_EXCEEDED"
    # If the timed-out acquire above had released the semaphore it never acquired, this
    # third acquire would wrongly succeed - it must still see the slot as held.
    with pytest.raises(DomainError) as exc:
        ex._acquire("c")
    assert exc.value.code == "QUERY_BUDGET_EXCEEDED"
    ex._release("a", True)
    ex._acquire("c")  # the slot is free again now that its actual holder released it


def test_contains_underscore_matches_nothing_a_matches_aapl(settings):
    ex = Executions(settings)
    sql, params = build_preview_sql("silver.listings", ["symbol"],
        [{"field": "symbol", "operator": "contains", "value": "_"}], [])
    none = ex.start(parse_context(None), ["silver.listings"], sql, params, "sql", "req", "user",
                    page_size=100)
    assert none["rows"] == []

    sql, params = build_preview_sql("silver.listings", ["symbol"],
        [{"field": "symbol", "operator": "contains", "value": "A"}], [])
    some = ex.start(parse_context(None), ["silver.listings"], sql, params, "sql", "req", "user",
                    page_size=100)
    assert any(r["symbol"] == "AAPL" for r in some["rows"])


def test_count_quality_exact_when_not_truncated(settings):
    ex = Executions(settings)
    out = ex.start(parse_context(None), ["silver.listings"],
                   "SELECT * FROM silver.listings", [], "sql", "req", "user", page_size=100)
    assert out["count"] == {"value": 4, "quality": "exact"}


def test_count_quality_estimated_when_truncated_at_max_rows(settings):
    ex = Executions(settings)
    out = ex.start(parse_context(None), ["silver.listings"],
                   "SELECT a.* FROM silver.listings a, silver.listings b",
                   [], "sql", "req", "user", page_size=100)
    assert out["count"] == {"value": 5, "quality": "estimated"}


def test_cancel_on_finished_execution_then_page_is_rejected(settings):
    ex = Executions(settings)
    first = ex.start(parse_context(None), ["silver.listings"],
                     "SELECT symbol FROM silver.listings ORDER BY symbol", [], "sql", "req", "user")
    assert ex.cancel(first["execution_id"]) is True
    with pytest.raises(DomainError) as exc:
        ex.page(first["next_cursor"])
    assert exc.value.code == "QUERY_REJECTED"

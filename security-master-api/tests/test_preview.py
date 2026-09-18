# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.errors import DomainError
from security_master_api.sql.preview import build_preview_sql


def test_structured_preview_is_bound_not_interpolated():
    sql, params = build_preview_sql(
        "gold.security_master", ["listing_id", "symbol"],
        [{"field": "status", "operator": "eq", "value": "active"},
         {"field": "symbol", "operator": "contains", "value": "AA"}],
        [{"field": "symbol", "direction": "asc"}])
    assert sql == ('SELECT "listing_id", "symbol" FROM gold."security_master" '
                   'WHERE "status" = ? AND "symbol" ILIKE ? ESCAPE \'\\\' ORDER BY "symbol" ASC')
    assert params == ["active", "%AA%"]


def test_contains_escapes_like_wildcards():
    sql, params = build_preview_sql("silver.listings", None,
        [{"field": "symbol", "operator": "contains", "value": "A_B"}], [])
    assert "ESCAPE '\\'" in sql
    assert params == ["%A\\_B%"]


def test_all_columns_when_none_named():
    sql, params = build_preview_sql("silver.listings", None, [], [])
    assert sql == 'SELECT * FROM silver."listings"' and params == []


@pytest.mark.parametrize("bad", [
    {"field": "sym\"bol", "operator": "eq", "value": "x"},
    {"field": "symbol", "operator": "regex", "value": "x"},
    {"field": "symbol", "operator": "in", "value": "not-a-list"},
])
def test_bad_filters_are_rejected(bad):
    with pytest.raises(DomainError) as exc:
        build_preview_sql("silver.listings", None, [bad], [])
    assert exc.value.code == "QUERY_REJECTED"


def test_in_and_between_and_null():
    sql, params = build_preview_sql("silver.listings", None,
        [{"field": "symbol", "operator": "in", "value": ["A", "B"]},
         {"field": "effective_from", "operator": "between", "value": ["2020-01-01", "2021-01-01"]},
         {"field": "effective_to", "operator": "is_null"}], [])
    assert '"symbol" IN (?, ?)' in sql and '"effective_from" BETWEEN ? AND ?' in sql
    assert '"effective_to" IS NULL' in sql
    assert params == ["A", "B", "2020-01-01", "2021-01-01"]

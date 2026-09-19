# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import duckdb
import pytest

from security_master_api.errors import DomainError
from security_master_api.sql.policy import validate_sql

ALLOWED = {"gold.security_master", "silver.listings"}


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("CREATE SCHEMA gold; CREATE SCHEMA silver")
    c.execute("CREATE TABLE gold.security_master(symbol VARCHAR, name VARCHAR)")
    c.execute("CREATE TABLE silver.listings(listing_id VARCHAR, symbol VARCHAR)")
    return c


def test_plain_select_passes(con):
    info = validate_sql(con, "SELECT symbol, upper(name) AS n FROM gold.security_master ORDER BY symbol LIMIT 10", ALLOWED)
    assert info.relations == ["gold.security_master"]
    assert "upper" in info.functions


def test_cte_and_join_pass(con):
    sql = ("WITH l AS (SELECT listing_id, symbol FROM silver.listings) "
           "SELECT s.symbol, count(*) FROM gold.security_master s JOIN l USING (symbol) GROUP BY 1")
    assert sorted(validate_sql(con, sql, ALLOWED).relations) == ["gold.security_master", "silver.listings"]


@pytest.mark.parametrize("sql", [
    "SELECT 1; SELECT 2",
    "DROP TABLE gold.security_master",
    "INSERT INTO silver.listings VALUES ('x','y')",
    "COPY gold.security_master TO '/tmp/x.csv'",
    "ATTACH '/tmp/other.db' AS o",
    "INSTALL httpfs",
    "LOAD httpfs",
    "PRAGMA database_list",
    "SET memory_limit='1GB'",
    "CREATE SECRET (TYPE S3)",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM 's3://bucket/x.parquet'",
    "SELECT * FROM glob('*')",
    "SELECT getenv('HOME')",
    "SELECT current_setting('home_directory')",
    "SELECT * FROM silver.nope",
    "SELECT * FROM ops.receipts",
    "EXPLAIN SELECT 1",
    "SELECT * FROM sniff_csv('x')",
])
def test_forbidden_statements_are_rejected(con, sql):
    with pytest.raises(DomainError) as exc:
        validate_sql(con, sql, ALLOWED)
    assert exc.value.code == "QUERY_REJECTED"


def test_explain_allowed_only_by_policy(con):
    info = validate_sql(con, "EXPLAIN SELECT symbol FROM gold.security_master", ALLOWED, allow_explain=True)
    assert info.relations == ["gold.security_master"]


def test_unparseable_sql_is_rejected_with_the_parser_message(con):
    with pytest.raises(DomainError) as exc:
        validate_sql(con, "SELEC symbol FROM gold.security_master", ALLOWED)
    assert "syntax" in exc.value.message.lower() or "parser" in exc.value.message.lower()


def test_a_semicolon_inside_a_literal_is_not_a_second_statement(con):
    info = validate_sql(con, "SELECT symbol FROM gold.security_master WHERE name = 'a;b'", ALLOWED)
    assert info.relations == ["gold.security_master"]


@pytest.mark.parametrize("sql", [
    "SELECT (SELECT count(*) FROM ops.receipts) AS n",
    "SELECT * FROM gold.security_master WHERE symbol IN (SELECT * FROM read_csv('/etc/passwd'))",
    "SELECT * FROM gold.security_master UNION ALL SELECT * FROM ops.receipts",
    "SELECT symbol FROM gold.security_master ORDER BY getenv(name)",
])
def test_the_walk_reaches_subqueries_and_modifiers(con, sql):
    with pytest.raises(DomainError) as exc:
        validate_sql(con, sql, ALLOWED)
    assert exc.value.code == "QUERY_REJECTED"


def test_window_call_with_a_disallowed_function_is_rejected(con):
    with pytest.raises(DomainError) as exc:
        validate_sql(con, "SELECT histogram(symbol) OVER () AS h FROM silver.listings", ALLOWED)
    assert exc.value.code == "QUERY_REJECTED"


@pytest.mark.parametrize("sql", [
    "SELECT row_number() OVER (ORDER BY symbol) AS r FROM silver.listings",
    "SELECT lag(symbol) OVER (ORDER BY symbol) AS r FROM silver.listings",
    "SELECT sum(1) OVER () AS r FROM silver.listings",
])
def test_allowed_window_calls_pass(con, sql):
    info = validate_sql(con, sql, ALLOWED)
    assert info.relations == ["silver.listings"]

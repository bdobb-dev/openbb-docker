# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import threading
import time

import pyarrow as pa
import pytest
from deltalake import write_deltalake

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import parse_context
from security_master_api.resolver.manifest import resolve_manifest
from security_master_api.sql.session import QuerySession
from security_master_api.store.seed import seed

# The brief's slow query was `range(200000000)`, a table function the policy denies by design.
# A recursive CTE is slow with nothing but allowed functions, so the budget path is what is tested.
SLOW = ("WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t WHERE n < 2000000000) "
        "SELECT count(*) AS c FROM t")


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k", timeout_ms=2000)
    seed(s)
    return s


def test_only_manifest_relations_are_visible(settings):
    ctx = parse_context(None)
    manifest = resolve_manifest(settings, ctx, ["silver.listings"])
    with QuerySession(settings, ctx, manifest) as session:
        rows = session.run("SELECT symbol FROM silver.listings ORDER BY symbol").to_pylist()
        assert {r["symbol"] for r in rows} >= {"AAPL", "META", "FB"}
        with pytest.raises(DomainError) as exc:
            session.run("SELECT * FROM silver.issuers")
        assert exc.value.code == "QUERY_REJECTED"


def test_external_access_is_off(settings):
    ctx = parse_context(None)
    with QuerySession(settings, ctx, resolve_manifest(settings, ctx, ["silver.listings"])) as s:
        assert s.con.execute("SELECT current_setting('enable_external_access')").fetchone()[0] is False
        with pytest.raises(Exception):
            s.con.execute("SET enable_external_access = true")


def test_pinned_version_is_what_is_read(settings):
    snap = parse_context({"mode": "delta_snapshot", "versions": {"silver.issuers": 0}})
    with QuerySession(settings, snap, resolve_manifest(settings, snap, ["silver.issuers"])) as s:
        assert s.run("SELECT count(*) AS n FROM silver.issuers").to_pylist()[0]["n"] == 0


def test_view_sql_is_created(settings):
    ctx = parse_context(None)
    manifest = resolve_manifest(settings, ctx, ["silver.listings"])
    with QuerySession(settings, ctx, manifest,
                      view_sql={"symbols": "SELECT symbol FROM silver.listings"}) as s:
        assert s.run("SELECT count(*) AS n FROM gold.symbols").to_pylist()[0]["n"] >= 3


def test_timeout_and_cancel_reach_duckdb(settings):
    ctx = parse_context(None)
    manifest = resolve_manifest(settings, ctx, ["silver.listings"])
    with QuerySession(settings, ctx, manifest) as s:
        started = time.monotonic()
        with pytest.raises(DomainError) as exc:
            s.run(SLOW, timeout_ms=300)
        assert exc.value.code == "QUERY_BUDGET_EXCEEDED"
        assert time.monotonic() - started < 5
    with QuerySession(settings, ctx, manifest) as s:
        threading.Timer(0.2, s.cancel).start()
        with pytest.raises(DomainError) as exc:
            s.run(SLOW, timeout_ms=10_000)
        assert exc.value.code == "QUERY_CANCELLED"


def test_describe_returns_columns(settings):
    ctx = parse_context(None)
    with QuerySession(settings, ctx, resolve_manifest(settings, ctx, ["silver.listings"])) as s:
        cols = s.describe("SELECT listing_id, symbol FROM silver.listings")
        assert cols == [{"name": "listing_id", "dtype": "VARCHAR"}, {"name": "symbol", "dtype": "VARCHAR"}]


def test_params_bind_by_name_and_position(settings):
    ctx = parse_context(None)
    with QuerySession(settings, ctx, resolve_manifest(settings, ctx, ["silver.listings"])) as s:
        by_pos = s.run("SELECT count(*) AS n FROM silver.listings WHERE symbol = ?", ["AAPL"])
        by_name = s.run("SELECT count(*) AS n FROM silver.listings WHERE symbol = $symbol",
                        {"symbol": "AAPL"})
        assert by_pos.to_pylist() == by_name.to_pylist()
        assert by_pos.to_pylist()[0]["n"] >= 1


def test_cancel_before_run_is_not_lost_and_does_not_mislabel_a_later_timeout(settings):
    ctx = parse_context(None)
    manifest = resolve_manifest(settings, ctx, ["silver.listings"])
    with QuerySession(settings, ctx, manifest) as s:
        s.cancel()
        started = time.monotonic()
        with pytest.raises(DomainError) as exc:
            s.run(SLOW, timeout_ms=10_000)
        elapsed = time.monotonic() - started
        assert exc.value.code == "QUERY_CANCELLED"
        assert elapsed < 0.5, f"cancel-before-run should not wait out the budget, took {elapsed}s"

        # The cancel was consumed above; a fresh timeout on the same session must not inherit it.
        with pytest.raises(DomainError) as exc:
            s.run(SLOW, timeout_ms=300)
        assert exc.value.code == "QUERY_BUDGET_EXCEEDED"


def test_bind_error_is_query_rejected_for_run_and_describe(settings):
    ctx = parse_context(None)
    with QuerySession(settings, ctx, resolve_manifest(settings, ctx, ["silver.listings"])) as s:
        with pytest.raises(DomainError) as exc:
            s.run("SELECT no_such_column FROM silver.listings")
        assert exc.value.code == "QUERY_REJECTED"
        assert exc.value.message == "the statement could not be bound"
        assert exc.value.details["duckdb"]

        with pytest.raises(DomainError) as exc:
            s.describe("SELECT no_such_column FROM silver.listings")
        assert exc.value.code == "QUERY_REJECTED"
        assert exc.value.message == "the statement could not be bound"
        assert exc.value.details["duckdb"]


def test_an_external_delta_table_is_reachable_by_manifest(tmp_path):
    bucket = tmp_path / "bucket"
    write_deltalake(str(bucket / "openbb" / "AAPL"),
                    pa.table({"symbol": ["AAPL"], "close": [1.0]}), mode="error")
    s = Settings(root=str(bucket / "security_master"), delta_base=str(bucket), preflight_secret="k")
    seed(s)
    ctx = parse_context({"mode": "delta_snapshot"})
    manifest = resolve_manifest(s, ctx, ["bronze.openbb.AAPL"])
    assert manifest == {"bronze.openbb.AAPL": 0}
    with QuerySession(s, ctx, manifest) as session:
        rows = session.run('SELECT count(*) AS n FROM bronze."openbb.AAPL"').to_pylist()
        assert rows[0]["n"] == 1

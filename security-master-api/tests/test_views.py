# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.resolver.context import parse_context
from security_master_api.resolver.views import GOLD_NAMES, assertions_sql, gold_sql, knowledge_filters
from security_master_api.sql.session import open_session
from security_master_api.store.seed import seed
from security_master_api.store.tables import append


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return s


def test_gold_names_match_the_catalog():
    assert GOLD_NAMES == ("security_master", "price_daily", "corporate_actions",
                          "universe_membership", "market_calendar", "odp_equity_info")


def test_known_at_filter_hides_close_rows_after_cutoff():
    ctx = parse_context({"mode": "known_at", "known_at": "2026-09-15T00:00:00Z"})
    sql = assertions_sql("silver.prices_normalized", ctx)
    assert "system_from <= TIMESTAMPTZ '2026-09-15T00:00:00+00:00'" in sql
    assert "available_at <= TIMESTAMPTZ '2026-09-15T00:00:00+00:00'" in sql
    assert "QUALIFY row_number() OVER (PARTITION BY assertion_id ORDER BY system_from DESC) = 1" in sql


def _closes(settings, ctx):
    with open_session(settings, ctx, ["gold.price_daily"]) as s:
        rows = s.run("SELECT close, assertion_id, capture_id FROM gold.price_daily "
                     "WHERE listing_id = 'lst_apple' AND market_date = DATE '2026-09-14'").to_pylist()
    return rows


def test_trade_correction_at_every_boundary(settings):
    before = parse_context({"mode": "known_at", "known_at": "2026-09-15T09:19:59.999999Z",
                            "effective_at": "2026-09-14T20:00:00Z"})
    at = parse_context({"mode": "known_at", "known_at": "2026-09-15T09:20:00Z",
                        "effective_at": "2026-09-14T20:00:00Z"})
    after = parse_context({"mode": "known_at", "known_at": "2026-09-15T09:20:00.000001Z",
                           "effective_at": "2026-09-14T20:00:00Z"})
    assert [r["close"] for r in _closes(settings, before)] == [231.40]
    assert _closes(settings, before)[0]["capture_id"] == "cap_00421"
    assert [r["close"] for r in _closes(settings, at)] == [231.74]
    assert [r["close"] for r in _closes(settings, after)] == [231.74]
    assert [r["close"] for r in _closes(settings, parse_context(None))] == [231.74]
    none = parse_context({"mode": "known_at", "known_at": "2026-09-14T20:04:59Z",
                          "effective_at": "2026-09-14T20:00:00Z"})
    assert _closes(settings, none) == []


def test_shares_outstanding_boundaries(settings):
    def value(known_at):
        ctx = parse_context({"mode": "known_at", "known_at": known_at})
        with open_session(settings, ctx, ["gold.odp_equity_info"]) as s:
            rows = s.run("SELECT shares_outstanding, filing_kind FROM gold.odp_equity_info "
                         "WHERE symbol = 'EXMP'").to_pylist()
        return rows
    assert value("2026-07-15T00:00:00Z") == [] or value("2026-07-15T00:00:00Z")[0]["shares_outstanding"] is None
    assert value("2026-08-05T12:00:00Z")[0]["shares_outstanding"] == 1.5e9
    assert value("2026-08-19T23:59:59Z")[0]["shares_outstanding"] == 1.5e9
    assert value("2026-08-20T12:00:00Z")[0] == {"shares_outstanding": 1.48e9, "filing_kind": "10-Q/A"}


def test_ticker_change_is_one_listing(settings):
    ctx = parse_context({"mode": "effective_on", "effective_at": "2022-06-07T00:00:00Z"})
    with open_session(settings, ctx, ["gold.security_master"]) as s:
        rows = s.run("SELECT symbol, listing_id FROM gold.security_master WHERE listing_id = 'lst_000042'").to_pylist()
    assert rows == [{"symbol": "FB", "listing_id": "lst_000042"}]
    ctx = parse_context({"mode": "effective_on", "effective_at": "2022-06-10T00:00:00Z"})
    with open_session(settings, ctx, ["gold.security_master"]) as s:
        rows = s.run("SELECT symbol FROM gold.security_master WHERE listing_id = 'lst_000042'").to_pylist()
    assert rows == [{"symbol": "META"}]
    with open_session(settings, parse_context(None), ["gold.price_daily"]) as s:
        n = s.run("SELECT count(*) AS n FROM gold.price_daily WHERE listing_id = 'lst_000042'").to_pylist()
    assert n[0]["n"] == 2


def test_captured_by_knowledge_filters():
    ctx = parse_context({"mode": "captured_by", "known_at": "2026-09-15T00:00:00Z"})
    assert knowledge_filters(ctx) == ("TRUE",
                                      "observed_at <= TIMESTAMPTZ '2026-09-15T00:00:00+00:00'")


def test_corporate_actions_exempt_from_effective_filter(settings):
    # effective_from lands AFTER the effective_on cutoff below: under the effective filter this
    # row would be hidden (effective_from <= effective_at fails), but corporate_actions is an
    # event-shaped relation whose own ex_date is the effective axis, so it must still appear.
    append(settings, "silver.corporate_actions", [{
        "listing_id": "lst_apple",
        "action_type": "split",
        "ex_date": "2026-06-01",
        "ratio": 2.0,
        "amount": None,
        "currency": None,
        "effective_from": "2026-06-01T00:00:00Z",
        "effective_to": None,
        "observed_at": "2026-05-01T00:00:00Z",
        "available_at": "2026-05-01T00:00:00Z",
        "system_from": "2026-05-01T00:00:00Z",
        "system_to": None,
        "capture_id": "cap_test_split",
        "assertion_id": "as_test_split",
        "assertion_status": "current",
        "supersedes_assertion_id": None,
    }])
    ctx = parse_context({"mode": "effective_on", "effective_at": "2026-01-01T00:00:00Z"})
    with open_session(settings, ctx, ["gold.corporate_actions"]) as s:
        rows = s.run("SELECT ex_date FROM gold.corporate_actions "
                     "WHERE assertion_id = 'as_test_split'").to_pylist()
    assert len(rows) == 1
    assert str(rows[0]["ex_date"]) == "2026-06-01"


def test_gold_sql_names_only_declared_dependencies():
    ctx = parse_context(None)
    for name in GOLD_NAMES:
        sql = gold_sql(name, ctx)
        assert "gold." not in sql, name

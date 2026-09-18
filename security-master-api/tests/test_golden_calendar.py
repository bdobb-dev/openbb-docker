# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.resolver.context import parse_context
from security_master_api.sql.session import open_session
from security_master_api.store.seed import seed


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return s


def rows(settings, calendar_id, known_at, start, end):
    ctx = parse_context({"mode": "known_at", "known_at": known_at, "effective_at": f"{start}T00:00:00Z"})
    with open_session(settings, ctx, ["gold.market_calendar"]) as s:
        out = s.run(
            "SELECT session_date, evidence_status, market_effect, authority_type, authority_name, "
            "holiday_family FROM gold.market_calendar WHERE calendar_id = ? "
            "AND session_date BETWEEN ? AND ? ORDER BY session_date",
            [calendar_id, start, end]).to_pylist()
    return {str(r["session_date"]): r for r in out}


def test_eid_before_at_after_religious_verification(settings):
    t1 = "2027-03-09T18:45:00Z"
    before = rows(settings, "cal_tadawul", "2027-03-09T18:44:59.999999Z", "2027-03-01", "2027-03-31")
    assert before["2027-03-10"]["evidence_status"] == "provisional"
    assert before["2027-03-10"]["market_effect"] == "pending"
    assert "2027-03-11" not in before or before["2027-03-11"]["holiday_family"] is None
    for cutoff in (t1, "2027-03-09T18:45:00.000001Z"):
        at = rows(settings, "cal_tadawul", cutoff, "2027-03-01", "2027-03-31")
        assert at["2027-03-11"]["evidence_status"] == "authority_confirmed"
        assert at["2027-03-11"]["market_effect"] == "pending"
        assert at["2027-03-11"]["authority_name"] == "Supreme Court of Saudi Arabia"
        assert "2027-03-10" not in at or at["2027-03-10"]["holiday_family"] is None


def test_eid_exchange_notice_makes_it_market_final(settings):
    t2 = "2027-03-10T08:00:00Z"
    before = rows(settings, "cal_tadawul", "2027-03-10T07:59:59.999999Z", "2027-03-01", "2027-03-31")
    assert before["2027-03-11"]["market_effect"] == "pending"
    for cutoff in (t2, "2027-03-10T08:00:00.000001Z"):
        at = rows(settings, "cal_tadawul", cutoff, "2027-03-01", "2027-03-31")
        assert at["2027-03-11"]["evidence_status"] == "market_final"
        assert at["2027-03-11"]["market_effect"] == "closed"
        assert at["2027-03-11"]["authority_type"] == "exchange"


def test_lunar_new_year_special_session(settings):
    t1 = "2027-01-20T09:00:00Z"
    before = rows(settings, "cal_xhkg", "2027-01-20T08:59:59.999999Z", "2027-02-01", "2027-02-28")
    assert before["2027-02-05"]["market_effect"] == "none"
    assert before.get("2027-02-06", {}).get("market_effect") != "closed"
    at = rows(settings, "cal_xhkg", t1, "2027-02-01", "2027-02-28")
    assert at["2027-02-05"]["market_effect"] == "early_close"
    assert at["2027-02-06"]["market_effect"] == "closed"
    assert at["2027-02-08"]["market_effect"] == "closed"


def test_religious_observance_never_closes_the_exchange(settings):
    before = rows(settings, "cal_xtae", "2027-05-01T12:00:00Z", "2027-05-01", "2027-05-31")
    assert before["2027-05-27"]["evidence_status"] == "authority_confirmed"
    assert before["2027-05-27"]["market_effect"] == "pending"
    at = rows(settings, "cal_xtae", "2027-05-02T00:00:00Z", "2027-05-01", "2027-05-31")
    assert at["2027-05-27"]["market_effect"] == "none"


def test_india_bakri_eid_correction(settings):
    early = rows(settings, "cal_xnse", "2023-06-01T00:00:00Z", "2023-06-27", "2023-06-30")
    assert early["2023-06-28"]["market_effect"] == "closed"
    mid = rows(settings, "cal_xnse", "2023-06-26T12:00:00Z", "2023-06-27", "2023-06-30")
    assert mid["2023-06-29"]["evidence_status"] == "authority_confirmed"
    assert mid["2023-06-29"]["authority_type"] == "government"
    assert mid["2023-06-29"]["market_effect"] == "pending"
    late = rows(settings, "cal_xnse", "2023-06-27T12:00:00Z", "2023-06-27", "2023-06-30")
    assert late["2023-06-28"]["market_effect"] == "open"
    assert late["2023-06-29"]["market_effect"] == "closed"


def session_status_current(settings, calendar_id, session_date):
    ctx = parse_context({"mode": "current_corrected"})
    with open_session(settings, ctx, ["gold.market_calendar"]) as s:
        out = s.run(
            "SELECT session_status FROM gold.market_calendar WHERE calendar_id = ? "
            "AND session_date = ?", [calendar_id, session_date]).to_pylist()
    return out[0]["session_status"] if out else None


def test_confirmed_open_day_has_open_session_status(settings):
    assert session_status_current(settings, "cal_xnse", "2023-06-28") == "open"
    assert session_status_current(settings, "cal_xdfm", "2024-04-15") == "open"


def test_dubai_branches_are_retained(settings):
    ctx = parse_context({"mode": "known_at", "known_at": "2024-04-08T20:00:05Z",
                         "effective_at": "2024-04-08T00:00:00Z"})
    with open_session(settings, ctx, ["gold.market_calendar"]) as s:
        out = s.run("SELECT session_date, market_effect, candidate_branches, selected_branch "
                    "FROM gold.market_calendar WHERE calendar_id = 'cal_xdfm' "
                    "AND session_date BETWEEN DATE '2024-04-08' AND DATE '2024-04-15' "
                    "ORDER BY session_date").to_pylist()
    by = {str(r["session_date"]): r for r in out}
    assert by["2024-04-15"]["market_effect"] == "open"
    assert by["2024-04-12"]["market_effect"] == "closed"
    assert "2024-04-09" in by["2024-04-08"]["candidate_branches"]
    assert by["2024-04-15"]["selected_branch"] is not None

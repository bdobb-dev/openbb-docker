# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import parse_context
from security_master_api.resolver.odp import model, project, registry
from security_master_api.store.seed import golden_fixtures, seed

# The golden fixtures name the resolver operation, not the ODP model that serves it:
# `resolve` is what ReferenceResolve projects, and `lineage` has no ODP model at all.
FIXTURE_MODELS = {"resolve": "ReferenceResolve"}


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return s


def test_registry_has_the_six_models_and_scenarios():
    models = {m["model_id"]: m for m in registry()}
    assert set(models) == {"EquityInfo", "EquitySearch", "EquityHistorical", "ReferenceSecurity",
                           "ReferenceResolve", "MarketCalendar"}
    assert models["MarketCalendar"]["primary_temporal_scenario"] == "lunar_holiday_correction"
    assert models["MarketCalendar"]["kind"] == "custom"
    assert len(models["MarketCalendar"]["fields"]) == 34
    assert model("EquityInfo")["route"] == "/api/v1/equity/profile"


def test_unknown_model_and_unsupported_mode(settings):
    with pytest.raises(DomainError) as exc:
        model("Nope")
    assert exc.value.code == "QUERY_REJECTED"
    ctx = parse_context({"mode": "known_at", "known_at": "2026-01-01T00:00:00Z"})
    with pytest.raises(DomainError) as exc:
        project(settings, ctx, "EquitySearch", {"query": "A"}, "req")
    assert exc.value.code == "TEMPORAL_MODE_UNSUPPORTED"


def test_missing_required_parameter_fails_before_duckdb(settings):
    with pytest.raises(DomainError) as exc:
        project(settings, parse_context(None), "EquityHistorical", {"symbol": "AAPL"}, "req")
    assert exc.value.code == "QUERY_REJECTED" and "start_date" in exc.value.message


def test_every_fixture_expectation_holds(settings):
    known = {m["model_id"] for m in registry()}
    for fixture in golden_fixtures():
        for exp in fixture["expectations"]:
            model_id = FIXTURE_MODELS.get(exp.get("model"), exp.get("model"))
            if model_id not in known:
                continue
            ctx = parse_context(exp["context"])
            if "error" in exp:
                with pytest.raises(DomainError) as exc:
                    project(settings, ctx, model_id, exp["params"], "req")
                assert exc.value.code == exp["error"], (fixture["fixture_id"], exp)
                continue
            out = project(settings, ctx, model_id, exp["params"], "req")
            rows = out["results"]
            assert out["receipt"]["temporal_mode"] == ctx.mode
            for absent in exp.get("absent_row_dates", []):
                assert absent not in {str(r["session_date"]) for r in rows}, \
                    (fixture["fixture_id"], exp, absent)
            if "row_count" in exp["expect"]:
                assert len(rows) == exp["expect"]["row_count"], (fixture["fixture_id"], exp)
                continue
            if "row_date" in exp:
                row = {str(r["session_date"]): r for r in rows}[exp["row_date"]]
            else:
                row = rows[0]
            for key, value in exp["expect"].items():
                assert json.loads(json.dumps(row[key], default=str)) == value, \
                    (fixture["fixture_id"], exp, key)


def test_market_calendar_projection_and_receipt(settings):
    ctx = parse_context({"mode": "known_at", "known_at": "2027-03-10T08:00:00Z",
                         "effective_at": "2027-04-01T00:00:00Z"})
    out = project(settings, ctx, "MarketCalendar",
                  {"calendar_id": "cal_tadawul", "start_date": "2027-03-01",
                   "end_date": "2027-04-30", "include_closed": True}, "req")
    row = next(r for r in out["results"] if str(r["session_date"]) == "2027-03-11")
    assert row["market_effect"] == "closed" and row["evidence_status"] == "market_final"
    assert row["interruptions"] == []
    deps = out["receipt"]["dependencies"]
    assert any(d["relation"] == "silver.calendar_exceptions" for d in deps)


def test_equity_search_matches_symbol_and_name(settings):
    out = project(settings, parse_context(None), "EquitySearch", {"query": "MET"}, "req")
    assert "lst_000042" in {r["listing_id"] for r in out["results"]}
    assert [c["name"] for c in out["columns"]][:2] == ["symbol", "name"]


def test_include_closed_drops_closed_sessions(settings):
    # current_corrected, not known_at: under known_at a range whose every session is closed
    # empties the projection, and an empty known_at result is NO_LOCAL_EVIDENCE by design.
    ctx = parse_context(None)
    params = {"calendar_id": "cal_tadawul", "start_date": "2027-03-01", "end_date": "2027-03-31"}
    closed = project(settings, ctx, "MarketCalendar", {**params, "include_closed": True}, "req")
    assert "2027-03-11" in {str(r["session_date"]) for r in closed["results"]}
    open_only = project(settings, ctx, "MarketCalendar", params, "req")
    assert "2027-03-11" not in {str(r["session_date"]) for r in open_only["results"]}


def test_market_calendar_needs_an_unambiguous_calendar(settings):
    with pytest.raises(DomainError) as exc:
        project(settings, parse_context(None), "MarketCalendar",
                {"start_date": "2027-03-01", "end_date": "2027-03-02"}, "req")
    assert exc.value.code == "IDENTITY_AMBIGUOUS"
    out = project(settings, parse_context(None), "MarketCalendar",
                  {"mic": "XSAU", "start_date": "2027-03-01", "end_date": "2027-03-31",
                   "include_closed": True}, "req")
    assert out["results"]


def test_reference_security_binds_a_candidate_without_a_listing(settings):
    # META has a listing, so the listing_id bind answers. Both of the listing's effective
    # intervals come back: the view carries identity history, and only an effective_on context
    # narrows it to one.
    out = project(settings, parse_context(None), "ReferenceSecurity", {"identifier": "META"}, "req")
    assert {r["symbol"] for r in out["results"]} == {"FB", "META"}
    assert {r["listing_id"] for r in out["results"]} == {"lst_000042"}
    # The cusip fixture carries no listing at all: the candidate's listing_id is null and the
    # security_id bind has to carry the filter rather than failing to bind.
    ctx = parse_context({"mode": "effective_on", "effective_at": "2026-06-30T00:00:00Z"})
    assert project(settings, ctx, "ReferenceSecurity", {"identifier": "000111AA1"},
                   "req")["results"] == []

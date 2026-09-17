# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from security_master_api.temporal_fixture import load_temporal_fixture, state_at

FIXTURES = Path(__file__).parent / "fixtures"


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.parametrize(
    ("filename", "expected_stages"),
    [
        ("india_bakri_eid_2023.json", ["T0", "T1", "T2"]),
        ("dubai_eid_al_fitr_2024.json", ["T0", "T1", "T2"]),
    ],
)
def test_state_at_is_inclusive_at_every_transition(filename, expected_stages):
    fixture = load_temporal_fixture(FIXTURES / filename)
    previous_state = None

    for expected, expected_stage in zip(fixture["expected_states"], expected_stages):
        cutoff = instant(expected["known_from"])
        assert state_at(fixture, cutoff - timedelta(microseconds=1)) is previous_state
        assert state_at(fixture, cutoff)["stage"] == expected_stage
        assert state_at(fixture, cutoff + timedelta(microseconds=1))["stage"] == expected_stage
        previous_state = expected


def test_india_fixture_states():
    fixture = load_temporal_fixture(FIXTURES / "india_bakri_eid_2023.json")
    india_t1 = state_at(fixture, "2023-06-26T12:00:00Z")
    india_t2 = state_at(fixture, "2023-06-27T12:00:00Z")

    assert india_t1["authority_status"] == "confirmed_by_government"
    assert india_t1["market_status"] == "pending"
    assert india_t2["market_dates"] == [
        {"date": "2023-06-28", "session": "open"},
        {"date": "2023-06-29", "session": "closed"},
    ]
    assert india_t2["expiry_date"] == "2023-06-28"
    assert india_t2["settlement_status"] == "revised"


def test_dubai_fixture_states():
    fixture = load_temporal_fixture(FIXTURES / "dubai_eid_al_fitr_2024.json")
    dubai_t0 = state_at(fixture, "2024-04-04T12:00:00Z")
    dubai_t1 = state_at(fixture, "2024-04-08T20:00:00Z")
    dubai_t2 = state_at(fixture, "2024-04-08T20:00:05Z")

    assert dubai_t0["selected_branch"] is None
    assert len(dubai_t0["candidate_branches"]) == 2
    assert dubai_t1["market_status"] == "pending_resolution"
    assert dubai_t2["selected_branch"] == {
        "condition": "eid_first_day=2024-04-10",
        "reopen_date": "2024-04-15",
    }
    assert len(dubai_t2["candidate_branches"]) == 2
    assert dubai_t2["market_dates"] == [
        {"date": "2024-04-12", "session": "closed"},
        {"date": "2024-04-15", "session": "open"},
    ]


def valid_fixture() -> dict:
    return {
        "schema": "security-master.temporal-fixture.v1",
        "fixture_id": "validation-fixture",
        "capture_time_semantics": "synthetic_test_capture",
        "exchange_mic": "XTEST",
        "exchange_name": "Test Exchange",
        "holiday_family": "Test Holiday",
        "events": [
            {
                "event_id": "event-1",
                "published_on": "2024-01-01",
                "known_from": "2024-01-01T00:00:00Z",
                "source_kind": "exchange_notice",
                "authority": "Test Exchange",
                "source_url": "https://example.test/notice",
                "assertion": "A test assertion."
            }
        ],
        "expected_states": [
            {
                "stage": "T0",
                "known_from": "2024-01-01T00:00:00Z",
                "evidence_event_ids": ["event-1"]
            }
        ]
    }


def write_fixture(tmp_path, fixture):
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    return path


@pytest.mark.parametrize("field", ["schema", "fixture_id", "events", "expected_states"])
def test_loader_rejects_missing_required_top_level_field(tmp_path, field):
    fixture = valid_fixture()
    del fixture[field]

    with pytest.raises(ValueError, match="missing required field"):
        load_temporal_fixture(write_fixture(tmp_path, fixture))


def test_loader_rejects_naive_timestamp(tmp_path):
    fixture = valid_fixture()
    fixture["events"][0]["known_from"] = "2024-01-01T00:00:00"

    with pytest.raises(ValueError, match="timezone-aware"):
        load_temporal_fixture(write_fixture(tmp_path, fixture))


def test_loader_rejects_non_http_source_url(tmp_path):
    fixture = valid_fixture()
    fixture["events"][0]["source_url"] = "file:///private/notice"

    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        load_temporal_fixture(write_fixture(tmp_path, fixture))


@pytest.mark.parametrize("second_known_from", ["2024-01-01T00:00:00Z", "2023-12-31T23:59:59Z"])
def test_loader_rejects_duplicate_or_descending_state_times(tmp_path, second_known_from):
    fixture = valid_fixture()
    second_state = deepcopy(fixture["expected_states"][0])
    second_state["stage"] = "T1"
    second_state["known_from"] = second_known_from
    fixture["expected_states"].append(second_state)

    with pytest.raises(ValueError, match="strictly increasing"):
        load_temporal_fixture(write_fixture(tmp_path, fixture))


def test_loader_rejects_unknown_evidence_event_id(tmp_path):
    fixture = valid_fixture()
    fixture["expected_states"][0]["evidence_event_ids"] = ["unknown-event"]

    with pytest.raises(ValueError, match="unknown event"):
        load_temporal_fixture(write_fixture(tmp_path, fixture))


def test_state_at_rejects_naive_cutoff():
    with pytest.raises(ValueError, match="timezone-aware"):
        state_at(valid_fixture(), datetime(2024, 1, 1, tzinfo=None))


def test_state_at_accepts_aware_datetime():
    fixture = valid_fixture()
    assert state_at(fixture, datetime(2024, 1, 1, tzinfo=timezone.utc))["stage"] == "T0"


def test_india_event_provenance_is_not_religious_authority_evidence():
    fixture = load_temporal_fixture(FIXTURES / "india_bakri_eid_2023.json")
    events = {event["event_id"]: event for event in fixture["events"]}

    assert events["india-t0-exchange-calendar"]["source_kind"] == "exchange_calendar"
    assert events["india-t1-government-notice"]["source_kind"] == "government_notice"
    assert events["india-t2-exchange-clearing-notice"]["source_kind"] == "exchange_clearing_notice"
    assert events["india-t1-government-notice"]["source_url"] == (
        "http://sppudocs.unipune.ac.in/sites/circulars/Administrative%20Circulars"
        "%20%20Non%20Teaching/Circular%20No.125-2023_27062023.pdf"
    )


def test_dubai_event_provenance_and_stable_ids():
    fixture = load_temporal_fixture(FIXTURES / "dubai_eid_al_fitr_2024.json")
    events = {event["event_id"]: event for event in fixture["events"]}

    assert events["dubai-t0-conditional-exchange-circular"]["source_kind"] == (
        "conditional_exchange_notice"
    )
    assert events["dubai-t1-moon-sighting-confirmation"]["source_kind"] == (
        "religious_authority_notice"
    )
    assert events["dubai-t2-derived-branch-selection"]["source_kind"] == (
        "derived_calendar_resolution"
    )


def test_loader_rejects_duplicate_event_id(tmp_path):
    fixture = valid_fixture()
    fixture["events"].append(deepcopy(fixture["events"][0]))

    with pytest.raises(ValueError, match="duplicate event_id"):
        load_temporal_fixture(write_fixture(tmp_path, fixture))


def test_loader_rejects_evidence_known_after_its_state(tmp_path):
    fixture = valid_fixture()
    fixture["events"][0]["known_from"] = "2024-01-01T00:00:01Z"

    with pytest.raises(ValueError, match="later than state known_from"):
        load_temporal_fixture(write_fixture(tmp_path, fixture))

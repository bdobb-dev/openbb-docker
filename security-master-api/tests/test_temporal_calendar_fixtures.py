# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from security_master_api import load_temporal_fixture, state_at

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(params=["india_bakri_eid_2023.json", "dubai_eid_al_fitr_2024.json"])
def temporal_fixture(request: pytest.FixtureRequest) -> dict:
    return load_temporal_fixture(FIXTURE_DIR / request.param)


def test_cutoffs_are_inclusive_and_do_not_leak_future_states(temporal_fixture: dict) -> None:
    states = temporal_fixture["expected_states"]
    for index, expected in enumerate(states):
        cutoff = datetime.fromisoformat(expected["known_from"].replace("Z", "+00:00"))
        before = state_at(temporal_fixture, cutoff - timedelta(microseconds=1))
        exact = state_at(temporal_fixture, cutoff)
        after = state_at(temporal_fixture, cutoff + timedelta(microseconds=1))

        if index == 0:
            assert before is None
        else:
            assert before["stage"] == states[index - 1]["stage"]
        assert exact["stage"] == expected["stage"]
        assert after["stage"] == expected["stage"]


def test_india_preserves_government_and_exchange_decisions_as_separate_events() -> None:
    fixture = load_temporal_fixture(FIXTURE_DIR / "india_bakri_eid_2023.json")
    events = {event["event_id"]: event for event in fixture["events"]}

    assert events["india-t1-government-notice"]["source_kind"] == "government_notice"
    assert events["india-t1-government-notice"]["source_kind"] != "religious_authority_notice"

    authority_state = state_at(fixture, "2023-06-26T12:00:00Z")
    assert authority_state["authority_status"] == "confirmed_by_government"
    assert authority_state["market_status"] == "pending"
    assert authority_state["expiry_date"] == "pending"

    final_state = state_at(fixture, "2023-06-27T12:00:00Z")
    assert final_state["market_dates"] == [
        {"date": "2023-06-28", "session": "open"},
        {"date": "2023-06-29", "session": "closed"},
    ]
    assert final_state["expiry_date"] == "2023-06-28"
    assert final_state["settlement_status"] == "revised"


def test_dubai_selects_the_exchange_branch_only_after_authority_confirmation() -> None:
    fixture = load_temporal_fixture(FIXTURE_DIR / "dubai_eid_al_fitr_2024.json")

    projected = state_at(fixture, "2024-04-04T12:00:00Z")
    assert len(projected["candidate_branches"]) == 2
    assert projected["selected_branch"] is None
    assert projected["market_status"] == "conditional"

    confirmed = state_at(fixture, "2024-04-08T20:00:00Z")
    assert confirmed["authority_status"] == "confirmed_by_religious_authority"
    assert confirmed["selected_branch"] is None
    assert confirmed["market_status"] == "pending_resolution"

    resolved = state_at(fixture, "2024-04-08T20:00:05Z")
    assert resolved["selected_branch"] == {
        "condition": "eid_first_day=2024-04-10",
        "reopen_date": "2024-04-15",
    }
    assert resolved["market_status"] == "final"
    assert len(resolved["candidate_branches"]) == 2


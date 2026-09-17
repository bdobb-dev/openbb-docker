# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime, timedelta

import pytest

from security_master_api.config import Settings
from security_master_api.resolver.context import parse_context
from security_master_api.sql.session import open_session
from security_master_api.store.jobs import (
    STAGES,
    TERMINAL,
    append_event,
    by_fingerprint,
    claim,
    create_job,
    events,
    job,
    queued,
)
from security_master_api.store.seed import seed
from security_master_api.store.tables import append


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return s


def test_stage_tables():
    assert STAGES[0] == "queued" and STAGES[-1] == "gold_ready"
    assert "cancel_pending" in TERMINAL and "gold_ready" in TERMINAL


def test_create_is_idempotent_by_fingerprint(settings):
    j = create_job(settings, "price_daily", {"symbols": ["AAPL"]}, "fp1", "th")
    assert j["state"] == "queued" and j["job_id"].startswith("job_")
    again = create_job(settings, "price_daily", {"symbols": ["AAPL"]}, "fp1", "th")
    assert again["job_id"] == j["job_id"]
    assert by_fingerprint(settings, "fp1")["job_id"] == j["job_id"]
    assert [q["job_id"] for q in queued(settings)] == [j["job_id"]]


def test_events_are_monotonic_and_drive_state(settings):
    j = create_job(settings, "price_daily", {}, "fp2", "th")
    assert claim(settings, j["job_id"], "w1") is True
    assert claim(settings, j["job_id"], "w2") is False
    append_event(settings, j["job_id"], "bronze_retained", {"captures": 1}, "w1")
    summary = job(settings, j["job_id"])
    assert summary["state"] == "bronze_retained"
    assert [e["stage"] for e in events(settings, j["job_id"])] == [
        "queued", "fetching", "bronze_retained"]
    assert [e["seq"] for e in events(settings, j["job_id"])] == [0, 1, 2]
    assert queued(settings) == []


def test_terminal_then_new_job_for_same_fingerprint(settings):
    j = create_job(settings, "price_daily", {}, "fp3", "th")
    append_event(settings, j["job_id"], "failed", {"error": "x"})
    assert job(settings, j["job_id"])["state"] == "failed"
    j2 = create_job(settings, "price_daily", {}, "fp3", "th")
    assert j2["job_id"] != j["job_id"]


def test_the_job_row_itself_records_the_terminal_state_and_summary(settings):
    """Read `ops.jobs` directly: a reader that never replays the event log must still be able
    to see how the job ended."""
    j = create_job(settings, "price_daily", {}, "fp5", "th")
    append_event(settings, j["job_id"], "failed", {"error": "x"})
    with open_session(settings, parse_context({"mode": "delta_snapshot"}), ["ops.jobs"]) as s:
        rows = s.run("SELECT state, summary, updated_at FROM ops.jobs WHERE job_id = ? "
                     "ORDER BY seq DESC LIMIT 1", [j["job_id"]]).to_pylist()
    assert rows[0]["state"] == "failed"
    assert rows[0]["summary"] == '{"error": "x"}'
    assert job(settings, j["job_id"])["summary"] == {"error": "x"}


def test_tied_seq_is_ordered_by_time_not_by_event_id(settings):
    """A second writer minting the same `seq` must not be able to sort itself first by having
    drawn a smaller uuid."""
    j = create_job(settings, "price_daily", {}, "fp6", "th")
    tied = append_event(settings, j["job_id"], "normalizing", {}, "w1")
    append(settings, "ops.job_events", [{
        "event_id": "ev_000000000000", "job_id": j["job_id"], "seq": tied["seq"],
        "stage": "validating", "at": datetime.now(UTC) + timedelta(seconds=5), "detail": "{}",
        "worker_id": "w2"}])
    assert [e["stage"] for e in events(settings, j["job_id"])] == [
        "queued", "normalizing", "validating"]
    assert job(settings, j["job_id"])["state"] == "validating"

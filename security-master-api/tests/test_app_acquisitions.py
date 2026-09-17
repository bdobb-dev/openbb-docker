# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest
from fastapi.testclient import TestClient

from security_master_api.app.main import create_app
from security_master_api.config import Settings
from security_master_api.store.jobs import append_event
from security_master_api.store.seed import seed
from tests.test_app_routes import V1, record

REQ = {"dataset": "price_daily", "identifiers": ["AAPL"], "start_date": "2026-09-01",
       "end_date": "2026-09-16", "policy": "missing_only", "price_basis": "raw"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "false")
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return TestClient(create_app(s))


def test_preflight_then_create_then_get_then_cancel(client):
    pf = client.post(f"{V1}/acquisitions/preflight", json={"request": REQ, "context": {}})
    assert pf.status_code == 200 and pf.json()["token"]
    record("acquisition_preflight", pf.json())
    created = client.post(f"{V1}/acquisitions", json={"request": REQ, "token": pf.json()["token"]})
    assert created.status_code == 201
    job = created.json()
    assert job["state"] == "queued" and job["kind"] == "price_daily"
    record("acquisition_job", job)
    got = client.get(f"{V1}/acquisitions/{job['job_id']}").json()
    assert got["job_id"] == job["job_id"] and got["stage_history"][0]["stage"] == "queued"
    dup = client.post(f"{V1}/acquisitions", json={"request": REQ, "token": pf.json()["token"]})
    assert dup.status_code == 200 and dup.json()["job_id"] == job["job_id"]
    c = client.post(f"{V1}/acquisitions/{job['job_id']}/cancel").json()
    assert c["state"] == "cancel_pending"
    record("acquisition_cancel", c)


def test_modified_or_missing_token_is_refused(client):
    pf = client.post(f"{V1}/acquisitions/preflight", json={"request": REQ, "context": {}}).json()
    r = client.post(f"{V1}/acquisitions",
                    json={"request": {**REQ, "end_date": "2026-09-17"}, "token": pf["token"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "QUERY_REJECTED"
    r = client.post(f"{V1}/acquisitions", json={"request": REQ})
    assert r.status_code == 422


def test_cancelling_a_terminal_job_is_refused(client):
    pf = client.post(f"{V1}/acquisitions/preflight", json={"request": REQ, "context": {}}).json()
    job = client.post(f"{V1}/acquisitions", json={"request": REQ, "token": pf["token"]}).json()
    append_event(client.app.state.settings, job["job_id"], "failed", {"error": "stub"})
    r = client.post(f"{V1}/acquisitions/{job['job_id']}/cancel")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "QUERY_REJECTED"
    assert r.json()["error"]["details"]["state"] == "failed"


def test_acquisition_disabled_by_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "false")
    s = Settings(root=str(tmp_path), preflight_secret="k", acquisition_policy="disabled")
    seed(s)
    c = TestClient(create_app(s))
    r = c.post(f"{V1}/acquisitions/preflight", json={"request": REQ, "context": {}})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ACQUISITION_REVIEW_REQUIRED"


def test_events_stream_replays_history(client):
    pf = client.post(f"{V1}/acquisitions/preflight", json={"request": REQ, "context": {}}).json()
    job = client.post(f"{V1}/acquisitions", json={"request": REQ, "token": pf["token"]}).json()
    append_event(client.app.state.settings, job["job_id"], "failed", {"error": "stub"})
    with client.stream("GET", f"{V1}/acquisitions/{job['job_id']}/events",
                       params={"replay": "1"}) as r:
        assert r.status_code == 200
        text = "".join(r.iter_text())
    assert "event: queued" in text and "event: failed" in text


def test_read_through_lookup_reports_review_required(client):
    r = client.post(f"{V1}/resolve",
                    json={"identifier": "ZZZZ", "context": {}, "lookup_policy": "review"})
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "ACQUISITION_REVIEW_REQUIRED"
    assert body["error"]["details"]["preflight"]["request"]["dataset"] == "security_lookup"
    record("error_review_required", body)

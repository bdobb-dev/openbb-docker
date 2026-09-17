# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import hashlib
import json

import pytest
import requests

from security_master_api.acquire.client import OpenbbClient
from security_master_api.acquire.preflight import fingerprint_of, preflight
from security_master_api.acquire.worker import run_once
from security_master_api.config import Settings
from security_master_api.resolver.context import parse_context
from security_master_api.sql.session import open_session
from security_master_api.store.jobs import append_event, create_job, job
from security_master_api.store.seed import seed
from security_master_api.store.tables import history, latest_version, open_table
from tests.stub_openbb import StubOpenbb

REQ = {"dataset": "price_daily", "identifiers": ["AAPL"], "start_date": "2026-09-15",
       "end_date": "2026-09-16", "policy": "missing_only", "price_basis": "raw"}
PRICES = {"results": [{"date": "2026-09-15", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
                       "volume": 10},
                      {"date": "2026-09-16", "open": 1, "high": 2, "low": 0.5, "close": 1.6,
                       "volume": 11}]}


@pytest.fixture
def stub():
    s = StubOpenbb()
    yield s
    s.close()


@pytest.fixture
def settings(tmp_path, stub):
    s = Settings(root=str(tmp_path), preflight_secret="k", openbb_url=stub.url,
                 openbb_username="u", openbb_password="p")
    seed(s)
    return s


def make_job(settings):
    return create_job(settings, "price_daily", REQ, fingerprint_of(settings, REQ), "th")


def test_happy_path_reaches_gold_ready(settings, stub):
    stub.routes["/api/v1/equity/price/historical"] = (200, PRICES, {})
    j = make_job(settings)
    assert run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1") == 1
    done = job(settings, j["job_id"])
    assert [s["stage"] for s in done["stage_history"]] == [
        "queued", "fetching", "bronze_retained", "normalizing", "validating", "silver_ready",
        "materializing", "gold_ready"]
    assert stub.requests[0][1]["provider"] == "eodhd" and stub.requests[0][1]["symbol"] == "AAPL.US"
    caps = open_table(settings, "bronze.source_captures").to_pyarrow_table().to_pylist()
    assert any(c["job_id"] == j["job_id"] and c["status"] == 200 for c in caps)
    with open_session(settings, parse_context(None), ["gold.price_daily"]) as s:
        rows = s.run("SELECT market_date, close FROM gold.price_daily WHERE listing_id = 'lst_apple' "
                     "AND market_date >= DATE '2026-09-15' ORDER BY market_date").to_pylist()
    assert [r["close"] for r in rows] == [1.5, 1.6]
    assert done["summary"]["dependencies"]
    assert run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1") == 0


def test_rate_limit_and_entitlement_are_terminal_with_bronze_kept(settings, stub):
    stub.routes["/api/v1/equity/price/historical"] = (429, {"detail": "slow down"},
                                                      {"Retry-After": "30"})
    j = make_job(settings)
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    done = job(settings, j["job_id"])
    assert done["state"] == "rate_limited" and done["stage_history"][-1]["detail"]["retry_after_s"] == 30
    caps = open_table(settings, "bronze.source_captures").to_pyarrow_table().to_pylist()
    assert any(c["job_id"] == j["job_id"] and c["status"] == 429 for c in caps)
    stub.routes["/api/v1/equity/price/historical"] = (403, {"detail": "plan"}, {})
    j2 = create_job(settings, "price_daily", {**REQ, "end_date": "2026-09-17"},
                    fingerprint_of(settings, {**REQ, "end_date": "2026-09-17"}), "th")
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    assert job(settings, j2["job_id"])["state"] == "entitlement_denied"


def test_validation_failure_keeps_bronze(settings, stub):
    stub.routes["/api/v1/equity/price/historical"] = (
        200, {"results": [{"date": "2026-09-15", "open": 1, "high": 0.1, "low": 0.5, "close": 1,
                           "volume": 1}]}, {})
    j = make_job(settings)
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    done = job(settings, j["job_id"])
    assert done["state"] == "validation_failed" and done["stage_history"][-1]["detail"]["problems"]
    caps = open_table(settings, "bronze.source_captures").to_pyarrow_table().to_pylist()
    assert any(c["job_id"] == j["job_id"] for c in caps)


def test_malformed_payload_fails_cleanly(settings, stub):
    """A 200 body that doesn't parse as JSON - or has no `results` container at all - is not
    the provider saying nothing; it's a shape the worker can't read. That's a validation
    failure, distinct from a well-formed but empty answer (see the `partial` test below).
    Bronze still keeps the raw evidence."""
    stub.routes["/api/v1/equity/price/historical"] = (200, "not json{", {})
    j = make_job(settings)
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    done = job(settings, j["job_id"])
    assert done["state"] == "validation_failed"
    assert done["stage_history"][-1]["detail"]["problems"] == ["malformed payload"]
    caps = open_table(settings, "bronze.source_captures").to_pyarrow_table().to_pylist()
    assert any(c["job_id"] == j["job_id"] for c in caps)


def test_empty_provider_answer_ends_partial_not_validation_failed(settings, stub):
    stub.routes["/api/v1/equity/price/historical"] = (200, {"results": []}, {})
    j = make_job(settings)
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    done = job(settings, j["job_id"])
    assert done["state"] == "partial"
    assert done["stage_history"][-1]["detail"] == {
        "problems": ["no rows"], "captures": done["stage_history"][-1]["detail"]["captures"]}
    assert len(done["stage_history"][-1]["detail"]["captures"]) == 1


def test_cancel_pending_is_honoured_between_requests(settings, stub):
    stub.routes["/api/v1/equity/price/historical"] = (200, PRICES, {})
    two = {**REQ, "identifiers": ["AAPL", "META"], "start_date": "2022-06-01",
           "end_date": "2026-09-16"}
    j = create_job(settings, "price_daily", two, fingerprint_of(settings, two), "th")
    append_event(settings, j["job_id"], "cancel_pending", {})
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    assert job(settings, j["job_id"])["state"] == "cancelled"


def test_restart_safety_a_claimed_job_is_not_reclaimed(settings, stub):
    stub.routes["/api/v1/equity/price/historical"] = (200, PRICES, {})
    j = make_job(settings)
    append_event(settings, j["job_id"], "fetching", {}, "w-dead")
    assert run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1") == 0
    assert job(settings, j["job_id"])["state"] == "fetching"


def test_openbb_api_is_behind_basic_auth_and_the_client_carries_it(stub):
    """openbb-api is credentialed; an unauthenticated GET must be refused, and the worker's
    only client must be the thing that presents the pair."""
    stub.routes["/probe"] = (200, {"ok": 1}, {})
    assert requests.get(f"{stub.url}/probe", timeout=5).status_code == 401
    assert OpenbbClient(stub.url, "u", "p").get("/probe", {}).status == 200


def test_refresh_closes_the_assertion_it_replaces(settings, stub):
    """`refresh` re-states a day the store already holds: the old assertion must be closed and
    the new one must name it, or Gold would show two current rows for one market date."""
    stub.routes["/api/v1/equity/price/historical"] = (
        200, {"results": [{"date": "2026-09-14", "open": 230.1, "high": 232.0, "low": 229.8,
                           "close": 999.0, "volume": 51230000}]}, {})
    req = {**REQ, "start_date": "2026-09-14", "end_date": "2026-09-14", "policy": "refresh"}
    j = create_job(settings, "price_daily", req, fingerprint_of(settings, req), "th")
    before = latest_version(settings, "silver.prices_normalized")
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    assert job(settings, j["job_id"])["state"] == "gold_ready"
    with open_session(settings, parse_context(None), ["gold.price_daily"]) as s:
        rows = s.run("SELECT close, supersedes_assertion_id FROM gold.price_daily "
                     "WHERE listing_id = 'lst_apple' AND market_date = DATE '2026-09-14'"
                     ).to_pylist()
    assert [r["close"] for r in rows] == [999.0]
    assert rows[0]["supersedes_assertion_id"] == "as_px_apple_20260914_2"
    # The close and the new row land in ONE commit: two commits would leave a window where the
    # old row is closed and the new one is not yet written - the day missing from Gold.
    after = latest_version(settings, "silver.prices_normalized")
    assert after == before + 1
    assert history(settings, "silver.prices_normalized")[0]["version"] == after


def test_missing_ranges_collapse_to_one_call_per_listing(settings, stub):
    """A gap in the store can split one listing's request into several missing ranges; the
    worker must still cost one HTTP call for that listing, matching preflight's quote."""
    stub.routes["/api/v1/equity/price/historical"] = (200, PRICES, {})
    req = {**REQ, "start_date": "2026-09-01", "end_date": "2026-09-16"}
    pf = preflight(settings, parse_context(None), req)
    assert len(pf["missing_ranges"]) == 2 and pf["expected_requests"] == 1
    j = create_job(settings, "price_daily", req, fingerprint_of(settings, req), "th")
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    assert job(settings, j["job_id"])["state"] == "gold_ready"
    price_reqs = [q for path, q in stub.requests if path == "/api/v1/equity/price/historical"]
    assert len(price_reqs) == 1
    assert price_reqs[0]["start_date"] == "2026-09-01" and price_reqs[0]["end_date"] == "2026-09-16"


def test_exchange_calendar_request_carries_the_provider(settings, stub):
    stub.routes["/api/v1/reference/exchange_details"] = (
        200, {"results": {"Code": "US", "Timezone": "America/New_York", "ExchangeHolidays": {
            "0": {"Holiday": "Christmas", "Date": "2026-12-25", "Type": "official"}}}}, {})
    req = {"dataset": "exchange_calendar", "exchange_code": "US", "calendar_id": "cal_xnys",
           "policy": "missing_only"}
    j = create_job(settings, "exchange_calendar", req, fingerprint_of(settings, req), "th")
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    assert job(settings, j["job_id"])["state"] == "gold_ready"
    cal_reqs = [q for path, q in stub.requests if path == "/api/v1/reference/exchange_details"]
    assert len(cal_reqs) == 1 and cal_reqs[0]["provider"] == "eodhd" and cal_reqs[0]["code"] == "US"


def test_content_hash_covers_the_truncated_payload_with_a_note_in_request_log(settings, stub):
    """A hash over the full wire body while only 2 MB is stored would describe evidence nobody
    can replay against what is actually on disk."""
    big = {"results": [{"date": "2026-09-15", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
                        "volume": 10, "pad": "x" * 2_200_000}]}
    body = json.dumps(big)
    assert len(body) > 2_000_000
    stub.routes["/api/v1/equity/price/historical"] = (200, big, {})
    j = make_job(settings)
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    caps = [c for c in open_table(settings, "bronze.source_captures").to_pyarrow_table().to_pylist()
           if c["job_id"] == j["job_id"]]
    assert len(caps) == 1
    cap = caps[0]
    assert len(cap["payload"]) == 2_000_000
    assert cap["content_hash"] == hashlib.sha256(cap["payload"].encode()).hexdigest()
    logs = [r for r in open_table(settings, "bronze.request_log").to_pyarrow_table().to_pylist()
           if r["job_id"] == j["job_id"]]
    assert len(logs) == 1
    assert logs[0]["error"] == f"payload truncated to 2000000 bytes (original {len(body)})"


def test_non_2xx_status_populates_request_log_error(settings, stub):
    stub.routes["/api/v1/equity/price/historical"] = (500, {"detail": "boom"}, {})
    j = make_job(settings)
    run_once(settings, OpenbbClient(stub.url, "u", "p"), "w1")
    assert job(settings, j["job_id"])["state"] == "failed"
    logs = [r for r in open_table(settings, "bronze.request_log").to_pyarrow_table().to_pylist()
           if r["job_id"] == j["job_id"]]
    assert len(logs) == 1 and logs[0]["error"] is not None

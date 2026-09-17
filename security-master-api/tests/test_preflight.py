# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import time

import pytest

from security_master_api.acquire.preflight import fingerprint_of, preflight, verify_token
from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import parse_context
from security_master_api.store.seed import seed


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k", preflight_ttl_s=2)
    seed(s)
    return s


REQ = {"dataset": "price_daily", "identifiers": ["AAPL"], "start_date": "2026-09-01",
       "end_date": "2026-09-16", "policy": "missing_only", "price_basis": "raw"}


def test_preflight_resolves_and_computes_missing_ranges(settings):
    out = preflight(settings, parse_context(None), REQ)
    assert out["identities"][0]["listing_id"] == "lst_apple"
    assert out["identities"][0]["provider_symbol"] == "AAPL.US"
    assert out["identities"][0]["issuer_id"] == "iss_apple"
    assert out["missing_ranges"] == [
        {"listing_id": "lst_apple", "start": "2026-09-01", "end": "2026-09-13"},
        {"listing_id": "lst_apple", "start": "2026-09-15", "end": "2026-09-16"}]
    assert out["expected_requests"] == 1
    assert out["policy"] == "review"
    assert out["token"] and out["fingerprint"] == fingerprint_of(settings, REQ)
    claims = verify_token(settings, out["token"], REQ)
    assert claims["fingerprint"] == out["fingerprint"]


def test_token_rejects_modified_request_and_expiry(settings):
    out = preflight(settings, parse_context(None), REQ)
    with pytest.raises(DomainError) as exc:
        verify_token(settings, out["token"], {**REQ, "end_date": "2026-09-17"})
    assert exc.value.code == "QUERY_REJECTED"
    time.sleep(2.1)
    with pytest.raises(DomainError):
        verify_token(settings, out["token"], REQ)


def test_refresh_prices_the_whole_requested_range(settings):
    """`refresh` re-fetches what is already stored, so the gap scan is the wrong price for it."""
    out = preflight(settings, parse_context(None), {**REQ, "policy": "refresh"})
    assert out["missing_ranges"] == [
        {"listing_id": "lst_apple", "start": "2026-09-01", "end": "2026-09-16"}]
    assert out["expected_requests"] == 1


def test_force_takes_the_whole_range_and_drops_calendar_warnings(settings):
    req = {"dataset": "price_daily", "identifiers": ["AAPL"], "start_date": "2027-03-01",
           "end_date": "2027-03-31", "policy": "force", "price_basis": "raw",
           "calendar_id": "cal_tadawul"}
    out = preflight(settings, parse_context({"mode": "known_at",
                                             "known_at": "2027-03-09T19:00:00Z"}), req)
    assert out["missing_ranges"] == [
        {"listing_id": "lst_apple", "start": "2027-03-01", "end": "2027-03-31"}]
    assert out["warnings"] == []


def test_unresolved_identifier_is_reported_not_guessed(settings):
    with pytest.raises(DomainError) as exc:
        preflight(settings, parse_context(None), {**REQ, "identifiers": ["ZZZZ"]})
    assert exc.value.code == "IDENTITY_UNRESOLVED"


@pytest.mark.parametrize("bad", [
    {k: v for k, v in REQ.items() if k != "start_date"},
    {**REQ, "end_date": "not-a-date"},
    {**REQ, "dataset": "sentiment"},
    {**REQ, "policy": "whatever"},
])
def test_a_malformed_request_is_refused_not_crashed(settings, bad):
    with pytest.raises(DomainError) as exc:
        preflight(settings, parse_context(None), bad)
    assert exc.value.code == "QUERY_REJECTED"


def test_calendar_warning_when_session_evidence_is_not_final(settings):
    req = {"dataset": "price_daily", "identifiers": ["AAPL"], "start_date": "2027-03-01",
           "end_date": "2027-03-31", "policy": "missing_only", "price_basis": "raw",
           "calendar_id": "cal_tadawul"}
    out = preflight(settings, parse_context({"mode": "known_at",
                                             "known_at": "2027-03-09T19:00:00Z"}), req)
    assert any("pending" in w for w in out["warnings"])
    assert out["calendar"]["calendar_id"] == "cal_tadawul"

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
from security_master_api.errors import CODES, DomainError


def test_every_spec_code_has_a_status():
    expected = {
        "NO_LOCAL_EVIDENCE", "NO_MATCHING_FACTS", "IDENTITY_AMBIGUOUS", "IDENTITY_UNRESOLVED",
        "TEMPORAL_MODE_UNSUPPORTED", "VERSION_NOT_RETAINED", "MATERIALIZATION_NOT_BUILT",
        "QUERY_REJECTED", "QUERY_BUDGET_EXCEEDED", "QUERY_CANCELLED",
        "ACQUISITION_REVIEW_REQUIRED", "PROVIDER_RATE_LIMITED", "PROVIDER_ENTITLEMENT_DENIED",
        "PROMOTION_VALIDATION_FAILED", "INTERNAL_ERROR",
    }
    assert set(CODES) == expected


def test_domain_error_carries_status_and_envelope():
    err = DomainError("TEMPORAL_MODE_UNSUPPORTED", "known_at is not supported", {"relation": "x"})
    assert err.status == 422
    assert err.envelope("req_1") == {
        "error": {
            "code": "TEMPORAL_MODE_UNSUPPORTED",
            "message": "known_at is not supported",
            "details": {"relation": "x"},
            "request_id": "req_1",
        }
    }


def test_unknown_code_is_refused():
    try:
        DomainError("NOT_A_CODE", "x")
    except ValueError as exc:
        assert "NOT_A_CODE" in str(exc)
    else:
        raise AssertionError("unknown code accepted")

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Domain errors and the standard error envelope."""

from __future__ import annotations

STATUS_BY_CODE: dict[str, int] = {
    "NO_LOCAL_EVIDENCE": 404,
    "NO_MATCHING_FACTS": 404,
    "IDENTITY_AMBIGUOUS": 409,
    "IDENTITY_UNRESOLVED": 404,
    "TEMPORAL_MODE_UNSUPPORTED": 422,
    "VERSION_NOT_RETAINED": 410,
    "MATERIALIZATION_NOT_BUILT": 409,
    "QUERY_REJECTED": 422,
    "QUERY_BUDGET_EXCEEDED": 429,
    "QUERY_CANCELLED": 499,
    "ACQUISITION_REVIEW_REQUIRED": 409,
    "PROVIDER_RATE_LIMITED": 429,
    "PROVIDER_ENTITLEMENT_DENIED": 403,
    "PROMOTION_VALIDATION_FAILED": 422,
}
CODES = tuple(STATUS_BY_CODE)


class DomainError(Exception):
    """A structured, client-facing failure. Every code maps to one HTTP status."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        if code not in STATUS_BY_CODE:
            raise ValueError(f"unknown domain error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.status = STATUS_BY_CODE[code]

    def envelope(self, request_id: str) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "request_id": request_id,
            }
        }

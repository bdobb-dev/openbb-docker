# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The worker's only network client: this stack's openbb-api, with Basic auth. No provider key."""

from __future__ import annotations

from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class Response:
    status: int
    body: str
    json: object | None
    retry_after_s: int | None


def classify(status: int) -> str:
    """What a status MEANS to a job. The distinctions matter because they end the job in
    different terminal states: a 429 is a retry the operator can schedule, a 403 is an
    entitlement the operator has to buy, and a 404 is coverage nobody has."""
    if 200 <= status < 300:
        return "ok"
    if status == 401:
        return "auth"
    if status == 403:
        return "entitlement"
    if status == 404:
        return "not_covered"
    if status == 429:
        return "rate_limited"
    return "transport"


class OpenbbClient:
    def __init__(self, base_url: str, username: str, password: str, timeout_s: int = 60):
        self.base_url = base_url.rstrip("/")
        self._auth = (username, password)
        self.timeout_s = timeout_s

    def get(self, path: str, params: dict) -> Response:
        # A transport failure is answered, not raised: the caller has Bronze to write and a
        # job to end, and an exception escaping here would leave both undone.
        try:
            r = requests.get(f"{self.base_url}{path}", params=params, auth=self._auth,
                             timeout=self.timeout_s)
        except requests.RequestException as exc:
            return Response(0, str(exc)[:500], None, None)
        retry = r.headers.get("Retry-After")
        try:
            parsed = r.json()
        except ValueError:
            parsed = None
        return Response(r.status_code, r.text, parsed,
                        int(retry) if retry and retry.isdigit() else None)

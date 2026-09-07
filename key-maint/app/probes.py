# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Concurrent provider probes. Detail strings are built ONLY from status codes
and exception class names — never from URLs, bodies, or key material."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from app.registry import PROVIDERS

TIMEOUT_S = 5.0


@dataclass(frozen=True)
class TestResult:
    # ok | auth_failed | error | no_response | skipped
    #
    # `no_response` and `error` are deliberately distinct: the widget paints
    # a vendor that never answered red, and one that answered with an error
    # amber. Collapsing them (as this did) made an outage indistinguishable
    # from a rejected request.
    result: str
    detail: str


async def _probe_one(client: httpx.AsyncClient, env_var: str, values: dict[str, str]) -> TestResult:
    provider = PROVIDERS[env_var]
    if provider.probe is None:
        return TestResult("skipped", "no probe defined")
    request = provider.probe.build(values)
    if request is None:
        return TestResult("skipped", "key not set")
    try:
        resp = await client.send(request)
    except httpx.HTTPError as e:
        return TestResult("no_response", type(e).__name__)
    if resp.status_code in (401, 403):
        return TestResult("auth_failed", f"HTTP {resp.status_code}")
    if resp.is_success:
        body = resp.text[:2000]
        for marker in provider.probe.invalid_markers:
            if marker in body:
                return TestResult("auth_failed", f"HTTP {resp.status_code}, rejected by body")
        return TestResult("ok", f"HTTP {resp.status_code}")
    return TestResult("error", f"HTTP {resp.status_code}")


async def probe_one_provider(env_var: str, values: dict[str, str]) -> TestResult:
    """Probe a single provider. Backs the widget's per-row 'Test this
    service' action, which must not fire ~18 vendor requests to check one."""
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        return await _probe_one(client, env_var, values)


async def run_probes(
    values: dict[str, str], client: httpx.AsyncClient | None = None
) -> dict[str, TestResult]:
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_S)
    try:
        names = list(PROVIDERS)
        results = await asyncio.gather(*(_probe_one(client, v, values) for v in names))
        return dict(zip(names, results))
    finally:
        if own:
            await client.aclose()

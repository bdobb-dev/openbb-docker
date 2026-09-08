# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

import asyncio

import httpx

from app.probes import _probe_one, run_probes

REAL_KEY = "sekret-value-123"


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(values, handler):
    async def go():
        async with _client(handler) as c:
            return await run_probes(values, client=c)

    return asyncio.run(go())


def _probe_with(*, raises=None, status=None, body="", key=REAL_KEY, env_var="EODHD_API_KEY"):
    """Drive _probe_one for a given provider (EODHD by default) against a
    stubbed httpx.AsyncClient. Either `raises` (an exception instance the
    mock transport raises) or `status` (plus optional `body`) must be given.

    This suite has no pytest-asyncio/anyio plugin declared as a dependency
    (pyproject.toml's dev extra is just pytest + ruff), so — following the
    existing `run()` helper above — coroutines are driven with asyncio.run()
    from a plain sync test function rather than @pytest.mark.asyncio.
    """

    def handler(request):
        if raises is not None:
            raise raises
        return httpx.Response(status, text=body)

    async def go():
        async with _client(handler) as c:
            return await _probe_one(c, env_var, {env_var: key})

    return asyncio.run(go())


class TestClassification:
    def test_2xx_is_ok(self):
        res = run({"EODHD_API_KEY": REAL_KEY}, lambda r: httpx.Response(200, json=[]))
        assert res["EODHD_API_KEY"].result == "ok"

    def test_401_and_403_are_auth_failed(self):
        for code in (401, 403):
            res = run({"EODHD_API_KEY": REAL_KEY}, lambda r, c=code: httpx.Response(c))
            assert res["EODHD_API_KEY"].result == "auth_failed"

    def test_2xx_with_invalid_marker_is_auth_failed(self):
        res = run(
            {"BLS_API_KEY": REAL_KEY},
            lambda r: httpx.Response(200, json={"status": "REQUEST_NOT_PROCESSED"}),
        )
        assert res["BLS_API_KEY"].result == "auth_failed"

    def test_network_error_is_no_response(self):
        # A transport failure (never got a response) is distinct from an
        # HTTP error response — see TestTransportFailureIsDistinct below.
        def boom(request):
            raise httpx.ConnectError("nope", request=request)

        res = run({"EODHD_API_KEY": REAL_KEY}, boom)
        assert res["EODHD_API_KEY"].result == "no_response"

    def test_missing_key_is_skipped(self):
        res = run({}, lambda r: httpx.Response(200))
        assert res["EODHD_API_KEY"].result == "skipped"

    def test_provider_without_probe_is_skipped(self):
        res = run({"BENZINGA_API_KEY": REAL_KEY}, lambda r: httpx.Response(200))
        assert res["BENZINGA_API_KEY"].result == "skipped"


class TestSecrecy:
    def test_key_value_never_in_detail(self):
        def boom(request):
            raise httpx.ConnectError(f"failed url {request.url}", request=request)

        res = run({"EODHD_API_KEY": REAL_KEY}, boom)
        for tr in res.values():
            assert REAL_KEY not in tr.detail

    def test_detail_mentions_status_for_ok(self):
        res = run({"EODHD_API_KEY": REAL_KEY}, lambda r: httpx.Response(200, json=[]))
        assert "200" in res["EODHD_API_KEY"].detail


class TestTransportFailureIsDistinct:
    """A vendor that never answers must not look like one that answered
    with an error: the widget paints the first red and the second amber."""

    def test_timeout_is_no_response(self):
        # httpx.HTTPError subclasses cover timeout/connect/DNS failures.
        result = _probe_with(raises=httpx.ConnectTimeout("boom"))
        assert result.result == "no_response"
        assert "ConnectTimeout" in result.detail

    def test_connect_error_is_no_response(self):
        result = _probe_with(raises=httpx.ConnectError("refused"))
        assert result.result == "no_response"

    def test_http_500_is_error_not_no_response(self):
        result = _probe_with(status=500)
        assert result.result == "error"

    def test_401_is_auth_failed(self):
        result = _probe_with(status=401)
        assert result.result == "auth_failed"

    def test_200_rejected_by_body_is_auth_failed(self):
        # Alpha Vantage and FMP report a bad key with HTTP 200 plus an error
        # string, so the status code alone is not the signal. EODHD (the
        # default provider above) has no invalid_markers, so this needs a
        # provider that does.
        result = _probe_with(status=200, body="Invalid API call", env_var="ALPHA_VANTAGE_API_KEY")
        assert result.result == "auth_failed"

    def test_detail_never_contains_the_key(self):
        result = _probe_with(status=500, key="supersecret123")
        assert "supersecret123" not in result.detail

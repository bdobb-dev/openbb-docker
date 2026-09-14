"""The lease leg is best-effort: a quote must survive live-grid being down."""

import pytest

from openbb_kdb.leasing import _auth_headers, lease


@pytest.mark.asyncio
async def test_lease_returns_true_when_granted():
    async def fake_post(url, json, timeout):
        return {"leases": {"AAPL": "2026-08-26T15:20:00+00:00"}}

    assert await lease("AAPL", post=fake_post) is True


@pytest.mark.asyncio
async def test_lease_returns_false_when_live_grid_is_unreachable():
    """The whole point: never raise into the fetcher."""
    async def boom(url, json, timeout):
        raise OSError("connection refused")

    assert await lease("AAPL", post=boom) is False


@pytest.mark.asyncio
async def test_lease_returns_false_on_a_malformed_response():
    async def weird(url, json, timeout):
        return {"unexpected": True}

    assert await lease("AAPL", post=weird) is False


@pytest.mark.asyncio
async def test_lease_sends_the_symbol_upper_cased():
    seen = {}

    async def capture(url, json, timeout):
        seen.update(json)
        return {"leases": {"AAPL": "2026-08-26T15:20:00+00:00"}}

    await lease("aapl", post=capture)
    assert seen["symbols"] == ["AAPL"]


def test_auth_headers_present_when_env_vars_set(monkeypatch):
    monkeypatch.setenv("OPENBB_API_USERNAME", "user")
    monkeypatch.setenv("OPENBB_API_PASSWORD", "pw")
    assert _auth_headers() == {"Authorization": "Basic dXNlcjpwdw=="}


def test_auth_headers_absent_when_env_vars_unset(monkeypatch):
    monkeypatch.delenv("OPENBB_API_USERNAME", raising=False)
    monkeypatch.delenv("OPENBB_API_PASSWORD", raising=False)
    assert _auth_headers() == {}

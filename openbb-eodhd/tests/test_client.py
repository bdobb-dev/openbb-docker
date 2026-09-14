"""The SDK must honour OpenBB's configured HTTP settings.

Every other provider reaches the network through openbb_core's helpers, so a
proxy / CA bundle / client cert / timeout set under python_settings.http
applies to all of them. This extension uses the official EODHD SDK, which
builds its own bare requests.Session -- these tests pin the wiring that closes
that gap, and the SDK-internal attribute names it depends on.
"""

import pytest

from openbb_eodhd.models._client import apply_openbb_http_settings, get_client


def test_pinned_sdk_still_exposes_the_attributes_we_configure():
    """Guards the SDK pin. If a bump renames these, this fails in CI rather
    than silently reverting provider="eodhd" to unconfigured HTTP."""
    from eodhd import APIClient

    client = APIClient("demo")
    assert hasattr(client, "_session"), "SDK no longer has _session -- pin moved"
    assert hasattr(client, "_timeout"), "SDK no longer has _timeout -- pin moved"


def test_session_is_replaced_with_openbbs_configured_session():
    from eodhd import APIClient

    client = APIClient("demo")
    before = client._session
    apply_openbb_http_settings(client)
    after = client._session
    # openbb-core hands back its own configured Session, carrying at minimum a
    # User-Agent that the SDK's bare Session does not set.
    assert after is not before
    assert after.headers.get("User-Agent")


def test_configured_timeout_is_applied(monkeypatch):
    import openbb_eodhd.models._client as mod
    from eodhd import APIClient

    client = APIClient("demo")
    monkeypatch.setattr(
        "openbb_core.provider.utils.helpers.get_python_request_settings",
        lambda: {"timeout": 42},
    )
    apply_openbb_http_settings(client)
    assert client._timeout == (42.0, 42.0)
    assert mod  # keep the import meaningful for linters


def test_no_configured_timeout_leaves_the_sdk_default():
    from eodhd import APIClient

    client = APIClient("demo")
    default = client._timeout
    apply_openbb_http_settings(client)
    assert client._timeout == default


def test_settings_failure_never_breaks_the_client(monkeypatch):
    """Transport preferences are a nicety; a failure resolving them must not
    take down a data request."""
    from eodhd import APIClient

    client = APIClient("demo")
    monkeypatch.setattr(
        "openbb_core.provider.utils.helpers.get_requests_session",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    apply_openbb_http_settings(client)  # must not raise


def test_get_client_requires_a_credential():
    from openbb_core.provider.utils.errors import UnauthorizedError

    with pytest.raises(UnauthorizedError):
        get_client({})

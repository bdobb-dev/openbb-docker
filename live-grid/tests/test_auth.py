"""The credential check, tested without building an app."""

import base64

import pytest

from app.auth import BasicAuthMiddleware, auth_enabled, credentials_ok


def _hdr(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "true")
    monkeypatch.setenv("OPENBB_API_USERNAME", "alice")
    monkeypatch.setenv("OPENBB_API_PASSWORD", "s3cret")


def test_the_right_credentials_pass():
    assert credentials_ok(_hdr("alice", "s3cret")) is True


def test_a_wrong_password_fails():
    assert credentials_ok(_hdr("alice", "wrong")) is False


def test_a_wrong_user_fails():
    assert credentials_ok(_hdr("bob", "s3cret")) is False


def test_a_missing_header_fails():
    assert credentials_ok(None) is False


def test_a_non_basic_scheme_fails():
    assert credentials_ok("Bearer s3cret") is False


def test_the_scheme_is_matched_case_insensitively():
    """RFC 7235 makes the scheme token case-insensitive; clients vary."""
    assert credentials_ok(_hdr("alice", "s3cret").replace("Basic", "basic")) is True


def test_undecodable_base64_fails_instead_of_raising():
    assert credentials_ok("Basic !!!not-base64!!!") is False


def test_a_header_with_no_colon_fails():
    only_user = base64.b64encode(b"alice").decode()
    assert credentials_ok(f"Basic {only_user}") is False


def test_it_fails_closed_when_the_password_is_unconfigured(monkeypatch):
    """Without the `username and password` guard, compare_digest("", "") is true
    on both halves and `Basic <base64 of ":">` would authenticate against an
    unconfigured pair."""
    monkeypatch.setenv("OPENBB_API_PASSWORD", "")
    assert credentials_ok(_hdr("", "")) is False
    assert credentials_ok(_hdr("alice", "")) is False


def test_it_fails_closed_when_the_username_is_unconfigured(monkeypatch):
    """With the username unconfigured, a request supplying the CORRECT password
    and an empty username must still fail -- otherwise compare_digest("", "")
    is true on the username half and only the password stands between an
    unconfigured service and anyone who can read api-auth.env's password."""
    monkeypatch.setenv("OPENBB_API_USERNAME", "")
    assert credentials_ok(_hdr("", "s3cret")) is False


def test_auth_enabled_follows_the_env(monkeypatch):
    for value, expected in (("true", True), ("1", True), ("yes", True),
                            ("false", False), ("", False)):
        monkeypatch.setenv("OPENBB_API_AUTH", value)
        assert auth_enabled() is expected


def test_auth_enabled_is_false_when_the_variable_is_absent(monkeypatch):
    """Local development and the existing suite must be unaffected."""
    monkeypatch.delenv("OPENBB_API_AUTH", raising=False)
    assert auth_enabled() is False


# --- BasicAuthMiddleware -----------------------------------------------
#
# Driven directly with hand-built ASGI scopes and fake receive/send
# callables -- no FastAPI, no TestClient -- so this stays standalone-testable
# the same way credentials_ok/auth_enabled are.


def _http_scope(headers=None, method="GET"):
    return {"type": "http", "method": method, "headers": headers or []}


def _ws_scope(headers=None):
    return {"type": "websocket", "headers": headers or []}


def _auth_header(user: str, pw: str) -> tuple[bytes, bytes]:
    return (b"authorization", _hdr(user, pw).encode())


class _App:
    """Fake inner ASGI app; records whether it was called."""

    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True


class _Send:
    """Fake ASGI `send`; records every message."""

    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)


async def _receive():
    raise AssertionError("receive() should not be called by this middleware")


@pytest.mark.asyncio
async def test_http_scope_without_credentials_gets_401_and_inner_app_not_called():
    app = _App()
    send = _Send()
    middleware = BasicAuthMiddleware(app)

    await middleware(_http_scope(), _receive, send)

    assert app.called is False
    start = send.messages[0]
    assert start["type"] == "http.response.start"
    assert start["status"] == 401
    assert (b"www-authenticate", b"Basic") in start["headers"]
    assert send.messages[1] == {"type": "http.response.body", "body": b""}


@pytest.mark.asyncio
async def test_http_scope_with_correct_credentials_calls_inner_app():
    app = _App()
    send = _Send()
    middleware = BasicAuthMiddleware(app)

    scope = _http_scope(headers=[_auth_header("alice", "s3cret")])
    await middleware(scope, _receive, send)

    assert app.called is True
    assert send.messages == []


@pytest.mark.asyncio
async def test_websocket_scope_without_credentials_closes_with_1008():
    app = _App()
    send = _Send()
    middleware = BasicAuthMiddleware(app)

    await middleware(_ws_scope(), _receive, send)

    assert app.called is False
    assert send.messages == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.asyncio
async def test_websocket_scope_with_correct_credentials_calls_inner_app():
    app = _App()
    send = _Send()
    middleware = BasicAuthMiddleware(app)

    scope = _ws_scope(headers=[_auth_header("alice", "s3cret")])
    await middleware(scope, _receive, send)

    assert app.called is True
    assert send.messages == []


@pytest.mark.asyncio
async def test_a_lifespan_scope_is_passed_straight_through():
    app = _App()
    send = _Send()
    middleware = BasicAuthMiddleware(app)

    await middleware({"type": "lifespan"}, _receive, send)

    assert app.called is True
    assert send.messages == []


@pytest.mark.asyncio
async def test_auth_disabled_passes_http_and_websocket_straight_through(monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "false")

    for scope in (_http_scope(), _ws_scope()):
        app = _App()
        send = _Send()
        middleware = BasicAuthMiddleware(app)
        await middleware(scope, _receive, send)
        assert app.called is True
        assert send.messages == []


@pytest.mark.asyncio
async def test_a_scope_with_no_headers_key_does_not_raise():
    app = _App()
    send = _Send()
    middleware = BasicAuthMiddleware(app)

    await middleware({"type": "http", "method": "GET"}, _receive, send)

    assert app.called is False
    assert send.messages[0]["status"] == 401


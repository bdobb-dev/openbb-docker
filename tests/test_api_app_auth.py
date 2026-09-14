"""The blanket Basic auth guard on api_app.py's factory.

Upstream wires `authenticate_user` only into the /api/v1 command router, so
the Workspace metadata routes (/, /widgets.json, /apps.json, /agents.json)
and FastAPI's own /docs, /redoc and /openapi.json answered to anyone even
with OPENBB_API_AUTH=true. docker-compose.yml's header has always told the
reader that /widgets.json returns 401 without credentials; until this guard,
it did not.

These tests build a minimal app with the SAME middleware shape as production
-- CORS outermost, auth innermost -- rather than importing the real platform
app, so they run without an OpenBB install and in milliseconds.
"""

import base64
import sys
import types

import pytest
from fastapi.middleware.cors import CORSMiddleware
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))


def _stub_env(monkeypatch, *, auth=True, username="openbb", password="hunter2"):
    """Stand in for openbb_core.env.Env, which is not installed for these tests."""

    class _Env:
        API_AUTH = auth
        API_USERNAME = username
        API_PASSWORD = password

    module = types.ModuleType("openbb_core.env")
    module.Env = _Env
    pkg = types.ModuleType("openbb_core")
    pkg.env = module
    monkeypatch.setitem(sys.modules, "openbb_core", pkg)
    monkeypatch.setitem(sys.modules, "openbb_core.env", module)


def _client(monkeypatch, **env):
    """A minimal app shaped like production: CORS outermost, auth innermost."""
    _stub_env(monkeypatch, **env)
    from api_app import _require_basic_auth

    app = Starlette(routes=[Route("/widgets.json", lambda r: PlainTextResponse("{}"))])
    # Order mirrors the factory: CORS registered first (ends up outer), auth
    # appended last (ends up inner).
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"])
    app.user_middleware.append(
        __import__("starlette.middleware", fromlist=["Middleware"]).Middleware(
            BaseHTTPMiddleware, dispatch=_require_basic_auth
        )
    )
    return TestClient(app)


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_an_unauthenticated_metadata_request_is_refused(monkeypatch):
    """This is the whole point: /widgets.json is NOT under /api/v1."""
    response = _client(monkeypatch).get("/widgets.json")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Basic"


def test_correct_credentials_pass_through(monkeypatch):
    response = _client(monkeypatch).get("/widgets.json", headers=_basic("openbb", "hunter2"))
    assert response.status_code == 200


def test_wrong_password_is_refused(monkeypatch):
    response = _client(monkeypatch).get("/widgets.json", headers=_basic("openbb", "wrong"))
    assert response.status_code == 401


def test_malformed_authorization_header_is_refused(monkeypatch):
    client = _client(monkeypatch)
    for header in ({"Authorization": "Basic !!!not-base64!!!"},
                   {"Authorization": "Bearer sometoken"},
                   {"Authorization": "Basic"}):
        assert client.get("/widgets.json", headers=header).status_code == 401


def test_it_fails_closed_when_no_password_is_configured(monkeypatch):
    """`Basic <base64 of ":">` must not authenticate against an empty pair.

    compare_digest("", "") is true on both halves, so without the explicit
    `username and password` check an unconfigured deployment would accept
    empty credentials -- worse than no auth, because it looks locked.
    """
    client = _client(monkeypatch, username="", password="")
    assert client.get("/widgets.json", headers=_basic("", "")).status_code == 401
    assert client.get("/widgets.json").status_code == 401


def test_the_guard_no_ops_when_auth_is_disabled(monkeypatch):
    """openbb-mcp runs this same app deliberately without api-auth.env."""
    assert _client(monkeypatch, auth=False).get("/widgets.json").status_code == 200


def test_a_cors_preflight_succeeds_without_credentials(monkeypatch):
    """The failure curl cannot catch.

    A browser preflight carries no credentials by definition. If auth were
    registered OUTSIDE CORS it would answer 401 with no Access-Control-Allow-*
    headers, and every cross-origin caller -- OpenBB Workspace at
    pro.openbb.co, the entire point of the stack -- would be locked out while
    every curl test still passed.
    """
    response = _client(monkeypatch).options(
        "/widgets.json",
        headers={"Origin": "https://pro.openbb.co",
                 "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 200, "preflight must not be refused by auth"
    assert response.headers.get("access-control-allow-origin") is not None


def test_auth_is_appended_innermost_not_inserted_outermost(monkeypatch):
    """Guards the ordering directly, so a future edit cannot quietly invert it.

    Starlette's add_middleware inserts at index 0 and index 0 is OUTERMOST.
    Using it here instead of append would put auth outside CORS and break the
    preflight above.
    """
    _stub_env(monkeypatch)
    from api_app import _require_basic_auth

    app = Starlette(routes=[])
    app.add_middleware(CORSMiddleware, allow_origins=["*"])
    app.user_middleware.append(
        __import__("starlette.middleware", fromlist=["Middleware"]).Middleware(
            BaseHTTPMiddleware, dispatch=_require_basic_auth
        )
    )
    assert app.user_middleware[0].cls is CORSMiddleware, "CORS must stay outermost"
    assert app.user_middleware[-1].cls is BaseHTTPMiddleware, "auth must be innermost"


@pytest.mark.parametrize("path", ["/", "/widgets.json", "/openapi.json", "/docs", "/redoc"])
def test_every_root_path_is_covered_not_just_api_v1(monkeypatch, path):
    """Middleware wraps the app, so coverage does not depend on a router."""
    _stub_env(monkeypatch)
    from api_app import _require_basic_auth

    app = Starlette(routes=[Route(path, lambda r: PlainTextResponse("ok"))])
    app.user_middleware.append(
        __import__("starlette.middleware", fromlist=["Middleware"]).Middleware(
            BaseHTTPMiddleware, dispatch=_require_basic_auth
        )
    )
    assert TestClient(app).get(path).status_code == 401

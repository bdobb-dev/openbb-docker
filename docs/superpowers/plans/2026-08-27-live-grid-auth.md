# live-grid Basic auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require HTTP Basic auth on every live-grid surface — REST and websockets — using the credentials already mounted from `api-auth.env`.

**Architecture:** One raw ASGI middleware appended to `app.user_middleware`, so it runs INSIDE CORS and sees websocket scopes as well as HTTP ones. Credentials come from `os.environ`; no new dependency.

**Tech Stack:** Python 3.12, FastAPI/Starlette, pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-live-grid-auth-design.md`

## Global Constraints

- Credentials are `OPENBB_API_AUTH`, `OPENBB_API_USERNAME`, `OPENBB_API_PASSWORD` — the same ones openbb-api uses, from the `api-auth.env` compose already mounts here (spec D1) — read with `os.environ` — **not** `openbb_core.env.Env`. live-grid does not depend on `openbb-core` and must not gain that dependency to read three strings (spec D2).
- **The guard must be INNERMOST.** `app.add_middleware` does `user_middleware.insert(0, ...)`, and index 0 is OUTERMOST. Appending puts the guard inside CORS. Get this backwards and a browser preflight — which carries no credentials by definition — gets a bare 401 with no `Access-Control-Allow-*` headers, locking out every cross-origin caller while every `curl` test still passes (spec D4).
- **It must be a raw ASGI middleware, not `BaseHTTPMiddleware`.** Starlette's `BaseHTTPMiddleware.__call__` begins `if scope["type"] != "http": await self.app(...); return` — verified in its source — so it never runs for websockets. live-grid has two (`/live_grid_ws`, `/ta_chart_ws`) carrying the same live data as the REST routes (spec D6).
- It no-ops when `OPENBB_API_AUTH` is unset, so local development and the existing 300-odd tests are unaffected (spec D5).
- Comparison uses `secrets.compare_digest` on both halves and fails CLOSED when either configured value is empty — otherwise `compare_digest("", "")` is true on both and `Basic <base64 of ":">` authenticates against an unconfigured pair.
- CORS preflight (`OPTIONS`) must still succeed without credentials.
- Do not change what live-grid serves, and do not touch `stores-explorer`.
- Run the suite as `PYTHONFAULTHANDLER=1 pytest -q --capture=sys` from `live-grid/`; live-grid's own `.venv` lacks `polars`, so use a venv built with `pip install -e ./kdb-store` then `pip install -e .[dev]`.

## File structure

| file | responsibility |
|---|---|
| `live-grid/app/auth.py` (new) | the credential check and the ASGI middleware — no FastAPI, no app knowledge, unit-testable alone |
| `live-grid/app/main.py` | appends the middleware after the CORS block |
| `live-grid/tests/test_auth.py` (new) | the guard's own unit tests |
| `live-grid/tests/test_auth_routes.py` (new) | end-to-end over TestClient, including websockets and preflight |

---

### Task 1: The credential check and ASGI middleware

_Implements spec **D1**, **D2**, **D3** (middleware rather than route dependencies, so nothing FastAPI registers itself is missed), **D5** and **D6**._

Kept in its own module with no FastAPI import, so the credential logic can be
tested without building an app.

**Files:**
- Create: `live-grid/app/auth.py`
- Test: `live-grid/tests/test_auth.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `credentials_ok(header: str | None) -> bool` — True when the `Authorization` header carries the configured Basic credentials. Reads the environment on every call.
  - `auth_enabled() -> bool` — True when `OPENBB_API_AUTH` is truthy.
  - `BasicAuthMiddleware(app)` — a raw ASGI middleware class, `async def __call__(self, scope, receive, send)`.

- [ ] **Step 1: Write the failing tests**

Create `live-grid/tests/test_auth.py`:

```python
"""The credential check, tested without building an app."""

import base64

import pytest

from app.auth import auth_enabled, credentials_ok


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
    monkeypatch.setenv("OPENBB_API_USERNAME", "")
    assert credentials_ok(_hdr("", "")) is False


def test_auth_enabled_follows_the_env(monkeypatch):
    for value, expected in (("true", True), ("1", True), ("yes", True),
                            ("false", False), ("", False)):
        monkeypatch.setenv("OPENBB_API_AUTH", value)
        assert auth_enabled() is expected


def test_auth_enabled_is_false_when_the_variable_is_absent(monkeypatch):
    """Local development and the existing suite must be unaffected."""
    monkeypatch.delenv("OPENBB_API_AUTH", raising=False)
    assert auth_enabled() is False
```

- [ ] **Step 2: Run them and verify they fail**

Run: `cd live-grid && pytest tests/test_auth.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auth'`

- [ ] **Step 3: Implement the module**

Create `live-grid/app/auth.py`:

```python
"""HTTP Basic auth for every live-grid surface.

Uses the SAME credentials as openbb-api, from the same `api-auth.env` that
compose already mounts into this service: OPENBB_API_AUTH, OPENBB_API_USERNAME,
OPENBB_API_PASSWORD. One credential for the stack's HTTP surfaces means one
thing to rotate.

Read with os.environ rather than openbb_core.env.Env: api_app.py uses Env
because it runs inside the Platform, but live-grid does not depend on
openbb-core and should not gain that dependency to read three strings.

A RAW ASGI middleware, not BaseHTTPMiddleware. Starlette's BaseHTTPMiddleware
begins `if scope["type"] != "http": await self.app(...); return`, so it never
runs for websockets -- and live-grid's two websockets carry the same live data
as its REST routes. Guarding only HTTP would leave them wide open while every
HTTP test passed.
"""

import base64
import binascii
import os
import secrets

_TRUE = ("1", "true", "yes", "on")


def auth_enabled() -> bool:
    """True when OPENBB_API_AUTH is set to a truthy value."""
    return os.environ.get("OPENBB_API_AUTH", "").strip().lower() in _TRUE


def credentials_ok(header: str | None) -> bool:
    """True when `header` carries the configured Basic credentials."""
    username = os.environ.get("OPENBB_API_USERNAME", "")
    password = os.environ.get("OPENBB_API_PASSWORD", "")

    supplied_user = supplied_pw = ""
    if header and header[:6].lower() == "basic ":
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            decoded = ""
        if ":" in decoded:
            supplied_user, _, supplied_pw = decoded.partition(":")

    ok_user = secrets.compare_digest(supplied_user.encode(), username.encode())
    ok_pw = secrets.compare_digest(supplied_pw.encode(), password.encode())
    # `username and password` fails CLOSED when either is unconfigured: without
    # it, compare_digest("", "") is true on both halves and `Basic <base64 of
    # ":">` would authenticate against an empty pair.
    return bool(username and password and ok_user and ok_pw)


class BasicAuthMiddleware:
    """Reject unauthenticated HTTP requests and websocket connections."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not auth_enabled():
            await self.app(scope, receive, send)
            return

        # A CORS preflight carries no credentials by definition. This guard is
        # registered INSIDE CORS so preflights are answered before reaching it,
        # but check anyway: a direct OPTIONS must not 401 either.
        if scope["type"] == "http" and scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        header = None
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                header = value.decode("latin-1")
                break

        if credentials_ok(header):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # Refuse before accept. A websocket client sees the handshake fail;
            # there is no 401 body to send on this transport.
            await send({"type": "websocket.close", "code": 1008})
            return

        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"www-authenticate", b"Basic"),
                (b"content-length", b"0"),
            ],
        })
        await send({"type": "http.response.body", "body": b""})
```

- [ ] **Step 4: Run them and verify they pass**

Run: `cd live-grid && pytest tests/test_auth.py -q`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add live-grid/app/auth.py live-grid/tests/test_auth.py
git commit -m "feat(live-grid): Basic auth credential check and ASGI middleware"
```

---

### Task 2: Wire it into the app, innermost

_Implements spec **D4** — the ordering trap._

**Files:**
- Modify: `live-grid/app/main.py` (after the CORS block, around line 204)
- Test: `live-grid/tests/test_auth_routes.py`

**Interfaces:**
- Consumes: `BasicAuthMiddleware` from Task 1.
- Produces: no new names. Every live-grid surface requires credentials when `OPENBB_API_AUTH` is set.

- [ ] **Step 1: Write the failing tests**

Create `live-grid/tests/test_auth_routes.py`:

```python
"""Auth over the real app, including the two surfaces easiest to miss."""

import base64

import pytest

from tests.test_main import make_client


def _hdr(user="alice", pw="s3cret"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "true")
    monkeypatch.setenv("OPENBB_API_USERNAME", "alice")
    monkeypatch.setenv("OPENBB_API_PASSWORD", "s3cret")
    return make_client()


def test_a_rest_route_401s_without_credentials(guarded):
    assert guarded.get("/widgets.json").status_code == 401


def test_the_401_carries_a_www_authenticate_header(guarded):
    """Without it a browser never prompts and a client cannot discover the scheme."""
    assert guarded.get("/widgets.json").headers.get("www-authenticate") == "Basic"


def test_a_rest_route_401s_with_wrong_credentials(guarded):
    assert guarded.get("/widgets.json", headers=_hdr(pw="wrong")).status_code == 401


def test_a_rest_route_succeeds_with_the_right_credentials(guarded):
    assert guarded.get("/widgets.json", headers=_hdr()).status_code == 200


def test_the_subscription_api_is_guarded_too(guarded):
    """It mutates durable state, so it is the one that matters most."""
    assert guarded.get("/api/subscriptions").status_code == 401
    assert guarded.post("/api/subscriptions", json={"symbol": "AAPL"}).status_code == 401


def test_the_websocket_is_refused_without_credentials(guarded):
    """THE trap: Starlette's BaseHTTPMiddleware skips non-http scopes, so an
    HTTP-only guard leaves this wide open while every REST test passes."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises((WebSocketDisconnect, Exception)):
        with guarded.websocket_connect("/live_grid_ws"):
            pass


def test_the_ta_chart_websocket_is_refused_too(guarded):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises((WebSocketDisconnect, Exception)):
        with guarded.websocket_connect("/ta_chart_ws?symbol=AAPL&macro=none"):
            pass


def test_the_websocket_connects_with_credentials(guarded):
    """The guard must not break the feature it protects."""
    with guarded.websocket_connect("/live_grid_ws", headers=_hdr()) as ws:
        ws.send_json({"params": {"symbol": "AAPL"}})


def test_a_cors_preflight_succeeds_without_credentials(guarded):
    """A preflight carries no credentials by definition. If the guard sits
    OUTSIDE CORS it answers with a bare 401 and no Access-Control-Allow-*,
    locking out every browser client while curl still passes."""
    r = guarded.options(
        "/widgets.json",
        headers={
            "Origin": "https://pro.openbb.co",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") is not None


def test_everything_passes_through_when_auth_is_disabled(monkeypatch):
    """Local development and the existing suite must be unaffected."""
    monkeypatch.delenv("OPENBB_API_AUTH", raising=False)
    client = make_client()
    assert client.get("/widgets.json").status_code == 200
    with client.websocket_connect("/live_grid_ws") as ws:
        ws.send_json({"params": {"symbol": "AAPL"}})
```

- [ ] **Step 2: Run them and verify they fail**

Run: `cd live-grid && pytest tests/test_auth_routes.py -q`
Expected: FAIL — the unauthenticated requests return 200, and the websockets connect

- [ ] **Step 3: Append the middleware**

In `live-grid/app/main.py`, add to the module imports:

```python
from app.auth import BasicAuthMiddleware
```

and immediately after the CORS `try/except` block (around line 204), add:

```python
    # APPEND, do not add_middleware. Starlette's add_middleware inserts at
    # index 0 and index 0 is OUTERMOST, so calling it here would put auth
    # outside CORS. A browser preflight carries no credentials by definition,
    # so it would get a bare 401 with no Access-Control-Allow-* headers and
    # every cross-origin caller -- including OpenBB Workspace -- would be
    # locked out. curl never sends a preflight, so no amount of curl testing
    # catches it. Appending makes auth INNERMOST, which still covers every
    # path: middleware wraps the whole app, not a router.
    app.user_middleware.append(Middleware(BasicAuthMiddleware))
```

Add `from starlette.middleware import Middleware` to the imports.

- [ ] **Step 4: Run them and verify they pass**

Run: `cd live-grid && pytest tests/test_auth_routes.py -q`
Expected: PASS (10 tests)

- [ ] **Step 5: Prove the ordering is real, not incidental**

Temporarily change the append to `app.add_middleware(BasicAuthMiddleware)` and
re-run only the preflight test:

Run: `cd live-grid && pytest tests/test_auth_routes.py -q -k preflight`
Expected: FAIL — this proves the test actually catches the ordering mistake.
Restore the append and confirm it passes again. **Report both results.**

- [ ] **Step 6: Run the whole live-grid suite**

Run: `cd live-grid && PYTHONFAULTHANDLER=1 pytest -q --capture=sys`
Expected: all pass. The existing ~309 tests run with `OPENBB_API_AUTH` unset, so
the guard no-ops for them — if any now fail, the no-op path is wrong.

- [ ] **Step 7: Commit**

```bash
git add live-grid/app/main.py live-grid/tests/test_auth_routes.py
git commit -m "feat(live-grid): require Basic auth on every surface"
```

---

### Task 3: Verify the widget still works, and document it

_Covers the spec's client-impact note, which flags the iframe credential question as needing testing rather than assuming._

The subscriptions page runs inside an iframe and calls its own API. Whether the
browser attaches credentials there is a real question the spec flags as
needing testing rather than assuming.

**Files:**
- Modify: `live-grid/README.md`
- Test: `live-grid/tests/test_auth_routes.py`

**Interfaces:**
- Consumes: everything from Tasks 1-2.
- Produces: no new names.

- [ ] **Step 1: Write the failing test**

Append to `live-grid/tests/test_auth_routes.py`:

```python
def test_the_subscriptions_page_itself_is_guarded(guarded):
    """The page is served by the same app, so it needs credentials too --
    and the browser will have them, because it authenticated to load the page."""
    assert guarded.get("/subscriptions").status_code == 401
    assert guarded.get("/subscriptions", headers=_hdr()).status_code == 200


def test_the_page_and_its_api_share_one_origin(guarded):
    """The page fetches /api/subscriptions with a root-relative URL, so the
    browser sends the same credentials it used for the page. If these ever
    diverge in origin, the widget breaks silently for authenticated users."""
    page = guarded.get("/subscriptions", headers=_hdr())
    assert "/api/subscriptions" in page.text
    assert "http://" not in page.text and "https://" not in page.text
```

- [ ] **Step 2: Run them and verify they fail or pass, and say which**

Run: `cd live-grid && pytest tests/test_auth_routes.py -q -k "subscriptions_page or one_origin"`
Expected: both PASS if Tasks 1-2 are correct. They are regression pins, not
new behaviour — record the result rather than assuming.

- [ ] **Step 3: Document it**

Add to `live-grid/README.md`, near where the other environment variables are
described:

```markdown
### Authentication

Every live-grid surface — REST routes, the subscriptions page and both
websockets — requires HTTP Basic auth when `OPENBB_API_AUTH` is set. The
credentials are `OPENBB_API_USERNAME` and `OPENBB_API_PASSWORD`, read from the
same `api-auth.env` that openbb-api uses and that compose already mounts here,
so there is one credential to rotate for the stack's HTTP surfaces.

Unset `OPENBB_API_AUTH` and the guard no-ops, which is what local development
and the test suite rely on.

A CORS preflight is answered without credentials — the guard is registered
inside the CORS layer deliberately. A browser preflight carries no credentials
by definition, so an auth layer outside CORS would answer it with a bare 401 and
lock out every cross-origin client while curl still worked.

Note this changes what a client needs: a live-grid Connection entry in the
desktop app must now carry an `Authorization` header, the same one the OpenBB
Platform backend uses.
```

- [ ] **Step 4: Commit**

```bash
git add live-grid/tests/test_auth_routes.py live-grid/README.md
git commit -m "docs(live-grid): document the auth requirement and pin the widget's origin"
```

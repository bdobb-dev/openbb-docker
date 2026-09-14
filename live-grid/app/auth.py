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

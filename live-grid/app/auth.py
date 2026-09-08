# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""HTTP Basic auth for every live-grid surface.

Uses the SAME credentials as openbb-api, from the same `api-auth.env` that
compose already mounts into this service: OPENBB_API_AUTH, OPENBB_API_USERNAME,
OPENBB_API_PASSWORD. One credential for the stack's HTTP surfaces means one
thing to rotate.

Read with os.environ rather than openbb_core.env.Env: api_app.py uses Env
because it runs inside the Platform, but live-grid does not depend on
openbb-core and should not gain that dependency to read three strings.

The credential may arrive as an `Authorization` QUERY PARAMETER as well as a
header. That exists for one reason: the subscriptions widget is an iframe, and
a browser frame issues its own request with no way to attach a header, so the
header the desktop client holds is unusable there. The query is the only
channel a frame has. It carries the same `Basic <base64>` value under the same
name, so there is one credential format, not two.

The cost is stated rather than hidden: a credential in a URL can reach proxy
and access logs. That is tolerable here only because this service is
tailnet-published and never funneled -- the same reasoning the client applies
to its websocket URL, which cannot carry a header either.

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

# The ONE path served without credentials: the subscriptions widget's static
# HTML.
#
# The widget is an iframe, and a browser frame issues its own navigation with
# no way to attach a header -- so under a blanket guard the frame 401s and
# paints blank before a single line of its script runs. The page carries no
# data and no state; everything it shows it fetches afterwards from
# /api/subscriptions, which stays guarded (see
# test_the_subscription_api_is_guarded_too -- it mutates durable state, so it
# is the one that matters most). Once loaded, the page asks its host for
# credentials over the `openbb-connect` / `openbb-auth` postMessage handshake
# and sends them on every call.
#
# Reaching this at all already requires being on the tailnet, where
# /live_grid_ws serves the same feed to anyone who asks. Exact match, so a
# future /subscriptions-admin does not quietly inherit the exemption.
_PUBLIC_PAGE = "/subscriptions"


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

        # HTTP only: a websocket to this path would not be the page load.
        if scope["type"] == "http" and scope.get("path") == _PUBLIC_PAGE:
            await self.app(scope, receive, send)
            return

        header = None
        for key, value in scope.get("headers") or []:
            if key == b"authorization":
                header = value.decode("latin-1")
                break

        # Fall back to the query only when no header was sent: a request that
        # supplies a header is judged on it alone, so a bad header cannot be
        # rescued by appending a good query string.
        if header is None:
            from urllib.parse import parse_qs

            raw_qs = scope.get("query_string") or b""
            supplied = parse_qs(raw_qs.decode("latin-1")).get("Authorization")
            if supplied:
                header = supplied[0]

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

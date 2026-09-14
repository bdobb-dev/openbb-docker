"""FastAPI app factory for `openbb-api` — the Platform REST app with
Private Network Access CORS enabled.

Why this file exists
--------------------
OpenBB Workspace runs at https://pro.openbb.co — a public origin. When the
browser classifies this backend's address as private/local (see Ep. 2's
gotchas), the fetch is gated behind an OPTIONS preflight carrying
`Access-Control-Request-Private-Network: true`. Starlette's CORSMiddleware
answers that header only when it was constructed with
`allow_private_network=True`, and OpenBB exposes no setting for it —
`api_settings.cors` carries allow_origins / allow_methods / allow_headers and
nothing else.

Earlier releases got there by rewriting `openbb_core/api/rest_api.py` inside
site-packages at image build time. That worked, but it was a text
substitution against upstream source: it goes silent the day upstream
reformats that block, and it hardcoded the interpreter's site-packages path,
so a base-image Python bump would have broken it too. `openbb-api` documents
a supported entrypoint for serving a customized instance — `--app <file>
--name <fn> --factory` — so the customization lives here, in version
controlled code, instead.

The subtlety that shapes the code below
---------------------------------------
`openbb-api` adds its OWN CORSMiddleware to whatever the factory returns
(`openbb_platform_api.utils.api.import_app`), and Starlette's
`add_middleware` does `user_middleware.insert(0, ...)` — index 0 is
OUTERMOST. A CORSMiddleware answers a preflight and returns without calling
anything downstream, so that outer layer, not ours, is the one the browser
actually talks to. Fixing only the instance `rest_api.py` registered would
therefore be invisible at runtime.

So this factory does two things:

1. Enables the flag on the CORS layer `rest_api.py` already registered.
2. Shadows `add_middleware` **on this one app object** (an instance
   attribute, not a patch of the class) so any CORS layer added after the
   factory returns inherits the flag as well.

Every failure mode here raises. A backend that answers preflights without the
private-network header is one Workspace silently cannot reach, which is worse
to debug than a container that refuses to start.
"""


async def _require_basic_auth(request, call_next):
    """Require HTTP Basic auth on every path when OPENBB_API_AUTH is set.

    Upstream wires `authenticate_user` only into the /api/v1 command router.
    The Workspace metadata routes that openbb-platform-api hangs off this same
    app (/, /widgets.json, /apps.json, /agents.json) and FastAPI's own /docs,
    /redoc and /openapi.json therefore answer to anyone, even with
    OPENBB_API_AUTH=true. Funnel can publish this port to the public internet,
    so the lock has to cover everything -- see docker-compose.yml's header,
    which already tells the reader /widgets.json returns 401 without
    credentials. Before this guard, it did not.

    Middleware rather than a route dependency: /docs and /openapi.json are
    registered by FastAPI itself and have no router to attach one to, and
    middleware stays correct if a future OpenBB adds another root route.

    No-ops when auth is off, which keeps the in-process openbb-mcp wrapper
    (started deliberately without api-auth.env) working.
    """
    # pylint: disable=import-outside-toplevel
    import base64
    import binascii
    import secrets

    from openbb_core.env import Env
    from starlette.responses import Response

    env = Env()
    if not env.API_AUTH:
        return await call_next(request)

    username = env.API_USERNAME or ""
    password = env.API_PASSWORD or ""
    header = request.headers.get("authorization", "")
    supplied_user = supplied_pw = ""
    if header[:6].lower() == "basic ":
        try:
            supplied_user, _, supplied_pw = (
                base64.b64decode(header[6:]).decode("utf8").partition(":")
            )
        except (binascii.Error, UnicodeDecodeError, ValueError):
            supplied_user = supplied_pw = ""

    ok_user = secrets.compare_digest(supplied_user.encode(), username.encode())
    ok_pw = secrets.compare_digest(supplied_pw.encode(), password.encode())
    # `username and password` fails CLOSED when either is unconfigured: without
    # it, compare_digest("", "") is true on both halves and `Basic <base64 of
    # ":">` would authenticate against an empty pair.
    if not (username and password and ok_user and ok_pw):
        return Response(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return await call_next(request)


def main():
    """Build the OpenBB Platform REST app with private-network CORS and auth."""
    # pylint: disable=import-outside-toplevel
    from inspect import signature

    from fastapi.middleware.cors import CORSMiddleware
    from openbb_core.api.rest_api import app
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    # Fail here, with this message, rather than as a TypeError raised from
    # inside uvicorn's startup when the middleware stack is finally built.
    if "allow_private_network" not in signature(CORSMiddleware.__init__).parameters:
        raise RuntimeError(
            "This Starlette's CORSMiddleware has no `allow_private_network` "
            "parameter. Chrome's Private Network Access preflight cannot be "
            "answered, so OpenBB Workspace would not reach this backend."
        )

    cors_layers = [mw for mw in app.user_middleware if mw.cls is CORSMiddleware]
    if not cors_layers:
        raise RuntimeError(
            "openbb_core.api.rest_api registered no CORSMiddleware — upstream "
            "changed. Refusing to start rather than serve a backend that "
            "Workspace cannot reach."
        )
    for layer in cors_layers:
        layer.kwargs["allow_private_network"] = True

    # `openbb-api` re-adds CORS outermost after this returns; make sure that
    # one carries the flag too. Bound to the instance so nothing else in the
    # process is affected — `setdefault`, so an explicit caller still wins.
    add_middleware = type(app).add_middleware

    def _add_middleware(middleware_class, *args, **kwargs):
        if middleware_class is CORSMiddleware:
            kwargs.setdefault("allow_private_network", True)
        return add_middleware(app, middleware_class, *args, **kwargs)

    app.add_middleware = _add_middleware

    # APPEND, do not add_middleware. Starlette's add_middleware inserts at
    # index 0 and index 0 is OUTERMOST, so calling it here would put auth
    # outside CORS. A browser preflight carries no credentials by definition,
    # so it would get a bare 401 with no Access-Control-Allow-* headers and
    # every cross-origin caller -- OpenBB Workspace at pro.openbb.co, the
    # whole point of the stack -- would be locked out. curl never sends a
    # preflight, so no amount of curl testing catches it.
    #
    # Appending makes auth INNERMOST, which holds whether or not
    # openbb-platform-api adds its own CORS layer after this factory returns.
    # Innermost still covers every path: middleware wraps the whole app, not a
    # router.
    app.user_middleware.append(
        Middleware(BaseHTTPMiddleware, dispatch=_require_basic_auth)
    )

    return app

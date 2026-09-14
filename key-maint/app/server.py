"""FastAPI app factory. The role fixed at construction decides the tier
ceiling: admin bind = tier 3 always; network bind = tier 2 for tailnet
clients (X-Forwarded-For in CGNAT 100.64/10, as set by tailscaled), tier 1
otherwise — including absent or unparseable XFF (fail toward less
disclosure). Funnel port note: serve proxies :10000 -> this app."""
from __future__ import annotations

import ipaddress
import logging
import os
import time

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.auth import make_guard
from app.credfile import load_with_warnings, parse_text, set_value
from app.probes import probe_one_provider, run_probes
from app.registry import PROVIDERS
from app.rows import build_rows, build_summary

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_audit = logging.getLogger("app.audit")

WIDGETS = {
    "provider_api_keys": {
        "name": "Provider API Keys",
        "description": "State of every provider API key on the backend: set/missing, "
        "public-demo-key highlight, and on-demand live key tests.",
        "category": "Admin",
        "type": "table",
        "endpoint": "keys",
        "gridData": {"w": 30, "h": 12},
        "data": {"dataKey": "rows", "table": {"showAll": True}},
        "params": [
            {
                "paramName": "run_tests",
                "label": "Run live key tests",
                "type": "boolean",
                "value": False,
                "description": "Fire one real request per configured provider "
                "(tailnet/admin connections only).",
            }
        ],
    },
    # A second view of the same endpoint for BDOBB, which renders type
    # "keys" natively (pills, reachability dots, per-row test, tier-3
    # editing). Workspace does not know this type, which is exactly why the
    # table above stays: this is additive.
    "provider_api_keys_panel": {
        "name": "Provider API Keys (panel)",
        "description": "Key state and vendor reachability, with per-row test "
        "and (admin only) editing.",
        "category": "Admin",
        "type": "keys",
        "endpoint": "keys",
        "gridData": {"w": 30, "h": 14},
        "data": {"dataKey": "rows"},
        # No raw view: it would expose every value the tier returns.
        "raw": False,
        "params": [],
    },
    # Third view of the same endpoint (Ep. 3). Workspace renders "markdown"
    # natively, so a dashboard gets a prose panel without another service:
    # /keys already carries the data, the tier gate, and the auth.
    "provider_api_keys_summary": {
        "name": "Provider API Keys (summary)",
        "description": "Which provider keys are configured, which are missing, "
        "and which are still on a public demo key.",
        "category": "Admin",
        "type": "markdown",
        "endpoint": "keys",
        "gridData": {"w": 15, "h": 8},
        "data": {"dataKey": "summary"},
        # Same reason as the panel above: tier 3 returns credential values in
        # the rows this summary is built from, so never offer the raw payload.
        "raw": False,
        "params": [],
    },
}


def _restart_required(cred_file: str, started_at: float) -> bool:
    """True once credentials.env has been written since this process's
    environ was frozen at startup -- os.environ never sees that write, so
    GET /keys reading the live file would otherwise show a value as "set"
    with nothing indicating the running API still has the old one."""
    try:
        return os.path.getmtime(cred_file) > started_at
    except OSError:
        return False


def _tier(role: str, request: Request) -> int:
    if role == "admin":
        return 3
    xff = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    try:
        if ipaddress.ip_address(xff) in _CGNAT:
            return 2
    except ValueError:
        pass
    return 1


def create_app(role: str, cred_file: str, auth_file: str) -> FastAPI:
    if role not in ("network", "admin"):
        raise ValueError(f"unknown role: {role}")
    started_at = time.time()
    app = FastAPI(
        dependencies=[Depends(make_guard(auth_file))],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://pro.openbb.co"],
        allow_methods=["GET", "PUT", "OPTIONS"],
        allow_headers=["Authorization"],
    )

    @app.get("/widgets.json")
    def widgets() -> Response:
        return JSONResponse(WIDGETS)

    @app.get("/keys")
    async def keys(request: Request, run_tests: bool = False) -> Response:
        tier = _tier(role, request)
        values, malformed = load_with_warnings(cred_file)
        tests = None
        if run_tests and tier >= 2 and values is not None:
            tests = await run_probes(values)
        rows = build_rows(values, tier, tests, malformed=malformed)
        return JSONResponse(
            {
                "tier": tier,
                "rows": rows,
                "summary": build_summary(rows),
                "restart_required": _restart_required(cred_file, started_at),
            }
        )

    @app.get("/keys/{env_var}/test")
    async def test_key(env_var: str, request: Request) -> Response:
        if env_var not in PROVIDERS:
            return JSONResponse({"detail": "unknown provider"}, status_code=404)
        if _tier(role, request) < 2:
            return JSONResponse({"detail": "not permitted"}, status_code=403)
        values, _ = load_with_warnings(cred_file)
        result = await probe_one_provider(env_var, values or {})
        return JSONResponse({"result": result.result, "detail": result.detail})

    @app.put("/keys/{env_var}")
    async def put_key(env_var: str, request: Request) -> Response:
        # Deliberately NOT a Pydantic body model: FastAPI's 422 response
        # echoes the offending input, which would print the secret. The body
        # is parsed by hand and never reflected.
        if env_var not in PROVIDERS:
            return JSONResponse({"detail": "unknown provider"}, status_code=404)
        if _tier(role, request) < 3:
            return JSONResponse({"detail": "not permitted"}, status_code=403)
        try:
            payload = await request.json()
            value = payload["value"]
            if not isinstance(value, str):
                raise ValueError
        except Exception:
            # No exception detail: the parsed body holds the secret.
            return JSONResponse({"detail": "body must be {\"value\": string}"}, status_code=400)

        # credentials.env is dotenv-with-compose-semantics (see
        # app.credfile's module docstring): a value containing " #" is cut
        # as an inline comment on the NEXT read, and leading/trailing
        # whitespace is stripped. Writing one of those would leave this
        # response's "set" status lying about what the container actually
        # loads on restart, so reject rather than silently write something
        # different from what the admin typed.
        if parse_text(f"{env_var}={value}").get(env_var) != value:
            return JSONResponse(
                {
                    "detail": "value would not round-trip through credentials.env "
                    "(no leading/trailing whitespace, no ' #')"
                },
                status_code=400,
            )

        try:
            set_value(cred_file, env_var, value)
        except ValueError as e:
            # Class name only, same reasoning as the OSError branch below.
            # This branch is load-bearing, not unreachable: the round-trip
            # check above only guards against parse_text/set_value
            # disagreeing on what a value parses back to, and a lone
            # surrogate (e.g. "\ud800") passes that check -- json.loads
            # accepts it and it round-trips through parse_text unchanged --
            # but then fails utf-8 encoding when set_value writes the file,
            # raising UnicodeEncodeError (a ValueError subclass). That case
            # reaches this handler in production.
            return JSONResponse(
                {"detail": f"invalid value: {type(e).__name__}"}, status_code=400
            )
        except OSError as e:
            # Class name only — the message could carry the path, and a
            # traceback could carry the value.
            return JSONResponse(
                {"detail": f"write failed: {type(e).__name__}"}, status_code=500
            )

        # Audit trail for who/when/what changed -- never the value itself.
        # role is always "admin" here (the tier<3 gate above already refused
        # anything else), logged anyway so the line still says so if that
        # gate ever changes.
        _audit.info(
            "credential %s: role=%s env_var=%s",
            "set" if value else "cleared",
            role,
            env_var,
        )

        # The running openbb-api cannot see this: OpenBB fills Credentials
        # from os.environ, a container's environ is frozen at process start,
        # and UserService is a singleton read once per process. Say so rather
        # than letting the user think the change took effect.
        return JSONResponse({"status": "set" if value else "empty", "restart_required": True})

    return app

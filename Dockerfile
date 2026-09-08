# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

# OpenBB Platform API + MCP server, containerized. Companion image for the
# Adventures in OpenBB series (v9.0.0).
#
# Scope (v9.0.0): the Platform REST API with OpenBB's standard providers,
# the analysis extensions, the official MCP server for AI agents, and the
# EODHD provider extension. No CLI/Terminal; other custom providers arrive
# with their episodes.

# --- kdb-x runtime (Ep. 10) -------------------------------------------------
# This image ships NO kdb+/kdb-x software. KX's licence does not permit this
# repo's owner to redistribute their binary -- not even the unlicensed q
# server -- so there is nothing to COPY in here. The operator supplies their
# own q, either by mounting it at /kdb (see kdb/README.md) or by running
# their own kdb container and pointing KDB_HOST at it. Either way, the image
# built from this Dockerfile contains no KX code at all.

FROM python:3.12-slim

# OpenBB version. Override with --build-arg to track a newer release.
ARG OPENBB_VERSION=4.7.2

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OPENBB_HOME=/root/.openbb_platform \
    # Applied to EVERY pip install below so nothing drags a shared lib past
    # what the stack tolerates. See extension-constraints.txt.
    PIP_CONSTRAINT=/tmp/extension-constraints.txt

COPY extension-constraints.txt /tmp/extension-constraints.txt

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip setuptools wheel

# OpenBB Platform + analysis extensions.
# NOTE: the `[all]` extra is intentionally NOT used; it pulls openbb-charting,
# whose GUI chart windows cannot render in a headless container. Chart-type
# widgets still work: endpoints return Plotly figure JSON and the *client*
# renders it (see Ep. 2).
RUN pip install \
        "openbb==${OPENBB_VERSION}" \
        "openbb-technical==1.6.2" \
        "openbb-quantitative==1.6.2" \
        "openbb-econometrics==1.7.2"

# Patch openbb_cftc.cftc_router.build_choices: upstream calls .strip() on
# contract fields that can be NULL. The call runs in the COT router's lifespan
# context, so an AttributeError there takes the whole REST server down at
# startup.
#
# Two shapes have to be covered. openbb-cftc 1.4.2 rewrote `d.name.strip()`
# as `getattr(d, "name", "").strip()`, and a getattr default does NOT help
# when the attribute exists and IS None -- which is exactly the CFTC case. The
# `openbb` distribution pins its providers with floors (>=1.4.1,<2.0.0), not
# exact versions, so a rebuild can land on either shape.
#
# The patch asserts it changed something, and that neither vulnerable shape
# survives. An ast.parse check alone passes happily on source that was never
# touched -- which is how the earlier version of this patch went silent when
# 1.4.2 shipped, quietly restoring the very crash it exists to prevent.
# Resolving the path from the module (rather than hardcoding site-packages)
# keeps it working across base-image Python bumps.
RUN python - <<'PY'
import ast, importlib.util, pathlib, re, sys

GETATTR = r"""getattr\(d,\s*(['"])(\w+)\1,\s*(?:''|"")\)\.strip\(\)"""
ATTR = r"""\bd\.(\w+)\.strip\(\)"""

spec = importlib.util.find_spec("openbb_cftc")
if spec is None or not spec.submodule_search_locations:
    sys.exit("openbb_cftc is not installed -- cannot apply the NULL-field patch")

path = pathlib.Path(list(spec.submodule_search_locations)[0]) / "cftc_router.py"
src, n_getattr = re.subn(GETATTR, r"""(getattr(d, \1\2\1, '') or '').strip()""", path.read_text())
src, n_attr = re.subn(ATTR, r"(d.\1 or '').strip()", src)

if not n_getattr + n_attr:
    sys.exit(f"cftc_router patch matched nothing in {path} -- upstream changed shape again")
for name, pattern in (("getattr", GETATTR), ("attribute", ATTR)):
    if re.search(pattern, src):
        sys.exit(f"cftc_router still has unguarded {name} .strip() calls after patching")

# Second defect in the same file, and a worse one. build_choices() fetches
# contracts from cftc.gov over the network, and upstream awaits it UNGUARDED
# in the router lifespan -- so a slow or unreachable CFTC aborts uvicorn's
# startup and exits the container. The whole API, every provider, gone
# because one government endpoint timed out. Observed on 2 of 10 CI runners
# building byte-identical images.
LIFESPAN = re.compile(r"(async def _cot_router_lifespan\(_\):\n)(    await build_choices\(\)\n)")
GUARD = (
    r"\1"
    "    try:\n"
    "        await build_choices()\n"
    "    except Exception as exc:  # noqa: BLE001\n"
    "        import sys as _sys\n"
    "        print(\n"
    "            f\"CFTC choices unavailable at startup ({type(exc).__name__}: {exc}). \"\n"
    "            f\"COT search has no prebuilt choices; other endpoints are unaffected.\",\n"
    "            file=_sys.stderr,\n"
    "            flush=True,\n"
    "        )\n"
)
src, n_life = LIFESPAN.subn(GUARD, src)
if n_life != 1:
    sys.exit(f"cftc_router lifespan guard matched {n_life} times -- upstream changed shape")

ast.parse(src)
path.write_text(src)

# Assert the invariant on the FINAL source, not on the substitution count: no
# bare `await` may remain at the lifespan's top level. This is the check the
# original patch lacked, which is how it went silent when 1.4.2 shipped.
tree = ast.parse(src)
fn = next((f for f in ast.walk(tree)
           if isinstance(f, ast.AsyncFunctionDef) and f.name == "_cot_router_lifespan"), None)
if fn is None:
    sys.exit("cftc_router has no _cot_router_lifespan -- upstream restructured")
if any(isinstance(st, ast.Expr) and isinstance(st.value, ast.Await) for st in fn.body):
    sys.exit("cftc_router lifespan still awaits unguarded -- a CFTC outage would kill startup")

print(f"cftc_router patched: {n_getattr} getattr + {n_attr} attribute call(s) guarded, "
      f"lifespan network call guarded")
PY

# Patch openbb_core.api.rest_api to require Basic auth on the WHOLE app.
# Upstream wires authenticate_user only into the /api/v1 command router, so
# the Workspace metadata routes that openbb_platform_api.main hangs off this
# same app (/, /widgets.json, /apps.json, /agents.json) and FastAPI's own
# /docs, /redoc and /openapi.json are served unauthenticated even with
# OPENBB_API_AUTH=true. Funnel can publish this port to the public internet,
# so the lock has to cover everything -- see docker-compose.yml's header,
# which has told the reader since v2.0.0 that /widgets.json returns 401
# without credentials. Until this patch, it did not.
#
# Middleware rather than a route dependency: /docs and /openapi.json are
# registered by FastAPI itself and have no router to attach one to, and
# middleware stays correct if a future OpenBB adds another root route.
#
# ORDER IS LOAD-BEARING. Starlette's add_middleware inserts at index 0 and
# builds the stack in reverse, so the middleware registered LAST runs FIRST.
# This block is injected ABOVE the CORSMiddleware registration, which leaves
# CORS outermost. Get it backwards and auth outranks CORS: a browser preflight
# (which by definition carries no credentials) gets a bare 401 with no
# Access-Control-Allow-* headers, and every cross-origin caller -- OpenBB
# Workspace at pro.openbb.co, the whole point of the stack -- is locked out.
# curl never sends a preflight, so no amount of curl testing catches it.
RUN python - <<'PY'
import pathlib
p = pathlib.Path("/usr/local/lib/python3.12/site-packages/openbb_core/api/rest_api.py")
src = p.read_text()
anchor = "app.add_middleware(\n    CORSMiddleware,"
assert anchor in src, "rest_api.py CORS registration not found - upstream changed"
guard = '''
import base64 as _base64
import binascii as _binascii
import secrets as _secrets

from starlette.responses import Response as _Response


@app.middleware("http")
async def _require_basic_auth(request, call_next):
    """Require HTTP Basic auth on every path when OPENBB_API_AUTH is set.

    No-ops when auth is off, which is what keeps the in-process openbb-mcp
    wrapper (started deliberately without api-auth.env) working.
    """
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
                _base64.b64decode(header[6:]).decode("utf8").partition(":")
            )
        except (_binascii.Error, UnicodeDecodeError, ValueError):
            supplied_user = supplied_pw = ""
    ok_user = _secrets.compare_digest(supplied_user.encode(), username.encode())
    ok_pw = _secrets.compare_digest(supplied_pw.encode(), password.encode())
    # `username and password` fails CLOSED when either is unconfigured:
    # without it, compare_digest("", "") is true on both halves and
    # `Basic <base64 of ":">` would authenticate against an empty pair.
    if not (username and password and ok_user and ok_pw):
        return _Response(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return await call_next(request)


'''
p.write_text(src.replace(anchor, guard + anchor, 1))
PY
RUN python -c "import ast; ast.parse(open('/usr/local/lib/python3.12/site-packages/openbb_core/api/rest_api.py').read()); print('rest_api auth patch parses OK')"

# Patch openbb_core.api.rest_api CORS setup to answer Chrome's Private Network
# Access preflight. OpenBB Workspace runs at https://pro.openbb.co (a public
# origin); when the browser classifies your backend's address as private/local
# (see Ep. 2's gotchas), the fetch is gated behind an OPTIONS preflight carrying
# Access-Control-Request-Private-Network: true. Starlette rejects it unless
# allow_private_network=True, and OpenBB exposes no setting for this.
RUN python - <<'PY'
import pathlib
p = pathlib.Path("/usr/local/lib/python3.12/site-packages/openbb_core/api/rest_api.py")
src = p.read_text()
anchor = "    allow_headers=system.api_settings.cors.allow_headers,\n)"
assert anchor in src, "rest_api.py CORS block not found - upstream changed"
p.write_text(src.replace(anchor, anchor.replace("\n)", "\n    allow_private_network=True,\n)"), 1))
PY
RUN python -c "import ast; ast.parse(open('/usr/local/lib/python3.12/site-packages/openbb_core/api/rest_api.py').read()); print('rest_api CORS patch parses OK')"

# Custom EODHD provider extension (Ep. 9): equity/ETF/crypto/forex historical
# (EOD + intraday) and fundamentals via the official SDK, pinned to a GitHub
# commit (the PyPI release predates the SDK's typed errors and timeouts).
COPY openbb-eodhd/ /opt/openbb-eodhd/
RUN pip install /opt/openbb-eodhd

# Shared kdb+ session/store plumbing (Ep. 10): openbb-kdb and live-grid both
# depend on the "kdb-store" distribution now, and it is not published to
# PyPI, so it must be installed from this checkout before openbb-kdb (whose
# own pyproject.toml lists it as a dependency) or pip has nothing to resolve
# "kdb-store" against.
COPY kdb-store /tmp/kdb-store
RUN pip install --no-cache-dir /tmp/kdb-store && rm -rf /tmp/kdb-store

# kdb read-through cache provider (Ep. 10).
COPY openbb-kdb /tmp/openbb-kdb
RUN pip install --no-cache-dir /tmp/openbb-kdb && rm -rf /tmp/openbb-kdb

# Official OpenBB MCP server (Ep. 6): wraps the Platform FastAPI app
# in-process and serves MCP over streamable-http. PIP_CONSTRAINT still
# applies, so it cannot drag shared libs anywhere the stack doesn't tolerate.
RUN pip install "openbb-mcp-server==1.4.1"
RUN python -c "import openbb_mcp_server; print('openbb-mcp-server import OK')"

# Pre-compile the static package so the first run is instant, and verify the
# platform registers at build time.
RUN python -c "import openbb; openbb.build(); from openbb import obb; \
assert 'eodhd' in obb.coverage.providers, 'eodhd provider not registered'; \
assert 'kdb' in obb.coverage.providers, 'kdb provider not registered'; \
print('OpenBB Platform OK:', len(obb.coverage.providers), 'providers (incl. eodhd, kdb)')"

WORKDIR /workspace

# Self-provision persistent mount points so the image is drop-in on any host
# (NAS container managers, plain Docker) with bind mounts to not-yet-created
# paths. OPENBB_HOME persists settings/credentials; /workspace holds user data.
ENV APP_DIRS="/root/.openbb_platform /workspace"
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Default: the REST API on loopback (the compose file's Tailscale sidecar is
# the only way in — see docker-compose.yml).
CMD ["openbb-api", "--host", "127.0.0.1", "--port", "6900"]

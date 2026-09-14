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

# ArcticDB store + provider extension (Ep. 11). Bars and ticks persisted to
# S3/MinIO can stand in for an upstream API call via provider="arcticdb".
#
# ArcticDB ships manylinux x86_64 wheels ONLY -- no aarch64. That is why the
# compose services pin platform: linux/amd64.
COPY openbb-arcticdb /tmp/openbb-arcticdb
RUN pip install --no-cache-dir /tmp/openbb-arcticdb && rm -rf /tmp/openbb-arcticdb

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
assert 'arcticdb' in obb.coverage.providers, 'arcticdb provider not registered'; \
print('OpenBB Platform OK:', len(obb.coverage.providers), 'providers (incl. eodhd, kdb, arcticdb)')"

# The FastAPI app factory `openbb-api --factory` serves (see api_app.py):
# the stock Platform app with Chrome's Private Network Access preflight
# answered. Replaces an earlier build-time rewrite of openbb_core's
# rest_api.py -- same effect, but through the documented `--app/--factory`
# entrypoint instead of a text substitution against upstream source.
COPY api_app.py /opt/api_app.py
RUN python -c "\
from openbb_platform_api.utils.api import import_app; \
from starlette.middleware.cors import CORSMiddleware; \
app = import_app('/opt/api_app.py', 'main', True); \
layers = [mw for mw in app.user_middleware if mw.cls is CORSMiddleware]; \
assert layers, 'api_app: no CORS middleware on the built app'; \
assert all(mw.kwargs.get('allow_private_network') is True for mw in layers), \
    'api_app: a CORS layer is missing allow_private_network'; \
print('api_app factory OK:', len(layers), 'CORS layer(s), all private-network enabled')"

WORKDIR /workspace

# stores-mcp (Ep. 11): read-only ArcticDB/kdb+ discovery/query MCP server.
# Runs `python /opt/mcp_stores/server.py` (see docker-compose.yml's
# stores-mcp service) directly against this image -- nothing extra to
# install: fastmcp came in with openbb-mcp-server above, arcticdb/pandas with
# openbb-arcticdb, pykx with openbb-kdb. Just the two files.
COPY mcp_stores/server.py mcp_stores/test_server.py /opt/mcp_stores/

# Self-provision persistent mount points so the image is drop-in on any host
# (NAS container managers, plain Docker) with bind mounts to not-yet-created
# paths. OPENBB_HOME persists settings/credentials; /workspace holds user data.
ENV APP_DIRS="/root/.openbb_platform /workspace"
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Default: the REST API on loopback (the compose file's Tailscale sidecar is
# the only way in — see docker-compose.yml). `--app/--factory` serves
# api_app.py's factory rather than the stock app, which is what answers
# Chrome's Private Network Access preflight.
CMD ["openbb-api", "--app", "/opt/api_app.py", "--name", "main", "--factory", \
     "--host", "127.0.0.1", "--port", "6900"]

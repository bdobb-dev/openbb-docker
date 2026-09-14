# Locking the Platform API's metadata endpoints

**Date:** 2026-08-05
**Status:** approved, not yet implemented

## Problem

`docker-compose.yml` states the stack's central invariant as *"Lock first, then
door: auth is REQUIRED before the funnel config exists, by construction."*
That is not true today. With `OPENBB_API_AUTH=true` the API serves eight root
paths to anyone, credentials or not.

Measured against `openbb-local:10.0.0` run with
`OPENBB_API_AUTH=true / OPENBB_API_USERNAME=ci / OPENBB_API_PASSWORD=ci-secret`:

| path | no creds | wrong creds | right creds |
|---|---|---|---|
| `/` (landing page) | 200 | 200 | 200 |
| `/widgets.json` | 200 | 200 | 200 |
| `/apps.json` | 200 | 200 | 200 |
| `/agents.json` | 200 | 200 | 200 |
| `/openapi.json` | 200 | 200 | 200 |
| `/docs` (Swagger UI) | 200 | 200 | 200 |
| `/redoc` | 200 | 200 | 200 |
| `/docs/oauth2-redirect` | 200 | 200 | 200 |
| `/api/v1/equity/price/quote` | 401 | 401 | 422 |

`ts-config/serve-funnel.json` sets `AllowFunnel` on `:443`, which proxies
`127.0.0.1:6900`. So on a funneled stack all eight are readable from the public
internet: the full 282-widget catalogue, every endpoint path and parameter
schema, and an interactive Swagger console.

No market data and no credentials leak — the `/api/v1` command router is
correctly locked, and `/agents.json` is empty while `/apps.json` holds only
OpenBB's bundled example apps. The exposure is the API's shape, not its
contents. The defect is that the invariant as written is false.

### Root cause

`openbb-api` runs `openbb_platform_api/main.py`, which imports the core FastAPI
`app` from `openbb_core.api.rest_api` and hangs `/`, `/widgets.json`,
`/apps.json` and `/agents.json` directly off it with no `dependencies=`.
`/docs`, `/redoc` and `/openapi.json` are FastAPI built-ins that were never
guarded at all. Upstream's `authenticate_user` → `get_user_settings` dependency
chain (`openbb_core/api/auth/user.py`) is reachable only from the `/api/v1`
command router. `OPENBB_API_AUTH` therefore *cannot* guard the metadata routes,
no matter how it is configured.

## Decision

Patch the image so Basic auth covers the whole app.

Two alternatives were considered and rejected:

- **Enforce at the Tailscale Serve layer.** Not possible. Serve has no
  Basic-auth primitive; its only auth is `tailscale_auth` identity-header
  injection, which by design carries nothing over Funnel — the reason
  `docker-compose.yml` notes that rss-ticker "would fail closed" if funneled.
  A reverse-proxy sidecar (Caddy/nginx) would work but adds an image, a config
  file and a hop on every request. It would also collide with in-flight work:
  another worktree is currently rewriting both `serve.json` and
  `serve-funnel.json` into Tailscale `Services`.
- **Accept and document.** Cheapest and zero-risk, but leaves a public Swagger
  console on a stack whose stated design principle is the opposite.

The chosen fix is also the exact diff an upstream PR would carry, so it does
not foreclose fixing this in OpenBB proper later.

## Design

### 1. Dockerfile patch

A third `RUN python - <<'PY'` block, placed immediately after the existing CORS
patch, following the idiom already established there and in the `cftc_router`
patch: assert the anchor, rewrite, then verify with a separate `ast.parse` RUN.
The `assert` matters — if a future OpenBB restructures `rest_api.py`, the build
fails loudly instead of silently producing an unlocked image.

It injects, ahead of `AppLoader.add_routers`, an HTTP middleware that:

- returns `await call_next(request)` untouched when `Env().API_AUTH` is falsy;
- otherwise requires a well-formed `Basic` header and compares both halves with
  `secrets.compare_digest`;
- returns `401` with `WWW-Authenticate: Basic` on anything else.

```python
anchor = "AppLoader.add_routers(\n    app=app,"
assert anchor in src, "rest_api.py router block not found - upstream changed"
```

The middleware body:

```python
@app.middleware("http")
async def _require_basic_auth(request, call_next):
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
    # `username and password` fails closed when either is unconfigured.
    if not (username and password and ok_user and ok_pw):
        return _Response(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return await call_next(request)
```

Three design points, each load-bearing:

**Middleware, not a route dependency.** `/docs`, `/redoc` and `/openapi.json`
are registered by FastAPI itself and have no router to attach `dependencies=`
to. Middleware also stays correct if a future OpenBB version adds another root
route — the failure mode of the dependency approach is silent.

**Gated on `Env().API_AUTH`, evaluated per request.** This is what keeps the
MCP server working. `docker-compose.yml` deliberately does *not* load
`api-auth.env` into `openbb-mcp` ("auth on this in-process wrapper breaks its
tool calls"), so `API_AUTH` is false there and the middleware no-ops. Evaluated
per request rather than hoisted to import time because `openbb_platform_api`
calls `Env()` *after* importing `rest_api`, and the verified-working probe used
the per-request form; the cost is an `os.environ` read.

**Fails closed on unconfigured credentials.** The `username and password`
conjunction is not redundant. Without it, an unset `OPENBB_API_USERNAME` and
`OPENBB_API_PASSWORD` would make `compare_digest("", "")` true on both halves,
so `Basic <base64 of ":">` would authenticate. Compose makes `api-auth.env`
required, so this cannot arise in this stack — but the patch should not depend
on a guarantee that lives in a different file.

### 2. Compose and docs

`docker-compose.yml` — the "Lock first, then door" header paragraph becomes
accurate rather than aspirational. It gains a sentence naming *why* the image
patch exists (upstream wires auth only into the command router) so a future
reader does not delete it as redundant.

`docs/funnel.md` — the §1 and §4 verification snippets already curl
`/widgets.json` expecting 401 then 200. Those become true as written. Add a
short subsection listing the full root surface with a one-liner that checks all
of it, so "verify before you funnel" means verifying everything rather than one
path.

`ts-config/serve.json` and `ts-config/serve-funnel.json` are **not touched** —
another worktree owns them right now.

### 3. CI

`build-smoke` in `.github/workflows/ci.yml` keeps its existing
`/widgets.json` → 401 assertion, which this change makes true for the first
time since v2.0.0, and gains a command-endpoint assertion:

- `/api/v1/equity/price/quote`: `401` no-creds, `401` wrong-creds, `422`
  right-creds. The 422 is the strongest cheap signal available — it proves auth
  passed and request validation rejected the empty query.
- Wrong-creds is asserted on both paths. Right-creds-200 alone does not prove a
  bad password is rejected.

Note for whoever picks this up: no branch in this repo currently carries a
"corrected" CI assertion. `main`, `ep10-kdb-cache`, `ep11-arcticdb-minio` and
all three claude worktrees still assert 401 on `/widgets.json`. This change
fixes the stack rather than the assertion.

## Success criteria

Verified against a freshly built image, not asserted:

1. All eight root paths (`/`, `/widgets.json`, `/apps.json`, `/agents.json`,
   `/openapi.json`, `/docs`, `/redoc`, `/docs/oauth2-redirect`): `401` with no
   credentials, `401` with wrong credentials, `200` with right credentials.
2. `/api/v1/equity/price/quote`: `401` / `401` / `422`.
3. `/widgets.json` with credentials still returns 282 widgets.
4. `openbb-mcp` launched from the same image with no `api-auth.env`: a full
   MCP `initialize` handshake returns `200` with an `mcp-session-id`.
5. `docker build` succeeds, including the `ast.parse` verification step.
6. `openbb-mcp` and `openbb-api` both start clean — no import-order regression
   from the injected `Env()` use.

Criteria 1–4 were already confirmed against a hand-patched
`openbb-local:10.0.0` before this spec was written; the implementation must
reproduce them from the Dockerfile.

## Out of scope

- Upstreaming to OpenBB. The patch is written so it can become a PR later, but
  no outbound work happens without a separate decision.
- Any change to `ts-config/*.json`, Tailscale Serve, or the Funnel topology.
- The other funneled port, `:10000` (key-maint), which enforces its own Basic
  auth via `key-maint/app/auth.py` and is unaffected.

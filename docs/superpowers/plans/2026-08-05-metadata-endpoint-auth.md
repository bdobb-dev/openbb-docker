# Metadata Endpoint Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `OPENBB_API_AUTH=true` lock every path the OpenBB Platform API serves, not just `/api/v1/*`, so the compose file's "lock first, then door" invariant is true by construction.

**Architecture:** A build-time patch injects a blanket HTTP Basic auth middleware into `openbb_core/api/rest_api.py`, ahead of `AppLoader.add_routers`. The middleware no-ops when `OPENBB_API_AUTH` is unset, which is what keeps the `openbb-mcp` container (deliberately started without `api-auth.env`) working. A new `scripts/verify-auth.sh` is the executable statement of the posture; CI asserts a two-path subset of it inline.

**Tech Stack:** Docker, Python 3.12, FastAPI/Starlette, bash, GitHub Actions.

## Global Constraints

- Patch target is `/usr/local/lib/python3.12/site-packages/openbb_core/api/rest_api.py` inside the image. Never edit `~/Developer/OpenBB` — that is a separate source checkout, not this build.
- Every Dockerfile patch follows the established idiom: `assert anchor in src, "... - upstream changed"`, then a **separate** `RUN python -c "import ast; ast.parse(...)"` verification line. A patch that silently no-ops is worse than a build failure.
- Do **not** modify `ts-config/serve.json` or `ts-config/serve-funnel.json`. Another worktree owns those files right now.
- The middleware must fail closed when `OPENBB_API_USERNAME` or `OPENBB_API_PASSWORD` is unset. Do not rely on compose making `api-auth.env` required.
- CI asserts `/widgets.json` and `/api/v1/equity/price/quote` only. The full eight-path sweep lives in `scripts/verify-auth.sh`, not in `ci.yml` — this was an explicit decision, do not "improve" CI into a full sweep.
- Image builds are slow (multi-minute). Each task that requires a build says so; do not rebuild when a task does not need it.

---

### Task 1: The auth posture check (failing test)

Write the verification script first. It must fail against the current image — that failure is the bug this plan fixes.

**Files:**
- Create: `scripts/verify-auth.sh`

**Interfaces:**
- Consumes: nothing.
- Produces: `scripts/verify-auth.sh`, invoked as
  `OPENBB_URL=<base> OPENBB_USER=<u> OPENBB_PASS=<p> scripts/verify-auth.sh`.
  Exit 0 = every path locked. Exit 1 = something is served unauthenticated.
  Referenced by Task 4's docs.

- [ ] **Step 1: Write the script**

Create `scripts/verify-auth.sh`:

```bash
#!/usr/bin/env bash
# Verify the API's auth posture: EVERY path requires credentials.
#
# Upstream wires Basic auth only into the /api/v1 command router, so the
# Workspace metadata routes and FastAPI's own docs were historically served
# to anyone. The Dockerfile patches that (see the "blanket Basic auth" block).
# This script is the check that the patch is still doing its job.
#
#   OPENBB_USER=openbb OPENBB_PASS=<password> \
#     OPENBB_URL=https://openbb.<your-tailnet>.ts.net scripts/verify-auth.sh
#
# or from inside the container's own namespace:
#
#   docker exec -i openbb-api bash -c \
#     'OPENBB_URL=http://127.0.0.1:6900 OPENBB_USER=<u> OPENBB_PASS=<p> bash -s' \
#     < scripts/verify-auth.sh
set -euo pipefail
: "${OPENBB_URL:?set OPENBB_URL to the base URL to test}"
: "${OPENBB_USER:?set OPENBB_USER}"
: "${OPENBB_PASS:?set OPENBB_PASS}"

# Every root path the stack serves outside /api/v1. The command router is
# checked separately: a correct request there returns 422 (auth passed, empty
# query rejected), not 200.
PATHS=(
  /
  /widgets.json
  /apps.json
  /agents.json
  /openapi.json
  /docs
  /redoc
  /docs/oauth2-redirect
)

fail=0

# check <label> <expected-code> [curl args...]
check() {
  local label="$1" expected="$2"
  shift 2
  local got
  got=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@" || echo 000)
  if [ "$got" = "$expected" ]; then
    printf '   OK   %-24s %s\n' "$label" "$got"
  else
    printf '   FAIL %-24s expected %s, got %s\n' "$label" "$expected" "$got" >&2
    fail=1
  fi
}

echo "1/4 no credentials -> 401 on every root path..."
for p in "${PATHS[@]}"; do
  check "$p" 401 "$OPENBB_URL$p"
done

# NOTE: if your real username is literally "wrong", this pass is meaningless.
echo "2/4 wrong credentials -> 401 on every root path..."
for p in "${PATHS[@]}"; do
  check "$p" 401 -u "wrong:wrong" "$OPENBB_URL$p"
done

echo "3/4 right credentials -> 200 on every root path..."
for p in "${PATHS[@]}"; do
  check "$p" 200 -u "$OPENBB_USER:$OPENBB_PASS" "$OPENBB_URL$p"
done

echo "4/4 the command router (422 = auth passed, empty query rejected)..."
q="$OPENBB_URL/api/v1/equity/price/quote"
check "quote no-creds"    401 "$q"
check "quote wrong-creds" 401 -u "wrong:wrong" "$q"
check "quote right-creds" 422 -u "$OPENBB_USER:$OPENBB_PASS" "$q"

if [ "$fail" -ne 0 ]; then
  echo "Auth posture FAILED — something is served unauthenticated." >&2
  exit 1
fi
echo "Auth posture verified: nothing is served without credentials."
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x scripts/verify-auth.sh
```

- [ ] **Step 3: Run it against the CURRENT image to watch it fail**

```bash
docker rm -f authcheck 2>/dev/null; docker build -t openbb-local:authcheck . && docker run -d --name authcheck -e OPENBB_API_AUTH=true -e OPENBB_API_USERNAME=ci -e OPENBB_API_PASSWORD=ci-secret openbb-local:authcheck
```

Wait for boot (the API takes 30–90s), then:

```bash
docker exec -i authcheck bash -c 'OPENBB_URL=http://127.0.0.1:6900 OPENBB_USER=ci OPENBB_PASS=ci-secret bash -s' < scripts/verify-auth.sh
```

Expected: **FAIL**, exit 1. Section 1/4 reports `expected 401, got 200` on all eight paths; section 2/4 the same. Sections 3/4 and 4/4 pass. This is the bug.

- [ ] **Step 4: Commit**

```bash
git add scripts/verify-auth.sh
git commit -m "test: assert every API path requires credentials (currently failing)"
```

---

### Task 2: The Dockerfile patch

**Files:**
- Modify: `Dockerfile` — insert after line 69 (the `rest_api CORS patch parses OK` verification), before the `# Custom EODHD provider extension` comment. The block's *position in the Dockerfile* is unchanged; what changed is the *anchor inside rest_api.py* it injects at.
- Modify: `scripts/verify-auth.sh` — add a preflight regression check.

**Interfaces:**
- Consumes: `scripts/verify-auth.sh` from Task 1.
- Produces: an image in which `OPENBB_API_AUTH=true` locks all paths. No new build args, env vars, ports, or files.

- [ ] **Step 1: Add the patch block**

Insert into `Dockerfile` immediately after the line
`RUN python -c "import ast; ast.parse(open('/usr/local/lib/python3.12/site-packages/openbb_core/api/rest_api.py').read()); print('rest_api CORS patch parses OK')"`:

```dockerfile
# Patch openbb_core.api.rest_api to require Basic auth on the WHOLE app.
# Upstream wires authenticate_user only into the /api/v1 command router, so
# the Workspace metadata routes that openbb_platform_api.main hangs off this
# same app (/, /widgets.json, /apps.json, /agents.json) and FastAPI's own
# /docs, /redoc and /openapi.json are served unauthenticated even with
# OPENBB_API_AUTH=true. Funnel can publish this port to the public internet,
# so the lock has to cover everything — see docker-compose.yml's header.
#
# Middleware rather than a route dependency: /docs and /openapi.json are
# registered by FastAPI itself and have no router to attach one to, and
# middleware stays correct if a future OpenBB adds another root route.
#
# ORDER IS LOAD-BEARING. Starlette's add_middleware inserts at index 0 and
# builds the stack in reverse, so the middleware registered LAST runs FIRST.
# This block must therefore be injected BEFORE the CORSMiddleware
# registration, which leaves CORS outermost. Get it backwards and auth
# outranks CORS: a browser preflight (which by definition carries no
# credentials) gets a bare 401 with no Access-Control-Allow-* headers, and
# every cross-origin caller — OpenBB Workspace at pro.openbb.co, the whole
# point of the stack — is locked out. curl never sends a preflight, so no
# amount of curl testing catches it.
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
```

Note on `Env()` being called per request rather than hoisted: `openbb_platform_api.main` calls `Env()` *after* importing `rest_api`, and the per-request form is what was verified working. The cost is an `os.environ` read.

- [ ] **Step 2: Rebuild and confirm the patch applied**

```bash
docker build -t openbb-local:authcheck .
```

Expected: build succeeds; the log contains both `rest_api CORS patch parses OK` and `rest_api auth patch parses OK`. If the build fails on `AssertionError: rest_api.py router block not found`, upstream restructured the file — stop and re-derive the anchor, do not weaken the assert.

- [ ] **Step 3: Run the Task 1 script — it must now pass**

```bash
docker rm -f authcheck 2>/dev/null
docker run -d --name authcheck -e OPENBB_API_AUTH=true -e OPENBB_API_USERNAME=ci -e OPENBB_API_PASSWORD=ci-secret openbb-local:authcheck
```

Wait for boot, then:

```bash
docker exec -i authcheck bash -c 'OPENBB_URL=http://127.0.0.1:6900 OPENBB_USER=ci OPENBB_PASS=ci-secret bash -s' < scripts/verify-auth.sh
```

Expected: **PASS**, exit 0, final line `Auth posture verified: nothing is served without credentials.`

- [ ] **Step 4: Confirm the widget catalogue is intact**

```bash
docker exec authcheck curl -fsS -u ci:ci-secret http://127.0.0.1:6900/widgets.json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)), "widgets")'
```

Expected: `282 widgets` (any count > 50 is acceptable; a sharp drop means the patch broke widget generation).

- [ ] **Step 5: Confirm the MCP server still works**

This is the regression the patch is most likely to cause. `openbb-mcp` wraps the same FastAPI app in-process and is started with **no** auth env vars.

```bash
docker rm -f mcpcheck 2>/dev/null
docker run -d --name mcpcheck openbb-local:authcheck \
  openbb-mcp --host 127.0.0.1 --port 6901 --default-categories equity --tool-discovery
```

Wait for boot, then:

```bash
docker exec mcpcheck curl -s -i -X POST http://127.0.0.1:6901/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' | head -20
```

Expected: `HTTP/1.1 200 OK`, an `mcp-session-id` header, and an SSE `data:` line containing `"serverInfo":{"name":"OpenBB MCP"`. A `401` here means the `Env().API_AUTH` gate is wrong.

- [ ] **Step 6: Clean up the probe containers**

```bash
docker rm -f authcheck mcpcheck; docker rmi openbb-local:authcheck
```

- [ ] **Step 7: Commit**

```bash
git add Dockerfile
git commit -m "fix: require Basic auth on every API path, not just /api/v1

OPENBB_API_AUTH guarded only the command router, leaving /, /widgets.json,
/apps.json, /agents.json, /openapi.json, /docs, /redoc and
/docs/oauth2-redirect readable by anyone — publicly so on a funneled stack.
Patch rest_api.py with a blanket Basic auth middleware, gated on API_AUTH so
the in-process openbb-mcp wrapper is unaffected."
```

---

### Task 3: Repair the two scripts the lock breaks

`scripts/smoke.sh` and `scripts/verify-isolation.sh` both curl the API with no credentials. `smoke.sh` has in fact been broken against an auth-enabled stack since v2.0.0 — its line 15 hits `/api/v1/...`, which already 401s today.

**Files:**
- Modify: `scripts/smoke.sh` (whole file)
- Modify: `scripts/verify-isolation.sh:8-9`

**Interfaces:**
- Consumes: the locked image from Task 2.
- Produces: `smoke.sh` now requires `OPENBB_USER` / `OPENBB_PASS`; Task 4 updates the README invocation to match.

- [ ] **Step 1: Rewrite `scripts/smoke.sh`**

Replace the whole file with:

```bash
#!/usr/bin/env bash
# Smoke test for a running stack. The API requires credentials on every path,
# so pass them:
#   OPENBB_USER=openbb OPENBB_PASS=<password> \
#     OPENBB_URL=https://openbb.<your-tailnet>.ts.net scripts/smoke.sh
# or test the API directly from inside the container's namespace:
#   docker exec -i openbb-api bash -c \
#     'OPENBB_URL=http://127.0.0.1:6900 OPENBB_USER=<u> OPENBB_PASS=<p> bash -s' \
#     < scripts/smoke.sh
set -euo pipefail
: "${OPENBB_URL:?set OPENBB_URL to the base URL to test}"
: "${OPENBB_USER:?set OPENBB_USER (the API requires auth on every path)}"
: "${OPENBB_PASS:?set OPENBB_PASS}"

auth=(-u "$OPENBB_USER:$OPENBB_PASS")

echo "1/2 widgets.json is served and non-trivial..."
count=$(curl -fsS "${auth[@]}" "$OPENBB_URL/widgets.json" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
[ "$count" -gt 50 ] || { echo "FAIL: only $count widgets"; exit 1; }
echo "   OK: $count widgets"

echo "2/2 a keyless data endpoint answers..."
curl -fsS "${auth[@]}" "$OPENBB_URL/api/v1/equity/price/historical?symbol=AAPL&provider=yfinance" >/dev/null
echo "   OK"
echo "Smoke test passed."
```

- [ ] **Step 2: Fix `scripts/verify-isolation.sh` lines 8-9**

Replace:

```bash
echo "1/2 the front door answers over HTTPS..."
curl -fsS --max-time 10 "https://$host/widgets.json" >/dev/null && echo "   OK"
```

with:

```bash
echo "1/2 the front door answers over HTTPS and is locked..."
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$host/widgets.json" || echo 000)
[ "$code" = "401" ] || { echo "   FAIL: expected 401, got $code" >&2; exit 1; }
echo "   OK: 401 (answering, and refusing anonymous callers)"
```

A 401 proves both halves at once: Serve is proxying, and the API is locked. `curl -fsS` would now abort on the 401 and report a failure that is actually correct behaviour.

- [ ] **Step 3: Syntax-check both scripts**

```bash
bash -n scripts/smoke.sh && bash -n scripts/verify-isolation.sh && echo "both parse OK"
```

Expected: `both parse OK`.

- [ ] **Step 4: Run smoke.sh against a locked container**

```bash
docker build -t openbb-local:authcheck . && docker rm -f authcheck 2>/dev/null
docker run -d --name authcheck -e OPENBB_API_AUTH=true -e OPENBB_API_USERNAME=ci -e OPENBB_API_PASSWORD=ci-secret openbb-local:authcheck
```

Wait for boot, then:

```bash
docker exec -i authcheck bash -c 'OPENBB_URL=http://127.0.0.1:6900 OPENBB_USER=ci OPENBB_PASS=ci-secret bash -s' < scripts/smoke.sh
```

Expected: `Smoke test passed.` (Step 2 needs network egress for yfinance; if the sandbox has none, note the failure as environmental rather than a code defect.)

Then clean up:

```bash
docker rm -f authcheck; docker rmi openbb-local:authcheck
```

`verify-isolation.sh` runs from a second tailnet device and cannot be exercised here — `bash -n` plus review is its check.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke.sh scripts/verify-isolation.sh
git commit -m "fix(scripts): pass credentials now that every path is locked

smoke.sh had been broken against an auth-enabled stack since v2.0.0 — its
/api/v1 call has always 401'd. verify-isolation.sh now asserts 401 rather
than 200, which proves the door answers AND that it is locked."
```

---

### Task 4: CI assertions

**Files:**
- Modify: `.github/workflows/ci.yml:61-65` (the `Auth is enforced` step in `build-smoke`)

**Interfaces:**
- Consumes: the locked image from Task 2.
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Replace the assertion step**

Replace lines 61-65 of `.github/workflows/ci.yml`:

```yaml
      - name: Auth is enforced (401 without, 200 with)
        run: |
          test "$(docker exec api curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:6900/widgets.json)" = "401"
          count=$(docker exec api curl -fsS -u ci:ci-secret http://127.0.0.1:6900/widgets.json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
          echo "widgets: $count"; test "$count" -gt 50
```

with:

```yaml
      - name: Metadata endpoints are locked (401 / 401 / 200)
        run: |
          w=http://127.0.0.1:6900/widgets.json
          test "$(docker exec api curl -s -o /dev/null -w '%{http_code}' $w)" = "401"
          test "$(docker exec api curl -s -o /dev/null -w '%{http_code}' -u ci:wrong $w)" = "401"
          count=$(docker exec api curl -fsS -u ci:ci-secret $w | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
          echo "widgets: $count"; test "$count" -gt 50
      - name: Command router is locked (401 / 401 / 422)
        run: |
          q=http://127.0.0.1:6900/api/v1/equity/price/quote
          test "$(docker exec api curl -s -o /dev/null -w '%{http_code}' $q)" = "401"
          test "$(docker exec api curl -s -o /dev/null -w '%{http_code}' -u ci:wrong $q)" = "401"
          test "$(docker exec api curl -s -o /dev/null -w '%{http_code}' -u ci:ci-secret $q)" = "422"
```

The 422 on right-creds is the strongest cheap signal: it proves auth passed and request validation rejected the empty query. Wrong-creds is asserted on both paths because right-creds-200 alone does not prove a bad password is refused.

Leave the `Boot the API with Basic auth` step alone — its `[ "$code" = "401" -o "$code" = "200" ]` readiness loop still works, and now takes the 401 branch.

- [ ] **Step 2: Validate the YAML parses**

```bash
python3 -c "import yaml,sys; d=yaml.safe_load(open('.github/workflows/ci.yml')); print([s['name'] for s in d['jobs']['build-smoke']['steps'] if 'name' in s])"
```

Expected: `['Build the image', 'Boot the API with Basic auth', 'Metadata endpoints are locked (401 / 401 / 200)', 'Command router is locked (401 / 401 / 422)']`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: assert auth on both the metadata and command routes

The /widgets.json 401 assertion has failed on every run since v2.0.0 because
the stack never returned it. The Dockerfile patch makes it true; add a
command-endpoint assertion so a regression in either router fails CI."
```

---

### Task 5: Compose header and docs

**Files:**
- Modify: `docker-compose.yml:10-25` (header comment)
- Modify: `docs/funnel.md` (§1)
- Modify: `README.md:135` (smoke.sh invocation)

**Interfaces:**
- Consumes: `scripts/verify-auth.sh` from Task 1, the credential-taking `smoke.sh` from Task 3.
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Update the compose header**

In `docker-compose.yml`, replace this paragraph:

```
# From v2.0.0 (Ep. 2) the API enforces HTTP Basic auth, and port 443 MAY be
# published to the public internet via Tailscale Funnel (swap serve.json for
# serve-funnel.json + allow the `funnel` node attribute in your tailnet
# policy — see docs/funnel.md). Lock first, then door: auth is REQUIRED
# before the funnel config exists, by construction.
```

with:

```
# From v2.0.0 (Ep. 2) the API enforces HTTP Basic auth, and port 443 MAY be
# published to the public internet via Tailscale Funnel (swap serve.json for
# serve-funnel.json + allow the `funnel` node attribute in your tailnet
# policy — see docs/funnel.md). Lock first, then door: auth is REQUIRED
# before the funnel config exists, by construction.
#
# "Lock" means the WHOLE app, which takes a patch. Upstream wires Basic auth
# into the /api/v1 command router only, so /, /widgets.json, /apps.json,
# /agents.json, /openapi.json, /docs and /redoc answer 200 to anyone even
# with OPENBB_API_AUTH=true — the widget catalogue, every endpoint path and
# parameter schema, and a live Swagger console, public the moment you funnel.
# The Dockerfile's "blanket Basic auth" patch closes that. Do not remove it
# as redundant with OPENBB_API_AUTH: it is what makes OPENBB_API_AUTH mean
# what this comment says. Verify with scripts/verify-auth.sh.
```

- [ ] **Step 2: Update the compose verify block**

Replace:

```
# Verify after start (see README):
#   from any tailnet device:  https://openbb.<your-tailnet>.ts.net/widgets.json
#                             -> 401 without credentials, 200 with
#   from a SECOND device:     the raw port :6900 must be unreachable
```

with:

```
# Verify after start (see README):
#   from any tailnet device:  OPENBB_URL=https://openbb.<your-tailnet>.ts.net \
#                             OPENBB_USER=<u> OPENBB_PASS=<p> scripts/verify-auth.sh
#                             -> every path 401 without credentials, 200 with
#   from a SECOND device:     the raw port :6900 must be unreachable
```

- [ ] **Step 3: Extend `docs/funnel.md` §1**

In `docs/funnel.md`, after the existing §1 code block (the two `curl` lines checking `/widgets.json`), add:

````markdown
`/widgets.json` is representative, not exhaustive. The stack serves eight
paths outside `/api/v1` — `/`, `/widgets.json`, `/apps.json`, `/agents.json`,
`/openapi.json`, `/docs`, `/redoc` and `/docs/oauth2-redirect` — and upstream
OpenBB guards **none** of them: `OPENBB_API_AUTH` is wired into the command
router only. The image carries a patch that puts Basic auth in front of the
whole app (see the Dockerfile), because otherwise funneling port 443 would
publish the full widget catalogue, every parameter schema, and a live Swagger
console to the internet. No market data leaks either way — the `/api/v1`
routes were always locked — but "the lock goes on before the door opens" has
to mean the whole door.

Check all of it in one go before you funnel anything:

```bash
OPENBB_USER=openbb OPENBB_PASS=<password> \
  OPENBB_URL=https://openbb.<your-tailnet>.ts.net scripts/verify-auth.sh
```

That exits non-zero if any path answers without credentials.
````

- [ ] **Step 4: Fix the README smoke.sh invocation**

In `README.md`, replace line 135:

```
OPENBB_URL=https://openbb.<your-tailnet>.ts.net scripts/smoke.sh
```

with:

```
OPENBB_USER=openbb OPENBB_PASS=<password> \
  OPENBB_URL=https://openbb.<your-tailnet>.ts.net scripts/smoke.sh
```

`README.md` lines 97-98 already document 401-without / 200-with on
`/widgets.json` and need no change — they become true with this work.

- [ ] **Step 5: Run the scrub gate**

Docs changes are the likeliest place to leak a real tailnet name.

```bash
bash scripts/scrub-check.sh
```

Expected: passes (exit 0).

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml docs/funnel.md README.md
git commit -m "docs: the lock covers the whole app, and say why it takes a patch"
```

---

### Task 6: Final verification

**Files:** none modified.

- [ ] **Step 1: Build clean from scratch**

```bash
docker build --no-cache -t openbb-local:final . 2>&1 | tail -30
```

Expected: succeeds, log shows `rest_api CORS patch parses OK`, `rest_api auth patch parses OK`, `cftc_router patch parses OK`, and `OpenBB Platform OK: <n> providers (incl. eodhd)`.

- [ ] **Step 2: Full posture check**

```bash
docker rm -f finalcheck 2>/dev/null
docker run -d --name finalcheck -e OPENBB_API_AUTH=true -e OPENBB_API_USERNAME=ci -e OPENBB_API_PASSWORD=ci-secret openbb-local:final
```

Wait for boot, then:

```bash
docker exec -i finalcheck bash -c 'OPENBB_URL=http://127.0.0.1:6900 OPENBB_USER=ci OPENBB_PASS=ci-secret bash -s' < scripts/verify-auth.sh
```

Expected: exit 0, `Auth posture verified: nothing is served without credentials.`

- [ ] **Step 3: Confirm auth-off mode still serves everything**

Proves the middleware is genuinely gated and did not become unconditional.

```bash
docker rm -f openrun 2>/dev/null
docker run -d --name openrun openbb-local:final
```

Wait for boot, then:

```bash
docker exec openrun curl -s -o /dev/null -w 'widgets.json no-auth-mode: %{http_code}\n' http://127.0.0.1:6900/widgets.json
```

Expected: `200`. With `OPENBB_API_AUTH` unset the app is open, exactly as upstream behaves — the patch adds no lock the operator did not ask for.

- [ ] **Step 4: Clean up**

```bash
docker rm -f finalcheck openrun; docker rmi openbb-local:final
```

- [ ] **Step 5: Confirm the tree is clean and review the diff**

```bash
git status --short && git log --oneline -6 && git diff --stat HEAD~5
```

Expected: no uncommitted changes; five commits from this plan (test, fix, scripts, ci, docs) sitting on top of the spec commit. The diffstat must show only `Dockerfile`, `scripts/verify-auth.sh`, `scripts/smoke.sh`, `scripts/verify-isolation.sh`, `.github/workflows/ci.yml`, `docker-compose.yml`, `docs/funnel.md`, `README.md`. If `ts-config/` appears, revert it — another worktree owns those files.

---

## Success criteria

Taken from the spec; all are checked by the steps above:

1. All eight root paths: 401 no-creds, 401 wrong-creds, 200 right-creds — Task 6 Step 2.
2. `/api/v1/equity/price/quote`: 401 / 401 / 422 — Task 6 Step 2 (section 4/4).
3. `/widgets.json` with credentials returns 282 widgets — Task 2 Step 4.
4. MCP `initialize` returns 200 with a session id — Task 2 Step 5.
5. `docker build` succeeds including the `ast.parse` step — Task 6 Step 1.
6. Both `openbb-api` and `openbb-mcp` start clean — Task 2 Step 5, Task 6 Steps 2-3.

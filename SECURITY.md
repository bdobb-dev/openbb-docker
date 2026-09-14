# Security

## Reporting

Open an issue, or contact the maintainer directly for anything you would
rather not file in public.

## API auth covered only `/api/v1` (fixed 2026-08-26)

**Affected: every release from v2.0.0 through v11.1.1, and `main` before the
fix.**

OpenBB wires its `authenticate_user` dependency into the `/api/v1` command
router only. Everything else served by the same app answered without
credentials, even with `OPENBB_API_AUTH=true`:

    /                /widgets.json    /apps.json    /agents.json
    /openapi.json    /docs            /redoc        /docs/oauth2-redirect

`/api/v1` itself was never affected — it returned 401 correctly throughout.

This mattered for two reasons. `docker-compose.yml` has instructed readers
since v2.0.0 to verify that `/widgets.json` returns 401 without credentials,
so the documentation asserted a posture the stack did not have. And
`/openapi.json` enumerates every endpoint, parameter and schema the API
exposes. Tailscale Funnel can publish this port to the public internet; on a
funneled deployment those paths were internet-reachable.

### Am I affected?

Run this against a deployment, from inside the container's network namespace:

    docker exec -i \
      -e OPENBB_URL=http://127.0.0.1:6900 \
      -e OPENBB_USER=<username> -e OPENBB_PASS=<password> \
      openbb-api bash -s < scripts/verify-auth.sh

`Auth posture FAILED` means the deployment is affected. `Auth posture
verified` means it is not. The script is shipped in every fixed release.

### The fix

A middleware requiring HTTP Basic auth on every path when
`OPENBB_API_AUTH` is set — not a route dependency, because `/docs` and
`/openapi.json` are registered by FastAPI itself and have no router to
attach one to. It no-ops when auth is disabled, which keeps the in-process
`openbb-mcp` wrapper working; that service is bound to loopback and is
never funneled.

`main` applies it in `api_app.py`'s factory. Tagged releases apply it as a
Dockerfile patch of `openbb_core/api/rest_api.py`, because `api_app.py`
postdates every tag.

The guard is registered **inside** the CORS layer, deliberately. A browser
preflight carries no credentials by definition, so an auth layer outside
CORS would answer it with a bare 401 and no `Access-Control-Allow-*`
headers — locking OpenBB Workspace out entirely, while every `curl` test
still passed. `verify-auth.sh` checks the preflight for exactly this reason.

### What was done

All eight affected tags were re-cut onto commits carrying the fix, the same
practice used for the CFTC startup-crash fix on 2026-08-21. The git tags
therefore point at fixed source. **Published container images are rebuilt
separately**, so if you pulled an image before its rebuild, re-pull and
re-run `verify-auth.sh`.

### Remediating a running deployment

Re-cutting the tags fixes the *source*. A long-lived deployment still runs
whatever image it was built from, and on 2026-08-26 an Ep. 11 stack was found
still serving every path above without credentials, behind Funnel — that is,
reachable from the public internet, not merely from the tailnet. Assume a
running stack is affected until `verify-auth.sh` says otherwise; do not infer
it from the tag the repo is checked out at.

**Which remediation applies depends on how the API is started**, and getting
this wrong takes the stack down rather than fixing it:

- **`command: ["openbb-api", ...]` with no `--app`** — the container serves
  `openbb_core.api.rest_api` directly. Pull the republished image for its
  version and recreate.

- **`command: ["openbb-api", "--app", "/opt/api_app.py", "--factory", ...]`** —
  the container serves the `api_app.py` factory, which postdates every tag.
  Swapping to a published tagged image **will not start**: that image is built
  from a tree with no `api_app.py`, so the file the command names does not
  exist. Instead, add the guard to the image already in use:

      FROM <the image the stack already runs>
      COPY api_app.py /opt/api_app.py
      RUN python -c "import ast; ast.parse(open('/opt/api_app.py').read())"

  Diff the container's `/opt/api_app.py` against this repo's before copying.
  If they differ by more than the guard, the deployment carries local changes
  that a blind copy would discard.

Deployments whose services share a Tailscale sidecar via
`network_mode: service:tailscale` need one more step. Recreating any one
service restarts the sidecar, which leaves every sibling attached to a dead
network namespace: they continue to report `Up` while returning 502. Recreate
the siblings too, and re-check each published port afterwards rather than
trusting `docker ps`.

### Credit

Found on 2026-08-05 while auditing the metadata endpoints. The fix sat
unmerged on a branch for three weeks before being rediscovered and shipped.

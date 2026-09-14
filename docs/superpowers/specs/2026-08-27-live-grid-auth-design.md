# Basic auth for live-grid — design

Require HTTP Basic auth on live-grid, using the same credentials `openbb-api`
already uses, so the service stops depending on network placement for its
protection.

## Why

live-grid serves `/live_grid`, `/series`, `/chart`, `/ta_chart`, `/subscriptions`
and a subscription API that mutates durable state — with **no authentication of
any kind**. Today the only thing protecting it is that it binds `127.0.0.1`
inside the tailscale sidecar's network namespace, so only namespace members and
Tailscale Serve can reach it.

That has shaped every architectural decision around this stack. Three network
designs were explored on 2026-08-27 — a dedicated bridge, a tailnet gateway, and
per-source TCP proxies — and each had to argue about what replaces the loopback
boundary, because the service itself has no boundary of its own.

The credentials were always there. `docker-compose.yml` already mounts
`api-auth.env` into live-grid, exactly as it does into `openbb-api`. The file
holds `OPENBB_API_AUTH`, `OPENBB_API_USERNAME`, `OPENBB_API_PASSWORD`. live-grid
receives all three and reads none of them.

So this is not new capability. It is using what is already mounted, and it
removes the reason the network has been carrying a security burden.

## Decisions

**D1. Same credentials as openbb-api, from the same file.**
`OPENBB_API_AUTH`, `OPENBB_API_USERNAME`, `OPENBB_API_PASSWORD` from
`api-auth.env`, already mounted. One credential for the stack's HTTP surfaces
means one thing to rotate and one thing for a client to hold — bdobb-v2 already
stores exactly this pair for its `Personal OpenBB (NAS)` backend.

Rejected: a live-grid-specific credential. It would double the rotation burden
and the client configuration for no isolation benefit, since both services front
the same data.

**D2. Read the variables with `os.environ`, not `openbb_core.env.Env`.**
`api_app.py` uses `Env()` because it runs inside the Platform. live-grid's
dependencies are fastapi, uvicorn, eodhd, kdb-store, pandas, httpx, polars,
pyyaml — no `openbb-core`. Taking a large dependency to read three strings would
be the wrong trade.

**D3. Middleware, not route dependencies.**
The same reasoning `api_app.py` records: a route dependency has to be attached
to every router, and misses anything FastAPI registers itself. Middleware covers
the whole app and stays correct when a route is added later.

**D4. Auth is INNERMOST, inside CORS.**
Registered so that CORS runs first. A browser preflight carries no credentials
by definition, so an auth layer outside CORS answers it with a bare 401 and no
`Access-Control-Allow-*` headers — locking out every cross-origin caller while
every `curl` test still passes. This exact trap is documented in `api_app.py`
and cost a fix round on the API. Starlette's `add_middleware` inserts at index 0
and index 0 is OUTERMOST, so the guard must be appended to `user_middleware`
rather than added.

**D5. It no-ops when `OPENBB_API_AUTH` is unset.**
Matching `api_app.py`, so a developer running live-grid alone, and the existing
test suite, are unaffected. The NAS sets it; a laptop does not.

**D6. The websockets are covered too, or explicitly are not.**
`/live_grid_ws` and `/ta_chart_ws` are the surfaces a middleware most easily
misses: Starlette's `BaseHTTPMiddleware` does not run for websocket scopes. The
guard must therefore be written as a raw ASGI middleware that inspects
`scope["type"]`, or the websockets must be guarded separately and the gap
documented. **Leaving them unguarded silently is not acceptable** — they carry
the same live data as the REST surfaces.

## Scope

In: every HTTP route live-grid serves, and both websockets.

Out: `stores-explorer` (same weakness, its own change), embedding kdb, the
network-namespace fragility, and any change to what live-grid serves.

## Behaviour

| request | response |
|---|---|
| no credentials, `OPENBB_API_AUTH` set | 401 with `WWW-Authenticate: Basic` |
| wrong credentials | 401 |
| correct credentials | the route's normal response |
| any request, `OPENBB_API_AUTH` unset | passes through unauthenticated |
| CORS preflight (`OPTIONS`, no credentials) | 200 with `Access-Control-Allow-*` |
| websocket without credentials | connection refused before accept |

Credential comparison uses `secrets.compare_digest` on both halves, and fails
closed when either the configured username or password is empty — without that,
`compare_digest("", "")` is true on both and `Basic <base64 of ":">`
authenticates against an unconfigured pair.

## Consequences

**What this unlocks.** Once live-grid authenticates, its network placement stops
being a security decision. The bridge migration, the tailnet gateway and the
per-source proxy designs were all arguing about how to replace a boundary that
this makes unnecessary. Those designs do not become correct or incorrect — they
become optional.

**What it does not fix.** The sidecar restart that strands the stack
(2026-08-27) is untouched. That is a availability problem, not a security one,
and the network-anchor approach remains its cheapest answer.

**Client impact.** bdobb-v2's `Personal OpenBB (NAS)` backend already holds
these credentials. A live-grid Connection entry needs the same `Authorization`
header configured — the subscriptions widget's own page fetches same-origin and
will inherit the browser's credentials for its iframe origin, which must be
verified rather than assumed.

## Testing

- 401 with no credentials, on a REST route and on a websocket
- 401 with wrong credentials
- 200 with correct credentials
- pass-through when `OPENBB_API_AUTH` is unset, proving the existing suite and
  local development are unaffected
- CORS preflight answered 200 with `Access-Control-Allow-*` and no credentials —
  the D4 trap, tested explicitly because `curl` never catches it
- the subscriptions page can still call its own API from inside the iframe

<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Key-Maint: API key maintenance widget service

**Date:** 2026-08-04
**Status:** Approved

> **Post-deployment update (2026-08-04):** the `:10000` funnel described
> below was later deliberately removed — key-maint is **tailnet-only** now,
> reachable at `:10000` on the node and via the Tailscale Service
> `svc:openbb-keys` (443 → 8447). Tier 1 (public) is retired; the tier
> mechanics below are otherwise unchanged. See `ts-config/serve.json`.

## Purpose

A small FastAPI service (`key-maint/`, mirroring `live-grid/`'s layout) that
serves one OpenBB table widget reporting the state of every provider API key in
`credentials.env`: set / empty / missing, whether the value is the provider's
public demo key, and an on-demand live test of each key against its provider.
Rendered by BDOBB as an additional backend; usable from OpenBB Workspace too.

What a caller may see is decided by **how the request arrived**, not by
configuration:

| Tier | Transport | Sees |
|---|---|---|
| 1 Public | Tailscale Funnel (port 10000) | provider, status, demo highlight |
| 2 Tailnet | Tailscale Serve, on-tailnet client | tier 1 + `run_tests` live probes |
| 3 Admin | SSH tunnel to NAS host loopback | tier 2 + actual key values; phase-2 editing |

Key values are visible only to someone who can SSH into the NAS. There is no
password, token, or mode flag that reveals them over the network paths —
the admin bind is simply unreachable except through the host's loopback.

## Access-tier mechanics

One FastAPI app, two uvicorn binds inside the tailscale sidecar's netns:

- **`127.0.0.1:8447` — network-facing bind.** `serve.json` proxies
  `${TS_CERT_DOMAIN}:10000 → http://127.0.0.1:8447`, and `AllowFunnel` gains
  `"${TS_CERT_DOMAIN}:10000": true`. (Funnel supports only 443/8443/10000;
  443 and 8443 are taken.) Requests on this bind are at most tier 2.
- **Unix socket — admin bind.** (Amended 2026-08-04: the original
  `127.0.0.1:8446` + compose `ports:` publish cannot work — Docker forwards
  published ports to the container's bridge IP, not its loopback; binding
  wider would expose the admin API on the tailnet interface.) The admin
  instance binds a unix socket at `/config/admin/key-maint-admin.sock`,
  bind-mounted from the NAS host at
  `<stack-dir>/key-maint-admin/` — a directory owned by the NAS
  admin user with mode 0700. Reaching the socket requires filesystem access
  to that path, i.e. an SSH session as the NAS admin: tier 3 is literally
  "allowed to ssh into the server path". Access:
  `ssh -L 18446:<stack-dir>/key-maint-admin/key-maint-admin.sock <host>`
  then `http://localhost:18446`. This is stricter than the TCP variant:
  sibling containers sharing the netns loopback can no longer reach the
  admin API at all.

Within the 8447 bind, tier 1 vs 2 is decided by the client address tailscaled
reports in `X-Forwarded-For`: `100.64.0.0/10` → tailnet (tier 2), anything
else → funnel (tier 1). Implementation must verify tailscaled replaces (not
appends to) a client-supplied `X-Forwarded-For`; if that cannot be
demonstrated, default to tier 1 on ambiguity. The stakes are low by design:
header spoofing could at worst trigger quota-burning probes (tier 2), never
reveal values (tier 3 is port-gated).

`app.main` starts two uvicorn servers in one process, each wrapping its own
app instance constructed with an explicit role (`network` / `admin`); the
role is fixed at construction, so there is no per-request detection to get
wrong. The `/keys` response includes `"tier"` so clients can label the view.

## Endpoints

- `GET /widgets.json` — one widget: type `table`, category "Admin", name
  "Provider API Keys". Params: `run_tests` (boolean, default false). No
  mode param — tier decides everything.
- `GET /keys?run_tests=<bool>` — array of rows:

```json
{
  "provider": "EODHD",
  "env_var": "EODHD_API_KEY",
  "status": "set",            // set | empty | missing
  "demo": true,               // value equals a known public demo key
  "value": "demo",            // tier 3 only; omitted otherwise (not masked, absent)
  "test": {"result": "ok", "detail": "200 in 412ms"}  // run_tests=true, tier >= 2
}
```

- `PUT /keys/{env_var}` — tier 3 only; returns **501** in phase 1. Reserved
  for phase 2 (editing + apply mechanism, deliberately undesigned here).
- CORS: allow origin `https://pro.openbb.co` (live-grid precedent). BDOBB
  bypasses CORS via Tauri.
- All endpoints on both binds require the same HTTP Basic auth as the main
  API: credentials read from `api-auth.env` (mounted read-only). Even tier 1's
  status view is an infrastructure inventory; it is not anonymous.

## Reading key state

- `credentials.env` is bind-mounted **read-only** at `/config/credentials.env`
  and parsed per compose dotenv semantics (`KEY=value` lines, values taken
  verbatim; the repo convention keeps comments on their own lines — see
  docs on the 2026-08-04 inline-comment incident). The file is re-read on
  every request (it is tiny); what the widget shows is exactly what the next
  container restart will load — file truth, not process-env truth.
- A static **registry** (`app/registry.py`) maps each known env var to:
  display name, provider URL, known public demo values (e.g. EODHD `demo`,
  Alpha Vantage `demo`), and a probe spec. Non-credential vars in the file
  (`KDB_HOST`, `DELTA_S3_*`, `DELTA_LIBRARY`, `DELTA_URI`, ...) are listed in
  an explicit ignore set.
- Coverage rules: registry entry with no line in the file → `status:
  "missing"`. `*_API_KEY`/`*_TOKEN`/`*_SECRET` line with no registry entry →
  generic row (no demo detection, no probe). A pytest asserts every
  credential var in `credentials.env.example` is either in the registry or
  the ignore set, so they cannot drift.

## Live testing (tier ≥ 2, `run_tests=true`)

- Each registry probe spec is the cheapest authenticated call for that
  provider (EODHD: 1-bar EOD AAPL; FRED: 1 observation; BLS: 1 series; EIA:
  minimal STEO query; ...). Probes run concurrently via httpx, 5 s timeout
  each.
- Classification: `ok` (2xx), `auth_failed` (401/403 or the provider's
  known invalid/missing-key body shape), `error` (timeout, network, 5xx),
  `skipped` (key not set, or provider has no probe spec).
- Key values never appear in logs, error details, or probe URLs echoed back;
  probe `detail` strings are built from status/latency only.
- Probes never fire on widget load — only when the user flips `run_tests`.
  Tier 1 requests ignore `run_tests` entirely (`test` field absent).

## Deployment

New service in `docker-compose.yml`:

```yaml
  key-maint:
    build: ./key-maint            # python:3.12-slim + fastapi/uvicorn/httpx
    container_name: openbb-key-maint
    restart: unless-stopped
    network_mode: service:tailscale
    depends_on: [tailscale]
    command: ["python", "-m", "app.main"]   # unix-socket admin bind + 127.0.0.1:8447 network bind
    volumes:
      - ./credentials.env:/config/credentials.env:ro
      - ./api-auth.env:/config/api-auth.env:ro
      - ./key-maint-admin:/config/admin
```

`serve.json` additions: `"TCP": {"10000": {"HTTPS": true}}`,
`"Web": {"${TS_CERT_DOMAIN}:10000": {"Handlers": {"/": {"Proxy":
"http://127.0.0.1:8447"}}}}`, `"AllowFunnel": {"${TS_CERT_DOMAIN}:10000":
true}`. containerboot re-applies the file live (no restart needed).

Client setup:
- BDOBB / Workspace (any device): backend `https://openbb.<tailnet>.ts.net:10000`
  with the standard Authorization header. On-tailnet browsers get tier 2
  automatically; off-tailnet get tier 1.
- Admin session, primary: `ssh <host> "curl -s -u openbb:<password> --unix-socket
  <stack-dir>/key-maint-admin/key-maint-admin.sock
  http://localhost/keys"` — works over a plain SSH exec session, regardless
  of the NAS sshd's local-forwarding policy.
- Admin session, alternative (only if the NAS sshd permits local forwarding —
  many NAS sshd builds deny it via `AllowTcpForwarding`/`streamlocal`):
  `ssh -L 18446:<stack-dir>/key-maint-admin/key-maint-admin.sock
  <host>`, then backend `http://localhost:18446` (same Authorization header).
  BDOBB and `scripts/smoke_live.py` both need this form, since they make an
  HTTP request against a URL rather than an SSH exec call.

## Error handling

- `credentials.env` missing/unreadable → every registry row `status:
  "unknown"` plus a synthetic first row explaining the mount problem; HTTP
  200 always. The widget must degrade, never blank.
- Malformed lines are skipped and reported as a synthetic warning row.
- Probe failures are per-row results, never request failures.
- Unknown query params ignored; `PUT` on phase 1 → 501 with a one-line body.

## Testing

- **pytest (CI):** dotenv parsing (incl. comment-line convention and the
  empty-value+inline-comment hazard), registry/example sync, ignore-set
  completeness, tier resolution (role flag → tier; XFF CGNAT → 2, public →
  1, absent/ambiguous → 1), value presence/absence per tier, demo detection,
  probe classification against mocked httpx, Basic-auth enforcement on both
  binds, 501 on PUT.
- **`scripts/smoke_live.py` (manual):** real probes against live providers
  using the operator's own credentials.env; prints the table. Not run in CI.
- Follow live-grid's test layout and naming.

## Phase 2 seam (explicitly out of scope)

Editing: `PUT /keys/{env_var}` on the admin bind writes a staged
`credentials.env` change; the apply mechanism (docker socket vs host agent vs
manual `docker compose up -d`) is a separate brainstorm/spec. Nothing in
phase 1 may assume a particular choice; the 501 stub and the read-only mount
are the whole phase-1 footprint.

## Non-goals

- No Workspace "app" packaging, no charts — one table widget.
- No secrets management beyond display (no encryption-at-rest changes;
  credentials.env stays the source of truth).
- No per-user identity: tiers are transport-based, not user-based.

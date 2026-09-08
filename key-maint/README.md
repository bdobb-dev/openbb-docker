<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# key-maint

Transport-tiered OpenBB widget reporting provider API key state — the
"key status" widget from *Adventures in OpenBB, Ep. 3*. Design:
`../docs/key-maint-design.md`.

The idea: **what you can see depends on how you connected.** Same service,
three postures:

| Tier | How you connect | Sees |
|---|---|---|
| 1 *(retired)* | Public internet via Tailscale Funnel on `:10000` — the funnel was deliberately removed; the service is tailnet-only now | key status + demo flag |
| 2 | `https://openbb.<your-tailnet>.ts.net:10000` or `https://openbb-keys.<your-tailnet>.ts.net` from a tailnet device | + `run_tests` live probes |
| 3 | Unix socket on the host, via an SSH exec session | + the key values themselves |

All tiers require the main API's Basic auth (`api-auth.env`) — tier 3
included. What tier 3 does not have is a **port**: the admin server binds a
**unix socket** in a host directory you create with mode 0700, so being able to
open that file is what raises an authenticated caller to tier 3. The filesystem
stands in for a second credential, not for the first one — there is no separate
admin password to store, leak or rotate.

## Deploy

1. One-time, beside the compose file:
   `mkdir -p key-maint-admin && chmod 700 key-maint-admin`
2. `docker compose up -d --build key-maint`
3. Serve publishes the network server on `:10000` and as the Tailscale
   Service `svc:openbb-keys` (see `ts-config/serve.json`) — tailnet-only.
   Neither serve config funnels it; tier 1 exists only if you add an
   `AllowFunnel` entry for `:10000` yourself.

## Use

- **Widget:** add `https://openbb-keys.<your-tailnet>.ts.net` (Tailscale
  Service) or `https://openbb.<your-tailnet>.ts.net:10000` as a backend in
  BDOBB or OpenBB Workspace (same Authorization header as the main API) — the
  key status widget appears in the library.
- **Tier 3 (values):** run curl on the host over SSH — works regardless of the
  host's sshd forwarding policy, since it never opens a forwarded channel:

      ssh <host> "curl -s -u openbb:<password> \
        --unix-socket <stack-dir>/key-maint-admin/key-maint-admin.sock \
        http://localhost/keys"

  Alternative, only if your sshd permits streamlocal forwarding (many NAS
  builds deny it):
  `ssh -L 18446:<stack-dir>/key-maint-admin/key-maint-admin.sock <host>` →
  `http://localhost:18446`. `scripts/smoke_live.py` needs this `-L` form.

## Test

    pip install -e . && pytest

The suite mocks all probes; nothing needs a live deployment.

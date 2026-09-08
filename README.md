<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# openbb-docker

Self-hosted **OpenBB Platform** in Docker, behind a Tailscale sidecar — the
backend for the **Adventures in OpenBB** series. Each tagged release is the
companion code for one episode: check out the tag, follow that episode's
"For the tinkerers" section, and everything you need is here — and nothing
from later chapters is.

| Release | Episode | What it adds |
|---|---|---|
| v1.0.0 | Ep. 1 — Your Own Bloomberg in a Closet | Tailscale sidecar + OpenBB Platform API, Serve-only ingress, provider keys |

## What you get (this release: v1.0.0)

Two containers, one tailnet node, zero exposed ports:

- a small **Tailscale sidecar** that joins your tailnet as a node named
  `openbb`;
- the **OpenBB Platform REST API** (all standard providers + the technical,
  quantitative, and econometrics extensions), which the sidecar reaches over a
  private `openbb-internal` bridge — it has no tailnet presence of its own.

**Tailscale Serve is the only way in** — real HTTPS with a Let's Encrypt
certificate at `https://openbb.<your-tailnet>.ts.net`, reachable from every
device on your tailnet and invisible to everything off this host.

Worth stating plainly while there is still no authentication (that arrives in
Ep. 2): the API sits on a private Docker bridge, so other processes **on this
Docker host** can reach it directly. Nothing on your LAN can, and nothing on
your tailnet can get to it except through Serve.

## Quick start

Prereqs: any Docker Compose host (a NAS, a Linux box), a free Tailscale
account with **MagicDNS** and **HTTPS certificates** enabled in the admin
console.

```bash
# 1. Clone and configure
git clone https://github.com/artcashin/openbb-docker
cd openbb-docker
cp ts.env.example ts.env            # paste a tagged, reusable auth key; chmod 600 ts.env
cp credentials.env.example credentials.env   # optional — keyless providers work with none

# 2. Build and start
docker compose up -d --build

# 3. Verify the front door (from any tailnet device)
curl https://openbb.<your-tailnet>.ts.net/widgets.json

# 4. Verify the walls (from a SECOND tailnet device)
scripts/verify-isolation.sh openbb.<your-tailnet>.ts.net
```

Step 4 is not optional ceremony — it is the step that catches the one
mistake that silently exposes everything (see the compose file's
`TS_USERSPACE` comment, and the episode's Gotchas).

## Provider keys

A surprising amount works with **no keys at all**: yfinance for prices, SEC
for filings, the Federal Reserve for rates and economic series, plus OECD,
IMF, ECB and friends. When you add keyed providers, they go in
`credentials.env` as bare UPPERCASE names (`FMP_API_KEY=…`) — git-ignored,
injected at container start, empty values skipped. Keep comments on their own
lines (compose's dotenv parser treats an inline comment after an empty value
as the value).

## Notes

- **Charting is data-only by design.** The image deliberately skips OpenBB's
  GUI chart backend (it cannot render headless); chart endpoints return Plotly
  figure JSON and the *client* renders it — which is exactly what Episode 2's
  browser setup and BDOBB do.
- The image carries two small patches to upstream, each documented in the
  Dockerfile: a CFTC router startup crash guard, and CORS
  `allow_private_network` so browser clients can pass Chrome's Private
  Network Access preflight.
- All hostnames in this repo are placeholders (`<your-tailnet>.ts.net`).
  CI runs `scripts/scrub-check.sh` to keep it that way.

## Testing

```bash
# with the stack running, from a tailnet device:
OPENBB_URL=https://openbb.<your-tailnet>.ts.net scripts/smoke.sh

# CI equivalent (no tailnet needed): build the image, boot the API, hit
# widgets.json from inside the container — see .github/workflows/ci.yml
```

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
| v2.0.0 | Ep. 2 — The Borrowed Terminal | HTTP Basic auth on the API, Tailscale Funnel (port 443 only) |
| v3.0.0 | Ep. 3 — (with BDOBB v3.0.0) | key-maint: the transport-tiered key status widget backend |
| v6.0.0 | Ep. 6 — The Analyst | OpenBB MCP server (tool-discovery mode), agent deploy configs |
| v8.0.0 | Ep. 8 — All the News That Fits, We Print | rss-ticker news wire joins the stack |
| v9.0.0 | Ep. 9 — The Tape | EODHD provider extension + live-grid streaming service |

## What you get (this release: v9.0.0)

Six containers, one tailnet node, zero exposed ports:

- a small **Tailscale sidecar** that joins your tailnet as a node named
  `openbb` — the only one with a tailnet address;
- the **OpenBB Platform REST API** (all standard providers + the technical,
  quantitative, and econometrics extensions), the **OpenBB MCP server**,
  **key-maint**, **live-grid** and **rss-ticker**, all on a private
  `openbb-internal` bridge that the sidecar reaches them over.

**Tailscale Serve is the only way in** — real HTTPS with a Let's Encrypt
certificate at `https://openbb.<your-tailnet>.ts.net`, reachable from every
device on your tailnet and invisible to everything off this host.

The services sit on a private Docker bridge, so other processes **on this
Docker host** can reach them directly. The API and key-maint answer with Basic
auth, rss-ticker with its manifest key and per-user tokens. **live-grid and the
MCP server have no auth** — so on this host they are callable, and they carry
your EODHD and provider keys respectively. Nothing on your LAN reaches any of
them, and Serve still gates the tailnet path.

**New in v9.0.0 (Ep. 9):** the tape. The **openbb-eodhd provider
extension** (`provider="eodhd"`: equity/ETF/crypto/forex historical, EOD +
intraday, and fundamentals — symbols qualified invisibly, unknown fields
passed through, SDK pinned to a commit) and the **live-grid** service — a
Workspace `live_grid` backend streaming EODHD websocket prices with REST
snapshot seeding, per-asset-class feeds, debounced rebuilds and ~4
coalesced flushes/second. Serve publishes it on :6903, tailnet-only. See
[live-grid/README.md](live-grid/README.md) and
[openbb-eodhd/README.md](openbb-eodhd/README.md).

**New in v8.0.0 (Ep. 8):** the wire. The
**[rss-ticker](https://github.com/artcashin/rss-ticker)** news service joins
the sidecar: it polls your RSS feeds (conditional GETs, jitter, backoff),
dedupes into SQLite, streams new articles over a websocket, and serves a
Bloomberg-style **news window / news rail** widget. Running behind this
stack it uses **`manifest_key` + per-user tokens**. It cannot use
`tailscale_auth` here: that mode trusts the `Tailscale-User-Login` header Serve
injects, which is only sound when Serve is the *only* way to reach the port —
so rss-ticker refuses to start under it unless `bind_host` is loopback, and on
the shared `openbb-internal` bridge any attached container could connect
directly and forge the header. Serve publishes it on :8088, tailnet-only, never
funneled. Compose builds it straight from its repo; configure
`rss-ticker-config/config.yaml` (from the example) and `rss-ticker.env` (admin
key, manifest key, one token per user), then add the backend in Workspace or
BDOBB with the **manifest key** as the API key.

**New in v6.0.0 (Ep. 6, pairs with BDOBB v6.0.0):** the **OpenBB MCP
server** — the analyst's hands. Same image, wrapping the same Platform
in-process, served on :8443 (tailnet-only, never funneled, no api-auth —
see the compose comments). Launched in **tool-discovery mode**, which is
load-bearing: the full 219-tool catalogue is ~152k prompt tokens against a
local model's 65k slot and hard-errors every chat request; discovery mode
is ~1.4k tokens with categories activated on demand. `install_skill`'s
write target is neutralized. The agent itself (Agent Rita) deploys from
BDOBB's `deploy/spark/` runbook.

**New in v3.0.0 (Ep. 3, with BDOBB v3.0.0):** the **key status widget**
backend — `key-maint/`, a transport-tiered service where *what you can see
depends on how you connected*: public sees key status, tailnet adds live
probes, and the key values themselves are only reachable over a unix socket
whose 0700 host directory is the authorization. See
[key-maint/README.md](key-maint/README.md) and
[docs/key-maint-design.md](docs/key-maint-design.md).

**New in v2.0.0 (Ep. 2):** the API enforces **HTTP Basic auth**
(`api-auth.env`, required — the stack will not start without it), and port
443 can optionally be published to the public internet via **Tailscale
Funnel** — lock first, then door. See [docs/funnel.md](docs/funnel.md) for
the Funnel walkthrough, the pro.openbb.co connection settings, and the
gotchas.

## Quick start

Prereqs: any Docker Compose host (a NAS, a Linux box), a free Tailscale
account with **MagicDNS** and **HTTPS certificates** enabled in the admin
console.

```bash
# 1. Clone and configure
git clone https://github.com/artcashin/openbb-docker
cd openbb-docker
cp ts.env.example ts.env            # paste a tagged, reusable auth key; chmod 600 ts.env
cp api-auth.env.example api-auth.env         # REQUIRED — set a strong password; chmod 600
cp credentials.env.example credentials.env   # optional — keyless providers work with none

# 2. Build and start
docker compose up -d --build

# 3. Verify the front door (from any tailnet device)
#    The lock covers every path, metadata included — the image patches OpenBB
#    to enforce Basic auth as middleware, not just on the /api/v1 router.
curl https://openbb.<your-tailnet>.ts.net/api/v1/equity/price/quote                        # 401
curl -u openbb:<password> https://openbb.<your-tailnet>.ts.net/api/v1/equity/price/quote   # 422 — auth accepted, symbol required
curl https://openbb.<your-tailnet>.ts.net/widgets.json                                     # 401 — metadata is locked too
curl -u openbb:<password> https://openbb.<your-tailnet>.ts.net/widgets.json                # 200

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

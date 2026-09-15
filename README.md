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
| v5.3.0 | Ep. 5 — (with BDOBB v5.3.0) | openbb-trading-calendar: exchange trading calendars (pandas-market-calendars) and the Trading calendar widget |

## What you get (this release: v5.4.0)

Two containers, one tailnet node, zero exposed ports:

- a small **Tailscale sidecar** that joins your tailnet as a node named
  `openbb`;
- the **OpenBB Platform REST API** (all standard providers + the technical,
  quantitative, and econometrics extensions), which the sidecar reaches over a
  private `openbb-internal` bridge — it has no tailnet presence of its own.

**Tailscale Serve is the only way in** — real HTTPS with a Let's Encrypt
certificate at `https://openbb.<your-tailnet>.ts.net`, reachable from every
device on your tailnet and invisible to everything off this host.

The services sit on a private Docker bridge, so other processes **on this Docker
host** can reach them directly — but they meet the same HTTP Basic auth as
everyone else, on every path. Nothing on your LAN can reach them at all, and
key-maint's admin surface is a unix socket that never touches the bridge.

**New in v5.3.0 (Ep. 5, with BDOBB v5.3.0):** **exchange trading calendars**
— `openbb-trading-calendar/`, an OpenBB Platform router extension in the
`openbb-api` image, computed by pandas-market-calendars for XNYS, XLON, XTSE,
XETR, XTKS and XHKG, 1990–2040. `/api/v1/calendar/trading` answers in EODHD's
exchange-details v2 shape, which is what BDOBB's period expressions read for
`trade_date`, the session anchors and business-day offsets. It is not a
widget. `/api/v1/calendar/trading_table` is: the **Trading calendar** table,
with an exchange dropdown and a year. See [Trading calendars](#trading-calendars).

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

## Trading calendars

Both routes take `exchange` (a MIC: `XNYS XLON XTSE XETR XTKS XHKG`) and
`year` (1990–2040); anything else is a 404. They sit behind the same Basic
auth as every other path.

```bash
curl -u openbb:<password> "https://openbb.<your-tailnet>.ts.net/api/v1/calendar/trading?exchange=XNYS&year=2026"
curl -u openbb:<password> "https://openbb.<your-tailnet>.ts.net/api/v1/calendar/trading_table?exchange=XHKG&year=2026"
```

**Local demo, no tailnet.** Build the image and run `openbb-api` on its own,
published on loopback with its Basic-auth env:

```bash
docker compose build openbb-api
docker run -d --name openbb-api-demo -p 127.0.0.1:6900:6900 --env-file api-auth.env \
  openbb-local:5.4.0 openbb-api --host 0.0.0.0 --port 6900
curl -u openbb:<password> "http://127.0.0.1:6900/api/v1/calendar/trading?exchange=XNYS&year=2026"
docker rm -f openbb-api-demo      # when done
```

`--host 0.0.0.0` is inside the container, which a published port needs.
`-p 127.0.0.1:6900:6900` keeps it off your LAN. OpenBB's default CORS policy admits
any origin, so BDOBB in browser mode (`http://localhost:1420`) can use
`http://127.0.0.1:6900` as a backend; the Basic auth still guards every path.

The **Trading calendar** widget's `year` default is set once, from the year
the API process started — a container that keeps running across New Year
shows last year's default until it is restarted, though the `year` param can
always be changed in the widget itself.

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

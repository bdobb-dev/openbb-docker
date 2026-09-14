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
| v10.0.0 | Ep. 10 — The Cache | kdb+ read-through cache (`provider="kdb"`) + tick recording and a unified chart in `live-grid` |
| v11.0.0 | Ep. 11 — The Shared Store | MinIO as its own tailnet node + ArcticDB (`provider="arcticdb"`) + `tick-lab` + the `live_chart` widget |
| v11.1.0 | Ep. 11 — The Shared Store | `tick-lab`'s EODHD-through-the-API reference adapter — the per-minute 2023 comparison yfinance cannot serve |
| v11.1.1 | Ep. 11 — The Shared Store | `tick-lab`'s in-process OpenBB reference adapter (`--reference eodhd-local`) — the same call made locally, and what it costs versus `eodhd-api` |

Ep. 11's three tags point at the same commit. The chapter was built and
verified as one body of work — the rows above describe what each release
*adds*, not three separate states of the code, and the two later reference
adapters are `--reference` options you select at runtime.

## What you get (this release: v11.1.1)

Eight services across two tailnet nodes, zero exposed ports. The backbone,
unchanged since Ep. 1:

- a small **Tailscale sidecar** that owns the network namespace and joins your
  tailnet as a node named `openbb`;
- the **OpenBB Platform REST API** (all standard providers + the technical,
  quantitative, and econometrics extensions) sharing that namespace, bound to
  loopback only.

Everything else — `openbb-mcp`, `key-maint`, `live-grid`, `rss-ticker`, and
`minio` as its own second tailnet node — arrived in the episodes below.

**Tailscale Serve is the only way in** — real HTTPS with a Let's Encrypt
certificate at `https://openbb.<your-tailnet>.ts.net`, reachable from every
device on your tailnet and invisible to everything else.

**New in v11.0.0 (Ep. 11):** the shared store. **MinIO joins the tailnet as
its own node**, `minio.<your-tailnet>.ts.net`, with a real Let's Encrypt
certificate — not a Serve route on the `openbb` node, because S3's SigV4
signing covers the `Host` header and a reverse proxy in that path is a
failure mode this chapter doesn't need. `tailscaled` runs *inside* the MinIO
container rather than a sidecar (a sidecar's control socket is a file, and
`network_mode` only shares the network namespace, so a sidecar daemon is
unreachable from another container no matter what's mounted), and certificate
renewal lives there too, signalling its own `minio` child with `SIGHUP` —
measured: MinIO ignores a certificate rewritten on disk, but reloads it on
`SIGHUP` — certificate serial updated, container uptime untouched (no
restart); connections in flight during the signal were never observed.
Nothing is published to the host; `:9000` is reachable on the tailnet and,
as a documented limit of the posture, from the Docker bridge — in this
deployment, that means never from the LAN, but that depends on the reader's
host networking, not on anything this compose file guarantees. The
**openbb-arcticdb provider extension** (`provider="arcticdb"`) puts that
store behind the Platform's normal historical-price interface, tick data
included (pass `interval` to resample ticks into OHLCV on read).
**`tick-lab`** is a new, separate CLI — install it locally, point it at the
same store via `minio.env`'s `ARCTICDB_S3_*` values, load FirstRate Data's
free tick sample (GOOG + MSFT, 2023-05-12 — **not committed here**, it's
third-party licensed data you download yourself), and it rolls your stored
ticks into 1-minute bars and checks them against a reference source —
`eodhd-api` by default since v11.1.0, with `yfinance` and the in-process
`eodhd-local` also available via `--reference`. **Apple Silicon readers, read
this:** the Platform image is pinned `linux/amd64` because ArcticDB
publishes no aarch64 Linux wheels — on an M-series Mac it runs under
emulation, so expect a slower build and slower queries (correctness is
unaffected). `tick-lab` itself is unaffected either way: it runs on your
laptop, not in the image, and ArcticDB does publish native macOS arm64
wheels. See [tick-lab/README.md](tick-lab/README.md) and
[docs/arcticdb-minio-design.md](docs/arcticdb-minio-design.md).
This release also ships the **`live_chart` widget** — a data-only
`live-grid/widgets.json` declaration (Workspace type `live_chart`) over the
existing `GET /series` + `live_grid_ws`, so the Ep. 10 chart streams in
Workspace itself with no new server code.

**New in v10.0.0 (Ep. 10):** the cache, tick recording, and one unified
chart. The **openbb-kdb provider extension** (`provider="kdb"`) puts an
in-memory kdb+ database in front of any other provider (`KDB_UPSTREAM`,
default `eodhd` — any installed provider works): a request serves whatever
bars it already holds and fetches only the date ranges it's missing.
Verified live: a one-year chart, then the same request again, then widened
to three years, came back `miss` → `hit` → `partial` with exactly one gap
fetched. The cache is **memory-only**, on purpose — a restart means a cold
cache, not a lost dataset.

**This repo ships no KX software.** KX's licence does not permit
redistributing their `q` binary, so it is not in the Dockerfile, not in the
published image, and not on PyPI — you supply it. Two ways, tried in that
order:

- **Option A — drop your own q into `./kdb`** (see
  [kdb/README.md](kdb/README.md)). It's mounted read-only at `/opt/kx`;
  `openbb-api` spawns it bound to `127.0.0.1:5000` and every other service in
  the stack reaches it over the shared tailscale network namespace — no new
  port, nothing else to configure.
- **Option B — run your own kdb container** and point `KDB_HOST` at it in
  `credentials.env` (see `credentials.env.example` for the platform-specific
  value — `host.docker.internal` on Docker Desktop, `172.17.0.1` on a Linux
  host — and how to publish it to host loopback only).

Bring your own `kc.lic` too (drop it in `kdb-license/`, git-ignored, and
point `QLIC` at the mount) — without a reachable q, or without a licence, the
provider passes straight through to the upstream and reports
`cache: "bypass"`, so the stack still works, just uncached (a failed connect
suppresses retries for 60 s so a q-less reader doesn't pay a doomed spawn or
connect attempt per request — then it tries again, so mounting a licence or
starting a container after the fact re-arms the cache within a minute).

**q must bind `127.0.0.1`, never `0.0.0.0`** — every service in this stack
shares the tailscale network namespace, so a loopback bind reaches every
sibling and no tailnet peer, while `0.0.0.0` would publish an
unauthenticated q (which executes arbitrary q code) to the whole tailnet.
For option A that bind is this repo's own code and already correct. For
**option B the reader's own image controls its bind** — get it wrong and
either the connection fails (bind `127.0.0.1` *inside* a separate container
and `-p` cannot reach it) or q ends up on the LAN (bind `0.0.0.0` and publish
it beyond host loopback). `scripts/verify-isolation.sh`'s port-5000 check is
what catches that mistake — run it after standing up your own kdb container.

The shared q plumbing (`config.py`, `session.py`, `store.py`, `ranges.py`)
lives in its own **`kdb-store`** distribution so it has exactly one
implementation, used by both `openbb-kdb` and **`live-grid`**, which now
*records* the tick stream it already receives, aggregates it in q (`xbar`),
and serves a chart that joins those live bars to cached history at the point
where the tick window begins — replacing the earlier standalone
`cache-chart` service, now removed. Serve continues to publish `live-grid`
on :6903, tailnet-only. See [openbb-kdb/README.md](openbb-kdb/README.md),
[live-grid/README.md](live-grid/README.md),
[kdb-store/README.md](kdb-store/README.md) and
[docs/tick-chart-design.md](docs/tick-chart-design.md) (which supersedes
[docs/kdb-cache-design.md](docs/kdb-cache-design.md) for anything
chart-related).

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
stack's Serve it uses **`tailscale_auth`** — the caller's verified Tailscale
identity replaces every per-user token, so no credential appears in any URL.
Serve publishes it on :8088, tailnet-only, never funneled. Compose builds it
straight from its repo; configure `rss-ticker-config/config.yaml` (from the
example) and `rss-ticker.env` (admin key), then add the backend in Workspace
or BDOBB with a **blank API key** — your Serve identity is the credential.

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
cp minio.env.example minio.env     # REQUIRED for the store; chmod 600
cp rss-ticker.env.example rss-ticker.env     # REQUIRED — admin key; chmod 600

# 2. Build and start
docker compose up -d --build

# 3. Verify the front door (from any tailnet device)
#    The lock is on the DATA routes. widgets.json is metadata and answers 200
#    with or without credentials — OpenBB's Basic auth is a dependency of the
#    /api/v1 router, and nothing else, so test it there.
curl https://openbb.<your-tailnet>.ts.net/api/v1/equity/price/quote                        # 401
curl -u openbb:<password> https://openbb.<your-tailnet>.ts.net/api/v1/equity/price/quote   # 422 — auth accepted, symbol required
curl https://openbb.<your-tailnet>.ts.net/widgets.json                                     # 200 — metadata, by design

# 4. Verify the walls (from a SECOND tailnet device)
scripts/verify-isolation.sh openbb.<your-tailnet>.ts.net minio.<your-tailnet>.ts.net
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
- **Two upstream rough edges are handled, in two different ways.** The CFTC
  router calls `.strip()` on contract fields that can be NULL, which takes
  the REST server down at startup; the image patches that in place, and the
  patch now *fails the build* if it stops matching rather than going quietly
  no-op (see the Dockerfile). CORS `allow_private_network` — needed so
  browser clients can pass Chrome's Private Network Access preflight, and not
  exposed as an OpenBB setting — is no longer a patch at all: `openbb-api`
  runs [`api_app.py`](api_app.py) through its documented `--app/--factory`
  entrypoint, so that customization is version-controlled code instead of a
  text substitution against upstream source.
- All hostnames in this repo are placeholders (`<your-tailnet>.ts.net`).
  CI runs `scripts/scrub-check.sh` to keep it that way.

## Testing

```bash
# with the stack running, from a tailnet device:
OPENBB_URL=https://openbb.<your-tailnet>.ts.net scripts/smoke.sh

# CI equivalent (no tailnet needed): build the image, boot the API, hit
# widgets.json from inside the container — see .github/workflows/ci.yml
```

## License

AGPL-3.0-only — this repo builds and serves OpenBB Platform itself, which is
AGPL-3.0-only upstream.

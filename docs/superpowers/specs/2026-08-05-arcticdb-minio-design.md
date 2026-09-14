# Ep. 11 — ArcticDB + MinIO: the shared store, and getting data out

Design spec for Adventures in OpenBB, Chapter 11. Companion releases:
`v11.0.0`, `v11.1.0`, `v11.1.1`.

## Context

Every episode so far has moved data *into* OpenBB or rendered it *inside*
OpenBB Workspace. This one runs the other way: a tick dataset lands in a
shared object store, and a plain Python program on the reader's laptop —
knowing nothing about OpenBB — reads it, builds 1-minute bars, and checks
them against an independent source.

The store is [ArcticDB](https://arcticdb.io) over S3, and the S3 is a
self-hosted [MinIO](https://min.io) that joins the tailnet as its own node.

Pieces that already exist and are being brought together rather than built:

- `openbb-arcticdb` — a working provider + `store` API + `.arcticdb` OBBject
  accessor, including tick→OHLCV resampling on read
  (`github.com/artcashin/openbb-arcticdb`, currently `0.1.0`, untagged).
- The `ARCTICDB_URI` switch, already anticipated in `credentials.env`.
- The tick sample source: FirstRate Data's free bundle — GOOG and MSFT,
  trades and quotes, **2023-05-12**, US Eastern timestamps.

## Goals

1. MinIO runs as a first-class tailnet node with real TLS and nothing
   published to the LAN.
2. `provider="arcticdb"` is available in the Platform image, so a stored
   dataset can stand in for an upstream API call.
3. A reader can go from the free FirstRate zip to a discrepancy report with
   one venv and two commands, from their own machine.
4. The whole chapter is reproducible with **no paid API key**.

## Non-goals

- No OpenBB Workspace widget. The chapter's point is that the data is usable
  *without* OpenBB; a widget would dilute it.
- No kdb+ interaction. Ep. 10's cache is orthogonal.
- Not an ops migration of the NAS's existing `arcticdb-minio` container. This
  design supersedes its broken host-port publish, but retiring it is separate
  work.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | MinIO gets **its own tailnet node**, not a Serve route on the `openbb` node | S3 SigV4 signs the `Host` header; a reverse proxy in the signing path is a failure mode we simply don't need. Direct node, no proxy. |
| D2 | **Real TLS** via `tailscale cert`, ArcticDB URI scheme `s3s://` | Consistent with the series' "real certs" posture. Verified working on a tagged node (S1). |
| D3 | Cert renewal runs **inside the MinIO container**, signalling its own child with `SIGHUP` | MinIO ignores on-disk cert changes but reloads on SIGHUP with zero downtime (S2). Signalling from a sibling container would require mounting the Docker socket — unacceptable here. |
| D4 | An **`mc` init container** creates the bucket; ArcticDB authenticates with the **MinIO root key** | ArcticDB will not create a bucket. Root key keeps it to one credential in one file, per operator preference. |
| D5 | The `ARCTICDB_S3_*` env change is made in the **vendored copy only**; ported upstream later | Ships the chapter without a second repo's release cycle. Tracked as a follow-up. |
| D6 | Vendored extensions adopt **chapter-matched versions**: arcticdb `11.0.0`, eodhd `8.0.0`, kdb `10.0.0` | Reader maps extension to episode at a glance. |
| D7 | The image is pinned to **`platform: linux/amd64`** | ArcticDB publishes no aarch64 Linux wheels (S4). Emulation verified working on Apple Silicon. |
| D8 | Ticks are stored **tz-aware UTC** | Verified to round-trip through ArcticDB intact (S3). EODHD returns UTC with `gmtoffset: 0` (S5). A tz slip would present as a 100% discrepancy rate. |
| D9 | `--session regular` is the **default**, `all` is opt-in | FirstRate ticks include extended hours (condition `E`); reference bars are regular-session. The single largest discrepancy generator, so it is an explicit knob, not a buried assumption. |

## Architecture

### The `minio` node — three compose services

| Service | Role |
|---|---|
| `minio-ts` | `tailscale/tailscale` sidecar. `TS_HOSTNAME=minio`, `TS_USERSPACE=false`, state in `./ts-state-minio`. Reuses the same reusable tagged auth key from `ts.env` — no second key for the reader. **No `TS_SERVE_CONFIG`**: nothing is proxied; the node's own tailnet address is the endpoint. |
| `minio` | `network_mode: service:minio-ts`. S3 API `:9000`, console `:9001`. **No host port mappings.** Built from a thin local Dockerfile (below). |
| `minio-init` | One-shot `mc`: `alias set` then `mb --ignore-existing openbb`. Exits 0. This is what makes `docker compose up` sufficient. |

`minio-ts`'s state directory is also mounted into `minio`, so the renewal
wrapper can reach `tailscaled`'s socket.

Because the sidecar runs in kernel mode with no published ports, `:9000` is
reachable on the tailscale interface and on loopback within the namespace.
It is *also* reachable from the Docker bridge — i.e. from the container host
and its sibling containers. That is a deliberate, documented limit of the
posture, not an oversight; nothing on the LAN can reach it.

### The MinIO image and cert renewal

A thin `minio/Dockerfile`:

```
FROM tailscale/tailscale:<tag> AS ts
FROM quay.io/minio/minio:<tag>
COPY --from=ts /usr/local/bin/tailscale /usr/local/bin/tailscale
COPY entrypoint.sh /entrypoint.sh
```

Both base images are pinned to explicit tags — never `latest` — matching how
every other image in this repo is pinned. The exact tags are chosen at
implementation time.

`entrypoint.sh`:

1. Block until `tailscaled`'s socket is up and the node has a DNS name.
2. `tailscale cert --cert-file … --key-file …` into MinIO's certs directory.
3. `exec`-free start of `minio server` as a **background child**; record its PID.
4. Loop on an interval: re-run `tailscale cert`; if the written cert differs
   from the one in force, `kill -HUP $MINIO_PID`.
5. Forward `SIGTERM`/`SIGINT` to the child so `docker stop` stays clean.

Let's Encrypt certs are 90 days and the official Tailscale image ships no
renewal; community sidecars solve this with cron
([hhftechnology](https://github.com/hhftechnology/tailscale-sidecar),
[ericwastaken](https://github.com/ericwastaken/docker-tailscale-sharing-certs)).
A plain shell loop is enough here and adds no dependency.

### Credentials and configuration

New `minio.env.example` → `minio.env` (git-ignored, `chmod 600`):

```
MINIO_ROOT_USER=
MINIO_ROOT_PASSWORD=
```

Loaded by `minio`, `minio-init`, and `openbb-api`. The extension gains an
alternative to a hand-built URI — **this is the one change to the vendored
extension**, roughly 20 lines in `openbb_arcticdb/utils.resolve_config`:

```
ARCTICDB_S3_ENDPOINT=minio.<your-tailnet>.ts.net
ARCTICDB_S3_PORT=9000
ARCTICDB_S3_BUCKET=openbb
ARCTICDB_S3_ACCESS / ARCTICDB_S3_SECRET
ARCTICDB_S3_SECURE=true      # s3s:// when true
```

Resolution precedence is unchanged and extended at the end:
explicit arg → OpenBB credential → `ARCTICDB_URI` → **assembled from
`ARCTICDB_S3_*`** → LMDB default. An explicit `ARCTICDB_URI` still wins, so
nothing that works today changes behaviour.

Assembled form, exactly as verified in S3:

```
s3s://<endpoint>:<bucket>?port=<port>&access=<key>&secret=<secret>&use_virtual_addressing=false
```

Note the shape: host and bucket separated by `:`, **port as a query
parameter**. `host:port:bucket` is not valid.

The same env names are read by `tick-lab`, so container and laptop share one
convention and `minio.env` is the single source of truth.

### `openbb-api` changes

- `Dockerfile`: `COPY openbb-arcticdb/` + `pip install`, and extend the
  build-time check to `assert 'arcticdb' in obb.coverage.providers`.
- `docker-compose.yml`: `platform: linux/amd64` on the build/image services;
  `minio.env` added to `openbb-api`'s `env_file`.
- `extension-constraints.txt`: reconcile ArcticDB's transitive pins
  (`numpy`, `pandas`, `protobuf<7`) against OpenBB 4.7.2's ceilings and add
  entries only where a conflict is demonstrated.
- Per D6, bump the two existing vendored extensions to chapter-matched
  versions at the same time: `openbb-eodhd` `0.1.0` → `8.0.0`, `openbb-kdb`
  `0.1.0` → `10.0.0`. Version metadata only; no behaviour change.
- `scripts/verify-isolation.sh`: assert `:9000`/`:9001` are neither published
  to the LAN nor reachable off-tailnet.

## `tick-lab`

One local CLI in `tick-lab/`, its own `pyproject.toml`, two subcommands over
one config module. Runs on the reader's machine — it never enters a container.
macOS arm64 ArcticDB wheels exist, so Apple Silicon is unaffected here.

### `tick-lab load <zip>`

- Detects FirstRate trade vs quote files **by column count, not by the
  vendor's spec** — the bundled format document lists the quote line with
  eight fields but names "offer price" twice, so it cannot be trusted
  literally.
- Timestamps are tz-naive US Eastern in the file → localized to
  `America/New_York` → stored **tz-aware UTC** (D8). DST-ambiguous and
  nonexistent times are an error, not a silent coercion.
- Library `ticks` keyed by plain symbol (`MSFT`), library `quotes` likewise —
  so `provider="arcticdb", library="ticks"` reads naturally.
- `store.write` semantics: overwrite, so re-running is idempotent.
- Per-file failures are reported and the run continues; exit code is non-zero
  if any file failed.

### `tick-lab compare --symbol MSFT --date 2023-05-12`

1. Read ticks from ArcticDB over S3, `date_range`-filtered server-side.
2. Roll to 1-minute OHLCV: open=first, high=max, low=min, close=last,
   volume=sum. Left-closed, left-labeled.
3. Apply the session filter (D9). `regular` = 09:30–16:00 ET.
4. Ask the reference adapter for `1m`. **Print the real error.** Step down to
   the finest interval that source can serve for the window, and print the
   fallback that was chosen.
5. Aggregate our 1-minute bars up to the reference's interval; join on
   timestamp.
6. Report.

The report distinguishes three things that are usually conflated:

- **bars compared** — timestamps present on both sides
- **price discrepancies** — differences beyond `--tol` (default `0.01`, in
  the instrument's price units, i.e. one cent for a USD equity), counted per
  field, with the **largest close discrepancy in absolute terms and in basis
  points, and its timestamp**
- **coverage gaps** — bars present on exactly one side, counted separately
  and never mixed into the price-discrepancy count

`--json` emits the same content machine-readably.

### Reference adapters

A small interface — `fetch(symbol, start, end, interval) -> DataFrame` plus a
declared list of supported intervals — so swapping the source is a genuinely
small diff. That is the chapter's teaching point. `--reference` selects the
adapter and defaults to the newest one available in the installed release
(`yfinance` in v11.0.0, `eodhd-api` from v11.1.0 on).

| Adapter | Release | Path |
|---|---|---|
| `yfinance` | v11.0.0 | `yf.Ticker.history` directly. No OpenBB involved — this is the "plain Python against a shared store" demonstration. |
| `eodhd-api` | v11.1.0 | `GET /api/v1/equity/price/historical?provider=eodhd&interval=1m` on the stack's REST API over the tailnet, with Basic auth. **The EODHD key never leaves the server** and the laptop needs no OpenBB install — the payoff of running the stack. |
| `eodhd-local` | v11.1.1 | `from openbb import obb` in-process with the eodhd extension. Included for education: it shows the same call made locally, and what it costs you. |

Adapters must classify failures rather than return an empty frame —
authentication, plan entitlement, symbol not covered, and empty-window are
four distinct outcomes with four distinct messages.

## Release arc

| Release | Contents |
|---|---|
| **v11.0.0** | MinIO node + TLS + init, vendored `openbb-arcticdb` 11.0.0 with `ARCTICDB_S3_*`, `provider="arcticdb"` in the image, `tick-lab load`, `tick-lab compare --reference yfinance`, docs, isolation checks. |
| **v11.1.0** | `eodhd-api` adapter. The per-minute comparison becomes real. |
| **v11.1.1** | `eodhd-local` adapter. |

## Spike evidence

Run 2026-08-05, before this spec was written.

| # | Question | Result |
|---|---|---|
| S1 | Can a container on a **tagged** node fetch a cert? | **Yes.** `tailscale cert` inside the sidecar on a `tag:server` node: exit 0, cert and key written. |
| S2 | Does MinIO reload a rewritten cert? | **No** — unchanged after 36s. **`SIGHUP` reloads it**, serial changed, container uptime not reset. Restart also works but is unnecessary. → D3 |
| S3 | Exact ArcticDB↔MinIO URI | `s3://<host>:<bucket>?port=&access=&secret=&use_virtual_addressing=false` — worked first attempt. Write+read verified; tz-aware UTC index preserved. → D8 |
| S4 | ArcticDB wheels | **No aarch64 Linux wheels on PyPI**; `manylinux_2_17_x86_64` only. NAS is x86_64. Emulated `linux/amd64` on Apple Silicon resolves `arcticdb-6.21.0` correctly. → D7 |
| S5 | EODHD demo key on intraday | **MSFT 1m for 2023-05-12 returns real bars, HTTP 200**, first bar 13:30:00 UTC. **GOOG returns 403 Forbidden.** So both the happy path and the error path are free and reproducible. |
| S6 | yfinance failure mode for a 2023 1m request | Returns an empty frame by default, but raises `yfinance.exceptions.YFPricesMissingError` carrying Yahoo's verbatim *"must be within the last 30 days"* when exceptions are enabled. `1h` fails the same way at 730 days; only `1d` returns data. Current API is `yf.config.debug.hide_exceptions = False` — `raise_errors=` is deprecated. |

S5 is why the chapter needs no paid key: MSFT carries the real per-minute
comparison, GOOG demonstrates the error path.

## Success criteria

1. Clean checkout → `docker compose up -d --build` yields a `minio` tailnet
   node serving a valid Let's Encrypt cert on `:9000`, bucket `openbb`
   present, and `arcticdb` in `obb.coverage.providers`.
2. `scripts/verify-isolation.sh` passes, including the new `:9000`/`:9001`
   assertions.
3. `tick-lab load` of the free sample writes MSFT and GOOG trades and quotes,
   readable **from a second tailnet machine**.
4. `tick-lab compare --reference yfinance --symbol MSFT --date 2023-05-12`
   prints the failed 1m attempt with Yahoo's own wording, the chosen fallback
   interval, and a non-empty report.
5. The hand-rolled 1-minute bars equal what
   `provider="arcticdb", interval="1m"` returns for the same window.
6. v11.1.0: `--reference eodhd-api --symbol MSFT` produces a per-minute
   comparison across the regular session.
7. v11.1.1: `--reference eodhd-local --symbol GOOG` produces a specific
   entitlement error naming the 403, not an empty frame.
8. A cert rotation forced during a run does not interrupt in-flight S3 traffic.

## Testing

Test-driven throughout, per the project's normal workflow.

- **Unit, no network** — the roll-up against synthetic ticks: bucket
  boundaries, session edges (09:30:00 and 16:00:00 inclusive/exclusive),
  DST handling, tolerance arithmetic, bps computation, coverage gaps counted
  separately from price discrepancies, empty input.
- **Unit** — reference-adapter failure classification, with recorded
  responses for the 403, the yfinance retention error, and an empty window.
- **Integration** — against a MinIO container in CI: bucket creation, URI
  assembly, write/read round-trip, tz preservation.
- **Integration** — hand-rolled bars vs `provider="arcticdb"` (criterion 5).
- **Manual, documented** — the real FirstRate zip. **It is not committed**:
  it is third-party licensed data. Tests use a small synthetic fixture in the
  FirstRate format.

CI runs on x86_64 runners, so the arm64 wheel gap does not affect it.

## Risks and follow-ups

- **Cert renewal is the least-tested path.** It only fires every ~60 days.
  The renewal loop must be exercised deliberately in testing by forcing a
  cert change, not just observed to work at startup.
- **Apple Silicon readers run the Platform image under emulation.** ArcticDB
  is native-extension-heavy, so expect a slow build and slower queries. This
  needs an explicit README note — it is a performance caveat, not a
  correctness one, and `tick-lab` on the laptop is unaffected because macOS
  arm64 wheels exist.
- **`ARCTICDB_S3_*` is vendor-only for now** (D5). Port it to
  `github.com/artcashin/openbb-arcticdb` after the chapter ships, or the
  published extension can only reach MinIO via a hand-built URI.
- **Root key in `ARCTICDB_S3_SECRET`** (D4). A bucket-scoped key would be
  better posture; deliberately deferred to keep the reader's setup to one
  credential.
- **The demo EODHD key is a moving target.** It is EODHD's, not ours, and
  could stop covering MSFT. Criterion 6 should fail loudly rather than
  silently degrade if that happens.
- Retiring the NAS's existing `arcticdb-minio` container once this lands.

## Out of scope

Workspace widget; kdb+ interaction; quote-data analytics (quotes are loaded
and stored, but the comparison uses trades only); any paid FirstRate bundle.

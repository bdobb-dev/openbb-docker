# ArcticDB + MinIO: the shared store

**Date:** 2026-08-05
**Status:** Shipped
**Ships as:** v11.0.0 through v11.1.1 — *Adventures in OpenBB, Ep. 11*

## Purpose

Every earlier episode moved data *into* OpenBB or rendered it *inside*
OpenBB Workspace. This one runs the other way: a tick dataset lands in a
shared object store, and a plain Python program on the reader's own
machine — `tick-lab`, which never imports OpenBB — reads it back, builds
1-minute bars, and checks them against an independent source. The store is
[ArcticDB](https://arcticdb.io) over S3, and the S3 is a self-hosted
[MinIO](https://min.io) that joins the tailnet as its own node.

## MinIO is its own tailnet node, not a Serve route

Every service so far in this repo has been reached through **one** front
door — `https://openbb.<your-tailnet>.ts.net`, terminated by Tailscale
Serve. MinIO breaks that pattern on purpose.

S3 clients sign every request with SigV4, and the `Host` header is part of
what gets signed. A Serve route is a reverse proxy: it sits in front of the
origin and can rewrite or re-terminate that header on the way through. Put
MinIO behind `openbb.<your-tailnet>.ts.net/minio/...` and the signature
ArcticDB computed no longer matches the request MinIO receives — a failure
mode this chapter has no reason to go looking for. So `minio` gets a **node
of its own**, `minio.<your-tailnet>.ts.net`, with a real Let's Encrypt
certificate issued directly for that name. ArcticDB — in the container and
on the reader's laptop — connects straight to it. No proxy sits in the
signing path, because none needs to.

## `tailscaled` runs inside the MinIO container, not a sidecar

Every other service in this stack joins the tailnet by attaching to the
`openbb` node's network namespace (`network_mode: service:tailscale`) — one
sidecar owns the interface, everything else rides along. That pattern does
**not** extend to a second, independent node, and the reason is filesystem,
not network.

`tailscaled`'s control socket is a file. `network_mode` shares only the
network *namespace* between containers, never the filesystem — so a `minio`
container attached to a `minio-ts` sidecar's namespace can see the sidecar's
network interfaces but can never open `/var/run/tailscale/tailscaled.sock`,
because that socket lives in the sidecar's own filesystem. A `tailscale`
CLI invocation inside `minio` would spin waiting for a daemon it structurally
cannot reach, and the `mc` bucket-init step would exhaust its retries the
same way. This was tried and confirmed broken before the current shape was
settled on.

The fix moves the daemon in with the process that needs it. `minio/Dockerfile`
layers the `tailscale`/`tailscaled` binaries from the official Tailscale image
onto the MinIO base image; `minio/entrypoint.sh` starts `tailscaled`, brings
the node up (`tailscale up --authkey=... --hostname=minio`), obtains a
certificate, and only then execs `minio server`. The daemon, the CLI, and the
MinIO process that must eventually be signalled all share one filesystem and
one container lifecycle. It's still its own tailnet node — nothing above
changes — it simply has no sidecar of its own.

## TLS is real, and renewal signals its own child with SIGHUP

Consistent with this series' posture everywhere else, MinIO doesn't get a
self-signed certificate: `tailscale cert` issues a real Let's Encrypt cert
for `minio.<your-tailnet>.ts.net`, and ArcticDB connects with `s3s://`.

Let's Encrypt certs are 90 days, and Tailscale's official image ships no
renewal loop. `minio/cert-sync.sh` is a plain shell script run on a timer
(`MINIO_CERT_RENEW_SECONDS`, default 43200s / 12h) from inside
`entrypoint.sh`. It writes a fresh cert to a staging directory, `cmp`s it
against what's live, and only promotes and signals if the bytes actually
changed — a failed renewal never overwrites a working certificate.

The reason the loop lives in *this* container, signalling *its own child*,
is a measured fact, not a guess: **MinIO does not notice a certificate
rewritten on disk out from under it — verified unchanged 36 seconds after
the file changed — but it reloads on `SIGHUP`: the certificate serial
updates and container uptime stays untouched, i.e. no restart.** That's
what was measured; connections in flight during the signal were never
observed, so this is not a claim about zero dropped connections. That
result is what makes the whole design work: renewal can be a
dumb shell loop instead of a supervised restart cycle. The alternative —
signalling MinIO from a sibling container — would need the Docker socket
mounted into that sibling so it could reach across container boundaries,
which this repo doesn't do for anything and isn't starting here. Co-locating
the renewer with the process it signals sidesteps that entirely: `kill -HUP
$(cat /run/minio.pid)` needs nothing but a PID already living in the same
container.

## `ARCTICDB_S3_*`: one file, two consumers

`minio.env` is the single source of truth for the store's connection
details — copied once (`cp minio.env.example minio.env && chmod 600
minio.env`), read twice:

- `openbb-api` loads it as a compose `env_file`, and
  `openbb_arcticdb.utils.resolve_config` assembles an S3 URI from it
  (`ARCTICDB_S3_ENDPOINT`, `_PORT`, `_BUCKET`, `_ACCESS`, `_SECRET`,
  `_SECURE`) whenever `ARCTICDB_URI` itself isn't set — the same precedence
  chain the `openbb-arcticdb` extension has always had: explicit arg →
  OpenBB credential → `ARCTICDB_URI` → `ARCTICDB_S3_*` → the local LMDB
  default.
- `tick-lab` reads the identical variable names into its own `.env`
  (`tick_lab.config.from_env`) and assembles the same URI shape
  independently, because it deliberately never imports OpenBB.

Neither side invents its own naming convention, so `minio.env`'s values —
copied verbatim into `tick-lab/.env` — are guaranteed to point both the
container and the reader's laptop at the same bucket. The URI shape is
specific enough to be worth stating plainly: host and bucket are joined with
`:`, and the port is a **query parameter**:

```
s3s://<endpoint>:<bucket>?port=<port>&access=<key>&secret=<secret>&use_virtual_addressing=false
```

`host:port:bucket` looks plausible and is not valid ArcticDB syntax.

**`ARCTICDB_S3_ACCESS`/`_SECRET` are `MINIO_ROOT_USER`/`_PASSWORD` — the
root/admin credential, not a scoped one.** The same value that authenticates
`openbb-api` and `tick-lab` to the S3 API also logs into the MinIO console
on `:9001`, reachable by every tailnet peer. A bucket-scoped key would be
better posture; this is a deliberate trade-off to keep the reader's setup to
one credential instead of provisioning IAM policies for a demo store.

One wrinkle this convention runs into: Compose interpolates `${...}` only
from the shell environment or a root `.env` file, **never** from a service's
`env_file:` entries. So `minio/entrypoint.sh` cannot be handed
`ARCTICDB_S3_ENDPOINT` as a compose-level `MINIO_CERT_DOMAIN` — it reads
`ARCTICDB_S3_ENDPOINT` directly from its own process environment instead,
where `env_file: ./minio.env` did put it. `minio.env` stays the one file to
edit either way.

## The amd64 pin, and what it means on Apple Silicon

ArcticDB publishes no `aarch64` Linux wheels — PyPI carries
`manylinux_2_17_x86_64` only. Since the Platform image now bundles
`openbb-arcticdb`, `openbb-api` and `openbb-mcp` are pinned to
`platform: linux/amd64` in `docker-compose.yml`. On an x86_64 host (the NAS
this series targets) that pin is a no-op. **On an Apple Silicon Mac it means
the image runs under emulation** — verified working, but expect a
noticeably slower `docker compose build` and slower ArcticDB queries than a
native image would give you. This is a performance caveat, not a
correctness one: nothing behaves differently under emulation, it's just
slower.

`tick-lab` is unaffected by any of this. It runs on the reader's machine, not
in the container, and ArcticDB **does** publish macOS `arm64` wheels — so an
M-series laptop resolves a native `arcticdb` install for `tick-lab` even
while the Docker image it talks to is running emulated x86_64 next to it.

## `:9000` is reachable from the Docker bridge, not just the tailnet

The `minio` container has no `ports:` mapping in `docker-compose.yml` — the
S3 API (`:9000`) and console (`:9001`) are not published to the host or the
LAN, and `scripts/verify-isolation.sh` checks that the S3 API answers over
TLS with a valid certificate from a second tailnet device (there's no "must
be refused" check for it the way there is for `:6900` or `:5000`, since
`minio` is *supposed* to answer on its own node).

The caveat that must not get lost in that story: **the Docker bridge network
can also reach `:9000`.** `minio` has no `network_mode: service:...`
override of its own — it sits on the same default compose network as every
other service, discoverable by its container name — so any sibling
container, or the Docker host itself, can reach `https://minio:9000`
directly, no Tailscale involved (`minio server --certs-dir` makes the
endpoint TLS-only by construction — there is no plaintext `http://` to
fall back to). That is a deliberate, documented limit of
this posture, not an oversight: nothing on the LAN can reach it, because
nothing routes from the LAN into the Docker bridge in this deployment. But
it is a real boundary, one narrower than "only the tailnet can reach this,"
and stating it any more strongly than that would overstate what's actually
true.

## See also

- [`tick-lab/README.md`](../tick-lab/README.md) — installing and running the
  CLI this store exists to demonstrate.
- [`docs/kdb-cache-design.md`](kdb-cache-design.md) — the same
  "co-locate the daemon with the process it must signal" and "shared
  network namespace, not a shared filesystem" reasoning shows up there for a
  different service (kdb+ inside `openbb-api`, not MinIO).

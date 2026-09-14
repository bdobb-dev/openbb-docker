> **SUPERSEDED** by `2026-08-27-internal-network-from-ep1-design.md`, which
> builds the internal network from Ep. 1 instead of migrating to it at Ep. 11.
> This document remains the reference for migrating the **running NAS**, which
> is on Ep. 11 and is a separate problem from what the series ships.

# Removing the network-namespace coupling — design

Move the openbb stack's seven services off `network_mode: service:tailscale`
onto a dedicated bridge, with Tailscale Serve proxying to container names. A
sidecar restart then cannot strand anything.

## Why

On 2026-08-27 the whole stack returned 502 for several minutes — the API,
key-maint and live-grid together. Nothing had crashed. The sidecar's own log
gives the mechanism exactly:

    boot: 19:54:23  Running 'tailscale up'
    boot: 19:55:23  Sending SIGTERM to tailscaled
    boot: 19:55:23  failed to auth tailscale: tailscale up failed: signal: killed

containerboot kills `tailscale up` at 60s, exits 0, and `restart: unless-stopped`
bounces the container. The retry succeeded in **one second**, so the stall was
transient, not a broken config. It will recur.

The restart is the trigger. The damage comes from `network_mode:
service:tailscale`: seven containers are bound to the *sandbox* the sidecar
owned when they started. A restart creates a new one and the children hold the
corpse — mutually reachable on loopback, invisible to Serve, every container
still reporting `Up`.

The same failure is documented in `<nas-path>/gitea-netns-reconcile.sh` from a
different trigger (WAN not up after a reboot). Three stacks on this NAS run
watchdogs that restart stranded children on a timer. This design removes the
coupling instead.

## Decisions

**D1. A dedicated bridge replaces the loopback boundary.**
`openbb-internal` carries only these containers. Docker networks are isolated by
default, so nothing outside reaches the services even though they will bind
`0.0.0.0`.

Accepted cost, stated plainly: any container later attached to
`openbb-internal` gets unauthenticated access to live-grid, stores-explorer,
both MCP services and **kdb**, none of which authenticate. Today that requires
joining a network namespace — a more deliberate act than
`--network openbb-internal`. kdb is the sharpest edge: a q instance with no auth
on a shared network.

Rejected: adding auth to live-grid and stores-explorer first (correct, but a
separate project that would block this indefinitely), and migrating only the
already-authenticated services (leaves both models running and half the stack
still stranding).

**D2. Serve proxies to container names.**
Verified, not assumed: `tailscale serve --bg --https=9443
http://some-container:1234` was accepted against the running sidecar and
reported back as `proxy http://some-container:1234`. Acceptance is not delivery;
the rehearsal proves delivery.

**D3. The sidecar joins the bridge in addition to its own namespace.**
It keeps `NET_ADMIN`, `/dev/net/tun` and its own netns for WireGuard, and gains
a bridge interface so it can resolve service names.

**This is the one genuinely unproven element of the design.** tailscaled
manipulates routing and iptables in its namespace; whether it behaves correctly
while dual-homed is exactly what the rehearsal exists to establish. If it does
not, the fallback is the network-anchor approach in "Rejected alternatives".

**D4. Rehearse on a throwaway stack before touching the NAS.**
A separate compose project on the NAS — own project name, own container names,
the same images, native amd64. Not this Mac: emulated, and its Docker had a
socket fault the same day.

**D5. key-maint's bind is a prerequisite, not a step.**
Every other service's bind address is settable from compose. key-maint's is
hardcoded at `key-maint/app/main.py:33` (`host="127.0.0.1"`, port
`NETWORK_PORT = 8447`) with no env var, so it needs a source change, review and
a rebuilt image *before* the migration can begin. Adding `KEY_MAINT_HOST`
defaulting to `127.0.0.1` changes nothing until the migration sets it.

Only that one server is affected. key-maint runs two: an admin server on a unix
socket (`uds=admin_socket`, `main.py:28`) and the network server on 8447. The
admin socket is not a TCP bind and needs no change — which also means the
admin surface stays off the bridge entirely, unchanged by this design.

## Target topology

    before                            after
      openbb-ts ──┐                     openbb-ts ──┬── wg0 (own netns)
      kdb ────────┤ one shared          Serve ──────┘
      openbb-api ─┤ netns, all                │ openbb-internal (bridge)
      live-grid ──┤ on 127.0.0.1        ┌─────┼──────┬───────────┬────────┐
      …7 total ───┘                    kdb  openbb-api live-grid  …7 total
                                       :5000   :6900     :6903

`openbb-data` and `minio` were never on the namespace and are unaffected.

## The four layers of change

**Layer 1 — membership.** Seven services (`kdb`, `openbb-api`, `live-grid`,
`openbb-mcp`, `stores-mcp`, `stores-explorer`, `key-maint`) drop
`network_mode: service:tailscale` and gain `networks: [openbb-internal]`. The
sidecar gains the same network alongside its existing configuration.

**Layer 2 — bind addresses.** Three different mechanisms, which is why this
layer is not uniform:

| service | bind lives in | change |
|---|---|---|
| kdb | compose env `KX_PORT=127.0.0.1:5000` | `5000` |
| openbb-api | compose `command:` | `--host 0.0.0.0` |
| openbb-mcp | compose `command:` | `--host 0.0.0.0` |
| stores-mcp | compose env `STORES_HOST` | `0.0.0.0` |
| live-grid | **Dockerfile `CMD`** | compose `command:` override |
| stores-explorer | **Dockerfile `CMD`** | compose `command:` override |
| key-maint | **hardcoded, `app/main.py:33`** | prerequisite PR (D5) |

live-grid and stores-explorer are overridden in compose rather than rebuilt, so
their images keep binding loopback by default — a compose that forgets the
override fails closed rather than exposing them.

**Layer 3 — Serve routes.** Six entries in `ts-config/serve.json` change target:
`6900→openbb-api`, `6901→openbb-mcp`, `6902→stores-mcp`, `6903→live-grid`,
`6904→stores-explorer`, `8447→key-maint`. Ports and the `TCP` block are
unchanged.

**Layer 4 — inter-service references.** `127.0.0.1` is also how the services
reach *each other*: `KX_PORT=127.0.0.1:5000` (openbb-api), `KDB_HOST=127.0.0.1`
(`deps.env`), `KX_HOST=127.0.0.1` (stores-mcp, stores-explorer) all become
`kdb`.

`deps.env` is shared across services, so that single edit lands everywhere at
once. It therefore cannot change until every consumer has moved.

## Why this is one window, not several

Layer 3 is one file and Layer 4 contains one shared file. Neither can be
half-applied. Combined with D5, the sequence is fixed:

1. prerequisite PR — `KEY_MAINT_HOST`, merged and the image rebuilt
2. rehearsal — full migrated stack on a scratch project
3. one cutover — all four layers together

Per-service migration was considered and rejected: a mixed stack would need
`KDB_HOST` to resolve both ways simultaneously, which is more total complexity,
not less.

## Rehearsal

Must prove, in priority order:

1. tailscaled works dual-homed (D3 — the unknown)
2. Serve delivers to a container name, not merely accepts one (D2)
3. services reach each other by name, especially kdb over PyKX's raw IPC
4. **killing the sidecar strands nobody** — today's failure, reproduced and absent

Item 4 is the acceptance criterion. Without it the migration bought nothing.

**Cost requiring a decision:** the rehearsal stack needs its own tailscale node,
so a hostname and an auth key must be supplied, and a second device appears in
the tailnet until teardown. The reduced alternative — a `curl` container standing
in for Serve — leaves D2 and D3 unproven, which are the two riskiest items, so it
is not recommended.

## Cutover and rollback

    1. back up docker-compose.yml, deps.env, ts-config/serve.json  (.pre-bridge)
    2. apply all four layers in one edit
    3. docker compose up -d
    4. verify every surface
    5. deliberately `docker restart openbb-ts`, then verify again

Step 5 is not optional: on production, the migration is only proven by causing
the failure it exists to prevent.

Rollback is three files plus `docker compose up -d`. No image changes are
involved, so rollback requires no rebuild. (The `KEY_MAINT_HOST` prerequisite is
backward-compatible by construction and does not need reverting.)

## Verification

| check | expectation |
|---|---|
| `:443` without / with credentials | 401 / 200 |
| `:6903`, `:6904` | 200 |
| `:10000` with credentials | 200 |
| `/equity/price/quote?provider=kdb` | rows — proves service→service over the bridge |
| `/subscriptions`, then add a symbol | 200/201 and the feed starts |
| restart the sidecar, repeat all of the above | all still pass |

## Rejected alternatives

**Network anchor.** A do-nothing container (`alpine`, `sleep infinity`) owns the
namespace; everything including the sidecar uses
`network_mode: service:netns-anchor`. A sidecar restart leaves the namespace
intact because the anchor still holds it.

Roughly an order of magnitude smaller: no bind changes, no inter-service config
changes, no `serve.json` changes — one new container and eight edited
`network_mode:` lines. It preserves the loopback boundary entirely, so it trades
away no security posture.

Rejected in favour of the bridge because the bridge is the conventional Docker
model and gives per-service network identity. Recorded here because it remains
the fallback if D3 fails in rehearsal, and because it is the smaller change on
every axis except conventionality.

**A netns-reconcile watchdog**, as the wp-* and gitea stacks run. Restarts
stranded children on a timer. Rejected as a periodic repair for a structural
problem, though it is what those three stacks rely on today.

**Fixing only the trigger** — a sidecar healthcheck plus
`depends_on: condition: service_healthy`. Closes the exact 2026-08-27 sequence
(children attaching to a sidecar about to bounce) but not a bounce that happens
after they attach. A narrower net than it appears.

## Out of scope

Authentication for live-grid, stores-explorer, the MCP services or kdb.
Investigating why `tailscale up` stalled past 60s. The three other stacks on
this NAS that share the same coupling.

## Addendum: rss-ticker joins the migration

Written while implementing this design, not part of the original decision
above -- Layers 1-4 were drafted for the seven services this doc names, and
rss-ticker was deliberately left out because it isn't one of them. Leaving it
out for real would have kept it stranded by the exact sidecar-restart failure
this whole design exists to fix, so it was pulled in as an eighth service.

It needed its own prerequisite first, in rss-ticker's own repo (not this
one): `tailscale_auth` hard-required a loopback `bind_host`, on the same "only
Serve can reach a loopback port" reasoning D1 replaces for everything else
here. Relaxed to also trust `0.0.0.0`, under the same bridge-isolation
guarantee D1 already accepts as an operator responsibility the code cannot
itself verify -- anything that isn't loopback or `0.0.0.0` is still rejected
outright. See rss-ticker's `feat/ticker-server` branch, commit `107775c`.

With rss-ticker included, Layer 1 is eight services, Layer 3 gains a sixth
migrated Serve route (`:8088` -> `rss-ticker:8088`), and the "D1 accepted
cost" framing above needs one caveat: rss-ticker is the one migrated service
that still authenticates on the bridge (via `tailscale_auth`, not by being
unauthenticated like the other seven's edge case).

Status: drafted, UNVERIFIED, same as the rest of Layers 1-4 -- not rehearsed,
not cut over.

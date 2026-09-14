# The internal network, from Ep. 1 — design

Build the stack on a dedicated Docker bridge from Episode 1. Every later
episode's service joins that bridge as it is introduced.
`network_mode: service:tailscale` never appears in the series.

**Supersedes** `2026-08-27-bridge-network-design.md`, which designed the same
end state as a seven-service cutover on the Ep. 11 stack. That document
remains the reference for migrating the running NAS, which is a separate
problem from what the series ships.

## Why this belongs at Ep. 1, not Ep. 11

The evidence is an outage. On 2026-08-27 the whole stack returned 502 for
several minutes — API, key-maint and live-grid together — with nothing crashed.
The sidecar log gives the mechanism exactly:

    boot: 19:54:23  Running 'tailscale up'
    boot: 19:55:23  Sending SIGTERM to tailscaled
    boot: 19:55:23  failed to auth tailscale: tailscale up failed: signal: killed

containerboot kills `tailscale up` at 60s, exits 0, and `restart: unless-stopped`
bounces the container. The retry succeeded in **one second** — a transient stall,
not a broken config, so it will recur.

The restart is only the trigger. The damage is `network_mode:
service:tailscale`: the children are bound to the *sandbox* the sidecar owned
when they started. A restart creates a new one and the children hold the corpse
— mutually reachable on loopback, invisible to Serve, every container still
reporting `Up`. Three other stacks on the same NAS run watchdog scripts for
this; `<nas-path>/gitea-netns-reconcile.sh` documents the identical failure
from a different trigger.

Fixing it at Ep. 11 means a cutover: four layers that cannot be half-applied, a
rehearsal stack with its own tailnet node, and a rollback plan. Introducing it
at Ep. 1 means a compose file that reads differently. Same end state, none of
the cutover risk — and each later episode then adds one service to a network
that already exists, instead of inheriting a coupling it will later have to
shed.

There is a second reason, specific to a teaching series. The coupling is not
merely fragile, it is *sharp*: under a shared namespace a single `--host
0.0.0.0` publishes the API to the entire tailnet. Teaching the bridge first
means never teaching a structure whose worst typo is that bad.

## Decisions

**D1. A user-defined bridge, `openbb-internal`.**
Docker networks are isolated from each other by default, so the bridge is the
boundary the shared namespace used to be. Services bind `0.0.0.0` *on that
bridge* rather than `127.0.0.1` inside a namespace they do not own.

**D2. Serve proxies to service names, not `127.0.0.1`.**
`ts-config/serve.json` targets `http://openbb-api:6900`. Compose's embedded DNS
resolves both service and container names on a user-defined network; the spec
standardises on the **service** name, which is stable even when `container_name`
differs from it (`key-maint` vs `openbb-key-maint` at Ep. 3).

Verified, not assumed: `tailscale serve --bg --https=9443
http://some-container:1234` was accepted against a running sidecar and reported
back as `proxy http://some-container:1234`. Acceptance is not delivery — see
Verification.

**D3. The sidecar is dual-homed.**
It keeps `NET_ADMIN`, `/dev/net/tun` and its own namespace for WireGuard, and
gains an `openbb-internal` interface so it can resolve and reach service names.

**This remains the one unproven element.** tailscaled manipulates routing and
iptables in its namespace; whether it behaves correctly while dual-homed is what
Verification exists to establish. If it does not, the fallback is the network
anchor in Rejected alternatives.

**D4. `TS_USERSPACE=false` stays, and its comment changes meaning.**
Kernel mode is still mandatory, but not for the reason the current comment
gives. Today it is what makes the tailnet IP a real interface so that the
loopback socket beside it is unreachable. On the bridge the API is not in that
namespace at all, and kernel mode is required because userspace mode terminates
inbound connections in tailscaled and forwards them to `127.0.0.1` — which,
post-change, is a loopback with nothing on it. The comment must be rewritten,
not carried over; it currently explains a boundary that no longer exists.

**D5. The forward contract.** Every later episode adds a service by giving it
`networks: [openbb-internal]`, a `0.0.0.0` bind, and a `serve.json` entry
targeting its service name. No episode reintroduces `network_mode:`. Two
consequences are load-bearing enough to state:

- Any service whose bind address is baked into a Dockerfile `CMD` gets a compose
  `command:` override rather than an image change, so an image that loses the
  override fails closed on loopback instead of exposing itself on the bridge.
- key-maint (Ep. 3) hardcodes `host="127.0.0.1"` in Python
  (`key-maint/app/main.py:27` at v3.0.0). Under this design it must read the
  bind from the environment **in the episode that introduces it** — it is no
  longer a migration prerequisite, it is part of key-maint's first appearance.
  Its admin server is a unix socket (`uds=admin_socket`) and stays off the
  bridge entirely, unchanged.

## The isolation property — what actually changes

This is the claim Ep. 1 is built on, so the change deserves to be exact rather
than reassuring. `scripts/verify-isolation.sh` probes
`http://openbb.<your-tailnet>:6900/` from a second tailnet device.

| reachable from | today (shared namespace) | on the bridge |
|---|---|---|
| a second tailnet device, `:6900` | refused — the tailnet IP is a real interface, nothing listens on it at 6900 | refused — the sidecar has **no listener on 6900 at all** |
| the LAN | no — no ports published | no — bridge subnets are not routed off-host, no ports published |
| **another process on the Docker host** | **no** — a container's loopback is private to its namespace | **yes** — the host can route to the bridge subnet |

Two findings, in opposite directions.

**The main case improves.** `verify-isolation.sh` keeps passing, and for a
better reason: there is no socket in the tailnet-facing namespace to
misconfigure into visibility. The failure modes are not comparable in severity —
under the shared namespace, `--host 0.0.0.0` is a one-word typo that publishes
the API to every tailnet peer; on the bridge, `0.0.0.0` *is* the correct value
and the equivalent mistake is adding a `ports:` block, which is conspicuous in
a diff and in review.

**One case regresses, and it lands exactly at Ep. 1.** Any process on the
Docker host can reach the API on the bridge IP, which it cannot today. Ep. 1 is
the one episode with no authentication — Basic auth arrives in Ep. 2 and closes
this. The regression is narrow (host-local only; not the LAN, not the tailnet)
but it is real, and Ep. 1's README currently promises the API is "invisible to
everything else." That sentence has to become invisible to everything *off this
host*, or it is false.

Accepted rather than mitigated. `internal: true` on the network is **not** the
answer and must not be reached for: it blocks egress, and the API's entire
purpose is outbound calls to data providers.

## Ep. 1, concretely

Three files. `docker-compose.yml`:

```yaml
networks:
  openbb-internal:
    driver: bridge

services:
  tailscale:
    # unchanged except:
    networks: [openbb-internal]

  openbb-api:
    # network_mode: service:tailscale   <- removed
    networks: [openbb-internal]
    command: ["openbb-api", "--host", "0.0.0.0", "--port", "6900"]
```

`ts-config/serve.json`, one target:

```json
"/": { "Proxy": "http://openbb-api:6900" }
```

And the prose in both `docker-compose.yml`'s header and `README.md`, which
currently opens "The tailscale container owns the network namespace; the API
joins it." That is the sentence being replaced. The stack still presents as one
tailnet node; it does so because only the sidecar is on the tailnet, not because
the two share a namespace.

## What each later episode inherits

The contract in D5, applied. This table is the forward plan, not work in this
spec:

| episode | service | bind lives in | joins the bridge as |
|---|---|---|---|
| 3 | key-maint | **hardcoded, `app/main.py:27`** | env-driven bind, from its first appearance (D5) |
| 6 | openbb-mcp | compose `command:` | `--host 0.0.0.0` |
| 8 | rss-ticker | — | `networks:` + serve entry |
| 9 | live-grid | **Dockerfile `CMD`** | compose `command:` override |
| 10 | kdb | compose env `KX_PORT` | `5000`; consumers use `kdb`, not `127.0.0.1` |
| 11 | stores-mcp, stores-explorer | env / **Dockerfile `CMD`** | `0.0.0.0` / compose override |

Ep. 10 is where inter-service references start mattering: `KX_PORT`,
`KDB_HOST`, `KX_HOST` all name `127.0.0.1` today and become `kdb`. Under the
old migration design that was a shared-file edit that had to land in one window;
introduced episode by episode, each lands with the service that needs it.

## Verification

Ep. 1 has two containers, so this is a check rather than a rehearsal. In
priority order:

1. **tailscaled works dual-homed** (D3 — the unknown). The node comes up, Serve
   gets its certificate, `https://openbb.<your-tailnet>/widgets.json` answers.
2. **Serve delivers to a service name** (D2). Acceptance of the config is not
   delivery; the 200 above is the proof.
3. **`scripts/verify-isolation.sh` still passes from a second tailnet device.**
   Non-negotiable — it is the episode's claim.
4. **Restarting the sidecar strands nobody.** `docker restart openbb-ts`, then
   repeat 1–3. This is the acceptance criterion; without it the change bought
   nothing.
5. **The new regression is bounded.** From the Docker host, `curl` the API's
   bridge IP on 6900 — expect it to answer, confirming the documented cost is
   the documented cost and not something wider. From the LAN, the same address
   must fail.

Check 5 exists because a cost that is written down but never measured is a
guess.

### Results — rehearsed on the NAS, 2026-08-27

Run as an isolated compose project (`<nas-checkout>-ep1`, containers
`ep1-ts`/`ep1-api`, its own `ts-state`, no published ports) alongside the live
Ep. 11 stack. The nine running `openbb-*` containers were confirmed up after
every step. Ep. 1's `openbb-local:1.0.0` was substituted with the
`openbb-local:11.0.4` already on the host: every check below exercises the
network path, which does not depend on which API image sits behind it.

| # | check | result |
|---|---|---|
| 1 | tailscaled dual-homed (**D3**) | **PASS.** Came up with the bridge as `eth0: 172.29.28.2/22`, `Bringing router up`, `netfilter running in iptables mode`. No routing, iptables or permission errors before or after authorization. Node joined as `<ep1-node-ip> openbb-ep1`. |
| 2 | Serve delivers to a service name (**D2**) | **PASS.** `tailscale serve status` reports `/ proxy http://openbb-api:6900`; from a second tailnet device `https://openbb-ep1.<your-tailnet>/widgets.json` returned **200** with the widget manifest. Delivery, not merely acceptance. |
| 3 | the raw port is sealed | **PASS.** `http://openbb-ep1.<your-tailnet>:6900/` refused from a second tailnet device. |
| 4 | a sidecar restart strands nobody | **PASS**, twice. After `docker restart ep1-ts`, `ep1-api`'s `StartedAt` and bridge IP were unchanged, the restarted sidecar still reached it by service name, and `https://.../widgets.json` still returned 200 with `:6900` still refused. |
| 5 | the documented regression is bounded | **PASS.** From the Docker host, `http://172.29.28.3:6900/widgets.json` → **200**, confirming the cost the README now states. From a second tailnet device the same address was unreachable, confirming the bridge is not routed off-host. |

D3 was the one element the design could not assert in advance. It holds: nothing
in tailscaled's routing or netfilter setup objects to a second interface.

Not demonstrated: the counterfactual. Restarting production's `openbb-ts` to
show the old topology stranding its children would cause a real outage on a
host where that has already happened once. The 2026-08-27 incident log stands
as the evidence for the failure; this rehearsal is the evidence for its
absence.

## Rejected alternatives

**Network anchor.** A do-nothing container (`alpine`, `sleep infinity`) owns the
namespace; everything including the sidecar uses
`network_mode: service:netns-anchor`. A sidecar restart leaves the namespace
intact because the anchor still holds it. It preserves the loopback boundary
exactly, so it has no host-reachability cost at all.

Rejected because it teaches a stranger structure than the conventional Docker
one, and because per-service network identity is worth more across eleven
episodes than it is in a one-off repair. Recorded because it remains the
fallback if D3 fails, and because on the Ep. 11 NAS — where the cutover risk is
real and the pedagogy is irrelevant — it is still the cheaper answer.

**A netns-reconcile watchdog**, as three other stacks on this NAS run. A
periodic repair for a structural problem. Whatever its merits for the running
NAS, shipping it in a teaching series would teach the workaround instead of the
fix.

**Leaving Ep. 1–10 alone and introducing the bridge later.** Every episode
before it would teach the coupling, and the episode that introduced the bridge
would have to spend its budget on a seven-service migration rather than on its
own subject.

## Out of scope

Migrating the running NAS, which is on Ep. 11 and covered by the superseded
document. Authentication for live-grid, stores-explorer, the MCP services or
kdb. Why `tailscale up` stalled past 60s. The three other stacks on the NAS
that share the coupling. The release mechanics — whether this re-cuts `v1.0.0`
in place or ships as `v1.0.1` — which is a decision for the plan, not a design
question.

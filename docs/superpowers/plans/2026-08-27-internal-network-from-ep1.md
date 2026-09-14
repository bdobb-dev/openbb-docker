# Internal network from Ep. 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Ep. 1 on a dedicated Docker bridge instead of `network_mode: service:tailscale`, so no episode in the series ever teaches the coupling.

**Architecture:** A user-defined bridge `openbb-internal` carries both containers. The API binds `0.0.0.0` on that bridge instead of `127.0.0.1` inside a namespace it does not own, and Tailscale Serve proxies to the service name. The sidecar keeps its own namespace for WireGuard and is dual-homed onto the bridge.

**Tech Stack:** Docker Compose, Tailscale (containerboot + Serve), bash.

**Spec:** `docs/superpowers/specs/2026-08-27-internal-network-from-ep1-design.md`

## Global Constraints

- **Release mechanics are an open decision, and Task 1 assumes one.** This plan
  lands on a `release/v1.0.x` branch cut from `v1.0.0`, shipping as **v1.0.1** —
  mirroring the pattern `release/v3.0.x` established in
  [artcashin/openbb-docker#17](https://github.com/artcashin/openbb-docker/pull/17).
  The alternative is re-cutting the `v1.0.0` tag in place, which the repo has
  precedent for (`backup/pre-auth-patch-tags`) and which is the only option that
  makes a checkout of `v1.0.0` free of the coupling. **Confirm before Step 7 of
  Task 1** — everything up to the commit is identical either way.
- **`TS_USERSPACE=false` stays.** It is still mandatory. Its *comment* is now
  wrong and gets rewritten (spec D4) — do not delete the setting.
- **Never add `internal: true`** to the network. It blocks egress, and the API
  exists to make outbound provider calls.
- **Never add a `ports:` block.** On the bridge that is the mistake that
  replaces the old `--host 0.0.0.0` typo, and it is the one thing that would
  publish the API to the LAN.
- Serve targets use the **service** name (`openbb-api`), not the container name
  — stable when the two differ in later episodes (spec D2).
- The API's bind becomes `0.0.0.0`. On the bridge that is correct, not a
  loosening; the boundary is the network, not the socket.

## File structure

| file | responsibility |
|---|---|
| `docker-compose.yml` | the network, both services' membership, the API bind, and the header prose |
| `ts-config/serve.json` | the one Proxy target |
| `README.md` | the two isolation claims that stop being true as written |
| `scripts/check-no-netns.sh` (new) | CI guard: the coupling must not come back in any later episode |
| `.github/workflows/ci.yml` | runs the guard in the existing `scrub` job |

---

### Task 1: The bridge, and a guard that keeps it

The change cannot be usefully split: compose, `serve.json` and the prose
describe one topology, and a half-applied edit is a broken stack rather than a
testable increment.

**Files:**
- Modify: `docker-compose.yml`, `ts-config/serve.json`, `README.md`, `.github/workflows/ci.yml`
- Create: `scripts/check-no-netns.sh`

**Interfaces:**
- Consumes: nothing.
- Produces: the `openbb-internal` network that every later episode's service joins (spec D5).

- [ ] **Step 1: Write the failing guard**

Create `scripts/check-no-netns.sh`:

```bash
#!/usr/bin/env bash
# No service may share the Tailscale sidecar's network namespace.
#
# `network_mode: service:tailscale` binds a container to the SANDBOX the
# sidecar owned when that container started. containerboot kills `tailscale up`
# at 60s and exits 0, `restart: unless-stopped` bounces the sidecar, and the
# restart creates a NEW sandbox -- so every child is left holding the corpse:
# mutually reachable on loopback, invisible to Serve, and still reporting `Up`.
#
# The stack uses the openbb-internal bridge instead, from Ep. 1 onward. This
# guard exists so a later episode cannot quietly reintroduce the coupling when
# it adds its own service.
#
# Scoped to the sidecar deliberately: `network_mode: service:<other>` is a
# legitimate pattern elsewhere (Ep. 11's minio-init shares minio's namespace on
# purpose, and minio is not something a restart of ours can strand).
set -euo pipefail

cd "$(dirname "$0")/.."

if grep -nE '^[[:space:]]*network_mode:[[:space:]]*"?service:tailscale"?' docker-compose.yml; then
  echo "FAIL: the lines above share the sidecar's network namespace." >&2
  echo "      Use: networks: [openbb-internal]  -- and bind 0.0.0.0, not 127.0.0.1." >&2
  exit 1
fi

echo "OK: nothing shares the sidecar's network namespace"
```

Then `chmod +x scripts/check-no-netns.sh`.

- [ ] **Step 2: Run it and verify it fails**

Run: `./scripts/check-no-netns.sh`
Expected: FAIL, printing the `openbb-api` line — the tree still has the
coupling. **Record the output.** A guard that has never failed is not known to
work.

- [ ] **Step 3: Add the network and move both services**

In `docker-compose.yml`, add a top-level `networks:` block (beside the existing
`volumes:` block at the end):

```yaml
networks:
  openbb-internal:
    driver: bridge
```

On the `tailscale` service, add — keeping `devices:`, `cap_add:`, and every
existing key:

```yaml
    networks: [openbb-internal]
```

On `openbb-api`, **remove** `network_mode: service:tailscale`, add the same
`networks:` line, and change the bind:

```yaml
    command: ["openbb-api", "--host", "0.0.0.0", "--port", "6900"]
```

Keep `depends_on: - tailscale`.

- [ ] **Step 4: Repoint Serve**

In `ts-config/serve.json`, the single handler target:

```json
"/": { "Proxy": "http://openbb-api:6900" }
```

- [ ] **Step 5: Rewrite the prose that is now false**

Three places. These are not cosmetic — each currently states the old topology
as fact.

`docker-compose.yml` header, first paragraph. Replace:

> The tailscale container owns the network namespace; the API joins it, so the
> pair appears on your tailnet as one node named "openbb".

with a statement of the actual arrangement: the two containers share a private
bridge, only the sidecar is on the tailnet, and the pair still presents as one
node named `openbb` — because the API has no tailnet presence, not because the
two share a namespace.

`docker-compose.yml`, the `TS_USERSPACE=false` comment (spec D4). It currently
explains that kernel mode makes the tailnet IP a real interface "so the
127.0.0.1 socket below is genuinely unreachable." There is no such socket now.
Rewrite it to say kernel mode is mandatory because userspace mode terminates
inbound connections inside tailscaled and forwards them to `127.0.0.1`, which
after this change is a loopback with nothing listening on it.

`README.md`, lines 17-21 and 25. The bullets say the sidecar "owns the network
namespace" and the API is "sharing that namespace, bound to loopback only" —
replace with the bridge. Then line 25:

> device on your tailnet and invisible to everything else.

**must** become invisible to everything *off this host*. Per the spec's
isolation table this is the one real regression: any process on the Docker host
can now reach the API on its bridge IP, and Ep. 1 has no authentication (Basic
auth arrives in Ep. 2). Leaving the sentence as-is would make the README false.

- [ ] **Step 6: Run the guard and the existing gates**

`docker compose config` reads `env_file:` targets, and `ts.env` is gitignored
and absent from a fresh checkout — verified: without it the command fails with
`env file ... not found`, which has nothing to do with this change. Create an
empty stub first, and do not commit it:

```bash
[ -f ts.env ] || : > ts.env       # gitignored; only so `config` can resolve env_file
./scripts/check-no-netns.sh          # now OK
bash scripts/scrub-check.sh          # still passes with a new file present
docker compose config -q          # exits 0 — the YAML edits are well-formed
```

Expected: all three pass. `docker compose config -q` is the only mechanical
check that the YAML edits are well-formed — run it even though nothing is
started.

- [ ] **Step 7: Wire the guard into CI**

In `.github/workflows/ci.yml`, in the `scrub` job, after the existing scrub
step:

```yaml
      - name: No service shares the sidecar's network namespace
        run: bash scripts/check-no-netns.sh
```

The `scrub` job is a bash-only runner already, so this costs no extra CI time.

- [ ] **Step 8: Confirm release mechanics, then commit**

Per Global Constraints, confirm v1.0.1-on-a-branch versus re-cutting `v1.0.0`
before committing. For the branch route:

`scripts/check-no-netns.sh` is a new file, so `-a` alone would silently leave it
out and ship a plan whose own guard never reaches the repo:

```bash
git add scripts/check-no-netns.sh
git commit -am "feat: build the stack on an internal bridge from Ep. 1"
```

The commit message must state the isolation trade in full — main case improves,
host-local reachability regresses, Ep. 1 has no auth — because that is the
detail a reader of the history will need and the diff does not show it.

---

### Task 2: Prove it on a real tailnet node

Task 1 is unverifiable on a laptop: spec **D3** — whether tailscaled behaves
correctly while dual-homed — is the one genuinely unproven element of the
design, and only a live node answers it. Do not report Task 1 as working before
this task runs.

**Files:** none. This task produces evidence.

**Interfaces:**
- Consumes: the stack from Task 1.
- Produces: a go/no-go on D3. On failure, the network anchor in the spec's
  Rejected alternatives is the fallback, and that is a design decision to
  escalate — not something to fix by improvising.

- [ ] **Step 1: Bring the stack up on a Linux Docker host**

Not a Mac — the Ep. 11 rehearsal notes record that this Mac runs amd64 images
emulated and had a Docker socket fault. Use a host with a real tun device.

```bash
docker compose up -d
docker compose ps
```

Expected: both containers `Up`.

- [ ] **Step 2: D3 — tailscaled works dual-homed**

```bash
docker logs openbb-ts 2>&1 | tail -30
```

Expected: the node authenticates and Serve starts, with no routing or iptables
errors. **This is the step the whole design rests on.** If tailscaled fails
here, stop and escalate — do not work around it.

- [ ] **Step 3: D2 — Serve delivers to a service name**

From any tailnet device:

```bash
curl -fsS https://openbb.<your-tailnet>.ts.net/widgets.json | head -c 200
```

Expected: JSON. Serve *accepting* a service-name target was already verified;
this is the proof it *delivers*.

- [ ] **Step 4: The episode's own claim still holds**

From a SECOND tailnet device:

```bash
scripts/verify-isolation.sh openbb.<your-tailnet>.ts.net
```

Expected: `Isolation verified.` Non-negotiable — this script is Ep. 1's central
promise. It passes for a stronger reason than before: the sidecar now has no
listener on 6900 at all.

- [ ] **Step 5: The acceptance criterion — a restart strands nobody**

```bash
docker restart openbb-ts
sleep 30
curl -fsS https://openbb.<your-tailnet>.ts.net/widgets.json >/dev/null && echo "SURVIVED"
```

Expected: `SURVIVED`, with no restart of `openbb-api`. Under the old topology
this is precisely what returned 502s. **Without this step the change bought
nothing** — it is the reason the design exists.

- [ ] **Step 6: Measure the documented regression**

The spec accepts one cost; an unmeasured cost is a guess. On the Docker host:

```bash
ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' openbb-api)
curl -s -o /dev/null -w '%{http_code}\n' --max-time 5 "http://$ip:6900/widgets.json"
```

Expected: `200` — confirming the cost is exactly what the README now says.
Then from a LAN device that is **not** the Docker host and not on the tailnet,
the same address must fail to connect. If it answers, the bridge is routed off
the host and the design's boundary does not hold — stop and escalate.

- [ ] **Step 7: Record the results**

Append the six outcomes to the spec under Verification, as measurements rather
than expectations. Task 2's deliverable is the evidence; a plan that says
"expected: 200" and a run that observed it are different artifacts.

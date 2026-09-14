> **SUPERSEDED** by `2026-08-27-internal-network-from-ep1.md`. This plan
> migrated the seven-service Ep. 11 stack in one cutover; the series now builds
> on the bridge from Ep. 1. Still the reference for migrating the running NAS.

# Bridge-network migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the openbb stack off `network_mode: service:tailscale` onto a dedicated bridge, so a tailscale sidecar restart can no longer strand every other container.

**Architecture:** A new `openbb-internal` bridge carries all eight containers. The sidecar joins it alongside its own namespace and Serve proxies to container names instead of `127.0.0.1`. Four layers change together — membership, bind addresses, Serve routes, and the inter-service references that also use loopback.

**Tech Stack:** Docker Compose, Tailscale (containerboot + Serve), Python/uvicorn, pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-bridge-network-design.md`

## Global Constraints

- **Deviation from the spec, applied here:** the spec names the new env var `KEY_MAINT_HOST`. key-maint's existing variables are `KEYMAINT_CRED_FILE`, `KEYMAINT_AUTH_FILE`, `KEYMAINT_ADMIN_SOCKET` (`key-maint/app/main.py:53-55`). Use **`KEYMAINT_NETWORK_HOST`** to match. The spec's intent is unchanged.
- The default for `KEYMAINT_NETWORK_HOST` is `127.0.0.1`. Merging the prerequisite must change nothing until the migration sets it.
- key-maint's admin server binds a **unix socket** (`uds=admin_socket`), not TCP. It is unaffected by every task here and must stay that way.
- Bind addresses live in three places: compose env, compose `command:`, and Dockerfile `CMD`. live-grid and stores-explorer are `CMD`-baked and get a compose `command:` override — do NOT edit their Dockerfiles, so an image forgetting the override fails closed on loopback rather than exposing itself.
- `ts-config/serve.json` keeps its `TCP` block and all six ports unchanged. Only `Proxy` targets change.
- `deps.env` is shared across services. It cannot change until every consumer has moved, which is why Task 4 is one window.
- The NAS's `<nas-checkout>/docker-compose.yml` is hand-maintained and diverges from the repo's. Task 2 changes the repo; Task 4 applies the equivalent to the NAS by hand.
- Every NAS command runs with `PATH=<nas-path>/container-station/bin:$PATH`, and `HOME`/`DOCKER_CONFIG` pointed at a writable dir. The NAS has no `git` and no `python3`.
- Rollback is always three files plus `docker compose up -d`. No task rebuilds an image except Task 1.

## File structure

| file | responsibility |
|---|---|
| `key-maint/app/main.py` | gains `KEYMAINT_NETWORK_HOST`; the only source change in the plan |
| `key-maint/tests/test_main.py` | existing test pins `("127.0.0.1", 8447)`; extended for the env |
| `docker-compose.yml` | the four layers, in the repo's template |
| `ts-config/serve.json.example` | the Serve targets, if the repo carries one |

---

### Task 1: `KEYMAINT_NETWORK_HOST` — the prerequisite

_Implements spec **D5**._

key-maint is the only service whose bind address is hardcoded in Python
(`main.py:33`), so it cannot be moved from compose like the other six. This task
makes it configurable and changes nothing else. It must merge and the image must
be rebuilt before Task 3 or 4 can run.

**Files:**
- Modify: `key-maint/app/main.py:31-36` (`build_servers`), and the env block at `:53-55`
- Test: `key-maint/tests/test_main.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `KEYMAINT_NETWORK_HOST` env, default `127.0.0.1`, consumed by `build_servers`. Task 2 and Task 4 set it to `0.0.0.0`.
- `build_servers(cred_file: str, auth_file: str, admin_socket: str) -> list[uvicorn.Server]` keeps its signature. The host is read from the environment inside it, not passed as an argument — every other setting in this module follows that pattern.

- [ ] **Step 1: Write the failing tests**

Append to `key-maint/tests/test_main.py`, inside `class TestBuildServers`:

```python
    def test_network_host_defaults_to_loopback(self, tmp_path, monkeypatch):
        """The default must be unchanged, so merging this is a no-op until the
        bridge migration sets the variable."""
        monkeypatch.delenv("KEYMAINT_NETWORK_HOST", raising=False)
        _, network = build_servers(
            str(tmp_path / "c.env"), str(tmp_path / "a.env"), str(tmp_path / "s.sock")
        )
        assert (network.config.host, network.config.port) == ("127.0.0.1", 8447)

    def test_network_host_is_settable_from_the_environment(self, tmp_path, monkeypatch):
        """The bridge migration needs this bound on all interfaces; without an
        env var key-maint is the one service that cannot be moved from compose."""
        monkeypatch.setenv("KEYMAINT_NETWORK_HOST", "0.0.0.0")
        _, network = build_servers(
            str(tmp_path / "c.env"), str(tmp_path / "a.env"), str(tmp_path / "s.sock")
        )
        assert network.config.host == "0.0.0.0"

    def test_the_admin_server_never_binds_tcp_whatever_the_env_says(
        self, tmp_path, monkeypatch
    ):
        """The admin surface is a unix socket and must stay off the network
        entirely -- the migration puts the network server on a shared bridge,
        and admin must not follow it there."""
        monkeypatch.setenv("KEYMAINT_NETWORK_HOST", "0.0.0.0")
        admin, _ = build_servers(
            str(tmp_path / "c.env"), str(tmp_path / "a.env"), str(tmp_path / "s.sock")
        )
        assert admin.config.uds == str(tmp_path / "s.sock")
        assert admin.config.host in (None, "127.0.0.1")
```

- [ ] **Step 2: Run them and verify the env one fails**

Run: `cd key-maint && pytest tests/test_main.py -q`
Expected: `test_network_host_is_settable_from_the_environment` FAILS with
`AssertionError: assert '127.0.0.1' == '0.0.0.0'`. The other two pass already —
they pin behaviour that must not change.

- [ ] **Step 3: Make the host configurable**

In `key-maint/app/main.py`, add the constant beside `NETWORK_PORT` (line 15):

```python
NETWORK_PORT = 8447
# Bind address for the network server. Loopback by default: on the shared
# tailscale namespace that is the only thing keeping this off the tailnet
# before Serve's Basic auth. The bridge migration sets it to 0.0.0.0, where
# a dedicated internal network is the boundary instead.
DEFAULT_NETWORK_HOST = "127.0.0.1"
```

and in `build_servers`, replace the hardcoded host:

```python
    network_cfg = uvicorn.Config(
        create_app(role="network", cred_file=cred_file, auth_file=auth_file),
        host=os.environ.get("KEYMAINT_NETWORK_HOST", DEFAULT_NETWORK_HOST),
        port=NETWORK_PORT,
        log_level="info",
    )
```

`os` is already imported at module level (line 9). Do not touch `admin_cfg`.

- [ ] **Step 4: Run them and verify all pass**

Run: `cd key-maint && pytest tests/test_main.py -q`
Expected: PASS

- [ ] **Step 5: Run the whole key-maint suite**

Run: `cd key-maint && pytest -q`
Expected: all pass (99 before this task; 102 after)

- [ ] **Step 6: Document the variable**

Add to `key-maint/README.md`, in whichever section lists environment variables
(match the existing formatting there):

```markdown
`KEYMAINT_NETWORK_HOST` — bind address for the network server on port 8447.
Defaults to `127.0.0.1`. Set to `0.0.0.0` only where a network boundary other
than loopback protects it; the admin server is a unix socket and is unaffected.
```

- [ ] **Step 7: Commit**

```bash
git add key-maint/app/main.py key-maint/tests/test_main.py key-maint/README.md
git commit -m "feat(key-maint): make the network bind address configurable"
```

- [ ] **Step 8: Open a PR and merge it, then rebuild the image**

This is a prerequisite, not a step of the migration — Tasks 3 and 4 cannot run
against an image that lacks the variable. Rebuild `openbb-key-maint:latest` on
the NAS from the merged commit before proceeding:

```bash
ssh nas
export PATH="<nas-path>/container-station/bin:$PATH"
export HOME=<nas-checkout>/.build DOCKER_CONFIG=$HOME/.docker
mkdir -p "$DOCKER_CONFIG"
cd <nas-checkout> && DOCKER_BUILDKIT=0 docker build -t openbb-key-maint:latest ./key-maint
```

Verify the variable is present in the built image before continuing:

```bash
docker run --rm --entrypoint sh openbb-key-maint:latest -c \
  'grep -c KEYMAINT_NETWORK_HOST /app/app/main.py'
```
Expected: `1` or more. If `0`, the build did not pick up the merged commit.

---

### Task 2: Migrate the repo's compose

_Implements spec **D1** (the bridge as the new boundary), **D2** (Serve to container names), and all four layers._

The reviewable artifact: the same four layers, applied to the repo's template
compose. Nothing is deployed here. A reviewer can reject this task on its merits
without any production risk.

**Files:**
- Modify: `docker-compose.yml`
- Modify: `ts-config/serve.json.example` (only if the repo carries one — check first; the live file lives on the NAS)

**Interfaces:**
- Consumes: `KEYMAINT_NETWORK_HOST` from Task 1.
- Produces: a compose file Task 3 rehearses and Task 4 mirrors onto the NAS.

- [ ] **Step 1: Confirm what the repo actually carries**

Run:
```bash
ls ts-config/ 2>/dev/null; grep -n "network_mode: service:tailscale" docker-compose.yml
```
Record which services appear. The expected seven are `kdb`, `openbb-api`,
`live-grid`, `openbb-mcp`, `stores-mcp`, `stores-explorer`, `key-maint`. **If the
repo's list differs from those seven, stop and report it** — the spec's layer
analysis was done against the NAS file, and a divergence changes the work.

- [ ] **Step 2: Declare the network**

Add at the top level of `docker-compose.yml`, beside the existing `volumes:` key:

```yaml
networks:
  # Replaces the shared tailscale network namespace. A sidecar restart used to
  # strand every service bound to its sandbox (2026-08-27: whole stack 502 with
  # nothing crashed). A bridge outlives the sidecar, so a restart is survivable.
  #
  # This network IS the security boundary now: live-grid, stores-explorer, both
  # MCP services and kdb have no authentication, and they bind 0.0.0.0 here.
  # Attach nothing to this network that should not reach them.
  openbb-internal:
    driver: bridge
```

- [ ] **Step 3: Move the sidecar onto it, keeping everything else**

In the `tailscale` service, add `networks` without removing `cap_add`,
`devices`, or any existing key:

```yaml
    networks:
      - openbb-internal
```

- [ ] **Step 4: Move the seven services and fix their binds**

For each of the seven, delete `network_mode: service:tailscale` and add:

```yaml
    networks:
      - openbb-internal
```

Then apply the bind change each one needs:

- `kdb` — env `KX_PORT=127.0.0.1:5000` becomes `KX_PORT=5000`
- `openbb-api` — in `command:`, `"--host", "127.0.0.1"` becomes `"--host", "0.0.0.0"`
- `openbb-mcp` — in `command:`, `"--host", "127.0.0.1"` becomes `"--host", "0.0.0.0"`
- `stores-mcp` — env `STORES_HOST=127.0.0.1` becomes `STORES_HOST=0.0.0.0`
- `live-grid` — add a `command:` override (do NOT edit its Dockerfile):
  ```yaml
      command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "6903"]
  ```
- `stores-explorer` — add a `command:` override (do NOT edit its Dockerfile):
  ```yaml
      command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "6904"]
  ```
- `key-maint` — add env `KEYMAINT_NETWORK_HOST=0.0.0.0` (Task 1 supplies it)

- [ ] **Step 5: Re-point the inter-service references**

These are how the services reach *each other*, not how Serve reaches them:

- `openbb-api` env: `KX_PORT=127.0.0.1:5000` becomes `KX_PORT=kdb:5000`
- `stores-mcp` env: `KX_HOST=127.0.0.1` becomes `KX_HOST=kdb`
- `stores-explorer` env: `KX_HOST=127.0.0.1` becomes `KX_HOST=kdb`
- `deps.env`: `KDB_HOST=127.0.0.1` becomes `KDB_HOST=kdb`

`deps.env` is shared, so this single edit lands on every consumer at once. If
the repo's `deps.env` is an `.example`, edit that and note the live file is
Task 4's.

- [ ] **Step 6: Verify the compose parses and the layers are complete**

Run:
```bash
docker compose config >/dev/null && echo "valid"
grep -c "network_mode: service:tailscale" docker-compose.yml
grep -c "127.0.0.1" docker-compose.yml
```
Expected: `valid`; `0` remaining `network_mode` lines; and every surviving
`127.0.0.1` is inside a comment — check each one by eye and say so in the report.

- [ ] **Step 7: Commit**

```bash
git add docker-compose.yml deps.env* ts-config/ 2>/dev/null
git commit -m "feat: move the stack onto a dedicated bridge network"
```

---

### Task 3: Rehearse on a throwaway stack

_Implements spec **D4**, and is the only thing that settles **D3** — the design's stated unknown._

**BLOCKED until a human supplies a tailscale auth key and hostname.** Do not
attempt to reuse the production node's key without asking — it would add a
second device to the tailnet under a name someone else chose.

The rehearsal exists to prove one thing the design cannot assert: that
tailscaled works while dual-homed. Everything else in the migration is
mechanical.

**Files:**
- Create on the NAS: `<nas-checkout>-rehearsal/` (compose + ts-config, derived from Task 2's file)

**Interfaces:**
- Consumes: Task 2's `docker-compose.yml`; Task 1's rebuilt key-maint image.
- Produces: a pass/fail answer on the four rehearsal criteria. No artifact the later tasks depend on.

- [ ] **Step 1: Ask for the auth key and hostname, and stop until you have them**

Report to the controller: the rehearsal needs a Tailscale auth key and a
hostname (suggest `openbb-rehearsal`). Do not proceed without both.

- [ ] **Step 2: Build the rehearsal stack**

On the NAS, create `<nas-checkout>-rehearsal/` containing a copy of
Task 2's `docker-compose.yml` with, and ONLY with, these differences:

- every `container_name:` prefixed `rehearsal-` (so no collision with production)
- the tailscale service's `TS_HOSTNAME` set to the supplied hostname
- its own `ts-state/` and `ts.env` holding the supplied auth key
- `ts-config/serve.json` copied from production, with the six `Proxy` targets
  already changed to container names (`http://openbb-api:6900`,
  `http://openbb-mcp:6901`, `http://stores-mcp:6902`, `http://live-grid:6903`,
  `http://stores-explorer:6904`, `http://key-maint:8447`)
- the network renamed `rehearsal-internal`

Use the SAME images as production — they are already on the box. Do not rebuild.

- [ ] **Step 3: Bring it up**

```bash
cd <nas-checkout>-rehearsal && docker compose up -d
sleep 30 && docker compose ps
```
Expected: all containers Up.

- [ ] **Step 4: Prove criterion 1 — tailscaled works dual-homed**

```bash
docker exec rehearsal-ts tailscale status | head -3
docker exec rehearsal-ts ip -o addr | grep -cE "tailscale0|eth"
```
Expected: `tailscale status` shows the node as its hostname with an address, and
at least two interfaces (`tailscale0` plus a bridge `eth`). **If tailscaled fails
to come up or reports no address, STOP — this is the design's stated unknown
(D3) failing, and the fallback is the network-anchor alternative in the spec.**

- [ ] **Step 5: Prove criterion 2 — Serve delivers to a container name**

```bash
docker exec rehearsal-ts tailscale serve status
curl -s -o /dev/null -w '%{http_code}\n' https://<host>.<your-tailnet>.ts.net:6903/widgets.json
```
Expected: the serve status lists `proxy http://live-grid:6903`, and the curl
returns `200`. A `502` means Serve resolved the name but could not connect —
check the service is binding `0.0.0.0` and not `127.0.0.1`.

- [ ] **Step 6: Prove criterion 3 — services reach each other by name**

```bash
curl -s -u <user>:<pass> 'https://<host>.<your-tailnet>.ts.net/api/v1/equity/price/quote?symbol=AAPL&provider=kdb' \
  | head -c 200
```
Expected: a JSON body with `results`. This exercises openbb-api → kdb over the
bridge via PyKX's raw IPC socket, which is the connection least like HTTP and
most likely to behave differently.

- [ ] **Step 7: Prove criterion 4 — the acceptance test**

```bash
docker restart rehearsal-ts
sleep 40
curl -s -o /dev/null -w 'live-grid  %{http_code}\n' https://<host>.<your-tailnet>.ts.net:6903/widgets.json
curl -s -o /dev/null -w 'api        %{http_code}\n' https://<host>.<your-tailnet>.ts.net/widgets.json
```
Expected: `6903 -> 200` and `api -> 401` (unauthenticated). **This is the whole
point of the migration.** Under the old model this sequence produced 502 on
every surface. If anything 502s here, the migration does not solve the problem
and must not be applied to production.

- [ ] **Step 8: Tear the rehearsal down and report**

```bash
cd <nas-checkout>-rehearsal && docker compose down
docker exec rehearsal-ts tailscale logout 2>/dev/null || true
```

Then remove the rehearsal node from the tailnet admin console — tell the
controller it needs doing; do not assume it happened.

Report each of the four criteria as pass or fail with the command output.

---

### Task 4: Cut over the NAS

_Applies **D1**, **D2** and all four layers to production, in the single window the spec's ordering forces._

Only after Task 3 passes all four criteria.

**Files:**
- Modify on the NAS: `<nas-checkout>/docker-compose.yml`, `deps.env`, `ts-config/serve.json`

**Interfaces:**
- Consumes: Task 2's compose as the model; Task 3's pass.
- Produces: nothing later depends on.

- [ ] **Step 1: Back up all three files**

```bash
cd <nas-checkout>
cp docker-compose.yml docker-compose.yml.pre-bridge
cp deps.env deps.env.pre-bridge
cp ts-config/serve.json ts-config/serve.json.pre-bridge
ls -la *.pre-bridge ts-config/*.pre-bridge
```
Expected: three backups. **Do not proceed if any is missing** — these are the
entire rollback.

- [ ] **Step 2: Apply the four layers to the NAS compose**

Mirror Task 2's changes onto the NAS file. It is hand-maintained and diverges
from the repo's, so apply the changes by meaning, not by patch. Then:

```bash
docker compose config >/dev/null && echo valid
grep -c "network_mode: service:tailscale" docker-compose.yml   # expect 0
```

- [ ] **Step 3: Re-point serve.json**

Change the six `Proxy` values from `http://127.0.0.1:PORT` to the container
names, leaving the `TCP` block and every port untouched:

`6900→openbb-api`, `6901→openbb-mcp`, `6902→stores-mcp`, `6903→live-grid`,
`6904→stores-explorer`, `8447→key-maint`.

Validate it is still JSON before continuing — the NAS has no `python3`, so
check from this machine:
```bash
ssh nas 'cat <nas-checkout>/ts-config/serve.json' | python3 -m json.tool >/dev/null && echo valid
```

- [ ] **Step 4: Re-point deps.env**

`KDB_HOST=127.0.0.1` becomes `KDB_HOST=kdb`.

- [ ] **Step 5: Bring the stack up**

```bash
cd <nas-checkout> && docker compose up -d
sleep 40 && docker compose ps
```
Expected: every container Up. The sidecar restarting during this is now
harmless — that is the point.

- [ ] **Step 6: Verify every surface**

```bash
H=https://openbb.<your-tailnet>.ts.net
curl -s -o /dev/null -w ':443 nocreds %{http_code}\n'  $H/widgets.json
curl -s -o /dev/null -w ':443 creds   %{http_code}\n'  -H "Authorization: $HV" $H/widgets.json
curl -s -o /dev/null -w ':6903       %{http_code}\n'  $H:6903/widgets.json
curl -s -o /dev/null -w ':6904       %{http_code}\n'  $H:6904/widgets.json
curl -s -o /dev/null -w ':10000      %{http_code}\n'  -H "Authorization: $HK" $H:10000/keys
curl -s -o /dev/null -w 'kdb quote   %{http_code}\n'  -H "Authorization: $HV" \
  "$H/api/v1/equity/price/quote?symbol=AAPL&provider=kdb"
```
Expected: `401, 200, 200, 200, 200, 200`. `$HV` and `$HK` are the API and
key-maint credentials from bdobb-v2's `backends.json`.

- [ ] **Step 7: The acceptance test — cause the failure on purpose**

```bash
docker restart openbb-ts
sleep 40
```
Then re-run every command from Step 6. Expected: identical results.

**This step is not optional.** On production the migration is only proven by
causing the failure it exists to prevent. If anything 502s, roll back at once
using Step 8.

- [ ] **Step 8: Rollback procedure (only if Step 6 or 7 fails)**

```bash
cd <nas-checkout>
cp docker-compose.yml.pre-bridge docker-compose.yml
cp deps.env.pre-bridge deps.env
cp ts-config/serve.json.pre-bridge ts-config/serve.json
docker compose up -d --force-recreate
sleep 40
```
Then recreate the netns siblings, because the old model needs them re-attached
after any sidecar restart:
```bash
docker compose up -d --force-recreate live-grid openbb-mcp stores-mcp stores-explorer key-maint kdb openbb-api
```
Re-run Step 6's checks. No image rebuild is needed — no task changed an image
except Task 1, which is backward-compatible.

- [ ] **Step 9: Record the outcome**

Append to `live-grid/README.md` or the repo's ops notes (match where the netns
trap is currently documented, if anywhere) that the stack no longer shares a
network namespace, and that recreating one service no longer requires recreating
the others.

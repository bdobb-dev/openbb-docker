<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Bring-Your-Own kdb Container Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop shipping KX's `q` binary inside the published image — the licence forbids redistributing it — and replace the boolean `KDB_EMBEDDED` switch with a resolution chain that finds q locally if the operator supplied one, and otherwise connects to a kdb container they run themselves on the same machine.

**Architecture:** `KdbSession` stops asking "am I embedded?" and instead walks an ordered list of ways to reach q: spawn one from a directory the operator mounted, else connect to loopback (picking up a q another service in the shared network namespace already started), else connect to an external `KDB_HOST`. Every consumer reads the same variables, so `live-grid` and `openbb-api` land on the same q with no separate wiring.

**Tech Stack:** Python 3.12, PyKX 3.2 (IPC/unlicensed), Docker Compose, pytest.

## Global Constraints

- **The licence forbids the repo owner redistributing KX's software.** No image built or published from this repo may contain `q`, its libraries, or a `.lic`. This is the reason the whole change exists — if a step would put KX code in the image, it is wrong.
- **q must bind `127.0.0.1`, never `0.0.0.0`.** Every service shares the tailscale container's network namespace, so a `0.0.0.0` bind publishes an unauthenticated q — which executes arbitrary q code — to every tailnet peer. `scripts/verify-isolation.sh` checks port 5000 and that check is now load-bearing, because a reader-supplied image controls its own bind.
- **PyKX aborts the process when touched from more than one thread** — not merely under concurrency, but on strictly sequential calls from different threads. Every PyKX call stays on `KdbSession`'s single owner thread.
- **`live-grid` must never spawn q.** It shares a namespace with `openbb-api`; two spawners race for port 5000.
- **`live-grid` is a shipped feature.** With no q reachable at all, its grid must stream exactly as it does today.
- **The cache never fails a request.** No reachable q ⇒ `provider="kdb"` passes through to the upstream and reports `cache: "bypass"`.
- Style: `line-length = 100`; tests need no kdb licence, API key or network. Use `python3`.
- Suites must stay green: `kdb-store` 83, `openbb-kdb` 90, `live-grid` 98.

### Verified facts (do not re-derive)

- From inside a shared network namespace, **`host.docker.internal` resolves and is reachable** (via Docker's embedded DNS — it is *not* in the joiner's `/etc/hosts`). Measured on Docker Desktop.
- The raw bridge gateway `172.17.0.1` was **not** reachable on Docker Desktop, because that gateway lives in the VM rather than on the host. On a plain Linux host it *is* the host. The two platforms therefore need different `KDB_HOST` values, which is precisely what the variable absorbs.
- PyKX ships `libq.so` for embedded mode but **no standalone `q` executable**, so a pip install alone cannot provide a q to spawn.
- q reads its console from stdin and exits on EOF, so a spawned q needs its stdin held open.
- **The correct bind inverts depending on deployment.** Measured with a real q IPC handshake (a `nc -z` against a published port gives a false positive — docker-proxy accepts before reaching the backend):

  | Their kdb runs as | Bind INSIDE the container | Exposure controlled by |
  |---|---|---|
  | Their own separate container | **`0.0.0.0`** (`KX_PORT=5000`) — a `127.0.0.1` bind is **unreachable** through `-p`, handshake fails | `-p 127.0.0.1:5000:5000` on the **host** |
  | A compose service sharing the tailscale namespace | **`127.0.0.1`** — `0.0.0.0` would be the tailnet IP | nothing needed; already loopback-only |

  Getting this backwards either breaks the connection or publishes an unauthenticated q. Both rows must appear in the reader-facing docs.

---

## The resolution chain

| Step | openbb-api | live-grid |
|---|---|---|
| 1 | Spawn q from `KDB_LOCAL_QHOME` if a runnable one is there | skipped (never spawns) |
| 2 | Connect `127.0.0.1:KDB_PORT` | Connect `127.0.0.1:KDB_PORT` — picks up whatever openbb-api spawned |
| 3 | Connect `KDB_HOST:KDB_PORT` | Connect `KDB_HOST:KDB_PORT` |
| fail | `cache: "bypass"` | grid unaffected; chart serves history only |

## File Structure

| File | Change |
|---|---|
| `kdb-store/kdb_store/config.py` | `local_qhome`; `host` becomes optional; spawning derived from whether q is present |
| `kdb-store/kdb_store/session.py` | `_connection()` walks the chain instead of branching on a boolean |
| `Dockerfile` | Delete the `kdbx` stage, the `COPY --from=kdbx`, and the licence assertions — nothing KX remains to assert |
| `.github/workflows/ci.yml` | Drop the checks that inspect `/opt/kx` (they test something no longer in the image) |
| `kdb/` **(new)** | Where the operator drops their own q; `.gitignore` makes it impossible to commit |
| `docker-compose.yml` | Mount `./kdb`; `KDB_LOCAL_QHOME`; `KDB_HOST` on both consumers |
| `credentials.env.example`, `README.md`, `live-grid/README.md`, `openbb-kdb/README.md`, `docs/kdb-cache-design.md` | The new setup story |

---

### Task 1: Configuration

**Files:**
- Modify: `kdb-store/kdb_store/config.py`
- Modify: `kdb-store/tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces on `KdbConfig`: `local_qhome: str`, `host: str | None`, and `may_spawn: bool`. Existing fields `port`, `memory_mb`, `watermark`, `upstream`, `qlic`, and the property `q_workspace_mb` are unchanged. The field previously called `embedded` is renamed `may_spawn`; the field previously defaulting `host` to `127.0.0.1` now defaults to `None`.
- Produces: `has_local_q(local_qhome: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `kdb-store/tests/test_config.py`:

```python
def test_local_qhome_defaults_to_slash_kdb(monkeypatch):
    monkeypatch.delenv("KDB_LOCAL_QHOME", raising=False)
    monkeypatch.delenv("QHOME", raising=False)
    assert resolve_config().local_qhome == "/kdb"


def test_local_qhome_from_env(monkeypatch):
    monkeypatch.setenv("KDB_LOCAL_QHOME", "/opt/mine")
    assert resolve_config().local_qhome == "/opt/mine"


def test_host_is_none_when_unset(monkeypatch):
    """No KDB_HOST means there is no external kdb to fall back to."""
    monkeypatch.delenv("KDB_HOST", raising=False)
    assert resolve_config().host is None


def test_host_from_env(monkeypatch):
    monkeypatch.setenv("KDB_HOST", "host.docker.internal")
    assert resolve_config().host == "host.docker.internal"


def test_may_spawn_is_false_when_no_local_q(monkeypatch, tmp_path):
    """Nothing to spawn: the chain should fall through to KDB_HOST."""
    monkeypatch.setenv("KDB_LOCAL_QHOME", str(tmp_path))
    monkeypatch.delenv("KDB_EMBEDDED", raising=False)
    assert resolve_config().may_spawn is False


def test_may_spawn_is_true_when_a_runnable_q_is_present(monkeypatch, tmp_path):
    qbin = tmp_path / "bin" / "q"
    qbin.parent.mkdir(parents=True)
    qbin.write_text("#!/bin/sh\n")
    qbin.chmod(0o755)
    monkeypatch.setenv("KDB_LOCAL_QHOME", str(tmp_path))
    monkeypatch.delenv("KDB_EMBEDDED", raising=False)
    assert resolve_config().may_spawn is True


def test_a_present_but_non_executable_q_does_not_count(monkeypatch, tmp_path):
    qbin = tmp_path / "bin" / "q"
    qbin.parent.mkdir(parents=True)
    qbin.write_text("not executable")
    qbin.chmod(0o644)
    monkeypatch.setenv("KDB_LOCAL_QHOME", str(tmp_path))
    monkeypatch.delenv("KDB_EMBEDDED", raising=False)
    assert resolve_config().may_spawn is False


def test_kdb_embedded_false_overrides_a_present_q(monkeypatch, tmp_path):
    """live-grid sets this: two spawners in one namespace race for the port."""
    qbin = tmp_path / "bin" / "q"
    qbin.parent.mkdir(parents=True)
    qbin.write_text("#!/bin/sh\n")
    qbin.chmod(0o755)
    monkeypatch.setenv("KDB_LOCAL_QHOME", str(tmp_path))
    monkeypatch.setenv("KDB_EMBEDDED", "false")
    assert resolve_config().may_spawn is False


def test_kdb_embedded_true_forces_spawn_even_with_no_q(monkeypatch, tmp_path):
    """An explicit true is a deliberate choice; let the spawn fail loudly."""
    monkeypatch.setenv("KDB_LOCAL_QHOME", str(tmp_path))
    monkeypatch.setenv("KDB_EMBEDDED", "true")
    assert resolve_config().may_spawn is True
```

Update every existing test and helper in the `kdb-store` and `live-grid` suites that constructs `KdbConfig(...)` directly or asserts on `.embedded` / `.host`: rename `embedded=` to `may_spawn=`, add `local_qhome=`, and change any assertion expecting `host == "127.0.0.1"` by default. Find them with:

```bash
grep -rn "embedded\|KdbConfig(" kdb-store/tests live-grid/tests openbb-kdb/tests
```

Do not delete any test — adapt it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd kdb-store && python3 -m pytest tests/test_config.py -q`
Expected: FAIL — `AttributeError: 'KdbConfig' object has no attribute 'local_qhome'`

- [ ] **Step 3: Implement**

In `kdb-store/kdb_store/config.py`:

Add the helper:

```python
def has_local_q(local_qhome: str) -> bool:
    """True when `local_qhome` holds a q we could actually execute.

    The operator mounts their own q here; this repo ships none, because the
    licence does not permit redistributing KX's binary.
    """
    import os

    candidate = os.path.join(local_qhome, "bin", "q")
    return os.path.isfile(candidate) and os.access(candidate, os.X_OK)
```

In `KdbConfig`, rename `embedded: bool` to `may_spawn: bool`, change `host: str` to `host: str | None`, and add `local_qhome: str`. Keep every other field and `q_workspace_mb` unchanged.

In `resolve_config`, replace the host-based derivation:

```python
    # Where the operator mounted their own q. QHOME is accepted as a fallback
    # for anyone carrying the older variable, but it is read ONCE per process
    # (see _qhome_once) because `import pykx` rewrites QHOME in place.
    local_qhome = (
        _pick("local_qhome", "KDB_LOCAL_QHOME", credentials)
        or _qhome_once()
        or _DEFAULTS["local_qhome"]
    )

    # No KDB_HOST means there is simply no external kdb to fall back to.
    host = _pick("host", "KDB_HOST", credentials) or None

    raw_embedded = _pick("embedded", "KDB_EMBEDDED", credentials)
    if raw_embedded is None:
        # Spawn only if the operator actually supplied a q. Otherwise the
        # chain falls through to KDB_HOST.
        may_spawn = has_local_q(local_qhome)
    else:
        may_spawn = str(raw_embedded).strip().lower() in ("1", "true", "yes", "on")
```

Set `_DEFAULTS["local_qhome"] = "/kdb"` and remove the `"host"` default. Rename the existing once-per-process QHOME reader to `_qhome_once` if it is not already named that, and have it return `None` rather than a default so the precedence above reads cleanly.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd kdb-store && python3 -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add kdb-store/
git commit -m "feat(kdb-store): config for a bring-your-own q, local or external"
```

---

### Task 2: The resolution chain

**Files:**
- Modify: `kdb-store/kdb_store/session.py`
- Modify: `kdb-store/tests/test_session.py`

**Interfaces:**
- Consumes: `KdbConfig` with `may_spawn`, `local_qhome`, `host` (Task 1).
- Produces: `KdbSession.connection()` unchanged in signature; internally it now walks the chain. Adds `KdbSession.endpoint` — a string describing what it actually connected to (`"spawned"`, `"loopback"`, or `"host:port"`), or `None` when nothing is connected. `/health` surfaces it.

- [ ] **Step 1: Write the failing tests**

Append to `kdb-store/tests/test_session.py`:

```python
def test_chain_spawns_first_when_a_local_q_is_available(monkeypatch):
    calls = []
    monkeypatch.setattr(KdbSession, "_spawn", lambda self: calls.append("spawn"))
    monkeypatch.setattr(KdbSession, "_connect_with_retry", lambda self: FakeConn())
    monkeypatch.setattr(KdbSession, "_connect", lambda self, host=None: FakeConn())
    s = KdbSession(cfg(may_spawn=True, host="elsewhere"))
    s.connection()
    assert calls == ["spawn"]
    assert s.endpoint == "spawned"


def test_chain_tries_loopback_before_the_external_host(monkeypatch):
    """live-grid picks up the q that openbb-api spawned in the shared namespace."""
    tried = []

    def fake_connect(self, host=None):
        tried.append(host)
        return FakeConn()

    monkeypatch.setattr(KdbSession, "_connect", fake_connect)
    s = KdbSession(cfg(may_spawn=False, host="host.docker.internal"))
    s.connection()
    assert tried == ["127.0.0.1"]
    assert s.endpoint == "loopback"


def test_chain_falls_through_to_the_external_host(monkeypatch):
    tried = []

    def fake_connect(self, host=None):
        tried.append(host)
        if host == "127.0.0.1":
            raise OSError("connection refused")
        return FakeConn()

    monkeypatch.setattr(KdbSession, "_connect", fake_connect)
    s = KdbSession(cfg(may_spawn=False, host="host.docker.internal"))
    s.connection()
    assert tried == ["127.0.0.1", "host.docker.internal"]
    assert s.endpoint == "host.docker.internal:5000"


def test_a_failed_spawn_still_falls_through_to_the_external_host(monkeypatch):
    """A broken local q must not cost the operator their external one."""
    tried = []

    def boom(self):
        raise OSError("q exited immediately")

    def fake_connect(self, host=None):
        tried.append(host)
        if host == "127.0.0.1":
            raise OSError("connection refused")
        return FakeConn()

    monkeypatch.setattr(KdbSession, "_spawn", boom)
    monkeypatch.setattr(KdbSession, "_connect", fake_connect)
    s = KdbSession(cfg(may_spawn=True, host="host.docker.internal"))
    s.connection()
    assert s.endpoint == "host.docker.internal:5000"


def test_no_local_q_and_no_host_raises(monkeypatch):
    def fake_connect(self, host=None):
        raise OSError("connection refused")

    monkeypatch.setattr(KdbSession, "_connect", fake_connect)
    s = KdbSession(cfg(may_spawn=False, host=None))
    with pytest.raises(KdbUnavailable):
        s.connection()


def test_a_failed_spawn_leaves_no_orphan_process(monkeypatch):
    stopped = []
    monkeypatch.setattr(KdbSession, "_spawn", lambda self: None)
    monkeypatch.setattr(KdbSession, "_stop_proc", lambda self: stopped.append(1))
    monkeypatch.setattr(
        KdbSession, "_connect_with_retry",
        lambda self: (_ for _ in ()).throw(OSError("no listener")),
    )
    monkeypatch.setattr(
        KdbSession, "_connect",
        lambda self, host=None: (_ for _ in ()).throw(OSError("refused")),
    )
    s = KdbSession(cfg(may_spawn=True, host=None))
    with pytest.raises(KdbUnavailable):
        s.connection()
    assert stopped, "a q we started must never be left running unsupervised"
```

Update the `cfg()` helper in that file to accept `may_spawn`, `local_qhome` and a `host` that may be `None`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd kdb-store && python3 -m pytest tests/test_session.py -q`
Expected: FAIL — `TypeError` on the renamed config field, or `AttributeError: 'KdbSession' object has no attribute 'endpoint'`

- [ ] **Step 3: Implement the chain**

In `kdb-store/kdb_store/session.py`:

Give `_connect` an explicit host parameter so the chain can aim it:

```python
    def _connect(self, host: str | None = None):
        """Open a PyKX IPC connection (unlicensed client mode -- no licence needed)."""
        import pykx as kx

        return kx.SyncQConnection(host or "127.0.0.1", self.config.port)
```

Add `self.endpoint = None` in `__init__`, and replace the body of `_connection()`'s try block with the chain:

```python
        self._conn = None
        self.endpoint = None
        spawned = False
        errors: list[str] = []

        # 1. A q the operator supplied and we can run ourselves.
        if self.config.may_spawn:
            try:
                self._spawn()
                spawned = True
                self._conn = self._connect_with_retry()
                self.endpoint = "spawned"
            except Exception as exc:  # noqa: BLE001 - fall through to the next link
                errors.append(f"spawn: {exc}")
                if spawned:
                    # We own this process; never leave a q we started running
                    # unsupervised just because we could not connect to it.
                    self._stop_proc()
                    spawned = False

        # 2. Loopback: in this stack every service shares one network
        #    namespace, so a q another service already spawned is right here.
        if self._conn is None:
            try:
                self._conn = self._connect("127.0.0.1")
                self.endpoint = "loopback"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"loopback: {exc}")

        # 3. A kdb container the operator runs themselves, same machine.
        if self._conn is None and self.config.host:
            try:
                self._conn = self._connect(self.config.host)
                self.endpoint = f"{self.config.host}:{self.config.port}"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{self.config.host}: {exc}")

        if self._conn is None:
            self._retry_after = time.monotonic() + _RETRY_AFTER_S
            detail = "; ".join(errors) or "no kdb+ endpoint configured"
            logger.warning(
                "kdb+ unavailable, falling back to upstream (retrying in %.0fs): %s",
                _RETRY_AFTER_S, detail,
            )
            raise KdbUnavailable(detail)

        self._retry_after = 0.0
        return self._conn
```

Keep the existing `_retry_after` latch semantics exactly: a success clears it, a total failure sets it, and `_connection` still raises early while it is live. Update `_q_argv` and `_spawn` to use `self.config.local_qhome` wherever they previously used `self.config.qhome`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd kdb-store && python3 -m pytest -q`
Expected: PASS

- [ ] **Step 5: Surface the endpoint on live-grid's health**

In `live-grid/app/main.py`'s `/health`, add the resolved endpoint under the existing `"ticks"` block, guarded so a session that has never connected reports `None` rather than raising:

```python
            "endpoint": getattr(recorder.store.session, "endpoint", None),
```

- [ ] **Step 6: Run every suite**

```bash
(cd kdb-store && python3 -m pytest -q)
(cd openbb-kdb && python3 -m pytest -q)
(cd live-grid && python3 -m pytest -q)
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add kdb-store/ live-grid/
git commit -m "feat(kdb-store): resolve q by chain -- spawn, loopback, then external host"
```

---

### Task 3: Take KX's software out of the image

**Files:**
- Modify: `Dockerfile`
- Modify: `.github/workflows/ci.yml`
- Create: `kdb/.gitignore`, `kdb/README.md`
- Modify: `.gitignore`

- [ ] **Step 1: Strip the Dockerfile**

Delete the `FROM ghcr.io/artcashin/kdb-x:1.0 AS kdbx` stage, its `RUN find ... -name '*.lic' -delete` and assertion, the `COPY --from=kdbx /root/.kx /opt/kx`, and the final-stage licence assertion. Leave PyKX installed — it comes from PyPI and provides the IPC client.

Replace the block's comment with a short statement of why nothing kdb ships here: the licence does not permit this repo's owner to redistribute KX's binary, so the operator mounts their own q at `/kdb` (or points `KDB_HOST` at their own container), and the image contains no KX code at all.

- [ ] **Step 2: Verify the image is genuinely clean**

```bash
cd /Users/artcashin/Developer/openbb-docker
docker build -t openbb-local:10.0.0 .
docker run --rm --entrypoint sh openbb-local:10.0.0 -c 'ls /opt/kx 2>&1; find / -name "*.lic" 2>/dev/null | head'
```
Expected: `/opt/kx` does not exist and no `.lic` is found. PyKX's own `lib/` will still be present — that is the pip package, not something this repo copied.

- [ ] **Step 3: Remove the CI checks that inspect the removed runtime**

`.github/workflows/ci.yml` has a step asserting the bundled q matches the image architecture and executes, and a step scanning image layers for a licence. Both test something that is no longer in the image. Delete them.

**Say so explicitly in your report:** those steps were added deliberately by the repo owner after a real incident — a single-arch base image silently bundled AArch64 binaries into an amd64 build — and removing them is correct only because the thing they guarded is gone. Do not remove anything else from that file.

- [ ] **Step 4: Create the drop-in directory**

```bash
mkdir -p kdb
```

Create `kdb/.gitignore`:

```gitignore
# The operator's own q lives here. It must never be committed: the licence
# does not permit redistributing KX's software, and a licence file is a
# credential.
*
!.gitignore
!README.md
```

Create `kdb/README.md`:

````markdown
# Your q goes here

This repository ships **no** kdb+ software. KX's licence does not permit
redistributing their binary, so you supply it — either here, or as your own
container.

## Option A — drop q in this directory

Download kdb-x or kdb+ Personal Edition from KX and unpack it so the layout is:

```
kdb/
  bin/q          <- the executable
  l64/  or m64/  <- the architecture directory that came with it
```

Nothing else is needed. `openbb-api` finds it at `/kdb`, starts it bound to
`127.0.0.1:5000`, and every service in the stack shares it.

## Option B — run your own kdb container

Leave this directory empty and set `KDB_HOST` in `credentials.env` to reach it.
See the repository README.

## Your licence

Mount `kc.lic` into `kdb-license/`, not here. Nothing in either directory is
committed — both are git-ignored.
````

- [ ] **Step 5: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git status --short   # confirm no q binary or .lic is staged
git add Dockerfile .github/workflows/ci.yml kdb/ .gitignore
git commit -m "fix: ship no KX software -- the operator supplies q"
```

---

### Task 4: Compose and the setup story

**Files:**
- Modify: `docker-compose.yml`, `credentials.env.example`
- Modify: `README.md`, `openbb-kdb/README.md`, `live-grid/README.md`, `docs/kdb-cache-design.md`, `docs/tick-chart-design.md`

- [ ] **Step 1: Wire compose**

On `openbb-api`: mount `./kdb:/kdb:ro`, set `KDB_LOCAL_QHOME=/kdb`, keep the `./kdb-license:/opt/kx-license:ro` mount and `QLIC`, and remove `QHOME=/opt/kx` (that path no longer exists). Keep `KDB_MEMORY_MB` and `mem_limit`.

On `live-grid`: keep `KDB_EMBEDDED=false` and add the same `./kdb-license` mount and `QLIC`. It needs no `./kdb` mount — it never spawns.

Both services must see `KDB_HOST` and `KDB_PORT` if the operator sets them; they already load `credentials.env`, so no per-service entry is needed.

Comment the `KDB_EMBEDDED=false` on live-grid with *why*: two spawners in one shared network namespace race for port 5000.

- [ ] **Step 2: Document the variables**

Add to `credentials.env.example`, with comments on their own lines (compose's dotenv parser treats an inline comment after an empty value as the value):

```bash
# --- kdb+ (Ep. 10) ---------------------------------------------------------
# Where the cache finds q. Two ways, tried in this order:
#
#   1. Drop your own q into ./kdb (see kdb/README.md) and it is used directly.
#   2. Run your own kdb container and point KDB_HOST at it.
#
# KDB_HOST is only needed for option 2. The value differs by platform:
#   Docker Desktop (macOS/Windows):  host.docker.internal
#   Linux host:                      172.17.0.1   (your docker0 gateway)
#
# Run your container so q listens on 0.0.0.0 INSIDE it and publish it to host
# loopback only:
#
#   docker run -d --name my-kdb -p 127.0.0.1:5000:5000 <your image>
#
# Both halves matter. A 127.0.0.1 bind inside the container is unreachable
# through -p (verified: the q handshake fails), and publishing to 0.0.0.0 on
# the host puts an unauthenticated q -- which executes arbitrary q -- on your
# LAN.
KDB_HOST=
KDB_PORT=5000
```

- [ ] **Step 3: Rewrite the setup story in `README.md`**

The v10.0.0 section currently tells readers the image contains q. Replace that with the two options above, and state plainly that this repo ships no KX software because the licence does not permit redistributing it. Delete the paragraph explaining how to repoint the `kdbx` build stage — that stage is gone.

Keep the existing warning that q must bind `127.0.0.1`, and make it stronger for option 2: the reader's own image controls its bind, and `verify-isolation.sh`'s port-5000 check is what catches a mistake.

- [ ] **Step 4: Correct the other docs**

- `openbb-kdb/README.md`: the config table still lists `QHOME` as "the kdb-x install q is launched from" with a `/opt/kx` default. Replace with `KDB_LOCAL_QHOME` (`/kdb`) and `KDB_HOST`, and describe the chain.
- `live-grid/README.md`: note that the chart finds q through the same variables, and that `/health` now reports which endpoint it resolved to.
- `docs/kdb-cache-design.md`: its "How q gets into the image, and licensing" section describes copying the runtime in and deleting the licence. That is no longer what happens. Rewrite it to describe the chain and the licence constraint that forced it, and keep the layer-versus-flattened-filesystem explanation as the *reason* the old approach was abandoned rather than as current behaviour.
- `docs/tick-chart-design.md`: check its config table and any statement about where q comes from.

- [ ] **Step 5: Validate and commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
docker compose config > /dev/null && echo "compose OK"
bash scripts/scrub-check.sh
git add -A
git commit -m "docs: bring-your-own q -- drop it in kdb/ or point KDB_HOST at your container"
```

---

### Task 5: Verify both paths for real

Neither path is proven by a mocked test. Both get exercised against a real q.

**Files:**
- Modify: `kdb-store/scripts/tick_check.py` (report the resolved endpoint)

**Do NOT run `docker compose up` or `scripts/verify-isolation.sh`** — this compose joins the operator's real tailnet as a node named `openbb` and could collide with their production node. Those stay operator steps; say so in your report.

- [ ] **Step 1: Report the endpoint in the real-q check**

Add a line to `kdb-store/scripts/tick_check.py` printing `session.endpoint` after connecting, so the check states which link of the chain answered. This is the difference between "the cache works" and "the cache works *the way you configured it*".

- [ ] **Step 2: Prove option A — a mounted q**

The scratchpad holds an extracted kdb-x runtime and a licence for local testing; neither may enter the repo.

```bash
SP=/private/tmp/claude-501/-Users-artcashin-Developer-openbb-docker/36d337ee-f0b7-45ab-bd8c-206f7577b0bf/scratchpad
docker run --rm \
  -v "$SP/kx:/kdb:ro" -v "$SP/kx:/opt/kx-license:ro" \
  -e KDB_LOCAL_QHOME=/kdb -e QLIC=/opt/kx-license -e KDB_MEMORY_MB=512 \
  -v /Users/artcashin/Developer/openbb-docker/kdb-store:/src:ro \
  --entrypoint sh openbb-local:10.0.0 \
  -c 'cp -r /src /tmp/ks && pip install -q /tmp/ks && python /tmp/ks/scripts/tick_check.py'
```

Expected: `FAILURES: none`, and the endpoint line reports `spawned`. Copy the source to a writable path before installing — installing from a read-only mount silently falls through to a stale copy already in the image.

- [ ] **Step 3: Prove option B — an external container**

Start a q in its own container, publish it on host loopback, and connect to it *without* any local q mounted:

```bash
SP=/private/tmp/claude-501/-Users-artcashin-Developer-openbb-docker/36d337ee-f0b7-45ab-bd8c-206f7577b0bf/scratchpad
docker rm -f byo-kdb >/dev/null 2>&1
# 0.0.0.0 INSIDE the container, published to host loopback only. A 127.0.0.1
# bind inside is unreachable through -p -- verified, the handshake fails.
docker run -d --name byo-kdb -e KX_PORT=5000 -p 127.0.0.1:5000:5000 kdb-x:arm64
sleep 5
docker run --rm \
  -v "$SP/kx:/opt/kx-license:ro" -e QLIC=/opt/kx-license \
  -e KDB_LOCAL_QHOME=/nonexistent -e KDB_HOST=host.docker.internal -e KDB_PORT=5000 \
  -v /Users/artcashin/Developer/openbb-docker/kdb-store:/src:ro \
  --entrypoint sh openbb-local:10.0.0 \
  -c 'cp -r /src /tmp/ks && pip install -q /tmp/ks && python /tmp/ks/scripts/tick_check.py'
docker rm -f byo-kdb
```

Expected: `FAILURES: none`, endpoint `host.docker.internal:5000`.

- [ ] **Step 4: Prove the fallthrough**

With no local q and no reachable host, confirm the provider degrades rather than failing:

```bash
docker run --rm -e KDB_LOCAL_QHOME=/nonexistent -e KDB_HOST=203.0.113.1 \
  -e KDB_UPSTREAM=yfinance --entrypoint python openbb-local:10.0.0 -c "
import asyncio
from openbb_kdb.models.historical import KdbEquityHistoricalFetcher as F
q = F.transform_query({'symbol':'AAPL','start_date':'2024-01-01','end_date':'2024-02-01'})
d = asyncio.run(F.aextract_data(q, None))
print('cache:', d['meta']['cache'], 'rows:', len(d['rows']))
"
```

Expected: `cache: bypass` with rows returned — the cache never fails a request.

- [ ] **Step 5: Run every suite and commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
(cd kdb-store && python3 -m pytest -q)
(cd openbb-kdb && python3 -m pytest -q)
(cd live-grid && python3 -m pytest -q)
bash scripts/scrub-check.sh
git add -A
git commit -m "test(kdb-store): report the resolved endpoint in the real-q check"
```

---

## Verification

1. All three suites pass with no kdb licence, key or network.
2. `openbb-local:10.0.0` contains no `/opt/kx` and no `.lic`.
3. Real-q check passes with a mounted q, reporting endpoint `spawned`.
4. Real-q check passes against an external container, reporting endpoint `host:port`.
5. With neither, `provider="kdb"` reports `cache: "bypass"` and still returns rows.
6. `live-grid`'s `/health` reports the resolved endpoint.
7. `docker compose config` parses; `scripts/scrub-check.sh` passes.
8. Left to the operator: `docker compose up` and `scripts/verify-isolation.sh`, whose port-5000 check is now the load-bearing guard against a reader-supplied image binding `0.0.0.0`.

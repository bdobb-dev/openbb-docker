# ArcticDB + MinIO (Ep. 11) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add MinIO as its own tailnet node with real TLS, put `provider="arcticdb"` in the Platform image, and ship `tick-lab` — a local CLI that reads FirstRate ticks from S3, rolls them into 1-minute bars, and diffs them against an independent source.

**Architecture:** MinIO joins the tailnet as a separate node (no reverse proxy in the S3 SigV4 signing path) and obtains a Let's Encrypt certificate via `tailscale cert`; because MinIO ignores on-disk cert changes but reloads on `SIGHUP`, renewal runs *inside* the MinIO container and signals its own child process. The vendored `openbb-arcticdb` extension gains an `ARCTICDB_S3_*` env convention so `minio.env` is the single credential source for both the container and the laptop. `tick-lab` is a standalone local package that talks to ArcticDB directly — it never imports OpenBB.

**Tech Stack:** Python 3.12, ArcticDB 6.21, MinIO, Tailscale, pandas, yfinance, pytest, ruff, Docker Compose.

## Global Constraints

- **Design spec:** `docs/superpowers/specs/2026-08-05-arcticdb-minio-design.md`. Decisions are referenced as D1–D9; do not contradict them.
- **Branch:** `ep11-arcticdb-minio`. It is based on an Ep. 10 commit that is still moving; rebase onto the final Ep. 10 tip before merge.
- **Platform:** every service built from this repo's Dockerfile pins `platform: linux/amd64` (D7). ArcticDB publishes no aarch64 Linux wheels.
- **ArcticDB S3 URI shape** (verified, S3): `s3s://<host>:<bucket>?port=<port>&access=<k>&secret=<s>&use_virtual_addressing=false`. Host and bucket separated by `:`; **port is a query parameter**. `host:port:bucket` is invalid.
- **Timestamps:** ticks are stored **tz-aware UTC** (D8). Never store naive timestamps.
- **Session default:** `regular` = 09:30:00 inclusive to 16:00:00 exclusive, US Eastern (D9).
- **Secrets:** no real credentials, hostnames, or tailnet names in any committed file. `scripts/scrub-check.sh` must pass before every commit.
- **Base images:** pin explicit tags, never `latest`.
- **kdb-x base image (Ep. 10's concern, NOT Ep. 11's):** this chapter does not touch the `kdbx` stage, the `openbb-kdb`/`kdb-store` packages, or the q runtime. It is recorded here only because Ep. 11's `platform: linux/amd64` pin is what *exposed* an Ep. 10 problem: `kdb-x:latest` was a single-arch **arm64** manifest, so an amd64 build copied AArch64 `q` binaries into an x86_64 image, and because `COPY --from` never *executes* anything the build stayed green while only the runtime q spawn failed. Ep. 10 fixed it by publishing multi-arch `:1.0` and `:latest`, and carries both a versioned pin and a `build-smoke` CI step asserting the bundled q's ELF architecture. **Ep. 11 must not re-add either** — it inherits them through the Task 17 rebase, and Step 4 there checks they survived it.
- **Extension versions (D6, partial):** `openbb-arcticdb` → `11.0.0`, `openbb-eodhd` `0.1.0` → `8.0.0`. `openbb-kdb`/`kdb-store` are DEFERRED — a concurrent Ep. 10 session is rewriting that package.
- **Python style:** ruff, `line-length = 100`, `target-version = "py310"`, matching `openbb-kdb/pyproject.toml`.
- **Licensing:** the FirstRate zip is third-party licensed data and is **never committed**. Tests use synthetic fixtures in the FirstRate format.

## File Structure

**Vendored extension** (copied from `github.com/artcashin/openbb-arcticdb`):
- `openbb-arcticdb/openbb_arcticdb/utils.py` — connection resolution; **the only file this plan modifies**
- `openbb-arcticdb/{__init__,accessor,store}.py`, `models/historical.py` — vendored unchanged
- `openbb-arcticdb/tests/` — vendored unchanged, plus new `test_s3_config.py`

**MinIO service:**
- `minio/Dockerfile` — MinIO + the `tailscale` binary
- `minio/entrypoint.sh` — starts MinIO as a child, forwards signals
- `minio/cert-sync.sh` — obtains/renews the cert, HUPs the child on change
- `minio/tests/test_cert_sync.py` — drives `cert-sync.sh` with a stubbed `tailscale`
- `minio.env.example`

**tick-lab** (local CLI, one responsibility per module):
- `tick-lab/tick_lab/config.py` — `ARCTICDB_S3_*` → validated `S3Config`
- `tick-lab/tick_lab/firstrate.py` — parse FirstRate trade/quote files
- `tick-lab/tick_lab/rollup.py` — ticks → 1m bars; bars → coarser bars
- `tick-lab/tick_lab/store.py` — ArcticDB read/write
- `tick-lab/tick_lab/reference/base.py` — adapter protocol, errors, interval step-down
- `tick-lab/tick_lab/reference/yfinance_adapter.py` (v11.0.0)
- `tick-lab/tick_lab/reference/eodhd_api.py` (v11.1.0)
- `tick-lab/tick_lab/reference/eodhd_local.py` (v11.1.1)
- `tick-lab/tick_lab/report.py` — comparison + report rendering
- `tick-lab/tick_lab/cli.py` — argparse wiring for `load` / `compare`
- `tick-lab/tests/` — mirrors the above

**Modified:** `Dockerfile`, `docker-compose.yml`, `extension-constraints.txt`, `scripts/verify-isolation.sh`, `.gitignore`, `README.md`, `openbb-eodhd/pyproject.toml`, `openbb-kdb/pyproject.toml`
**Created:** `docs/arcticdb-minio-design.md`, `tick-lab/README.md`

---

### Task 1: Vendor the openbb-arcticdb extension unchanged

Bring the extension in as-is so its own tests establish a green baseline before anything is modified.

**Files:**
- Create: `openbb-arcticdb/` (copy of `github.com/artcashin/openbb-arcticdb` at its latest commit)
- Modify: `openbb-arcticdb/pyproject.toml` (version only)

**Interfaces:**
- Consumes: nothing.
- Produces: `openbb_arcticdb.utils.resolve_config(uri, library, credentials) -> tuple[str, str]`; `openbb_arcticdb.utils.default_uri() -> str`; `openbb_arcticdb.store.store(...)`; the `arcticdb_provider` Provider object.

- [ ] **Step 1: Copy the extension source**

```bash
cd /Users/artcashin/Developer/openbb-docker-ep11
cp -R /Users/artcashin/Developer/openbb-docker-old/openbb-arcticdb ./openbb-arcticdb
rm -rf openbb-arcticdb/.git openbb-arcticdb/.pytest_cache openbb-arcticdb/.ruff_cache \
       openbb-arcticdb/openbb_arcticdb.egg-info
```

- [ ] **Step 2: Set the chapter-matched version**

In `openbb-arcticdb/pyproject.toml` change:

```toml
version = "0.1.0"
```

to:

```toml
version = "11.0.0"
```

- [ ] **Step 3: Run the vendored tests to establish a baseline**

```bash
cd openbb-arcticdb && python -m venv .venv && .venv/bin/pip install -q -e '.[dev]' && .venv/bin/python -m pytest -q
```

Expected: all tests PASS. If any fail, stop — the baseline is broken and Task 2 would be building on sand.

- [ ] **Step 4: Verify the scrub gate**

```bash
cd /Users/artcashin/Developer/openbb-docker-ep11 && bash scripts/scrub-check.sh
```

Expected: `Scrub check passed.`

- [ ] **Step 5: Commit**

```bash
git add openbb-arcticdb
git commit -m "feat(arcticdb): vendor the openbb-arcticdb extension (Ep. 11)"
```

---

### Task 2: `ARCTICDB_S3_*` configuration in the extension

Today the extension reads only `ARCTICDB_URI`, so the MinIO key would have to be pasted into a second file. This adds an assembled-from-parts fallback so `minio.env` is the single source of truth (D5).

**Files:**
- Modify: `openbb-arcticdb/openbb_arcticdb/utils.py`
- Test: `openbb-arcticdb/tests/test_s3_config.py`

**Interfaces:**
- Consumes: `resolve_config` from Task 1.
- Produces: `openbb_arcticdb.utils.s3_uri_from_env(env: Mapping[str, str] | None = None) -> str | None` — returns an assembled `s3://`/`s3s://` URI, or `None` when the required variables are absent. `resolve_config` precedence becomes: explicit arg → OpenBB credential → `ARCTICDB_URI` → `s3_uri_from_env()` → LMDB default.

- [ ] **Step 1: Write the failing tests**

Create `openbb-arcticdb/tests/test_s3_config.py`:

```python
"""ARCTICDB_S3_* assembly and its precedence against ARCTICDB_URI."""

import pytest

from openbb_arcticdb.utils import resolve_config, s3_uri_from_env

FULL = {
    "ARCTICDB_S3_ENDPOINT": "minio.example.ts.net",
    "ARCTICDB_S3_BUCKET": "openbb",
    "ARCTICDB_S3_ACCESS": "someaccesskey",
    "ARCTICDB_S3_SECRET": "somesecretkey",
}


def test_returns_none_when_nothing_is_set():
    assert s3_uri_from_env({}) is None


def test_returns_none_when_partially_configured():
    partial = dict(FULL)
    del partial["ARCTICDB_S3_SECRET"]
    assert s3_uri_from_env(partial) is None


def test_assembles_secure_uri_by_default():
    uri = s3_uri_from_env(FULL)
    assert uri == (
        "s3s://minio.example.ts.net:openbb"
        "?port=9000&access=someaccesskey&secret=somesecretkey"
        "&use_virtual_addressing=false"
    )


def test_plain_scheme_when_secure_is_false():
    uri = s3_uri_from_env({**FULL, "ARCTICDB_S3_SECURE": "false"})
    assert uri.startswith("s3://")


def test_custom_port():
    uri = s3_uri_from_env({**FULL, "ARCTICDB_S3_PORT": "9443"})
    assert "port=9443" in uri


def test_credentials_are_url_encoded():
    uri = s3_uri_from_env({**FULL, "ARCTICDB_S3_SECRET": "a/b+c=d&e"})
    assert "secret=a%2Fb%2Bc%3Dd%26e" in uri


def test_rejects_non_numeric_port():
    with pytest.raises(ValueError, match="ARCTICDB_S3_PORT"):
        s3_uri_from_env({**FULL, "ARCTICDB_S3_PORT": "nine-thousand"})


def test_rejects_unparseable_secure_flag():
    with pytest.raises(ValueError, match="ARCTICDB_S3_SECURE"):
        s3_uri_from_env({**FULL, "ARCTICDB_S3_SECURE": "yes-please"})


def test_explicit_uri_wins_over_s3_parts(monkeypatch):
    for k, v in FULL.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ARCTICDB_URI", "lmdb:///tmp/explicit")
    uri, _ = resolve_config()
    assert uri == "lmdb:///tmp/explicit"


def test_s3_parts_used_when_no_explicit_uri(monkeypatch):
    for k, v in FULL.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ARCTICDB_URI", raising=False)
    uri, library = resolve_config()
    assert uri.startswith("s3s://minio.example.ts.net:openbb")
    assert library == "openbb"


def test_falls_back_to_lmdb_when_nothing_configured(monkeypatch):
    for k in list(FULL) + ["ARCTICDB_URI", "ARCTICDB_S3_SECURE", "ARCTICDB_S3_PORT"]:
        monkeypatch.delenv(k, raising=False)
    uri, _ = resolve_config()
    assert uri.startswith("lmdb://")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd openbb-arcticdb && .venv/bin/python -m pytest tests/test_s3_config.py -q
```

Expected: FAIL — `ImportError: cannot import name 's3_uri_from_env'`

- [ ] **Step 3: Implement `s3_uri_from_env`**

In `openbb-arcticdb/openbb_arcticdb/utils.py`, add this immediately after `default_uri()`:

```python
_S3_REQUIRED = (
    "ARCTICDB_S3_ENDPOINT",
    "ARCTICDB_S3_BUCKET",
    "ARCTICDB_S3_ACCESS",
    "ARCTICDB_S3_SECRET",
)


def s3_uri_from_env(env: Any = None) -> str | None:
    """Assemble an ArcticDB S3 URI from ARCTICDB_S3_* parts.

    Returns None unless every required part is present, so a partially
    configured environment falls through to the LMDB default rather than
    producing a URI that fails deep inside ArcticDB.

    Shape matters: host and bucket are separated by ':' and the port is a
    QUERY parameter. 'host:port:bucket' is not valid ArcticDB syntax.
    """
    # pylint: disable=import-outside-toplevel
    from urllib.parse import quote

    e = os.environ if env is None else env
    if any(not e.get(k) for k in _S3_REQUIRED):
        return None

    port_raw = str(e.get("ARCTICDB_S3_PORT") or "9000").strip()
    if not port_raw.isdigit():
        raise ValueError(f"ARCTICDB_S3_PORT must be a number, got {port_raw!r}")

    secure_raw = str(e.get("ARCTICDB_S3_SECURE") or "true").strip().lower()
    if secure_raw not in ("true", "false"):
        raise ValueError(
            f"ARCTICDB_S3_SECURE must be 'true' or 'false', got {secure_raw!r}"
        )

    scheme = "s3s" if secure_raw == "true" else "s3"
    return (
        f"{scheme}://{e['ARCTICDB_S3_ENDPOINT']}:{e['ARCTICDB_S3_BUCKET']}"
        f"?port={port_raw}"
        f"&access={quote(e['ARCTICDB_S3_ACCESS'], safe='')}"
        f"&secret={quote(e['ARCTICDB_S3_SECRET'], safe='')}"
        f"&use_virtual_addressing=false"
    )
```

- [ ] **Step 4: Wire it into `resolve_config`**

In the same file, replace the `uri = (...)` assignment inside `resolve_config` with:

```python
    uri = (
        uri
        or creds.get("arcticdb_uri")
        or os.getenv("ARCTICDB_URI")
        or s3_uri_from_env()
        or default_uri()
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd openbb-arcticdb && .venv/bin/python -m pytest -q && .venv/bin/python -m ruff check openbb_arcticdb
```

Expected: all tests PASS (the Task 1 baseline tests included), ruff clean.

- [ ] **Step 6: Commit**

```bash
git add openbb-arcticdb/openbb_arcticdb/utils.py openbb-arcticdb/tests/test_s3_config.py
git commit -m "feat(arcticdb): assemble the S3 URI from ARCTICDB_S3_* parts"
```

---

### Task 3: The MinIO cert-sync script

MinIO ignores an on-disk cert swap but reloads on `SIGHUP` with no downtime (spike S2, D3). This is the renewal loop, isolated into its own script so it can be tested with a stubbed `tailscale`.

**Files:**
- Create: `minio/cert-sync.sh`
- Test: `minio/tests/test_cert_sync.py`, `minio/tests/conftest.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `minio/cert-sync.sh <cert-dir> <domain> <pid-file>` — writes `public.crt`/`private.key` into `<cert-dir>`; sends `SIGHUP` to the PID in `<pid-file>` **only when the certificate content changed**. Exit 0 on success, 1 when `tailscale cert` fails.

- [ ] **Step 1: Write the failing tests**

Create `minio/tests/conftest.py`:

```python
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "cert-sync.sh"


@pytest.fixture
def fake_tailscale(tmp_path):
    """A stub `tailscale` on PATH whose emitted cert body is controllable.

    Writing to <bindir>/cert-body changes what the next invocation emits.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    body = bindir / "cert-body"
    body.write_text("CERT-A")
    stub = bindir / "tailscale"
    stub.write_text(
        "#!/bin/sh\n"
        "# usage: tailscale cert --cert-file X --key-file Y DOMAIN\n"
        'if [ -f "$(dirname "$0")/fail" ]; then echo "boom" >&2; exit 1; fi\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    --cert-file) shift; CERT="$1" ;;\n'
        '    --key-file) shift; KEY="$1" ;;\n'
        "  esac\n"
        "  shift\n"
        "done\n"
        'cat "$(dirname "$0")/cert-body" > "$CERT"\n'
        'echo "KEY" > "$KEY"\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return bindir


@pytest.fixture
def run_sync(fake_tailscale):
    def _run(cert_dir, domain, pid_file):
        env = dict(os.environ, PATH=f"{fake_tailscale}:{os.environ['PATH']}")
        return subprocess.run(
            ["sh", str(SCRIPT), str(cert_dir), domain, str(pid_file)],
            capture_output=True, text=True, env=env,
        )
    return _run
```

Create `minio/tests/test_cert_sync.py`:

```python
"""cert-sync.sh: fetch a cert, and HUP the server only when it actually changed."""

import signal
import subprocess
import time
from pathlib import Path

HUP_CATCHER = (
    "import signal, sys, time\n"
    "p = sys.argv[1]\n"
    "signal.signal(signal.SIGHUP, lambda *_: open(p, 'a').write('HUP\\n'))\n"
    "time.sleep(30)\n"
)


def _start_catcher(tmp_path):
    marker = tmp_path / "hups.txt"
    script = tmp_path / "catcher.py"
    script.write_text(HUP_CATCHER)
    proc = subprocess.Popen(["python3", str(script), str(marker)])
    pid_file = tmp_path / "minio.pid"
    pid_file.write_text(str(proc.pid))
    time.sleep(1)  # let the handler install
    return proc, pid_file, marker


def test_writes_cert_and_key(tmp_path, run_sync):
    certs = tmp_path / "certs"
    certs.mkdir()
    pid_file = tmp_path / "none.pid"
    result = run_sync(certs, "minio.example.ts.net", pid_file)
    assert result.returncode == 0, result.stderr
    assert (certs / "public.crt").read_text() == "CERT-A"
    assert (certs / "private.key").exists()


def test_key_is_not_world_readable(tmp_path, run_sync):
    certs = tmp_path / "certs"
    certs.mkdir()
    run_sync(certs, "minio.example.ts.net", tmp_path / "none.pid")
    mode = (certs / "private.key").stat().st_mode
    assert mode & 0o077 == 0


def test_no_hup_on_first_write(tmp_path, run_sync):
    certs = tmp_path / "certs"
    certs.mkdir()
    proc, pid_file, marker = _start_catcher(tmp_path)
    try:
        run_sync(certs, "minio.example.ts.net", pid_file)
        time.sleep(1)
        assert not marker.exists(), "first write should not signal a reload"
    finally:
        proc.kill()


def test_no_hup_when_cert_is_unchanged(tmp_path, run_sync, fake_tailscale):
    certs = tmp_path / "certs"
    certs.mkdir()
    proc, pid_file, marker = _start_catcher(tmp_path)
    try:
        run_sync(certs, "minio.example.ts.net", pid_file)
        run_sync(certs, "minio.example.ts.net", pid_file)
        time.sleep(1)
        assert not marker.exists(), "unchanged cert must not signal a reload"
    finally:
        proc.kill()


def test_hups_when_cert_changes(tmp_path, run_sync, fake_tailscale):
    certs = tmp_path / "certs"
    certs.mkdir()
    proc, pid_file, marker = _start_catcher(tmp_path)
    try:
        run_sync(certs, "minio.example.ts.net", pid_file)
        (fake_tailscale / "cert-body").write_text("CERT-B")
        run_sync(certs, "minio.example.ts.net", pid_file)
        time.sleep(1)
        assert marker.exists() and "HUP" in marker.read_text()
        assert (certs / "public.crt").read_text() == "CERT-B"
    finally:
        proc.kill()


def test_reports_failure_when_tailscale_fails(tmp_path, run_sync, fake_tailscale):
    (fake_tailscale / "fail").write_text("")
    certs = tmp_path / "certs"
    certs.mkdir()
    result = run_sync(certs, "minio.example.ts.net", tmp_path / "none.pid")
    assert result.returncode == 1
    assert "boom" in result.stderr or "cert" in result.stderr.lower()


def test_existing_cert_is_kept_when_renewal_fails(tmp_path, run_sync, fake_tailscale):
    certs = tmp_path / "certs"
    certs.mkdir()
    run_sync(certs, "minio.example.ts.net", tmp_path / "none.pid")
    (fake_tailscale / "fail").write_text("")
    run_sync(certs, "minio.example.ts.net", tmp_path / "none.pid")
    assert (certs / "public.crt").read_text() == "CERT-A", "must not clobber a good cert"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /Users/artcashin/Developer/openbb-docker-ep11 && python -m pytest minio/tests -q
```

Expected: FAIL — `cert-sync.sh` does not exist.

- [ ] **Step 3: Implement `minio/cert-sync.sh`**

```sh
#!/bin/sh
# Fetch (or renew) this node's Let's Encrypt certificate and hand it to MinIO.
#
# MinIO does NOT notice a rewritten certificate on disk, but it DOES reload on
# SIGHUP, with no restart and no dropped connections. So: write to a staging
# path, compare, and only promote + signal when the content actually changed.
#
# usage: cert-sync.sh <cert-dir> <domain> <pid-file>
set -eu

CERT_DIR="${1:?usage: cert-sync.sh <cert-dir> <domain> <pid-file>}"
DOMAIN="${2:?missing domain}"
PID_FILE="${3:?missing pid file}"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# Never overwrite a working certificate with a failed renewal: write to the
# staging directory first and promote only on success.
if ! tailscale cert --cert-file "$STAGE/public.crt" --key-file "$STAGE/private.key" "$DOMAIN"; then
    echo "cert-sync: tailscale cert failed for $DOMAIN; keeping existing certificate" >&2
    exit 1
fi

if [ -f "$CERT_DIR/public.crt" ] && cmp -s "$STAGE/public.crt" "$CERT_DIR/public.crt"; then
    exit 0
fi

had_cert=0
[ -f "$CERT_DIR/public.crt" ] && had_cert=1

cp "$STAGE/public.crt" "$CERT_DIR/public.crt"
cp "$STAGE/private.key" "$CERT_DIR/private.key"
chmod 600 "$CERT_DIR/private.key"
chmod 644 "$CERT_DIR/public.crt"

# On the very first write MinIO has not started yet (or is starting with this
# cert), so there is nothing to reload.
if [ "$had_cert" -eq 1 ] && [ -f "$PID_FILE" ]; then
    pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
        echo "cert-sync: certificate changed, reloading MinIO (pid $pid)"
        kill -HUP "$pid"
    fi
fi
exit 0
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
chmod +x minio/cert-sync.sh && python -m pytest minio/tests -q
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add minio/cert-sync.sh minio/tests
git commit -m "feat(minio): cert-sync -- renew via tailscale, SIGHUP only on change"
```

---

### Task 4: The MinIO image and entrypoint

**Files:**
- Create: `minio/Dockerfile`, `minio/entrypoint.sh`

**Interfaces:**
- Consumes: `minio/cert-sync.sh` from Task 3.
- Produces: an image whose entrypoint honours `MINIO_CERT_DOMAIN` (required), `MINIO_CERT_DIR` (default `/root/.minio/certs`), `MINIO_CERT_RENEW_SECONDS` (default `43200`), and passes any remaining arguments to `minio server`.

- [ ] **Step 1: Write `minio/entrypoint.sh`**

```sh
#!/bin/sh
# Start MinIO with a Tailscale-issued certificate, and keep that certificate
# fresh for the life of the container.
#
# The renewal loop lives HERE, in the same container as MinIO, because a
# sibling container could only signal MinIO through the Docker socket -- and
# mounting the Docker socket into a network-facing service is exactly the kind
# of hole the rest of this stack goes out of its way to avoid.
set -eu

CERT_DIR="${MINIO_CERT_DIR:-/root/.minio/certs}"
RENEW_SECONDS="${MINIO_CERT_RENEW_SECONDS:-43200}"
PID_FILE=/run/minio.pid

# The certificate must be issued for this node's MagicDNS name, which is the
# same host ArcticDB clients connect to. Default to ARCTICDB_S3_ENDPOINT so
# minio.env stays the single source of truth: compose CANNOT interpolate an
# env_file value into the compose file, so this defaulting has to happen here.
DOMAIN="${MINIO_CERT_DOMAIN:-${ARCTICDB_S3_ENDPOINT:-}}"
if [ -z "$DOMAIN" ]; then
    echo "entrypoint: set ARCTICDB_S3_ENDPOINT (or MINIO_CERT_DOMAIN) in minio.env," \
         "e.g. minio.<your-tailnet>.ts.net" >&2
    exit 1
fi

mkdir -p "$CERT_DIR" /run

# tailscaled shares this container's network namespace via the sidecar, but the
# socket appears only once the sidecar is up and the node has a name.
echo "entrypoint: waiting for tailscaled..."
i=0
until tailscale status --json >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
        echo "entrypoint: tailscaled did not come up within 60s" >&2
        exit 1
    fi
    sleep 1
done

echo "entrypoint: obtaining certificate for $DOMAIN"
/usr/local/bin/cert-sync.sh "$CERT_DIR" "$DOMAIN" "$PID_FILE" || {
    echo "entrypoint: could not obtain a certificate; refusing to start plaintext" >&2
    exit 1
}

minio server --certs-dir "$CERT_DIR" "$@" &
MINIO_PID=$!
echo "$MINIO_PID" > "$PID_FILE"

term() {
    kill -TERM "$MINIO_PID" 2>/dev/null || true
    wait "$MINIO_PID" 2>/dev/null || true
    exit 0
}
trap term TERM INT

(
    while true; do
        sleep "$RENEW_SECONDS"
        /usr/local/bin/cert-sync.sh "$CERT_DIR" "$DOMAIN" "$PID_FILE" || true
    done
) &

wait "$MINIO_PID"
```

- [ ] **Step 2: Write `minio/Dockerfile`**

```dockerfile
# MinIO with a Tailscale-issued certificate (Ep. 11).
#
# The tailscale CLI is copied in so the renewal loop can run in THIS container
# and signal MinIO directly -- see entrypoint.sh for why that matters.
FROM tailscale/tailscale:v1.98.9 AS ts

FROM quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z

COPY --from=ts /usr/local/bin/tailscale /usr/local/bin/tailscale
COPY cert-sync.sh /usr/local/bin/cert-sync.sh
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/cert-sync.sh /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/data"]
```

- [ ] **Step 3: Verify the image builds**

```bash
docker build --platform linux/amd64 -t openbb-minio:test ./minio
```

Expected: build succeeds.

- [ ] **Step 4: Verify the entrypoint fails closed without a domain**

```bash
docker run --rm --platform linux/amd64 openbb-minio:test 2>&1 | tail -2; echo "exit=$?"
```

Expected: exits non-zero with `set ARCTICDB_S3_ENDPOINT (or MINIO_CERT_DOMAIN) in minio.env`. This is the fail-closed check — MinIO must never start without TLS.

- [ ] **Step 5: Commit**

```bash
git add minio/Dockerfile minio/entrypoint.sh
git commit -m "feat(minio): TLS-only image with in-container cert renewal"
```

---

### Task 5: Compose services and credentials

**Files:**
- Modify: `docker-compose.yml`, `.gitignore`
- Create: `minio.env.example`

**Interfaces:**
- Consumes: the image from Task 4.
- Produces: services `minio-ts`, `minio`, `minio-init`; volumes `minio-data`, `minio-ts-state`; `minio.env` supplying `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` and the `ARCTICDB_S3_*` variables consumed by Task 2.

- [ ] **Step 1: Create `minio.env.example`**

```bash
# MinIO credentials and the ArcticDB connection derived from them (Ep. 11).
#   cp minio.env.example minio.env && chmod 600 minio.env
#
# IMPORTANT: keep comments on their OWN lines. Compose's dotenv parser treats
# an inline "# comment" after an empty value as the value itself.

# --- MinIO root credentials. Set a strong password. ---
MINIO_ROOT_USER=
MINIO_ROOT_PASSWORD=

# --- Where ArcticDB finds the store ---
# Your tailnet's name, e.g. minio.tailXXXXXX.ts.net -- MagicDNS must be on.
ARCTICDB_S3_ENDPOINT=
ARCTICDB_S3_PORT=9000
ARCTICDB_S3_BUCKET=openbb
ARCTICDB_S3_SECURE=true
# These MUST match MINIO_ROOT_USER / MINIO_ROOT_PASSWORD above.
ARCTICDB_S3_ACCESS=
ARCTICDB_S3_SECRET=
# ArcticDB library used by the provider. The tick-lab loader writes its own.
ARCTICDB_LIBRARY=openbb
```

- [ ] **Step 2: Add `minio.env` to `.gitignore`**

In `.gitignore`, under the `# secrets & machine-specific config` block, after the `ts.env` line, add:

```
minio.env
```

- [ ] **Step 3: Add the services to `docker-compose.yml`**

Append before the `volumes:` block:

```yaml
  # --- Ep. 11: the shared store -------------------------------------------
  # MinIO joins the tailnet as its OWN node rather than riding behind Serve on
  # the openbb node. S3 SigV4 signs the Host header, so putting a reverse proxy
  # in the signing path buys a failure mode for nothing -- the node's own
  # tailnet address is the endpoint.
  #
  # Before first start:
  #   cp minio.env.example minio.env && chmod 600 minio.env
  minio-ts:
    image: tailscale/tailscale:v1.98.9
    container_name: openbb-minio-ts
    hostname: minio
    restart: unless-stopped
    env_file:
      - ./ts.env
    environment:
      - TS_HOSTNAME=minio
      # Same reasoning as the openbb node: userspace mode would forward tailnet
      # connections to 127.0.0.1 and make MinIO reachable around our intent.
      - TS_USERSPACE=false
      - TS_STATE_DIR=/var/lib/tailscale
    volumes:
      - minio-ts-state:/var/lib/tailscale
    devices:
      - /dev/net/tun:/dev/net/tun
    cap_add:
      - NET_ADMIN
      - NET_RAW

  # MinIO shares the sidecar's namespace. NOTHING is published to the host, so
  # :9000/:9001 exist on the tailscale interface (and the docker bridge) only.
  minio:
    build: ./minio
    image: openbb-minio:11.0.0
    platform: linux/amd64
    container_name: openbb-minio
    restart: unless-stopped
    network_mode: service:minio-ts
    depends_on:
      - minio-ts
    env_file:
      - path: ./minio.env
        required: true
    environment:
      # NOTE: the cert domain is NOT set here. Compose interpolates ${...} from
      # the shell or a root .env only -- never from env_file -- so referencing
      # ARCTICDB_S3_ENDPOINT here would silently expand to empty. The entrypoint
      # reads it from its own environment instead, where env_file did put it.
      - MINIO_CERT_RENEW_SECONDS=43200
    command: ["/data", "--console-address", ":9001"]
    volumes:
      - minio-data:/data
      # The renewal loop talks to tailscaled over this socket.
      - minio-ts-state:/var/lib/tailscale

  # One-shot: ArcticDB will NOT create its own bucket, so `docker compose up`
  # would otherwise leave you with a store that 404s on first write.
  minio-init:
    image: quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z
    container_name: openbb-minio-init
    network_mode: service:minio-ts
    depends_on:
      - minio
    env_file:
      - path: ./minio.env
        required: true
    entrypoint: ["/bin/sh", "-c"]
    command:
      - |
        set -eu
        for i in $$(seq 1 60); do
          if mc alias set store "https://$${ARCTICDB_S3_ENDPOINT}:$${ARCTICDB_S3_PORT}" \
               "$${MINIO_ROOT_USER}" "$${MINIO_ROOT_PASSWORD}" >/dev/null 2>&1; then
            mc mb --ignore-existing "store/$${ARCTICDB_S3_BUCKET}"
            echo "minio-init: bucket $${ARCTICDB_S3_BUCKET} ready"
            exit 0
          fi
          sleep 2
        done
        echo "minio-init: MinIO did not become reachable" >&2
        exit 1
    restart: "no"
```

In the `volumes:` block at the end of the file, add:

```yaml
  minio-data:
  minio-ts-state:
```

- [ ] **Step 4: Point the API at the store**

In the `openbb-api` service's `env_file:` list, add after the `credentials.env` entry:

```yaml
      # Ep. 11: ARCTICDB_S3_* -- the MinIO store, shared with tick-lab.
      - path: ./minio.env
        required: false
```

And add to that service:

```yaml
    platform: linux/amd64
```

with this comment above it:

```yaml
    # ArcticDB publishes no aarch64 Linux wheels, so the image is amd64.
    # On Apple Silicon this runs under emulation -- correct, but slower.
```

Apply the same `platform: linux/amd64` line to the `openbb-mcp` service, which builds the same image.

- [ ] **Step 5: Validate the compose file**

```bash
cp minio.env.example minio.env
printf 'MINIO_ROOT_USER=testuser\nMINIO_ROOT_PASSWORD=testpassword123\nARCTICDB_S3_ENDPOINT=minio.example.ts.net\nARCTICDB_S3_PORT=9000\nARCTICDB_S3_BUCKET=openbb\nARCTICDB_S3_SECURE=true\nARCTICDB_S3_ACCESS=testuser\nARCTICDB_S3_SECRET=testpassword123\n' > minio.env
docker compose config >/dev/null && echo "compose config OK"
```

Expected: `compose config OK`.

- [ ] **Step 6: Confirm nothing is published to the host**

```bash
docker compose config | grep -A3 -E "^  (minio|minio-ts):" | grep -c "ports:" || echo "0 published ports — correct"
```

Expected: `0 published ports — correct`.

- [ ] **Step 7: Remove the scratch env file and run the scrub gate**

```bash
rm -f minio.env && bash scripts/scrub-check.sh
```

Expected: `Scrub check passed.`

- [ ] **Step 8: Commit**

```bash
git add docker-compose.yml minio.env.example .gitignore
git commit -m "feat(minio): MinIO as its own tailnet node, bucket bootstrapped on up"
```

---

### Task 6: Install the extension into the Platform image

**Files:**
- Modify: `Dockerfile`, `extension-constraints.txt`, `openbb-eodhd/pyproject.toml`, `openbb-kdb/pyproject.toml`

**Interfaces:**
- Consumes: `openbb-arcticdb/` from Tasks 1–2.
- Produces: an image where `obb.coverage.providers` contains `arcticdb`.

- [ ] **Step 1: Add the extension to the Dockerfile**

In `Dockerfile`, immediately after the `openbb-kdb` install block, add:

```dockerfile
# ArcticDB store + provider extension (Ep. 11). Bars and ticks persisted to
# S3/MinIO can stand in for an upstream API call via provider="arcticdb".
#
# ArcticDB ships manylinux x86_64 wheels ONLY -- no aarch64. That is why the
# compose services pin platform: linux/amd64.
COPY openbb-arcticdb /tmp/openbb-arcticdb
RUN pip install --no-cache-dir /tmp/openbb-arcticdb && rm -rf /tmp/openbb-arcticdb
```

- [ ] **Step 2: Extend the build-time provider assertion**

Replace the existing verification `RUN` block with:

```dockerfile
RUN python -c "import openbb; openbb.build(); from openbb import obb; \
assert 'eodhd' in obb.coverage.providers, 'eodhd provider not registered'; \
assert 'kdb' in obb.coverage.providers, 'kdb provider not registered'; \
assert 'arcticdb' in obb.coverage.providers, 'arcticdb provider not registered'; \
print('OpenBB Platform OK:', len(obb.coverage.providers), 'providers (incl. eodhd, kdb, arcticdb)')"
```

- [ ] **Step 3: Apply the chapter-matched extension version to eodhd only (D6, partial)**

In `openbb-eodhd/pyproject.toml`: `version = "0.1.0"` → `version = "8.0.0"`

**Do NOT touch `openbb-kdb/pyproject.toml`.** A concurrent Ep. 10 session is splitting that package into a new `kdb-store` as this runs; bumping it here would guarantee a rebase conflict in a file being actively rewritten. `openbb-kdb` → `10.0.0` (and `kdb-store` likewise) is deferred to a follow-up once Ep. 10 settles. Version metadata only; change no behaviour.

- [ ] **Step 4: Build the image**

```bash
docker build --platform linux/amd64 -t openbb-local:11.0.0 .
```

Expected: build succeeds, ending with `OpenBB Platform OK: N providers (incl. eodhd, kdb, arcticdb)`.

- [ ] **Step 5: Record any dependency ceiling the build revealed**

If the build reported a resolver conflict, add the demonstrated ceiling to `extension-constraints.txt` with a comment naming the package that forced it, then rebuild. If there was no conflict, change nothing — do not add speculative pins.

- [ ] **Step 6: Verify the provider resolves at runtime**

```bash
docker run --rm --platform linux/amd64 openbb-local:11.0.0 \
  python -c "from openbb import obb; print('arcticdb' in obb.coverage.providers)"
```

Expected: `True`

- [ ] **Step 7: Commit**

```bash
git add Dockerfile extension-constraints.txt openbb-eodhd/pyproject.toml openbb-kdb/pyproject.toml
git commit -m "feat: register provider=arcticdb in the image; chapter-matched extension versions"
```

---

### Task 7: Isolation checks for the new ports

**Files:**
- Modify: `scripts/verify-isolation.sh`

**Interfaces:**
- Consumes: nothing.
- Produces: `verify-isolation.sh <openbb-host> [minio-host]` — when the second argument is given, additionally proves `:9000` speaks TLS and `:9001` is not exposed beyond the tailnet.

- [ ] **Step 1: Update the usage line and step count**

Replace the header's usage comment and `host=` line with:

```bash
#   scripts/verify-isolation.sh openbb.<your-tailnet>.ts.net
#   scripts/verify-isolation.sh openbb.<your-tailnet>.ts.net minio.<your-tailnet>.ts.net
set -euo pipefail
host="${1:?usage: verify-isolation.sh <tailnet-hostname> [minio-hostname]}"
minio_host="${2:-}"
```

Then change the three existing step labels from `N/3` to `N/5`.

- [ ] **Step 2: Append the MinIO checks**

Before the final `echo "Isolation verified."`, insert:

```bash
# Ep. 11: MinIO is its own node. Its S3 API must be TLS-only, and its console
# must never be reachable from outside the tailnet.
if [ -n "$minio_host" ]; then
  echo "4/5 the MinIO S3 API answers over TLS with a real certificate..."
  curl -fsS --max-time 10 "https://$minio_host:9000/minio/health/live" >/dev/null \
    && echo "   OK: valid certificate, health endpoint live"

  echo "5/5 the MinIO S3 API refuses plaintext..."
  if curl -fsS --max-time 5 "http://$minio_host:9000/minio/health/live" >/dev/null 2>&1; then
    echo "   FAIL: :9000 answered over plain HTTP — the entrypoint should refuse to start without a cert" >&2
    exit 1
  fi
  echo "   OK: plaintext refused"
else
  echo "4/5, 5/5 skipped (no MinIO hostname given)"
fi
```

- [ ] **Step 3: Verify the script still parses and the skip path works**

```bash
bash -n scripts/verify-isolation.sh && echo "syntax OK"
```

Expected: `syntax OK`. The live checks require a running stack on a second tailnet device and are run manually.

- [ ] **Step 4: Commit**

```bash
git add scripts/verify-isolation.sh
git commit -m "test: verify-isolation covers MinIO's TLS-only S3 API"
```

---

### Task 8: tick-lab scaffold and configuration

**Files:**
- Create: `tick-lab/pyproject.toml`, `tick-lab/tick_lab/__init__.py`, `tick-lab/tick_lab/config.py`, `tick-lab/tests/__init__.py`, `tick-lab/tests/test_config.py`, `tick-lab/.env.example`

**Interfaces:**
- Consumes: the `ARCTICDB_S3_*` names defined in Task 5.
- Produces: `tick_lab.config.S3Config` (frozen dataclass: `endpoint: str`, `bucket: str`, `access: str`, `secret: str`, `port: int = 9000`, `secure: bool = True`; property `uri -> str`) and `tick_lab.config.from_env(env: Mapping[str, str] | None = None) -> S3Config`, raising `tick_lab.config.ConfigError` on anything missing or malformed.

- [ ] **Step 1: Create the package skeleton**

`tick-lab/pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "tick-lab"
version = "11.0.0"
description = "Read ticks from the shared ArcticDB store, roll them into bars, and check them against another source"
readme = "README.md"
requires-python = ">=3.10"
license = { text = "AGPL-3.0-only" }
dependencies = [
    "arcticdb>=6.21",
    "pandas>=2.0",
    "yfinance>=1.5",
    "requests>=2.31",
]

[project.scripts]
tick-lab = "tick_lab.cli:main"

[project.optional-dependencies]
dev = ["ruff", "pytest"]

[tool.setuptools.packages.find]
include = ["tick_lab*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]

[tool.ruff]
target-version = "py310"
line-length = 100
```

`tick-lab/tick_lab/__init__.py`:

```python
"""Read ticks from the shared ArcticDB store and check derived bars against another source."""

__version__ = "11.0.0"
```

`tick-lab/tests/__init__.py`: empty file.

`tick-lab/.env.example`:

```bash
# tick-lab connects to the SAME MinIO store the container uses, so these are
# the same names and values as minio.env. Copy this to .env and fill it in:
#   cp .env.example .env && chmod 600 .env
ARCTICDB_S3_ENDPOINT=minio.<your-tailnet>.ts.net
ARCTICDB_S3_PORT=9000
ARCTICDB_S3_BUCKET=openbb
ARCTICDB_S3_SECURE=true
ARCTICDB_S3_ACCESS=
ARCTICDB_S3_SECRET=
```

- [ ] **Step 2: Write the failing tests**

`tick-lab/tests/test_config.py`:

```python
"""S3 connection settings read from the environment."""

import pytest

from tick_lab.config import ConfigError, S3Config, from_env

FULL = {
    "ARCTICDB_S3_ENDPOINT": "minio.example.ts.net",
    "ARCTICDB_S3_BUCKET": "openbb",
    "ARCTICDB_S3_ACCESS": "someaccesskey",
    "ARCTICDB_S3_SECRET": "somesecretkey",
}


def test_reads_a_complete_environment():
    cfg = from_env(FULL)
    assert cfg == S3Config(
        endpoint="minio.example.ts.net",
        bucket="openbb",
        access="someaccesskey",
        secret="somesecretkey",
        port=9000,
        secure=True,
    )


def test_uri_matches_the_verified_arcticdb_shape():
    assert from_env(FULL).uri == (
        "s3s://minio.example.ts.net:openbb"
        "?port=9000&access=someaccesskey&secret=somesecretkey"
        "&use_virtual_addressing=false"
    )


def test_plain_scheme_when_insecure():
    assert from_env({**FULL, "ARCTICDB_S3_SECURE": "false"}).uri.startswith("s3://")


def test_credentials_are_url_encoded():
    assert "secret=a%2Fb%26c" in from_env({**FULL, "ARCTICDB_S3_SECRET": "a/b&c"}).uri


def test_missing_variables_are_all_named_at_once():
    with pytest.raises(ConfigError) as exc:
        from_env({"ARCTICDB_S3_ENDPOINT": "minio.example.ts.net"})
    message = str(exc.value)
    for name in ("ARCTICDB_S3_BUCKET", "ARCTICDB_S3_ACCESS", "ARCTICDB_S3_SECRET"):
        assert name in message


def test_blank_values_count_as_missing():
    with pytest.raises(ConfigError, match="ARCTICDB_S3_SECRET"):
        from_env({**FULL, "ARCTICDB_S3_SECRET": "   "})


def test_rejects_non_numeric_port():
    with pytest.raises(ConfigError, match="ARCTICDB_S3_PORT"):
        from_env({**FULL, "ARCTICDB_S3_PORT": "nine"})


def test_rejects_unparseable_secure_flag():
    with pytest.raises(ConfigError, match="ARCTICDB_S3_SECURE"):
        from_env({**FULL, "ARCTICDB_S3_SECURE": "maybe"})


def test_secret_is_not_leaked_by_repr():
    assert "somesecretkey" not in repr(from_env(FULL))
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd tick-lab && python -m venv .venv && .venv/bin/pip install -q -e '.[dev]' && .venv/bin/python -m pytest tests/test_config.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'tick_lab.config'`

- [ ] **Step 4: Implement `tick-lab/tick_lab/config.py`**

```python
"""S3 connection settings, read from the same ARCTICDB_S3_* names the container uses.

Keeping one convention on both sides means `minio.env` is the single source of
truth: the laptop and the Platform container cannot drift apart.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote

REQUIRED = (
    "ARCTICDB_S3_ENDPOINT",
    "ARCTICDB_S3_BUCKET",
    "ARCTICDB_S3_ACCESS",
    "ARCTICDB_S3_SECRET",
)


class ConfigError(RuntimeError):
    """Raised when the environment cannot produce a usable connection."""


@dataclass(frozen=True)
class S3Config:
    endpoint: str
    bucket: str
    access: str
    secret: str
    port: int = 9000
    secure: bool = True

    def __repr__(self) -> str:
        # A traceback from a CLI lands in terminal scrollback and pasted bug
        # reports; the secret has no business being in either.
        return (
            f"S3Config(endpoint={self.endpoint!r}, bucket={self.bucket!r}, "
            f"access={self.access!r}, secret='***', port={self.port}, "
            f"secure={self.secure})"
        )

    @property
    def uri(self) -> str:
        """The ArcticDB connection URI.

        Host and bucket are separated by ':' and the port is a QUERY parameter.
        'host:port:bucket' looks plausible and is not valid ArcticDB syntax.
        """
        scheme = "s3s" if self.secure else "s3"
        return (
            f"{scheme}://{self.endpoint}:{self.bucket}"
            f"?port={self.port}"
            f"&access={quote(self.access, safe='')}"
            f"&secret={quote(self.secret, safe='')}"
            f"&use_virtual_addressing=false"
        )


def from_env(env: Mapping[str, str] | None = None) -> S3Config:
    """Build an S3Config from ARCTICDB_S3_* variables."""
    e = os.environ if env is None else env

    missing = [k for k in REQUIRED if not str(e.get(k, "")).strip()]
    if missing:
        raise ConfigError(
            "missing or empty required environment variable(s): "
            + ", ".join(missing)
            + " — copy tick-lab/.env.example to .env and fill it in"
        )

    port_raw = str(e.get("ARCTICDB_S3_PORT") or "9000").strip()
    if not port_raw.isdigit():
        raise ConfigError(f"ARCTICDB_S3_PORT must be a number, got {port_raw!r}")

    secure_raw = str(e.get("ARCTICDB_S3_SECURE") or "true").strip().lower()
    if secure_raw not in ("true", "false"):
        raise ConfigError(
            f"ARCTICDB_S3_SECURE must be 'true' or 'false', got {secure_raw!r}"
        )

    return S3Config(
        endpoint=str(e["ARCTICDB_S3_ENDPOINT"]).strip(),
        bucket=str(e["ARCTICDB_S3_BUCKET"]).strip(),
        access=str(e["ARCTICDB_S3_ACCESS"]).strip(),
        secret=str(e["ARCTICDB_S3_SECRET"]).strip(),
        port=int(port_raw),
        secure=secure_raw == "true",
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_config.py -q && .venv/bin/python -m ruff check tick_lab
```

Expected: 9 passed, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add tick-lab
git commit -m "feat(tick-lab): package scaffold and S3 configuration"
```

---

### Task 9: Parse FirstRate trade and quote files

**Files:**
- Create: `tick-lab/tick_lab/firstrate.py`, `tick-lab/tests/test_firstrate.py`, `tick-lab/tests/fixtures/MSFT_trades_sample.txt`, `tick-lab/tests/fixtures/MSFT_quotes_sample.txt`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `tick_lab.firstrate.detect_kind(first_line: str) -> str` — `"trade"` or `"quote"`; raises `FirstRateFormatError` otherwise.
  - `tick_lab.firstrate.parse(text: str) -> tuple[str, pandas.DataFrame]` — `(kind, frame)` with a tz-aware **UTC** `DatetimeIndex`, sorted. Trade columns: `price`, `volume`, `exchange`, `conditions`. Quote columns: `bid_price`, `bid_size`, `bid_exchange`, `ask_price`, `ask_size`, `ask_exchange`.
  - `tick_lab.firstrate.FirstRateFormatError`

- [ ] **Step 1: Create the fixtures**

`tick-lab/tests/fixtures/MSFT_trades_sample.txt` (synthetic, FirstRate trade format — five fields; note the pre-market row, the two trades inside one minute, and the 16:00:00 closing print):

```
2023-05-12 09:15:00.100000,309.50,100,1,E
2023-05-12 09:30:00.000000,310.55,500,2,O
2023-05-12 09:30:30.250000,310.65,200,1,
2023-05-12 09:30:59.999000,310.00,300,1,
2023-05-12 09:31:00.000000,310.03,150,1,
2023-05-12 15:59:30.000000,308.90,400,2,
2023-05-12 16:00:00.000000,308.97,10000,2,C
```

`tick-lab/tests/fixtures/MSFT_quotes_sample.txt` (seven fields):

```
2023-05-12 09:30:00.000000,310.50,100,1,310.60,200,2
2023-05-12 09:30:30.250000,310.60,300,1,310.70,100,2
```

- [ ] **Step 2: Write the failing tests**

`tick-lab/tests/test_firstrate.py`:

```python
"""Parsing FirstRate trade/quote files, including the timezone contract."""

from pathlib import Path

import pandas as pd
import pytest

from tick_lab.firstrate import FirstRateFormatError, detect_kind, parse

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name):
    return (FIXTURES / name).read_text()


def test_detects_trade_by_column_count():
    assert detect_kind("2023-05-12 09:30:00.000000,310.55,500,2,O") == "trade"


def test_detects_quote_by_column_count():
    assert detect_kind("2023-05-12 09:30:00.000000,310.5,100,1,310.6,200,2") == "quote"


def test_detects_quote_with_the_documented_eighth_field():
    """The vendor's format doc lists eight fields and names 'offer price' twice.

    Detection keys on the column count, not on that document, so an eight-field
    quote line is still a quote.
    """
    line = "2023-05-12 09:30:00.000000,310.5,100,1,310.6,200,2,310.6"
    assert detect_kind(line) == "quote"


def test_rejects_an_unrecognised_shape():
    with pytest.raises(FirstRateFormatError, match="3 field"):
        detect_kind("2023-05-12 09:30:00.000000,310.55,500")


def test_parses_trades_with_expected_columns():
    kind, df = parse(_read("MSFT_trades_sample.txt"))
    assert kind == "trade"
    assert list(df.columns) == ["price", "volume", "exchange", "conditions"]
    assert len(df) == 7


def test_trade_index_is_utc_and_shifted_from_eastern():
    """09:30 US/Eastern in May is EDT (UTC-4), so it must land at 13:30 UTC."""
    _, df = parse(_read("MSFT_trades_sample.txt"))
    assert str(df.index.tz) == "UTC"
    assert pd.Timestamp("2023-05-12 13:30:00", tz="UTC") in df.index


def test_index_is_sorted():
    _, df = parse(_read("MSFT_trades_sample.txt"))
    assert df.index.is_monotonic_increasing


def test_numeric_columns_are_numeric():
    _, df = parse(_read("MSFT_trades_sample.txt"))
    assert df["price"].dtype.kind == "f"
    assert df["volume"].dtype.kind in "iu"


def test_empty_conditions_become_empty_strings_not_nan():
    _, df = parse(_read("MSFT_trades_sample.txt"))
    assert df["conditions"].iloc[2] == ""


def test_parses_quotes():
    kind, df = parse(_read("MSFT_quotes_sample.txt"))
    assert kind == "quote"
    assert list(df.columns) == [
        "bid_price", "bid_size", "bid_exchange",
        "ask_price", "ask_size", "ask_exchange",
    ]
    assert len(df) == 2


def test_blank_lines_are_ignored():
    kind, df = parse("\n2023-05-12 09:30:00.000000,310.55,500,2,O\n\n")
    assert kind == "trade" and len(df) == 1


def test_empty_input_raises():
    with pytest.raises(FirstRateFormatError, match="no data"):
        parse("   \n\n")


def test_ragged_row_is_reported_with_its_line_number():
    text = (
        "2023-05-12 09:30:00.000000,310.55,500,2,O\n"
        "2023-05-12 09:30:01.000000,310.60,100\n"
    )
    with pytest.raises(FirstRateFormatError, match="line 2"):
        parse(text)
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_firstrate.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'tick_lab.firstrate'`

- [ ] **Step 4: Implement `tick-lab/tick_lab/firstrate.py`**

```python
"""Parse FirstRate Data tick files into UTC-indexed frames.

Two facts drive this module:

1. File shape is detected by COLUMN COUNT, not by the bundled format document.
   That document lists the quote line with eight fields and names "offer price"
   twice, so it cannot be trusted literally.
2. Timestamps in the files are naive US Eastern. They are localized and
   converted to UTC here, once, at the boundary. Everything downstream is UTC.
   A timezone slip does not fail loudly -- it shows up as a 100% discrepancy
   rate much later, which is far more expensive to debug.
"""

from __future__ import annotations

import io

import pandas as pd

EASTERN = "America/New_York"

TRADE_COLUMNS = ["timestamp", "price", "volume", "exchange", "conditions"]
QUOTE_COLUMNS = [
    "timestamp",
    "bid_price",
    "bid_size",
    "bid_exchange",
    "ask_price",
    "ask_size",
    "ask_exchange",
]


class FirstRateFormatError(ValueError):
    """The input does not look like a FirstRate trade or quote file."""


def detect_kind(first_line: str) -> str:
    """Classify a data line as 'trade' or 'quote' by its field count."""
    n = len(first_line.split(","))
    if n == len(TRADE_COLUMNS):
        return "trade"
    if n in (len(QUOTE_COLUMNS), len(QUOTE_COLUMNS) + 1):
        return "quote"
    raise FirstRateFormatError(
        f"unrecognised FirstRate line with {n} field(s): {first_line[:80]!r}"
    )


def parse(text: str) -> tuple[str, pd.DataFrame]:
    """Parse a FirstRate trade or quote file into (kind, UTC-indexed frame)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise FirstRateFormatError("no data rows found")

    kind = detect_kind(lines[0])
    columns = TRADE_COLUMNS if kind == "trade" else QUOTE_COLUMNS

    expected = len(columns)
    for offset, line in enumerate(lines, start=1):
        if len(line.split(",")) < expected:
            raise FirstRateFormatError(
                f"line {offset}: expected {expected} fields for a {kind} file, "
                f"got {len(line.split(','))}: {line[:80]!r}"
            )

    frame = pd.read_csv(
        io.StringIO("\n".join(lines)),
        header=None,
        names=columns,
        # A quote file may carry the extra eighth field; ignore anything past
        # the columns we name.
        usecols=range(expected),
        dtype={"conditions": "string"} if kind == "trade" else None,
    )

    stamps = pd.to_datetime(frame.pop("timestamp"), format="mixed")
    # ambiguous/nonexistent default to raising: a DST-invalid tick is a broken
    # file, not something to silently coerce.
    frame.index = (
        pd.DatetimeIndex(stamps)
        .tz_localize(EASTERN, ambiguous="raise", nonexistent="raise")
        .tz_convert("UTC")
    )

    if kind == "trade":
        frame["conditions"] = frame["conditions"].fillna("").astype(str)

    return kind, frame.sort_index()
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_firstrate.py -q && .venv/bin/python -m ruff check tick_lab
```

Expected: 13 passed, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add tick-lab/tick_lab/firstrate.py tick-lab/tests/test_firstrate.py tick-lab/tests/fixtures
git commit -m "feat(tick-lab): parse FirstRate trade/quote files into UTC frames"
```

---

### Task 10: Roll ticks into bars

The core computation, and the one place where the session filter (D9) applies.

**Files:**
- Create: `tick-lab/tick_lab/rollup.py`, `tick-lab/tests/test_rollup.py`

**Interfaces:**
- Consumes: `tick_lab.firstrate.parse` output shape (UTC index, `price`/`volume` columns).
- Produces:
  - `tick_lab.rollup.BAR_COLUMNS = ["open", "high", "low", "close", "volume"]`
  - `tick_lab.rollup.to_minute_bars(trades: DataFrame, session: str = "regular") -> DataFrame` — UTC-indexed 1-minute OHLCV, left-closed/left-labeled, empty buckets dropped.
  - `tick_lab.rollup.aggregate(bars: DataFrame, interval: str) -> DataFrame` — steps 1m bars up to a coarser interval. Accepts `1m`, `5m`, `15m`, `30m`, `1h`, `1d`.
  - `tick_lab.rollup.SessionError`

- [ ] **Step 1: Write the failing tests**

`tick-lab/tests/test_rollup.py`:

```python
"""Tick -> 1-minute OHLCV, and 1-minute -> coarser bars."""

import pandas as pd
import pytest

from tick_lab.rollup import BAR_COLUMNS, SessionError, aggregate, to_minute_bars


def ticks(rows):
    """rows: list of (eastern timestamp string, price, volume)."""
    idx = pd.DatetimeIndex([pd.Timestamp(t) for t, _, _ in rows]).tz_localize(
        "America/New_York"
    ).tz_convert("UTC")
    return pd.DataFrame(
        {"price": [p for _, p, _ in rows], "volume": [v for _, _, v in rows]},
        index=idx,
    )


def test_ohlcv_within_one_minute():
    df = to_minute_bars(ticks([
        ("2023-05-12 09:30:00", 310.55, 500),
        ("2023-05-12 09:30:30", 311.00, 200),
        ("2023-05-12 09:30:45", 310.00, 100),
        ("2023-05-12 09:30:59", 310.40, 300),
    ]))
    assert len(df) == 1
    bar = df.iloc[0]
    assert (bar.open, bar.high, bar.low, bar.close) == (310.55, 311.00, 310.00, 310.40)
    assert bar.volume == 1100


def test_columns_and_index_contract():
    df = to_minute_bars(ticks([("2023-05-12 09:30:00", 310.0, 100)]))
    assert list(df.columns) == BAR_COLUMNS
    assert str(df.index.tz) == "UTC"


def test_bars_are_left_labeled():
    df = to_minute_bars(ticks([("2023-05-12 09:30:45", 310.0, 100)]))
    assert df.index[0] == pd.Timestamp("2023-05-12 13:30:00", tz="UTC")


def test_empty_minutes_are_dropped_not_forward_filled():
    df = to_minute_bars(ticks([
        ("2023-05-12 09:30:00", 310.0, 100),
        ("2023-05-12 09:33:00", 311.0, 100),
    ]))
    assert len(df) == 2


def test_regular_session_excludes_premarket():
    df = to_minute_bars(ticks([
        ("2023-05-12 09:15:00", 309.5, 100),
        ("2023-05-12 09:30:00", 310.5, 100),
    ]))
    assert len(df) == 1
    assert df.index[0] == pd.Timestamp("2023-05-12 13:30:00", tz="UTC")


def test_regular_session_excludes_the_1600_print():
    """16:00:00 is exclusive, so a closing-auction print is out under 'regular'.

    This is the single biggest lever on the discrepancy count, which is why
    --session exists rather than being hard-coded.
    """
    df = to_minute_bars(ticks([
        ("2023-05-12 15:59:30", 308.90, 400),
        ("2023-05-12 16:00:00", 308.97, 10000),
    ]))
    assert len(df) == 1
    assert df.iloc[0].close == 308.90


def test_all_session_keeps_everything():
    df = to_minute_bars(
        ticks([
            ("2023-05-12 09:15:00", 309.5, 100),
            ("2023-05-12 09:30:00", 310.5, 100),
            ("2023-05-12 16:00:00", 308.97, 10000),
        ]),
        session="all",
    )
    assert len(df) == 3


def test_0930_boundary_is_inclusive():
    df = to_minute_bars(ticks([("2023-05-12 09:30:00", 310.5, 100)]))
    assert len(df) == 1


def test_unknown_session_is_rejected():
    with pytest.raises(SessionError, match="weekends"):
        to_minute_bars(ticks([("2023-05-12 09:30:00", 310.0, 100)]), session="weekends")


def test_empty_input_returns_empty_frame_with_columns():
    empty = pd.DataFrame(
        {"price": [], "volume": []},
        index=pd.DatetimeIndex([], tz="UTC"),
    )
    df = to_minute_bars(empty)
    assert df.empty and list(df.columns) == BAR_COLUMNS


def test_aggregate_to_daily_uses_eastern_calendar_days():
    bars = to_minute_bars(ticks([
        ("2023-05-12 09:30:00", 310.55, 500),
        ("2023-05-12 12:00:00", 312.00, 100),
        ("2023-05-12 15:59:00", 308.97, 400),
    ]))
    daily = aggregate(bars, "1d")
    assert len(daily) == 1
    row = daily.iloc[0]
    assert (row.open, row.high, row.low, row.close) == (310.55, 312.00, 308.97, 308.97)
    assert row.volume == 1000


def test_aggregate_to_five_minutes():
    bars = to_minute_bars(ticks([
        ("2023-05-12 09:30:00", 310.0, 100),
        ("2023-05-12 09:31:00", 311.0, 100),
        ("2023-05-12 09:36:00", 312.0, 100),
    ]))
    five = aggregate(bars, "5m")
    assert len(five) == 2
    assert five.iloc[0].high == 311.0


def test_aggregate_1m_is_a_passthrough():
    bars = to_minute_bars(ticks([("2023-05-12 09:30:00", 310.0, 100)]))
    assert aggregate(bars, "1m").equals(bars)


def test_aggregate_rejects_unknown_interval():
    bars = to_minute_bars(ticks([("2023-05-12 09:30:00", 310.0, 100)]))
    with pytest.raises(ValueError, match="7m"):
        aggregate(bars, "7m")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_rollup.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'tick_lab.rollup'`

- [ ] **Step 3: Implement `tick-lab/tick_lab/rollup.py`**

```python
"""Roll ticks into OHLCV bars, and roll those bars up further.

The session filter is the single largest lever on any comparison against a
consolidated reference feed: FirstRate ticks include extended hours, and most
reference bars do not. It is a parameter, never a hidden default.
"""

from __future__ import annotations

from datetime import time

import pandas as pd

EASTERN = "America/New_York"
BAR_COLUMNS = ["open", "high", "low", "close", "volume"]

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)

SESSIONS = ("regular", "all")

# pandas resample rules for the intervals a reference source may hand back.
_RULES = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "1d": "1D",
}

_AGG = {
    "open": ("open", "first"),
    "high": ("high", "max"),
    "low": ("low", "min"),
    "close": ("close", "last"),
    "volume": ("volume", "sum"),
}


class SessionError(ValueError):
    """An unknown --session value."""


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(
        {c: pd.Series(dtype="float64") for c in BAR_COLUMNS},
        index=pd.DatetimeIndex([], tz="UTC", name=None),
    )


def to_minute_bars(trades: pd.DataFrame, session: str = "regular") -> pd.DataFrame:
    """Aggregate trade ticks into 1-minute OHLCV bars.

    `trades` must carry a tz-aware UTC index and `price`/`volume` columns.
    Bars are left-closed and left-labeled: the 09:30 bar covers [09:30, 09:31).
    Minutes with no trades are dropped rather than forward-filled -- an absent
    bar and a flat bar are different facts, and the comparison counts them
    differently.
    """
    if session not in SESSIONS:
        raise SessionError(
            f"unknown session {session!r}; expected one of {', '.join(SESSIONS)}"
        )
    if trades.empty:
        return _empty_bars()

    df = trades
    if session == "regular":
        local = df.index.tz_convert(EASTERN)
        # 09:30 inclusive, 16:00 exclusive -- 390 one-minute bars.
        mask = (local.time >= REGULAR_OPEN) & (local.time < REGULAR_CLOSE)
        df = df[mask]
        if df.empty:
            return _empty_bars()

    bars = df.resample("1min", label="left", closed="left").agg(
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        volume=("volume", "sum"),
    )
    return bars.dropna(subset=["open"])[BAR_COLUMNS]


def aggregate(bars: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Roll 1-minute bars up to a coarser interval."""
    if interval not in _RULES:
        raise ValueError(
            f"unsupported interval {interval!r}; expected one of {', '.join(_RULES)}"
        )
    if interval == "1m" or bars.empty:
        return bars

    if interval == "1d":
        # A US equity session spans one Eastern calendar day but can straddle
        # nothing in UTC terms -- still, bucket in Eastern so the daily bar
        # matches what a daily reference feed calls that date.
        local = bars.tz_convert(EASTERN)
        out = local.resample("1D", label="left", closed="left").agg(**_AGG)
        return out.dropna(subset=["open"])[BAR_COLUMNS].tz_convert("UTC")

    out = bars.resample(_RULES[interval], label="left", closed="left").agg(**_AGG)
    return out.dropna(subset=["open"])[BAR_COLUMNS]
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_rollup.py -q && .venv/bin/python -m ruff check tick_lab
```

Expected: 14 passed, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add tick-lab/tick_lab/rollup.py tick-lab/tests/test_rollup.py
git commit -m "feat(tick-lab): 1-minute roll-up with an explicit session filter"
```

---

### Task 11: ArcticDB read/write

**Files:**
- Create: `tick-lab/tick_lab/store.py`, `tick-lab/tests/test_store.py`

**Interfaces:**
- Consumes: `tick_lab.config.S3Config`.
- Produces:
  - `tick_lab.store.TickStore(cfg: S3Config)` with `write(library: str, symbol: str, frame: DataFrame, metadata: dict | None = None) -> None`, `read(library: str, symbol: str, start=None, end=None) -> DataFrame`, `list_symbols(library: str) -> list[str]`, `has(library: str, symbol: str) -> bool`.
  - `tick_lab.store.to_bounds(start, end) -> tuple[pd.Timestamp | None, pd.Timestamp | None]` — a date `end` covers the whole day; a datetime `end` is exact.

- [ ] **Step 1: Write the failing tests**

`tick-lab/tests/test_store.py`:

```python
"""Date-range bounds, and the ArcticDB round-trip against a real MinIO.

The round-trip test is skipped unless TICK_LAB_TEST_S3=1 and the ARCTICDB_S3_*
variables point at a disposable MinIO. See tick-lab/README.md.
"""

import os
import uuid

import pandas as pd
import pytest

from tick_lab.config import from_env
from tick_lab.store import TickStore, to_bounds

pytestmark_integration = pytest.mark.skipif(
    os.getenv("TICK_LAB_TEST_S3") != "1",
    reason="set TICK_LAB_TEST_S3=1 with a disposable MinIO to run",
)


def test_date_end_covers_the_whole_day():
    _, end = to_bounds(None, "2023-05-12")
    assert end == pd.Timestamp("2023-05-12 23:59:59.999999999")


def test_datetime_end_is_exact():
    _, end = to_bounds(None, "2023-05-12 15:00:00")
    assert end == pd.Timestamp("2023-05-12 15:00:00")


def test_none_bounds_stay_none():
    assert to_bounds(None, None) == (None, None)


def test_start_is_parsed():
    start, _ = to_bounds("2023-05-12", None)
    assert start == pd.Timestamp("2023-05-12 00:00:00")


@pytestmark_integration
def test_round_trip_preserves_utc_index():
    cfg = from_env()
    store = TickStore(cfg)
    library = f"ticklabtest{uuid.uuid4().hex[:8]}"
    frame = pd.DataFrame(
        {"price": [1.0, 2.0], "volume": [10, 20]},
        index=pd.DatetimeIndex(
            ["2023-05-12T13:30:00Z", "2023-05-12T13:31:00Z"]
        ).tz_convert("UTC"),
    )
    store.write(library, "MSFT", frame)

    assert store.has(library, "MSFT")
    assert "MSFT" in store.list_symbols(library)

    got = store.read(library, "MSFT")
    assert str(got.index.tz) == "UTC"
    pd.testing.assert_frame_equal(got, frame)


@pytestmark_integration
def test_read_filters_by_date_range():
    cfg = from_env()
    store = TickStore(cfg)
    library = f"ticklabtest{uuid.uuid4().hex[:8]}"
    idx = pd.date_range("2023-05-12 13:30:00Z", periods=5, freq="1min")
    store.write(library, "MSFT", pd.DataFrame({"price": range(5), "volume": range(5)}, index=idx))

    got = store.read(library, "MSFT", start="2023-05-12 13:31:00", end="2023-05-12 13:32:00")
    assert len(got) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_store.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'tick_lab.store'`

- [ ] **Step 3: Implement `tick-lab/tick_lab/store.py`**

```python
"""ArcticDB access for tick-lab.

This talks to ArcticDB directly rather than through OpenBB: the whole point of
the chapter is that the store is a shared network service usable from any
Python process, with no Platform install on the client.
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import Any

import pandas as pd

from tick_lab.config import S3Config


def to_bounds(start: Any, end: Any) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """Build an ArcticDB `date_range` pair.

    A pure-date `end` is widened to the end of that day so "2023-05-12" means
    the whole session; a datetime `end` is honoured exactly.
    """
    start_ts = None if start is None else pd.Timestamp(start)

    if end is None:
        return start_ts, None

    end_ts = pd.Timestamp(end)
    is_pure_date = isinstance(end, date_type) and not isinstance(end, datetime)
    if isinstance(end, str):
        is_pure_date = ":" not in end
    if is_pure_date:
        end_ts = end_ts.normalize() + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    return start_ts, end_ts


class TickStore:
    """A thin, typed wrapper over an ArcticDB connection."""

    def __init__(self, cfg: S3Config):
        from arcticdb import Arctic

        self._cfg = cfg
        self._arctic = Arctic(cfg.uri)

    def _library(self, library: str, create: bool = True):
        return self._arctic.get_library(library, create_if_missing=create)

    def write(
        self,
        library: str,
        symbol: str,
        frame: pd.DataFrame,
        metadata: dict | None = None,
    ) -> None:
        """Overwrite `symbol`, so re-running a load is idempotent."""
        self._library(library).write(symbol, frame, metadata=metadata)

    def read(
        self,
        library: str,
        symbol: str,
        start: Any = None,
        end: Any = None,
    ) -> pd.DataFrame:
        """Read a symbol, filtering by date range on the server where possible."""
        bounds = to_bounds(start, end)
        date_range = None if bounds == (None, None) else bounds
        return self._library(library, create=False).read(
            symbol, date_range=date_range
        ).data

    def list_symbols(self, library: str) -> list[str]:
        if not self._arctic.has_library(library):
            return []
        return sorted(self._library(library, create=False).list_symbols())

    def has(self, library: str, symbol: str) -> bool:
        return symbol in self.list_symbols(library)
```

- [ ] **Step 4: Run the unit tests to verify they pass**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_store.py -q
```

Expected: 4 passed, 2 skipped.

- [ ] **Step 5: Run the integration tests against a disposable MinIO**

```bash
docker run -d --name ticklab-minio -p 19000:9000 \
  -e MINIO_ROOT_USER=testuser -e MINIO_ROOT_PASSWORD=testpassword123 \
  quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z server /data
sleep 8
cd tick-lab && .venv/bin/python - <<'PY'
import boto3
from botocore.client import Config
s3 = boto3.client("s3", endpoint_url="http://127.0.0.1:19000",
                  aws_access_key_id="testuser", aws_secret_access_key="testpassword123",
                  config=Config(signature_version="s3v4"), region_name="us-east-1")
s3.create_bucket(Bucket="openbb")
PY
TICK_LAB_TEST_S3=1 ARCTICDB_S3_ENDPOINT=127.0.0.1 ARCTICDB_S3_PORT=19000 \
  ARCTICDB_S3_BUCKET=openbb ARCTICDB_S3_SECURE=false \
  ARCTICDB_S3_ACCESS=testuser ARCTICDB_S3_SECRET=testpassword123 \
  .venv/bin/python -m pytest tests/test_store.py -q
docker rm -f ticklab-minio
```

Expected: 6 passed. (`boto3` is a test-only convenience for bucket creation; install it into `.venv` if absent.)

- [ ] **Step 6: Commit**

```bash
git add tick-lab/tick_lab/store.py tick-lab/tests/test_store.py
git commit -m "feat(tick-lab): ArcticDB read/write with date-range bounds"
```

---

### Task 12: Reference adapters — protocol, errors, and yfinance

**Files:**
- Create: `tick-lab/tick_lab/reference/__init__.py`, `tick-lab/tick_lab/reference/base.py`, `tick-lab/tick_lab/reference/yfinance_adapter.py`, `tick-lab/tests/test_reference_base.py`, `tick-lab/tests/test_yfinance_adapter.py`

**Interfaces:**
- Consumes: `tick_lab.rollup.BAR_COLUMNS`.
- Produces:
  - `tick_lab.reference.base.INTERVAL_LADDER = ("1m", "5m", "15m", "30m", "1h", "1d")`
  - `tick_lab.reference.base.ReferenceError(Exception)` with `.kind` in `{"retention", "empty", "auth", "entitlement", "not_covered", "transport"}` and `.detail: str`
  - `tick_lab.reference.base.Attempt` — dataclass `interval: str`, `error: ReferenceError | None`
  - `tick_lab.reference.base.ReferenceResult` — dataclass `frame: DataFrame`, `interval: str`, `attempts: list[Attempt]`
  - `tick_lab.reference.base.fetch_finest(adapter, symbol, start, end, wanted="1m") -> ReferenceResult` — walks `INTERVAL_LADDER` from `wanted`, recording every attempt; re-raises anything that is not `retention`/`empty`.
  - `tick_lab.reference.yfinance_adapter.YFinanceAdapter` with `name = "yfinance"`, `supported_intervals`, and `fetch(symbol, start, end, interval) -> DataFrame`

- [ ] **Step 1: Write the failing tests for the ladder**

`tick-lab/tests/test_reference_base.py`:

```python
"""Interval step-down: try the finest, record why each attempt failed."""

import pandas as pd
import pytest

from tick_lab.reference.base import (
    INTERVAL_LADDER,
    ReferenceError,
    fetch_finest,
)


class FakeAdapter:
    name = "fake"
    supported_intervals = INTERVAL_LADDER

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def fetch(self, symbol, start, end, interval):
        self.calls.append(interval)
        outcome = self.behaviour[interval]
        if isinstance(outcome, ReferenceError):
            raise outcome
        return outcome


def bars(n=1):
    idx = pd.date_range("2023-05-12 13:30:00Z", periods=n, freq="1min")
    return pd.DataFrame(
        {"open": [1.0] * n, "high": [1.0] * n, "low": [1.0] * n,
         "close": [1.0] * n, "volume": [1] * n},
        index=idx,
    )


def test_returns_the_finest_interval_that_works():
    adapter = FakeAdapter({"1m": bars()})
    result = fetch_finest(adapter, "MSFT", "2023-05-12", "2023-05-12")
    assert result.interval == "1m"
    assert adapter.calls == ["1m"]
    assert result.attempts[-1].error is None


def test_steps_down_past_retention_failures():
    adapter = FakeAdapter({
        "1m": ReferenceError("retention", "must be within the last 30 days"),
        "5m": ReferenceError("retention", "must be within the last 60 days"),
        "15m": ReferenceError("retention", "must be within the last 60 days"),
        "30m": ReferenceError("retention", "must be within the last 60 days"),
        "1h": ReferenceError("retention", "must be within the last 730 days"),
        "1d": bars(),
    })
    result = fetch_finest(adapter, "MSFT", "2023-05-12", "2023-05-12")
    assert result.interval == "1d"
    assert adapter.calls == list(INTERVAL_LADDER)


def test_every_attempt_is_recorded_for_the_report():
    adapter = FakeAdapter({
        "1m": ReferenceError("retention", "must be within the last 30 days"),
        "5m": bars(),
    })
    result = fetch_finest(adapter, "MSFT", "2023-05-12", "2023-05-12")
    assert [a.interval for a in result.attempts] == ["1m", "5m"]
    assert "last 30 days" in result.attempts[0].error.detail
    assert result.attempts[1].error is None


def test_entitlement_errors_are_not_swallowed_by_stepping_down():
    """A 403 means 'you may not have this', not 'try a coarser bar'."""
    adapter = FakeAdapter({"1m": ReferenceError("entitlement", "403 Forbidden")})
    with pytest.raises(ReferenceError) as exc:
        fetch_finest(adapter, "GOOG", "2023-05-12", "2023-05-12")
    assert exc.value.kind == "entitlement"
    assert adapter.calls == ["1m"]


def test_raises_when_the_whole_ladder_is_exhausted():
    adapter = FakeAdapter({i: ReferenceError("empty", "no rows") for i in INTERVAL_LADDER})
    with pytest.raises(ReferenceError, match="no interval"):
        fetch_finest(adapter, "MSFT", "2023-05-12", "2023-05-12")


def test_starts_from_the_requested_interval():
    adapter = FakeAdapter({"1h": bars(), "1d": bars()})
    result = fetch_finest(adapter, "MSFT", "2023-05-12", "2023-05-12", wanted="1h")
    assert adapter.calls == ["1h"]
    assert result.interval == "1h"


def test_skips_intervals_the_adapter_does_not_support():
    class DailyOnly(FakeAdapter):
        supported_intervals = ("1d",)

    adapter = DailyOnly({"1d": bars()})
    result = fetch_finest(adapter, "MSFT", "2023-05-12", "2023-05-12")
    assert adapter.calls == ["1d"]
    assert result.interval == "1d"
```

- [ ] **Step 2: Run to verify failure**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_reference_base.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'tick_lab.reference'`

- [ ] **Step 3: Implement `tick-lab/tick_lab/reference/__init__.py`**

```python
"""Reference price sources to compare our tick-derived bars against."""
```

- [ ] **Step 4: Implement `tick-lab/tick_lab/reference/base.py`**

```python
"""The reference-source contract.

Two rules make the comparison honest:

1. Adapters CLASSIFY failures. "No data" is not one thing: a retention limit,
   an unentitled symbol, a bad key and a genuinely empty window are four
   different facts, and only some of them mean "try a coarser bar".
2. Stepping down the interval ladder is explicit and recorded, so the report
   can show what was asked for, what came back, and why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import pandas as pd

INTERVAL_LADDER = ("1m", "5m", "15m", "30m", "1h", "1d")

# Only these mean "this source cannot serve this window at this resolution".
# Anything else is a real problem and must surface, not be stepped past.
_STEPPABLE = ("retention", "empty")


class ReferenceError(Exception):
    """A classified failure from a reference source."""

    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


@dataclass
class Attempt:
    interval: str
    error: ReferenceError | None = None


@dataclass
class ReferenceResult:
    frame: pd.DataFrame
    interval: str
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def stepped_down(self) -> bool:
        return len(self.attempts) > 1


class ReferenceAdapter(Protocol):
    name: str
    supported_intervals: tuple[str, ...]

    def fetch(
        self, symbol: str, start: Any, end: Any, interval: str
    ) -> pd.DataFrame: ...


def fetch_finest(
    adapter: ReferenceAdapter,
    symbol: str,
    start: Any,
    end: Any,
    wanted: str = "1m",
) -> ReferenceResult:
    """Fetch at the finest interval this source can actually serve.

    Walks the ladder from `wanted` downward in resolution, recording each
    attempt. Retention and empty-window failures step down; everything else
    (auth, entitlement, transport) is re-raised immediately.
    """
    if wanted not in INTERVAL_LADDER:
        raise ValueError(
            f"unsupported interval {wanted!r}; expected one of {', '.join(INTERVAL_LADDER)}"
        )

    attempts: list[Attempt] = []
    for interval in INTERVAL_LADDER[INTERVAL_LADDER.index(wanted) :]:
        if interval not in adapter.supported_intervals:
            continue
        try:
            frame = adapter.fetch(symbol, start, end, interval)
        except ReferenceError as err:
            attempts.append(Attempt(interval, err))
            if err.kind in _STEPPABLE:
                continue
            raise
        attempts.append(Attempt(interval))
        return ReferenceResult(frame=frame, interval=interval, attempts=attempts)

    raise ReferenceError(
        "empty",
        f"no interval from {adapter.name} could serve {symbol} over {start}..{end}",
    )
```

- [ ] **Step 5: Run the ladder tests to verify they pass**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_reference_base.py -q
```

Expected: 7 passed.

- [ ] **Step 6: Write the failing yfinance tests**

`tick-lab/tests/test_yfinance_adapter.py`:

```python
"""Mapping yfinance behaviour onto classified ReferenceErrors."""

import pandas as pd
import pytest

from tick_lab.reference.base import ReferenceError
from tick_lab.reference.yfinance_adapter import YFinanceAdapter, classify

RETENTION_1M = (
    '$MSFT: possibly delisted; no price data found  (1m 2023-05-12 -> 2023-05-13) '
    '(Yahoo error = "1m data not available for startTime=1683864000 and '
    'endTime=1683950400. The requested range must be within the last 30 days.")'
)
RETENTION_1H = (
    '$MSFT: possibly delisted; no price data found  (1h 2023-05-12 -> 2023-05-13) '
    '(Yahoo error = "1h data not available for startTime=1683864000 and '
    'endTime=1683950400. The requested range must be within the last 730 days.")'
)


def test_classifies_the_1m_retention_message():
    err = classify(RETENTION_1M)
    assert err.kind == "retention"
    assert "last 30 days" in err.detail


def test_classifies_the_1h_retention_message():
    assert classify(RETENTION_1H).kind == "retention"


def test_classifies_an_unknown_message_as_empty():
    assert classify("something else entirely").kind == "empty"


def test_supported_intervals_cover_the_ladder():
    assert "1m" in YFinanceAdapter().supported_intervals
    assert "1d" in YFinanceAdapter().supported_intervals


def test_normalises_columns_and_index(monkeypatch):
    adapter = YFinanceAdapter()

    raw = pd.DataFrame(
        {"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5], "Volume": [100]},
        index=pd.DatetimeIndex(["2023-05-12 09:30:00"], tz="America/New_York"),
    )
    monkeypatch.setattr(adapter, "_history", lambda *a, **k: raw)

    out = adapter.fetch("MSFT", "2023-05-12", "2023-05-13", "1m")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert str(out.index.tz) == "UTC"


def test_empty_frame_becomes_a_classified_error(monkeypatch):
    adapter = YFinanceAdapter()
    monkeypatch.setattr(adapter, "_history", lambda *a, **k: pd.DataFrame())
    with pytest.raises(ReferenceError) as exc:
        adapter.fetch("MSFT", "2023-05-12", "2023-05-13", "1m")
    assert exc.value.kind == "empty"
```

- [ ] **Step 7: Run to verify failure**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_yfinance_adapter.py -q
```

Expected: FAIL — no module `tick_lab.reference.yfinance_adapter`

- [ ] **Step 8: Implement `tick-lab/tick_lab/reference/yfinance_adapter.py`**

```python
"""yfinance as a reference source.

yfinance returns an EMPTY FRAME rather than raising when Yahoo refuses a
window, but Yahoo's own explanation ("The requested range must be within the
last 30 days") is worth surfacing verbatim -- it is the whole reason a 2023
tick sample cannot be checked minute-by-minute against this source. So
exceptions are switched on and the message is classified.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from tick_lab.reference.base import ReferenceError

# Yahoo's retention ceilings, for reference: 1m ~30 days, 2m-90m ~60 days,
# 1h ~730 days, 1d unlimited.
_SUPPORTED = ("1m", "5m", "15m", "30m", "1h", "1d")

_COLUMNS = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
}


def classify(message: str) -> ReferenceError:
    """Turn a yfinance error message into a classified ReferenceError."""
    if "must be within the last" in message:
        return ReferenceError("retention", message)
    return ReferenceError("empty", message)


class YFinanceAdapter:
    name = "yfinance"
    supported_intervals = _SUPPORTED

    def _history(self, symbol: str, start: Any, end: Any, interval: str) -> pd.DataFrame:
        import yfinance as yf

        # Surface Yahoo's explanation instead of an empty frame. The old
        # `raise_errors=` argument is deprecated in favour of this.
        try:
            yf.config.debug.hide_exceptions = False
        except AttributeError:  # pragma: no cover - older yfinance
            pass
        return yf.Ticker(symbol).history(
            start=start, end=end, interval=interval, auto_adjust=False
        )

    def fetch(self, symbol: str, start: Any, end: Any, interval: str) -> pd.DataFrame:
        try:
            raw = self._history(symbol, start, end, interval)
        except Exception as err:  # yfinance raises its own exception types
            raise classify(str(err)) from err

        if raw is None or raw.empty:
            raise ReferenceError(
                "empty", f"yfinance returned no rows for {symbol} at {interval}"
            )

        frame = raw.rename(columns=_COLUMNS)[list(_COLUMNS.values())]
        index = pd.DatetimeIndex(frame.index)
        frame.index = (
            index.tz_convert("UTC") if index.tz is not None else index.tz_localize("UTC")
        )
        return frame
```

- [ ] **Step 9: Run all reference tests to verify they pass**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_reference_base.py tests/test_yfinance_adapter.py -q \
  && .venv/bin/python -m ruff check tick_lab
```

Expected: 13 passed, ruff clean.

- [ ] **Step 10: Commit**

```bash
git add tick-lab/tick_lab/reference tick-lab/tests/test_reference_base.py tick-lab/tests/test_yfinance_adapter.py
git commit -m "feat(tick-lab): reference adapter contract + yfinance with classified errors"
```

---

### Task 13: The comparison report

**Files:**
- Create: `tick-lab/tick_lab/report.py`, `tick-lab/tests/test_report.py`

**Interfaces:**
- Consumes: `tick_lab.rollup.BAR_COLUMNS`.
- Produces:
  - `tick_lab.report.Discrepancy` — dataclass `timestamp: pd.Timestamp`, `field: str`, `ours: float`, `theirs: float`, `diff: float`, `bps: float`
  - `tick_lab.report.ComparisonReport` — dataclass `symbol: str`, `interval: str`, `tolerance: float`, `bars_compared: int`, `discrepancies: list[Discrepancy]`, `ours_only: list[pd.Timestamp]`, `theirs_only: list[pd.Timestamp]`, `notes: list[str]`; with `largest_close() -> Discrepancy | None`, `to_text() -> str`, `to_dict() -> dict`
  - `tick_lab.report.compare(ours: DataFrame, theirs: DataFrame, symbol: str, interval: str, tolerance: float = 0.01, notes: list[str] | None = None) -> ComparisonReport`

- [ ] **Step 1: Write the failing tests**

`tick-lab/tests/test_report.py`:

```python
"""Comparing two bar sets: price disagreements vs coverage gaps."""

import json

import pandas as pd

from tick_lab.report import compare

IDX = pd.date_range("2023-05-12 13:30:00Z", periods=3, freq="1min")


def frame(closes, index=IDX):
    n = len(closes)
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": list(closes),
            "volume": [1000] * n,
        },
        index=index[:n],
    )


def test_identical_frames_report_no_discrepancies():
    rep = compare(frame([100.0, 101.0, 102.0]), frame([100.0, 101.0, 102.0]), "MSFT", "1m")
    assert rep.bars_compared == 3
    assert rep.discrepancies == []
    assert rep.largest_close() is None


def test_differences_within_tolerance_are_not_counted():
    rep = compare(frame([100.000, 101.0, 102.0]), frame([100.005, 101.0, 102.0]), "MSFT", "1m")
    assert rep.discrepancies == []


def test_difference_beyond_tolerance_is_counted():
    rep = compare(frame([100.0, 101.0, 102.0]), frame([100.5, 101.0, 102.0]), "MSFT", "1m")
    closes = [d for d in rep.discrepancies if d.field == "close"]
    assert len(closes) == 1
    assert closes[0].diff == -0.5


def test_largest_close_discrepancy_is_reported_with_bps_and_timestamp():
    rep = compare(frame([100.0, 101.0, 90.0]), frame([100.5, 101.0, 100.0]), "MSFT", "1m")
    worst = rep.largest_close()
    assert worst.timestamp == IDX[2]
    assert worst.diff == -10.0
    assert round(worst.bps, 1) == -1000.0


def test_coverage_gaps_are_counted_separately_from_price_discrepancies():
    ours = frame([100.0, 101.0, 102.0])
    theirs = frame([100.0, 101.0], index=IDX)
    rep = compare(ours, theirs, "MSFT", "1m")
    assert rep.bars_compared == 2
    assert rep.ours_only == [IDX[2]]
    assert rep.theirs_only == []
    assert rep.discrepancies == []


def test_bars_missing_on_our_side_are_reported_too():
    ours = frame([100.0, 101.0], index=IDX)
    theirs = frame([100.0, 101.0, 102.0])
    rep = compare(ours, theirs, "MSFT", "1m")
    assert rep.theirs_only == [IDX[2]]


def test_volume_is_compared_but_never_becomes_the_largest_close():
    ours = frame([100.0])
    theirs = frame([100.0])
    theirs.loc[theirs.index[0], "volume"] = 5000
    rep = compare(ours, theirs, "MSFT", "1m")
    assert any(d.field == "volume" for d in rep.discrepancies)
    assert rep.largest_close() is None


def test_no_overlap_reports_zero_bars_compared():
    ours = frame([100.0], index=IDX)
    theirs = frame([100.0], index=pd.date_range("2024-01-02 13:30:00Z", periods=1, freq="1min"))
    rep = compare(ours, theirs, "MSFT", "1m")
    assert rep.bars_compared == 0
    assert len(rep.ours_only) == 1 and len(rep.theirs_only) == 1


def test_text_output_names_the_three_categories():
    rep = compare(frame([100.0, 101.0, 90.0]), frame([100.0, 101.0, 100.0]), "MSFT", "1m")
    text = rep.to_text()
    assert "bars compared" in text
    assert "price discrepancies" in text
    assert "coverage gaps" in text
    assert "MSFT" in text


def test_dict_output_is_json_serialisable():
    rep = compare(frame([100.0, 90.0]), frame([100.0, 100.0]), "MSFT", "1m")
    payload = json.dumps(rep.to_dict())
    assert "largest_close_discrepancy" in payload


def test_notes_are_carried_into_the_report():
    rep = compare(frame([100.0]), frame([100.0]), "MSFT", "1d", notes=["stepped down from 1m"])
    assert "stepped down from 1m" in rep.to_text()
```

- [ ] **Step 2: Run to verify failure**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_report.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'tick_lab.report'`

- [ ] **Step 3: Implement `tick-lab/tick_lab/report.py`**

```python
"""Compare two bar sets and render the result.

Three outcomes are kept apart on purpose, because conflating them is how
comparisons end up lying:

  bars compared      -- timestamps present on BOTH sides
  price discrepancies-- values that disagree beyond the tolerance
  coverage gaps      -- bars only one side has at all

A bar the reference simply does not have is not a pricing disagreement, and
counting it as one would inflate every number in the report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from tick_lab.rollup import BAR_COLUMNS


@dataclass
class Discrepancy:
    timestamp: pd.Timestamp
    field: str
    ours: float
    theirs: float
    diff: float
    bps: float

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "field": self.field,
            "ours": self.ours,
            "theirs": self.theirs,
            "diff": self.diff,
            "bps": self.bps,
        }


@dataclass
class ComparisonReport:
    symbol: str
    interval: str
    tolerance: float
    bars_compared: int
    discrepancies: list[Discrepancy] = field(default_factory=list)
    ours_only: list[pd.Timestamp] = field(default_factory=list)
    theirs_only: list[pd.Timestamp] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def largest_close(self) -> Discrepancy | None:
        closes = [d for d in self.discrepancies if d.field == "close"]
        return max(closes, key=lambda d: abs(d.diff)) if closes else None

    def to_text(self) -> str:
        lines = [
            f"{self.symbol} @ {self.interval} (tolerance {self.tolerance})",
            "",
            f"  bars compared          : {self.bars_compared}",
            f"  price discrepancies    : {len(self.discrepancies)}",
            f"  coverage gaps          : {len(self.ours_only)} ours-only, "
            f"{len(self.theirs_only)} reference-only",
        ]

        by_field = {c: 0 for c in BAR_COLUMNS}
        for d in self.discrepancies:
            by_field[d.field] = by_field.get(d.field, 0) + 1
        breakdown = ", ".join(f"{k}={v}" for k, v in by_field.items() if v)
        if breakdown:
            lines.append(f"  by field               : {breakdown}")

        worst = self.largest_close()
        if worst is not None:
            lines += [
                "",
                "  largest close discrepancy:",
                f"    at    {worst.timestamp.isoformat()}",
                f"    ours  {worst.ours}",
                f"    ref   {worst.theirs}",
                f"    diff  {worst.diff:+.4f} ({worst.bps:+.1f} bps)",
            ]
        else:
            lines += ["", "  largest close discrepancy: none"]

        if self.notes:
            lines += [""] + [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        worst = self.largest_close()
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "tolerance": self.tolerance,
            "bars_compared": self.bars_compared,
            "price_discrepancies": [d.to_dict() for d in self.discrepancies],
            "coverage_gaps": {
                "ours_only": [t.isoformat() for t in self.ours_only],
                "reference_only": [t.isoformat() for t in self.theirs_only],
            },
            "largest_close_discrepancy": worst.to_dict() if worst else None,
            "notes": self.notes,
        }


def compare(
    ours: pd.DataFrame,
    theirs: pd.DataFrame,
    symbol: str,
    interval: str,
    tolerance: float = 0.01,
    notes: list[str] | None = None,
) -> ComparisonReport:
    """Diff two OHLCV frames sharing a UTC DatetimeIndex."""
    shared = ours.index.intersection(theirs.index).sort_values()

    discrepancies: list[Discrepancy] = []
    for ts in shared:
        for column in BAR_COLUMNS:
            a = float(ours.at[ts, column])
            b = float(theirs.at[ts, column])
            diff = a - b
            if abs(diff) <= tolerance:
                continue
            discrepancies.append(
                Discrepancy(
                    timestamp=ts,
                    field=column,
                    ours=a,
                    theirs=b,
                    diff=diff,
                    bps=(diff / b * 10_000.0) if b else float("nan"),
                )
            )

    return ComparisonReport(
        symbol=symbol,
        interval=interval,
        tolerance=tolerance,
        bars_compared=len(shared),
        discrepancies=discrepancies,
        ours_only=list(ours.index.difference(theirs.index).sort_values()),
        theirs_only=list(theirs.index.difference(ours.index).sort_values()),
        notes=list(notes or []),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_report.py -q && .venv/bin/python -m ruff check tick_lab
```

Expected: 11 passed, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add tick-lab/tick_lab/report.py tick-lab/tests/test_report.py
git commit -m "feat(tick-lab): comparison report separating gaps from disagreements"
```

---

### Task 14: The CLI — `load` and `compare`

**Files:**
- Create: `tick-lab/tick_lab/cli.py`, `tick-lab/tests/test_cli.py`

**Interfaces:**
- Consumes: every module from Tasks 8–13.
- Produces:
  - `tick_lab.cli.main(argv: list[str] | None = None) -> int`
  - `tick_lab.cli.ADAPTERS: dict[str, Callable[[], ReferenceAdapter]]` — the registry later releases extend. In v11.0.0 it holds only `"yfinance"`.
  - `tick_lab.cli.symbol_from_filename(name: str) -> tuple[str, str]` — `("MSFT", "trade")` from `MSFT_trades_2023-05-12.txt`.

- [ ] **Step 1: Write the failing tests**

`tick-lab/tests/test_cli.py`:

```python
"""CLI argument handling and the filename -> (symbol, kind) convention."""

import pytest

from tick_lab.cli import ADAPTERS, main, symbol_from_filename


@pytest.mark.parametrize(
    "name,expected",
    [
        ("MSFT_trades_2023-05-12.txt", ("MSFT", "trade")),
        ("GOOG_quotes_2023-05-12.txt", ("GOOG", "quote")),
        ("MSFT_trade.txt", ("MSFT", "trade")),
        ("msft_quotes.csv", ("MSFT", "quote")),
    ],
)
def test_symbol_and_kind_from_filename(name, expected):
    assert symbol_from_filename(name) == expected


def test_unrecognised_filename_is_rejected():
    with pytest.raises(ValueError, match="cannot tell"):
        symbol_from_filename("random-data.txt")


def test_yfinance_is_registered():
    assert "yfinance" in ADAPTERS


def test_no_subcommand_exits_nonzero(capsys):
    assert main([]) == 2


def test_unknown_reference_is_rejected(capsys):
    with pytest.raises(SystemExit):
        main(["compare", "--symbol", "MSFT", "--date", "2023-05-12",
              "--reference", "nope"])


def test_bad_config_is_reported_without_a_traceback(capsys, monkeypatch):
    for key in ("ARCTICDB_S3_ENDPOINT", "ARCTICDB_S3_BUCKET",
                "ARCTICDB_S3_ACCESS", "ARCTICDB_S3_SECRET"):
        monkeypatch.delenv(key, raising=False)
    code = main(["compare", "--symbol", "MSFT", "--date", "2023-05-12"])
    assert code == 1
    assert "ARCTICDB_S3_ENDPOINT" in capsys.readouterr().err
```

- [ ] **Step 2: Run to verify failure**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_cli.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'tick_lab.cli'`

- [ ] **Step 3: Implement `tick-lab/tick_lab/cli.py`**

```python
"""tick-lab: load FirstRate ticks into the shared store, and check derived bars.

    tick-lab load  ./FirstRate_sample.zip
    tick-lab compare --symbol MSFT --date 2023-05-12
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

from tick_lab.config import ConfigError, from_env
from tick_lab.firstrate import FirstRateFormatError, parse
from tick_lab.reference.base import ReferenceError, fetch_finest
from tick_lab.reference.yfinance_adapter import YFinanceAdapter
from tick_lab.report import compare as compare_frames
from tick_lab.rollup import aggregate, to_minute_bars
from tick_lab.store import TickStore

# Later releases add entries here; the CLI needs no other change.
ADAPTERS = {
    "yfinance": YFinanceAdapter,
}

TRADE_LIBRARY = "ticks"
QUOTE_LIBRARY = "quotes"

_NAME = re.compile(r"^([A-Za-z0-9.\-]+)_(trade|quote)s?\b", re.IGNORECASE)


def symbol_from_filename(name: str) -> tuple[str, str]:
    """Derive (SYMBOL, kind) from a FirstRate file name."""
    match = _NAME.match(Path(name).name)
    if not match:
        raise ValueError(
            f"cannot tell the symbol and kind from {name!r}; "
            "expected something like MSFT_trades_2023-05-12.txt"
        )
    return match.group(1).upper(), match.group(2).lower()


def _iter_members(path: Path):
    """Yield (name, text) for each data file, from a zip or a directory."""
    if path.is_dir():
        for child in sorted(path.iterdir()):
            if child.is_file() and not child.name.startswith("_"):
                yield child.name, child.read_text()
        return

    with zipfile.ZipFile(path) as archive:
        for info in sorted(archive.infolist(), key=lambda i: i.filename):
            name = Path(info.filename).name
            if info.is_dir() or name.startswith(("_", ".")):
                continue
            yield name, archive.read(info).decode("utf-8")


def cmd_load(args) -> int:
    store = TickStore(from_env())
    failures = 0

    for name, text in _iter_members(Path(args.path)):
        try:
            symbol, kind_from_name = symbol_from_filename(name)
            kind, frame = parse(text)
        except (ValueError, FirstRateFormatError) as err:
            print(f"  SKIP {name}: {err}", file=sys.stderr)
            failures += 1
            continue

        if kind != kind_from_name:
            print(
                f"  SKIP {name}: filename says {kind_from_name}, contents look like {kind}",
                file=sys.stderr,
            )
            failures += 1
            continue

        library = TRADE_LIBRARY if kind == "trade" else QUOTE_LIBRARY
        if args.dry_run:
            print(f"  would write {len(frame):>8,} {kind} rows -> {library}/{symbol}")
            continue

        store.write(library, symbol, frame, metadata={"source": name, "kind": kind})
        print(f"  wrote {len(frame):>8,} {kind} rows -> {library}/{symbol}")

    return 1 if failures else 0


def cmd_compare(args) -> int:
    cfg = from_env()
    store = TickStore(cfg)

    trades = store.read(TRADE_LIBRARY, args.symbol, start=args.date, end=args.date)
    if trades.empty:
        print(
            f"no ticks stored for {args.symbol} on {args.date} "
            f"(library {TRADE_LIBRARY!r}) — run `tick-lab load` first",
            file=sys.stderr,
        )
        return 1

    ours_1m = to_minute_bars(trades, session=args.session)
    print(f"rolled {len(trades):,} ticks into {len(ours_1m):,} 1-minute bars "
          f"(session={args.session})")

    adapter = ADAPTERS[args.reference]()
    print(f"asking {adapter.name} for 1m bars...")

    try:
        result = fetch_finest(adapter, args.symbol, args.date, args.end or args.date)
    except ReferenceError as err:
        print(f"\n{adapter.name} cannot serve this window: {err.kind}", file=sys.stderr)
        print(f"  {err.detail}", file=sys.stderr)
        return 1

    notes = []
    for attempt in result.attempts:
        if attempt.error is not None:
            print(f"  {attempt.interval}: {attempt.error.kind} — {attempt.error.detail}")
    if result.stepped_down:
        notes.append(
            f"{adapter.name} could not serve 1m for this window; "
            f"compared at {result.interval} instead"
        )
        print(f"  falling back to {result.interval}")

    ours = aggregate(ours_1m, result.interval)
    report = compare_frames(
        ours, result.frame, args.symbol, result.interval,
        tolerance=args.tol, notes=notes,
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print()
        print(report.to_text())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tick-lab", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    load = sub.add_parser("load", help="load a FirstRate zip (or directory) into ArcticDB")
    load.add_argument("path", help="path to the .zip or an extracted directory")
    load.add_argument("--dry-run", action="store_true",
                      help="report what would be written, write nothing")
    load.set_defaults(func=cmd_load)

    comp = sub.add_parser("compare", help="compare stored ticks against a reference source")
    comp.add_argument("--symbol", required=True)
    comp.add_argument("--date", required=True, help="YYYY-MM-DD")
    comp.add_argument("--end", help="end date; defaults to --date")
    comp.add_argument("--reference", default="yfinance", choices=sorted(ADAPTERS))
    comp.add_argument("--session", default="regular", choices=["regular", "all"])
    comp.add_argument("--tol", type=float, default=0.01,
                      help="price tolerance in the instrument's units (default 0.01)")
    comp.add_argument("--json", action="store_true")
    comp.set_defaults(func=cmd_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return 2
    try:
        return args.func(args)
    except ConfigError as err:
        print(f"configuration error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd tick-lab && .venv/bin/python -m pytest -q && .venv/bin/python -m ruff check tick_lab
```

Expected: all tests pass (config, firstrate, rollup, store, reference, report, cli), ruff clean.

- [ ] **Step 5: Commit**

```bash
git add tick-lab/tick_lab/cli.py tick-lab/tests/test_cli.py
git commit -m "feat(tick-lab): load and compare subcommands"
```

---

### Task 15: Provider parity — the golden bars

Success criterion 5: the bars `tick-lab` computes by hand must equal what `provider="arcticdb", interval="1m"` returns. Both sides are pinned to one committed artifact so neither environment has to import the other.

**Files:**
- Create: `tick-lab/tests/fixtures/golden_1m_bars.csv`, `tick-lab/tests/test_golden_parity.py`, `tests/integration/test_provider_parity.py`, `tests/integration/README.md`

**Interfaces:**
- Consumes: `tick_lab.rollup.to_minute_bars`, `tick_lab.firstrate.parse`.
- Produces: `tick-lab/tests/fixtures/golden_1m_bars.csv` — the authoritative expected 1-minute bars for `MSFT_trades_sample.txt` under `--session regular`. Columns: `timestamp` (ISO-8601 UTC), `open`, `high`, `low`, `close`, `volume`.

- [ ] **Step 1: Generate the golden file and read it before trusting it**

```bash
cd tick-lab && .venv/bin/python - <<'PY'
from pathlib import Path
from tick_lab.firstrate import parse
from tick_lab.rollup import to_minute_bars

text = Path("tests/fixtures/MSFT_trades_sample.txt").read_text()
_, trades = parse(text)
bars = to_minute_bars(trades, session="regular")
bars.index.name = "timestamp"
bars.to_csv("tests/fixtures/golden_1m_bars.csv")
print(bars)
PY
```

Inspect the printed frame against `MSFT_trades_sample.txt` by hand before continuing. It must contain **exactly three bars**:

| UTC | from ticks | open / high / low / close | volume |
|---|---|---|---|
| 13:30 | 09:30:00, 09:30:30, 09:30:59 ET | 310.55 / 310.65 / 310.00 / 310.00 | 1000 |
| 13:31 | 09:31:00 ET | 310.03 / 310.03 / 310.03 / 310.03 | 150 |
| 19:59 | 15:59:30 ET | 308.90 / 308.90 / 308.90 / 308.90 | 400 |

The 09:15 pre-market tick and the 16:00:00 closing print must both be **absent** under `--session regular`. **If it does not match, the bug is in Task 10, not in the fixture — do not edit the fixture to make the test pass.**

- [ ] **Step 2: Write the parity test for the tick-lab side**

`tick-lab/tests/test_golden_parity.py`:

```python
"""tick-lab's roll-up must match the committed golden bars.

The same CSV is asserted against by tests/integration/test_provider_parity.py,
which runs provider="arcticdb" inside the Platform image. Pinning both sides to
one artifact is what makes the two environments comparable without either
importing the other.
"""

from pathlib import Path

import pandas as pd

from tick_lab.firstrate import parse
from tick_lab.rollup import to_minute_bars

FIXTURES = Path(__file__).parent / "fixtures"


def load_golden() -> pd.DataFrame:
    golden = pd.read_csv(FIXTURES / "golden_1m_bars.csv", index_col="timestamp")
    golden.index = pd.DatetimeIndex(golden.index).tz_convert("UTC")
    golden.index.name = None
    return golden


def test_rollup_matches_the_golden_bars():
    _, trades = parse((FIXTURES / "MSFT_trades_sample.txt").read_text())
    bars = to_minute_bars(trades, session="regular")
    bars.index.name = None
    pd.testing.assert_frame_equal(bars, load_golden(), check_like=True)


def test_golden_excludes_premarket_and_the_closing_print():
    golden = load_golden()
    assert pd.Timestamp("2023-05-12 13:15:00Z") not in golden.index
    assert pd.Timestamp("2023-05-12 20:00:00Z") not in golden.index
```

- [ ] **Step 3: Run it**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_golden_parity.py -q
```

Expected: 2 passed.

- [ ] **Step 4: Write the in-image provider parity test**

`tests/integration/test_provider_parity.py`:

```python
"""provider="arcticdb" must return the same 1-minute bars tick-lab computes.

Runs INSIDE the Platform image (it needs openbb + the arcticdb extension) and
asserts against the same golden CSV tick-lab asserts against. Requires a
reachable ArcticDB store configured through ARCTICDB_S3_* and the fixture
ticks already loaded into library 'ticks' as symbol 'MSFT'.

    docker compose run --rm openbb-api python -m pytest /workspace/tests/integration -q
"""

import os
from pathlib import Path

import pandas as pd
import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("TICK_LAB_TEST_S3") != "1",
    reason="needs a reachable ArcticDB store; see tests/integration/README.md",
)

GOLDEN = Path(__file__).resolve().parents[2] / "tick-lab/tests/fixtures/golden_1m_bars.csv"


def load_golden() -> pd.DataFrame:
    golden = pd.read_csv(GOLDEN, index_col="timestamp")
    golden.index = pd.DatetimeIndex(golden.index).tz_convert("UTC")
    return golden


def test_provider_returns_the_golden_bars():
    from openbb import obb

    result = obb.equity.price.historical(
        "MSFT",
        provider="arcticdb",
        library="ticks",
        interval="1m",
        start_date="2023-05-12",
        end_date="2023-05-12",
    )
    got = result.to_df()

    golden = load_golden()
    assert len(got) == len(golden), (
        f"provider returned {len(got)} bars, golden has {len(golden)}"
    )
    for column in ("open", "high", "low", "close"):
        pd.testing.assert_series_equal(
            got[column].reset_index(drop=True),
            golden[column].reset_index(drop=True),
            check_names=False,
        )
```

`tests/integration/README.md`:

```markdown
# Integration tests

These need a running store, so they are skipped unless `TICK_LAB_TEST_S3=1`.

    # 1. bring up the stack (MinIO node included)
    docker compose up -d

    # 2. load the fixture ticks from your machine
    cd tick-lab && .venv/bin/tick-lab load tests/fixtures/

    # 3. run the parity test inside the image
    cd .. && docker compose run --rm \
      -e TICK_LAB_TEST_S3=1 \
      openbb-api python -m pytest /workspace/tests/integration -q
```

- [ ] **Step 5: Verify the test collects and skips cleanly**

```bash
cd /Users/artcashin/Developer/openbb-docker-ep11 && python -m pytest tests/integration -q
```

Expected: 1 skipped (no error). The live run is the manual procedure in the README above.

- [ ] **Step 6: Commit**

```bash
git add tick-lab/tests/fixtures/golden_1m_bars.csv tick-lab/tests/test_golden_parity.py tests/integration
git commit -m "test: pin tick-lab and provider=arcticdb to the same golden bars"
```

---

### Task 16: Documentation

**Files:**
- Create: `tick-lab/README.md`, `docs/arcticdb-minio-design.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: no code.

- [ ] **Step 1: Write `tick-lab/README.md`**

Cover, in this order: what it does; install (`python -m venv .venv && .venv/bin/pip install -e '.[dev]'`); `cp .env.example .env` and where the values come from (`minio.env` — the same names); getting the free FirstRate sample from <https://firstratedata.com/tick-data> and that **it is not committed here** because it is third-party licensed data; `tick-lab load ./sample.zip --dry-run` then without; `tick-lab compare --symbol MSFT --date 2023-05-12`; the `--session` explanation from D9; `--tol` units; `--json`; and the running-the-tests section including the `TICK_LAB_TEST_S3=1` MinIO recipe from Task 11 Step 5.

Include the expected v11.0.0 output shape verbatim, so a reader knows what success looks like:

```
rolled 78,412 ticks into 390 1-minute bars (session=regular)
asking yfinance for 1m bars...
  1m: retention — $MSFT: possibly delisted; no price data found (Yahoo error =
      "1m data not available ... must be within the last 30 days.")
  ...
  falling back to 1d
```

- [ ] **Step 2: Write `docs/arcticdb-minio-design.md`**

The reader-facing counterpart to the spec — the spec is the process artifact, this is the published one. Cover: why MinIO is its own node rather than a Serve route (SigV4 signs Host); why the cert renewal lives inside the container and signals with SIGHUP (with the measured result: no reload on disk change, reload on SIGHUP with uptime preserved); the `ARCTICDB_S3_*` convention and why one file feeds both the container and the laptop; the amd64 pin and what Apple Silicon readers should expect; and the documented limit that `:9000` is reachable from the Docker bridge as well as the tailnet.

- [ ] **Step 3: Update the root `README.md`**

Add to the release table:

```markdown
| v11.0.0 | Ep. 11 — The Shared Store | MinIO as its own tailnet node + ArcticDB (`provider="arcticdb"`) + `tick-lab` |
```

Add a "New in v11.0.0 (Ep. 11)" section above the v10.0.0 one, matching the existing voice, covering: the MinIO node and its TLS; `provider="arcticdb"`; `tick-lab` and the FirstRate sample; and **an explicit Apple Silicon note** — the image is amd64 because ArcticDB ships no aarch64 Linux wheels, so it runs under emulation on M-series Macs and will build and query more slowly; `tick-lab` itself is unaffected because macOS arm64 wheels exist.

Update the Quick start to include:

```bash
cp minio.env.example minio.env     # REQUIRED for the store; chmod 600
```

and the verification step to `scripts/verify-isolation.sh openbb.<your-tailnet>.ts.net minio.<your-tailnet>.ts.net`.

- [ ] **Step 4: Run the scrub gate**

```bash
bash scripts/scrub-check.sh
```

Expected: `Scrub check passed.`

- [ ] **Step 5: Commit**

```bash
git add README.md docs/arcticdb-minio-design.md tick-lab/README.md
git commit -m "docs: Ep. 11 -- the shared store, tick-lab, and the amd64 caveat"
```

---

### Task 17: Tag v11.0.0

- [ ] **Step 1: Run the whole test suite**

```bash
cd /Users/artcashin/Developer/openbb-docker-ep11
(cd openbb-arcticdb && .venv/bin/python -m pytest -q)
(cd tick-lab && .venv/bin/python -m pytest -q)
python -m pytest minio/tests tests/integration -q
bash scripts/scrub-check.sh
```

Expected: every suite passes; scrub check passes. **Do not proceed on a failure.**

- [ ] **Step 2: Build the image one more time**

```bash
docker build --platform linux/amd64 -t openbb-local:11.0.0 .
```

Expected: ends with `OpenBB Platform OK: N providers (incl. eodhd, kdb, arcticdb)`.

- [ ] **Step 3: Rebase onto the final Ep. 10 tip**

```bash
git fetch origin && git rebase ep10-kdb-cache
```

Resolve conflicts in `Dockerfile`, `docker-compose.yml` and `openbb-kdb/pyproject.toml` — Ep. 10's `kdb-store` split touches the last of these. Re-run Step 1 after rebasing.


- [ ] **Step 4: (obsolete — no longer applicable)**

This step used to verify that the `kdbx` builder stage was pinned to a
multi-arch tag and that the bundled `q` executed, because Ep. 11's
`platform: linux/amd64` pin made a wrong-architecture q certain.

**Ep. 10's final form removed the kdb-x stage entirely.** The image now ships
**no KX software at all** — KX's licence does not permit redistributing their
q binary, so the operator supplies their own at runtime. There is no bundled
q left to check, and grepping for a `kdb-x` pin would fail against a Dockerfile
that correctly no longer has one.

Nothing to do here. Step 2's build already asserts the providers register.

- [ ] **Step 5: Tag**

```bash
git tag -a v11.0.0 -m "v11.0.0: the shared store — MinIO node, ArcticDB provider, tick-lab (Adventures in OpenBB, Ep. 11)"
```

---

### Task 18: v11.1.0 — the EODHD REST adapter

The comparison becomes per-minute: EODHD serves 1m history for 2023, and the key stays on the server.

**Files:**
- Create: `tick-lab/tick_lab/reference/eodhd_api.py`, `tick-lab/tests/test_eodhd_api.py`
- Modify: `tick-lab/tick_lab/cli.py` (registry + config), `tick-lab/.env.example`, `tick-lab/pyproject.toml` (version), `README.md`

**Interfaces:**
- Consumes: `tick_lab.reference.base`.
- Produces: `tick_lab.reference.eodhd_api.EodhdApiAdapter(base_url: str, username: str, password: str)` with `name = "eodhd-api"`, `supported_intervals = ("1m", "5m", "1h", "1d")`, and `fetch(...)`. Also `tick_lab.reference.eodhd_api.from_env() -> EodhdApiAdapter` reading `OPENBB_URL`, `OPENBB_API_USERNAME`, `OPENBB_API_PASSWORD`.

- [ ] **Step 1: Write the failing tests**

`tick-lab/tests/test_eodhd_api.py`:

```python
"""The OpenBB REST path: classify HTTP failures, normalise the payload."""

import pytest

from tick_lab.reference.base import ReferenceError
from tick_lab.reference.eodhd_api import EodhdApiAdapter, classify_status


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def adapter():
    return EodhdApiAdapter("https://openbb.example.ts.net", "user", "pass")


@pytest.mark.parametrize(
    "status,kind",
    [(401, "auth"), (403, "entitlement"), (404, "not_covered"), (500, "transport")],
)
def test_status_codes_are_classified(status, kind):
    assert classify_status(status, "").kind == kind


def test_results_are_normalised_to_bar_columns(monkeypatch):
    payload = {"results": [
        {"date": "2023-05-12T13:30:00", "open": 310.55, "high": 310.65,
         "low": 310.0, "close": 310.03, "volume": 446343},
    ]}
    a = adapter()
    monkeypatch.setattr(a, "_get", lambda *args, **kw: FakeResponse(200, payload))
    out = a.fetch("MSFT", "2023-05-12", "2023-05-12", "1m")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert str(out.index.tz) == "UTC"
    assert out.iloc[0].close == 310.03


def test_empty_results_are_classified_as_empty(monkeypatch):
    a = adapter()
    monkeypatch.setattr(a, "_get", lambda *args, **kw: FakeResponse(200, {"results": []}))
    with pytest.raises(ReferenceError) as exc:
        a.fetch("MSFT", "2023-05-12", "2023-05-12", "1m")
    assert exc.value.kind == "empty"


def test_forbidden_becomes_an_entitlement_error(monkeypatch):
    a = adapter()
    monkeypatch.setattr(a, "_get", lambda *args, **kw: FakeResponse(403, text="Forbidden"))
    with pytest.raises(ReferenceError) as exc:
        a.fetch("GOOG", "2023-05-12", "2023-05-12", "1m")
    assert exc.value.kind == "entitlement"
    assert "GOOG" in exc.value.detail
```

- [ ] **Step 2: Run to verify failure**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_eodhd_api.py -q
```

Expected: FAIL — no module `tick_lab.reference.eodhd_api`

- [ ] **Step 3: Implement `tick-lab/tick_lab/reference/eodhd_api.py`**

```python
"""EODHD through this stack's OpenBB REST API.

The point of the round trip: the EODHD key lives on the server, so this laptop
holds no provider credential and needs no OpenBB install -- just a URL and the
Basic-auth pair that already guards the API.
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd

from tick_lab.reference.base import ReferenceError

_SUPPORTED = ("1m", "5m", "1h", "1d")
_COLUMNS = ["open", "high", "low", "close", "volume"]

_PATH = "/api/v1/equity/price/historical"


def classify_status(status: int, body: str) -> ReferenceError:
    """Map an HTTP status onto a classified ReferenceError."""
    if status == 401:
        return ReferenceError("auth", f"401 from the OpenBB API — check Basic auth ({body[:120]})")
    if status == 403:
        return ReferenceError(
            "entitlement",
            f"403 Forbidden — the provider plan does not cover this request ({body[:120]})",
        )
    if status == 404:
        return ReferenceError("not_covered", f"404 — symbol or route not found ({body[:120]})")
    return ReferenceError("transport", f"HTTP {status} from the OpenBB API ({body[:120]})")


class EodhdApiAdapter:
    name = "eodhd-api"
    supported_intervals = _SUPPORTED

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self._auth = (username, password)

    def _get(self, url: str, params: dict) -> Any:
        import requests

        return requests.get(url, params=params, auth=self._auth, timeout=60)

    def fetch(self, symbol: str, start: Any, end: Any, interval: str) -> pd.DataFrame:
        response = self._get(
            f"{self.base_url}{_PATH}",
            {
                "symbol": symbol,
                "provider": "eodhd",
                "interval": interval,
                "start_date": str(start),
                "end_date": str(end),
            },
        )
        if response.status_code != 200:
            err = classify_status(response.status_code, response.text)
            raise ReferenceError(err.kind, f"{symbol} at {interval}: {err.detail}")

        rows = (response.json() or {}).get("results") or []
        if not rows:
            raise ReferenceError(
                "empty", f"the OpenBB API returned no rows for {symbol} at {interval}"
            )

        frame = pd.DataFrame(rows)
        index = pd.DatetimeIndex(pd.to_datetime(frame.pop("date")))
        frame.index = (
            index.tz_convert("UTC") if index.tz is not None else index.tz_localize("UTC")
        )
        missing = [c for c in _COLUMNS if c not in frame.columns]
        if missing:
            raise ReferenceError(
                "transport", f"response is missing column(s): {', '.join(missing)}"
            )
        return frame[_COLUMNS].sort_index()


def from_env() -> EodhdApiAdapter:
    """Build the adapter from OPENBB_URL / OPENBB_API_USERNAME / OPENBB_API_PASSWORD."""
    url = os.getenv("OPENBB_URL")
    user = os.getenv("OPENBB_API_USERNAME")
    password = os.getenv("OPENBB_API_PASSWORD")
    if not (url and user and password):
        raise ReferenceError(
            "auth",
            "set OPENBB_URL, OPENBB_API_USERNAME and OPENBB_API_PASSWORD to use "
            "--reference eodhd-api",
        )
    return EodhdApiAdapter(url, user, password)
```

- [ ] **Step 4: Register it in the CLI**

In `tick-lab/tick_lab/cli.py`, add the import:

```python
from tick_lab.reference.eodhd_api import from_env as eodhd_api_from_env
```

and extend the registry:

```python
ADAPTERS = {
    "yfinance": YFinanceAdapter,
    "eodhd-api": eodhd_api_from_env,
}
```

Change the `--reference` default from `"yfinance"` to `"eodhd-api"`, and in `cmd_compare` wrap adapter construction so a missing URL is reported cleanly:

```python
    try:
        adapter = ADAPTERS[args.reference]()
    except ReferenceError as err:
        print(f"cannot use --reference {args.reference}: {err.detail}", file=sys.stderr)
        return 1
```

- [ ] **Step 5: Extend `.env.example`**

Append:

```bash
# --- for --reference eodhd-api (v11.1.0) ---
# The EODHD key stays on the server; this laptop only needs the API's own
# Basic-auth pair from api-auth.env.
OPENBB_URL=https://openbb.<your-tailnet>.ts.net
OPENBB_API_USERNAME=
OPENBB_API_PASSWORD=
```

- [ ] **Step 6: Run all tests**

```bash
cd tick-lab && .venv/bin/python -m pytest -q && .venv/bin/python -m ruff check tick_lab
```

Expected: all pass, ruff clean.

- [ ] **Step 7: Verify against the live stack**

```bash
cd tick-lab && set -a && . ./.env && set +a && \
  .venv/bin/tick-lab compare --symbol MSFT --date 2023-05-12 --reference eodhd-api
```

Expected: ~390 bars compared at `1m`, no fallback line, and a largest-close discrepancy reported. Record the actual numbers in the commit message — they are the chapter's headline result.

- [ ] **Step 8: Bump the version, update the README, commit and tag**

Set `version = "11.1.0"` in `tick-lab/pyproject.toml`, add a v11.1.0 row to the README release table, then:

```bash
git add tick-lab README.md
git commit -m "feat(tick-lab): compare against EODHD through the stack's REST API"
git tag -a v11.1.0 -m "v11.1.0: per-minute comparison via the OpenBB API (Ep. 11)"
```

---

### Task 19: v11.1.1 — the in-process OpenBB adapter

Included for teaching: the same call made locally, and what it costs. The demo key covers MSFT but **not** GOOG, so this is also where the entitlement error path gets demonstrated (success criterion 7).

**Files:**
- Create: `tick-lab/tick_lab/reference/eodhd_local.py`, `tick-lab/tests/test_eodhd_local.py`
- Modify: `tick-lab/tick_lab/cli.py`, `tick-lab/pyproject.toml`, `tick-lab/README.md`, `README.md`

**Interfaces:**
- Consumes: `tick_lab.reference.base`.
- Produces: `tick_lab.reference.eodhd_local.EodhdLocalAdapter` with `name = "eodhd-local"`, `supported_intervals = ("1m", "5m", "1h", "1d")`, and `fetch(...)`; plus `classify_exception(err: Exception) -> ReferenceError`.

- [ ] **Step 1: Write the failing tests**

`tick-lab/tests/test_eodhd_local.py`:

```python
"""In-process OpenBB: the same call, locally -- including the 403 path."""

import pandas as pd
import pytest

from tick_lab.reference.base import ReferenceError
from tick_lab.reference.eodhd_local import EodhdLocalAdapter, classify_exception


def test_forbidden_is_classified_as_entitlement():
    err = classify_exception(RuntimeError("403 Client Error: Forbidden for url: ..."))
    assert err.kind == "entitlement"


def test_unauthorized_is_classified_as_auth():
    assert classify_exception(RuntimeError("401 Unauthorized")).kind == "auth"


def test_empty_message_is_classified_as_empty():
    assert classify_exception(RuntimeError("no data found")).kind == "empty"


def test_missing_openbb_is_reported_actionably(monkeypatch):
    adapter = EodhdLocalAdapter()

    def boom(*_args, **_kwargs):
        raise ImportError("No module named 'openbb'")

    monkeypatch.setattr(adapter, "_historical", boom)
    with pytest.raises(ReferenceError) as exc:
        adapter.fetch("MSFT", "2023-05-12", "2023-05-12", "1m")
    assert exc.value.kind == "transport"
    assert "pip install" in exc.value.detail


def test_frame_is_normalised(monkeypatch):
    adapter = EodhdLocalAdapter()
    frame = pd.DataFrame(
        {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10]},
        index=pd.DatetimeIndex(["2023-05-12 13:30:00"], tz="UTC"),
    )
    monkeypatch.setattr(adapter, "_historical", lambda *a, **k: frame)
    out = adapter.fetch("MSFT", "2023-05-12", "2023-05-12", "1m")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert str(out.index.tz) == "UTC"
```

- [ ] **Step 2: Run to verify failure**

```bash
cd tick-lab && .venv/bin/python -m pytest tests/test_eodhd_local.py -q
```

Expected: FAIL — no module `tick_lab.reference.eodhd_local`

- [ ] **Step 3: Implement `tick-lab/tick_lab/reference/eodhd_local.py`**

```python
"""EODHD via an in-process OpenBB Platform.

This is the same request as eodhd_api, made locally. It is here because seeing
both is instructive -- and because the difference is the point: this path needs
openbb installed here AND the provider key present here, which is exactly what
the server-side path spares you.

The committed demo key covers MSFT and not GOOG, so a GOOG request is expected
to surface a classified entitlement error rather than an empty frame.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from tick_lab.reference.base import ReferenceError

_SUPPORTED = ("1m", "5m", "1h", "1d")
_COLUMNS = ["open", "high", "low", "close", "volume"]


def classify_exception(err: Exception) -> ReferenceError:
    """Classify whatever the provider stack raised."""
    message = str(err)
    lowered = message.lower()
    if "403" in message or "forbidden" in lowered:
        return ReferenceError(
            "entitlement",
            f"403 Forbidden — this API key's plan does not cover the request ({message[:160]})",
        )
    if "401" in message or "unauthorized" in lowered or "invalid api" in lowered:
        return ReferenceError("auth", f"credentials rejected ({message[:160]})")
    if "no module named" in lowered:
        return ReferenceError(
            "transport",
            "openbb is not installed in this environment — "
            "pip install 'openbb' 'openbb-eodhd' to use --reference eodhd-local",
        )
    return ReferenceError("empty", message[:200])


class EodhdLocalAdapter:
    name = "eodhd-local"
    supported_intervals = _SUPPORTED

    def _historical(self, symbol: str, start: Any, end: Any, interval: str) -> pd.DataFrame:
        from openbb import obb

        return obb.equity.price.historical(
            symbol,
            provider="eodhd",
            interval=interval,
            start_date=str(start),
            end_date=str(end),
        ).to_df()

    def fetch(self, symbol: str, start: Any, end: Any, interval: str) -> pd.DataFrame:
        try:
            frame = self._historical(symbol, start, end, interval)
        except Exception as err:
            classified = classify_exception(err)
            raise ReferenceError(
                classified.kind, f"{symbol} at {interval}: {classified.detail}"
            ) from err

        if frame is None or frame.empty:
            raise ReferenceError("empty", f"openbb returned no rows for {symbol} at {interval}")

        missing = [c for c in _COLUMNS if c not in frame.columns]
        if missing:
            raise ReferenceError(
                "transport", f"result is missing column(s): {', '.join(missing)}"
            )

        index = pd.DatetimeIndex(frame.index)
        frame.index = (
            index.tz_convert("UTC") if index.tz is not None else index.tz_localize("UTC")
        )
        return frame[_COLUMNS].sort_index()
```

- [ ] **Step 4: Register it**

In `tick-lab/tick_lab/cli.py`:

```python
from tick_lab.reference.eodhd_local import EodhdLocalAdapter
```

```python
ADAPTERS = {
    "yfinance": YFinanceAdapter,
    "eodhd-api": eodhd_api_from_env,
    "eodhd-local": EodhdLocalAdapter,
}
```

Leave the default at `eodhd-api`.

- [ ] **Step 5: Run all tests**

```bash
cd tick-lab && .venv/bin/python -m pytest -q && .venv/bin/python -m ruff check tick_lab
```

Expected: all pass, ruff clean.

- [ ] **Step 6: Verify both live paths**

```bash
cd tick-lab && .venv/bin/pip install -q 'openbb' 'openbb-eodhd'
EODHD_API_KEY=OeAFFmMliFG5orCUuwAKQ8l4WWFQ67YX \
  .venv/bin/tick-lab compare --symbol MSFT --date 2023-05-12 --reference eodhd-local
EODHD_API_KEY=OeAFFmMliFG5orCUuwAKQ8l4WWFQ67YX \
  .venv/bin/tick-lab compare --symbol GOOG --date 2023-05-12 --reference eodhd-local
```

Expected: MSFT produces a per-minute comparison; **GOOG exits 1 with a classified `entitlement` message naming the 403** — success criterion 7. If GOOG returns data instead, EODHD has changed what the demo key covers: say so in the README rather than quietly dropping the example.

- [ ] **Step 7: Document, bump, commit and tag**

Set `version = "11.1.1"`. Add to `tick-lab/README.md` a short "Three ways to ask the same question" section contrasting the adapters, including the extra install and the key-on-your-laptop cost of `eodhd-local`, and the GOOG entitlement example with its real output. Add a v11.1.1 row to the root README table.

```bash
git add tick-lab README.md
git commit -m "feat(tick-lab): in-process OpenBB adapter, and the entitlement error it surfaces"
git tag -a v11.1.1 -m "v11.1.1: the in-process OpenBB path (Ep. 11)"
```

---

## Plan self-review

**Spec coverage.** Every spec section maps to a task: D1/D2 → Tasks 4–5; D3 → Task 3; D4 → Task 5; D5 → Task 2; D6 → Tasks 1 and 6; D7 → Tasks 5–6 and the README note in Task 16; D8 → Tasks 9 and 11; D9 → Task 10. Success criteria 1–3 → Tasks 5–7 plus the manual procedure in Task 15; criterion 4 → Task 14; criterion 5 → Task 15; criteria 6–7 → Tasks 18–19. Criterion 8 (a cert rotation mid-run must not interrupt S3 traffic) is covered by Task 3's `test_hups_when_cert_changes` at the unit level; the live rotation remains the manual check the spec's risk section calls for.

**Known gap, stated rather than hidden.** Task 3 tests cert renewal against a stubbed `tailscale` and a stub child process, not against real MinIO under load. The spec flags this as the least-tested path because it only fires every ~60 days; the plan does not close that gap, and pretending otherwise would be worse than saying so.

**Interval ladder.** `to_minute_bars` always produces 1-minute bars; `aggregate` steps them up to whatever interval the reference actually served. `INTERVAL_LADDER` and `rollup._RULES` cover the same six intervals, so any interval `fetch_finest` can return is one `aggregate` accepts.

---

### Task 20: Rework — run tailscaled inside the MinIO container

**Supersedes the architecture of Tasks 4 and 5.** Execute this immediately after Task 5; Tasks 6+ are unaffected.

**Why.** Tasks 4–5 gave MinIO a Tailscale *sidecar* and shared only `/var/lib/tailscale` between them. That cannot work: `tailscaled`'s socket lives at `/tmp/tailscaled.sock` inside the sidecar's own filesystem (`/var/run/tailscale/tailscaled.sock` is a symlink to it — verified on a live sidecar), and `network_mode` shares the network namespace, **not the filesystem**. MinIO's `tailscale` CLI therefore could never reach the daemon: the entrypoint would spin for 60s and exit 1, and `minio-init` would exhaust its retries. Rather than relocate the socket, the daemon moves in with the process that needs it — the CLI, the daemon, and the process to SIGHUP now share one filesystem and one lifecycle.

This node is still its own tailnet node (design decision D1 is intact); it simply has no sidecar.

**Files:**
- Modify: `minio/Dockerfile`, `minio/entrypoint.sh`, `docker-compose.yml`
- Unchanged: `minio/cert-sync.sh` and its tests — with `tailscaled` listening on the CLI's default socket path, the bare `tailscale cert` call in that script works as written. Do not modify it.

**Interfaces:**
- Consumes: `minio/cert-sync.sh` (unchanged contract: `cert-sync.sh <cert-dir> <domain> <pid-file>`).
- Produces: a single `minio` service that joins the tailnet itself. Env it honours: `TS_AUTHKEY` (required, from `ts.env`), `TS_HOSTNAME` (default `minio`), `TS_STATE_DIR` (default `/var/lib/tailscale`), `MINIO_CERT_DOMAIN` (defaults from `ARCTICDB_S3_ENDPOINT`), `MINIO_CERT_DIR`, `MINIO_CERT_RENEW_SECONDS`.

- [ ] **Step 1: Add the daemon to the image**

Replace `minio/Dockerfile` with:

```dockerfile
# MinIO joined to the tailnet, serving S3 over a Tailscale-issued certificate.
#
# tailscaled runs in THIS container rather than a sidecar. Its control socket
# is a file, and network_mode shares only the network namespace -- so a sidecar
# daemon is unreachable from here no matter what is mounted. Co-locating the
# daemon, the CLI, and the process that must be SIGHUP'd on renewal makes all
# three share one filesystem and one lifecycle.
FROM tailscale/tailscale:v1.98.9 AS ts

FROM quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z

COPY --from=ts /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY --from=ts /usr/local/bin/tailscale /usr/local/bin/tailscale
COPY cert-sync.sh /usr/local/bin/cert-sync.sh
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/cert-sync.sh /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/data"]
```

- [ ] **Step 2: Rewrite the entrypoint to own the daemon**

Replace `minio/entrypoint.sh` with:

```sh
#!/bin/sh
# Join the tailnet, obtain a certificate, serve S3 over TLS, keep the
# certificate fresh -- all in one container.
#
# tailscaled listens on the CLI's DEFAULT socket path, which is what lets
# cert-sync.sh call a bare `tailscale cert` with no --socket flag.
set -eu

CERT_DIR="${MINIO_CERT_DIR:-/root/.minio/certs}"
RENEW_SECONDS="${MINIO_CERT_RENEW_SECONDS:-43200}"
STATE_DIR="${TS_STATE_DIR:-/var/lib/tailscale}"
SOCKET=/var/run/tailscale/tailscaled.sock
NODE_NAME="${TS_HOSTNAME:-minio}"
PID_FILE=/run/minio.pid

# The certificate must be issued for this node's MagicDNS name, which is the
# same host ArcticDB clients connect to. Default from ARCTICDB_S3_ENDPOINT so
# minio.env stays the single source of truth: compose CANNOT interpolate an
# env_file value into the compose file, so this defaulting happens here.
DOMAIN="${MINIO_CERT_DOMAIN:-${ARCTICDB_S3_ENDPOINT:-}}"
if [ -z "$DOMAIN" ]; then
    echo "entrypoint: set ARCTICDB_S3_ENDPOINT (or MINIO_CERT_DOMAIN) in minio.env," \
         "e.g. minio.<your-tailnet>.ts.net" >&2
    exit 1
fi
if [ -z "${TS_AUTHKEY:-}" ]; then
    echo "entrypoint: TS_AUTHKEY must be set (it comes from ts.env)" >&2
    exit 1
fi

mkdir -p "$STATE_DIR" /var/run/tailscale "$CERT_DIR" /run

echo "entrypoint: starting tailscaled"
tailscaled --state="$STATE_DIR/tailscaled.state" --socket="$SOCKET" --tun=tailscale0 &
TAILSCALED_PID=$!

echo "entrypoint: waiting for tailscaled..."
i=0
until tailscale status --json >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
        echo "entrypoint: tailscaled did not come up within 60s" >&2
        exit 1
    fi
    sleep 1
done

# Idempotent: on a restart with persisted state this is a no-op re-auth.
echo "entrypoint: bringing up node $NODE_NAME"
tailscale up --authkey="$TS_AUTHKEY" --hostname="$NODE_NAME"

echo "entrypoint: obtaining certificate for $DOMAIN"
/usr/local/bin/cert-sync.sh "$CERT_DIR" "$DOMAIN" "$PID_FILE" || {
    echo "entrypoint: could not obtain a certificate; refusing to start plaintext" >&2
    exit 1
}

# Bind these BEFORE installing the trap. Under `set -u`, dereferencing an
# unset variable is a fatal error during word expansion -- it aborts before
# the `kill` and its `|| true` ever run. A signal arriving between the trap
# and the assignments below would otherwise kill the script on the unbound
# $RENEW_PID and never reach the tailscaled shutdown.
MINIO_PID=""
RENEW_PID=""

term() {
    [ -n "${MINIO_PID:-}" ] && kill -TERM "$MINIO_PID" 2>/dev/null || true
    [ -n "${MINIO_PID:-}" ] && wait "$MINIO_PID" 2>/dev/null || true
    [ -n "${RENEW_PID:-}" ] && kill -TERM "$RENEW_PID" 2>/dev/null || true
    # Unconditional: TAILSCALED_PID is always bound by this point, and the
    # daemon must come down even when the kills above were skipped.
    kill -TERM "$TAILSCALED_PID" 2>/dev/null || true
    exit 0
}
trap term TERM INT

minio server --certs-dir "$CERT_DIR" "$@" &
MINIO_PID=$!
echo "$MINIO_PID" > "$PID_FILE"

(
    while true; do
        sleep "$RENEW_SECONDS"
        /usr/local/bin/cert-sync.sh "$CERT_DIR" "$DOMAIN" "$PID_FILE" || true
    done
) &
RENEW_PID=$!

wait "$MINIO_PID"
```

- [ ] **Step 3: Collapse the two compose services into one**

In `docker-compose.yml`, **delete the entire `minio-ts:` service**, and replace the `minio:` service with:

```yaml
  # MinIO, joined to the tailnet as its own node named "minio". No sidecar:
  # tailscaled runs INSIDE this container because its control socket is a file
  # and network_mode shares only the network namespace -- see minio/Dockerfile.
  #
  # NOTHING is published to the host. :9000 (S3) and :9001 (console) exist on
  # the tailscale interface only.
  #
  # Before first start:
  #   cp minio.env.example minio.env && chmod 600 minio.env
  minio:
    build: ./minio
    image: openbb-minio:11.0.0
    platform: linux/amd64
    container_name: openbb-minio
    hostname: minio
    restart: unless-stopped
    env_file:
      # TS_AUTHKEY -- the same reusable tagged key the openbb node uses.
      - path: ./ts.env
        required: true
      - path: ./minio.env
        required: true
    environment:
      - TS_HOSTNAME=minio
      - TS_STATE_DIR=/var/lib/tailscale
      # NOTE: the cert domain is NOT set here. Compose interpolates ${...} from
      # the shell or a root .env only -- never from env_file -- so referencing
      # ARCTICDB_S3_ENDPOINT here would silently expand to empty. The entrypoint
      # reads it from its own environment instead, where env_file did put it.
      - MINIO_CERT_RENEW_SECONDS=43200
    command: ["/data", "--console-address", ":9001"]
    devices:
      - /dev/net/tun:/dev/net/tun
    cap_add:
      - NET_ADMIN
      - NET_RAW
    volumes:
      - minio-data:/data
      # Persist node identity across restarts, same as the openbb node's
      # ./ts-state -- otherwise every restart joins as a brand-new machine.
      - ./ts-state-minio:/var/lib/tailscale
```

Change `minio-init`'s `network_mode` to `service:minio` and its `depends_on` to `- minio`.

In the `volumes:` block, **remove `minio-ts-state`** and keep `minio-data`.

- [ ] **Step 4: Ignore the new state directory**

Add to `.gitignore` beside the existing `ts-state/` entry:

```
ts-state-minio/
```

- [ ] **Step 5: Build and re-verify fail-closed behaviour**

```bash
docker build --platform linux/amd64 -t openbb-minio:test ./minio
docker run --rm --platform linux/amd64 openbb-minio:test; echo "EXIT=$?"
docker run --rm --platform linux/amd64 -e ARCTICDB_S3_ENDPOINT=minio.example.ts.net openbb-minio:test; echo "EXIT=$?"
```

Expected: build succeeds. The first run exits 1 naming `ARCTICDB_S3_ENDPOINT`. The second run gets past the domain check and exits 1 naming `TS_AUTHKEY` — proving both guards fire in order and neither starts MinIO. Paste both transcripts verbatim.

- [ ] **Step 6: Confirm cert-sync tests still pass and nothing is published**

```bash
python3 -m pytest minio/tests -q
docker compose config | grep -c "published" || echo "0 published ports — correct"
```

Expected: 7 passed; no published ports.

- [ ] **Step 7: Commit**

```bash
git add minio docker-compose.yml .gitignore
git commit -m "fix(minio): run tailscaled in the container -- a sidecar's socket is unreachable"
```

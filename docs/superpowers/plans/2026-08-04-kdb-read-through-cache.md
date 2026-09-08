<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# kdb+ Read-Through Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `provider="kdb"` as an in-memory read-through cache over EODHD, so a chart zoomed from 1 year to 3 years fetches only the missing two years — once.

**Architecture:** A `q` process runs as a child of the openbb-api container on `127.0.0.1:5000`; PyKX connects to it as an unlicensed IPC client. The `openbb-kdb` extension keeps per-`(symbol, interval)` bar tables plus a coverage table of the ranges actually fetched, computes `requested − covered`, fetches only the gaps from an upstream provider resolved by name through OpenBB's registry, and returns the merged series with cache telemetry attached. A `cache-chart` FastAPI service serves a Workspace widget and a standalone Plotly page whose scroll gesture demonstrates the saving.

**Tech Stack:** Python 3.12, PyKX 3.2 (IPC/unlicensed mode), kdb-x q 5.0, OpenBB Platform provider extension API, FastAPI + uvicorn, plotly.js (vendored), pytest.

## Global Constraints

- **Design doc:** `docs/kdb-cache-design.md`. Every decision below traces to it.
- **The repo is public.** No license blob, real tailnet name, hostname, or key may enter it. `bash scripts/scrub-check.sh` must pass before every commit.
- **q binds `127.0.0.1:5000`, never `0.0.0.0`.** A default bind puts an unauthenticated q — arbitrary code execution — on the tailnet IP of every peer, because all services share the tailscale network namespace.
- **The cache never causes a failure.** Any kdb error (dead connection, missing license, absent q) degrades to upstream pass-through with `cache: "bypass"`.
- **The cache is memory-only.** No persistence, no reload on start.
- **`-w` is containment, not policy.** Crossing it kills q. q gets `-w` = `KDB_MEMORY_MB` × 1.25; eviction triggers at `KDB_CACHE_WATERMARK` × `KDB_MEMORY_MB` measured on `.Q.w[]` **`heap`** (not `used`).
- **`.Q.w[]` returns `str` keys** (`used`, `heap`, `wmax`, ...), verified against kdb-x 5.0.
- **`delete` does not free heap.** Every eviction is followed by `.Q.gc[]`.
- **CI requires no kdb license and no EODHD key.** All tests use a fake q connection and mocked HTTP.
- Config defaults: `KDB_EMBEDDED=true`, `KDB_HOST=127.0.0.1`, `KDB_PORT=5000`, `KDB_MEMORY_MB=8192`, `KDB_CACHE_WATERMARK=0.75`, `KDB_UPSTREAM=eodhd`.
- Style: follow `live-grid/` and `openbb-eodhd/` — lazy imports inside functions (`PLC0415` is ignored in ruff config), `line-length = 100`, tests under `tests/` with `pythonpath = ["."]`.

---

## File Structure

**`openbb-kdb/`** — the provider extension (packaged, installed into the image):

| File | Responsibility |
|---|---|
| `openbb_kdb/config.py` | Resolve env/credential config into a frozen `KdbConfig`. No I/O. |
| `openbb_kdb/ranges.py` | Pure date-range arithmetic: subtract, coalesce, tail-trim. No q, no I/O. |
| `openbb_kdb/session.py` | Own the q process and the IPC connection: spawn, connect, detect death, respawn. |
| `openbb_kdb/store.py` | All q statements: bars read/write, coverage read/write, LRU, memory stats, eviction. |
| `openbb_kdb/upstream.py` | Resolve an upstream provider by name via the registry and fetch a gap. |
| `openbb_kdb/cache.py` | The read-through algorithm, wiring the above together. |
| `openbb_kdb/models/historical.py` | The OpenBB `Fetcher` classes (equity/etf/crypto/currency/index). |
| `openbb_kdb/__init__.py` | Provider registration. |

`ranges.py` holds the only non-trivial logic with no I/O, which is why it is separated — it carries the bulk of the tests.

**`cache-chart/`** — the demo service, mirroring `live-grid/`'s layout:

| File | Responsibility |
|---|---|
| `app/openbb_client.py` | Call the OpenBB API on loopback; return bars + cache metadata. |
| `app/figure.py` | Build Plotly figure JSON from bars. |
| `app/main.py` | FastAPI routes. |
| `app/static/demo.html` | The standalone page: scroll handler + HUD. |
| `widgets.json` | Workspace widget contract. |

**Repo-level:** `Dockerfile` (q runtime + PyKX + extension), `docker-compose.yml` (q startup, `mem_limit`, license mount, Serve route), `ts-config/serve.json`, `scripts/verify-isolation.sh`, `.gitignore`, `README.md`.

---

### Task 1: Range arithmetic

The pure core: what to fetch given what is already covered. No q, no network.

**Files:**
- Create: `openbb-kdb/openbb_kdb/ranges.py`
- Create: `openbb-kdb/tests/test_ranges.py`
- Create: `openbb-kdb/pyproject.toml`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Range = tuple[datetime, datetime]` (inclusive start, inclusive end)
  - `coalesce(ranges: list[Range], step: timedelta = timedelta(0)) -> list[Range]`
  - `subtract(requested: Range, covered: list[Range], step: timedelta) -> list[Range]`
  - `trim_tail(r: Range, boundary: datetime) -> Range | None`
  - `interval_step(interval: str) -> timedelta`

- [ ] **Step 1: Create the package skeleton**

Create `openbb-kdb/pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "openbb-kdb"
version = "0.1.0"
description = "kdb+ read-through cache provider for the OpenBB Platform"
readme = "README.md"
requires-python = ">=3.10"
license = { text = "AGPL-3.0-only" }
dependencies = [
    "openbb-core>=1.5.8",
    "pykx>=3.2",
]

[project.entry-points."openbb_provider_extension"]
kdb = "openbb_kdb:kdb_provider"

[tool.setuptools.packages.find]
include = ["openbb_kdb*"]

[project.optional-dependencies]
dev = ["ruff", "pytest"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]

[tool.ruff]
target-version = "py310"
line-length = 100

[tool.ruff.lint]
# Lazy imports inside functions are intentional (heavy deps stay out of import time).
ignore = ["PLC0415"]
```

Then: `mkdir -p openbb-kdb/openbb_kdb openbb-kdb/tests && touch openbb-kdb/tests/__init__.py`

- [ ] **Step 2: Write the failing tests**

Create `openbb-kdb/tests/test_ranges.py`:

```python
"""Range arithmetic: the logic that decides what actually gets fetched."""

from datetime import datetime, timedelta

import pytest

from openbb_kdb.ranges import coalesce, interval_step, subtract, trim_tail

D = lambda s: datetime.fromisoformat(s)  # noqa: E731
DAY = timedelta(days=1)


def test_coalesce_merges_overlapping():
    out = coalesce([(D("2025-01-01"), D("2025-03-01")), (D("2025-02-01"), D("2025-04-01"))])
    assert out == [(D("2025-01-01"), D("2025-04-01"))]


def test_coalesce_merges_adjacent_given_a_step():
    """Jan 31 and Feb 1 are one bar apart: touching, so they merge."""
    out = coalesce(
        [(D("2025-01-01"), D("2025-01-31")), (D("2025-02-01"), D("2025-02-28"))], DAY
    )
    assert out == [(D("2025-01-01"), D("2025-02-28"))]


def test_coalesce_without_a_step_requires_real_overlap():
    out = coalesce([(D("2025-01-01"), D("2025-01-31")), (D("2025-02-01"), D("2025-02-28"))])
    assert len(out) == 2


def test_coalesce_keeps_disjoint_sorted():
    out = coalesce([(D("2025-06-01"), D("2025-06-30")), (D("2025-01-01"), D("2025-01-31"))])
    assert out == [(D("2025-01-01"), D("2025-01-31")), (D("2025-06-01"), D("2025-06-30"))]


def test_subtract_nothing_covered_is_full_request():
    req = (D("2024-01-01"), D("2025-01-01"))
    assert subtract(req, [], DAY) == [req]


def test_subtract_fully_covered_is_empty():
    req = (D("2024-06-01"), D("2024-12-01"))
    covered = [(D("2024-01-01"), D("2025-01-01"))]
    assert subtract(req, covered, DAY) == []


def test_subtract_returns_only_the_missing_prefix():
    """The 1y -> 3y zoom: two years cached, three requested, one gap fetched."""
    req = (D("2022-01-01"), D("2025-01-01"))
    covered = [(D("2024-01-01"), D("2025-01-01"))]
    gaps = subtract(req, covered, DAY)
    assert gaps == [(D("2022-01-01"), D("2023-12-31"))]


def test_subtract_returns_interior_hole():
    req = (D("2024-01-01"), D("2024-12-31"))
    covered = [(D("2024-01-01"), D("2024-03-31")), (D("2024-07-01"), D("2024-12-31"))]
    assert subtract(req, covered, DAY) == [(D("2024-04-01"), D("2024-06-30"))]


def test_subtract_ignores_coverage_outside_the_request():
    req = (D("2024-01-01"), D("2024-06-30"))
    covered = [(D("2023-01-01"), D("2023-06-30"))]
    assert subtract(req, covered, DAY) == [req]


def test_trim_tail_drops_incomplete_region():
    r = (D("2024-01-01"), D("2025-06-10"))
    assert trim_tail(r, D("2025-06-09")) == (D("2024-01-01"), D("2025-06-09"))


def test_trim_tail_returns_none_when_wholly_incomplete():
    assert trim_tail((D("2025-06-10"), D("2025-06-11")), D("2025-06-09")) is None


def test_trim_tail_leaves_older_range_untouched():
    r = (D("2024-01-01"), D("2024-02-01"))
    assert trim_tail(r, D("2025-06-09")) == r


@pytest.mark.parametrize(
    "interval,expected",
    [("1d", timedelta(days=1)), ("1m", timedelta(minutes=1)),
     ("5m", timedelta(minutes=5)), ("1h", timedelta(hours=1))],
)
def test_interval_step(interval, expected):
    assert interval_step(interval) == expected


def test_interval_step_rejects_unknown():
    with pytest.raises(ValueError):
        interval_step("1fortnight")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd openbb-kdb && python -m pytest tests/test_ranges.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'openbb_kdb.ranges'`

- [ ] **Step 4: Implement `ranges.py`**

Create `openbb-kdb/openbb_kdb/ranges.py`:

```python
"""Date-range arithmetic for the read-through cache.

Pure functions, no I/O. This is what decides that a 1y->3y zoom fetches two
years rather than three, so it carries the bulk of the extension's tests.

Ranges are (start, end) with BOTH ends inclusive. `step` is one bar width: two
ranges separated by exactly one step are adjacent and coalesce, and a gap's
boundary is pulled in by one step so it does not re-request a covered bar.
"""

from datetime import datetime, timedelta

Range = tuple[datetime, datetime]

_STEPS = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
}


def interval_step(interval: str) -> timedelta:
    """One bar width for an OpenBB interval string ('1d', '5m', '1h')."""
    import re

    m = re.fullmatch(r"(\d*)\s*([a-zA-Z]+)", str(interval).strip())
    if not m:
        raise ValueError(f"Could not parse interval {interval!r}.")
    n = int(m.group(1) or 1)
    unit = m.group(2).lower()
    if unit in ("mo", "mon", "month", "months"):
        return timedelta(days=30 * n)
    base = _STEPS.get(unit[0]) if unit[0] in _STEPS else None
    if base is None:
        raise ValueError(f"Unsupported interval {interval!r}.")
    return base * n


def coalesce(ranges: list[Range], step: timedelta = timedelta(0)) -> list[Range]:
    """Sort and merge ranges that overlap, or that sit within one `step`.

    With `step` given, ranges one bar apart (Jan 31 / Feb 1 for daily bars) are
    treated as contiguous — otherwise coverage would fragment into thousands of
    single-bar entries and every request would look like a gap.
    """
    if not ranges:
        return []
    ordered = sorted(ranges)
    out = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = out[-1]
        if start - step <= last_end:
            out[-1] = (last_start, max(last_end, end))
        else:
            out.append((start, end))
    return out


def subtract(requested: Range, covered: list[Range], step: timedelta) -> list[Range]:
    """Return the parts of `requested` not already present in `covered`."""
    req_start, req_end = requested
    if req_start > req_end:
        return []
    gaps: list[Range] = []
    cursor = req_start
    for cov_start, cov_end in coalesce(covered, step):
        if cov_end < cursor:
            continue
        if cov_start > req_end:
            break
        if cov_start > cursor:
            gaps.append((cursor, min(cov_start - step, req_end)))
        cursor = max(cursor, cov_end + step)
        if cursor > req_end:
            return gaps
    if cursor <= req_end:
        gaps.append((cursor, req_end))
    return [(s, e) for s, e in gaps if s <= e]


def trim_tail(r: Range, boundary: datetime) -> Range | None:
    """Clip a range at the last COMPLETED bar boundary.

    Coverage is never recorded past `boundary`, so the still-forming bar is
    refetched on every request instead of being cached half-built.
    """
    start, end = r
    if start > boundary:
        return None
    return (start, min(end, boundary))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd openbb-kdb && python -m pytest tests/test_ranges.py -q`
Expected: PASS, 14 passed

- [ ] **Step 6: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add openbb-kdb/pyproject.toml openbb-kdb/openbb_kdb/ranges.py openbb-kdb/tests/
git commit -m "feat(kdb): range arithmetic for gap-filling"
```

---

### Task 2: Configuration

**Files:**
- Create: `openbb-kdb/openbb_kdb/config.py`
- Create: `openbb-kdb/tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `KdbConfig` (frozen dataclass: `host: str`, `port: int`, `embedded: bool`, `memory_mb: int`, `watermark: float`, `upstream: str`, `qhome: str`) and `resolve_config(credentials: dict | None = None) -> KdbConfig`, plus `KdbConfig.q_workspace_mb` returning `int(memory_mb * 1.25)`.

- [ ] **Step 1: Write the failing tests**

Create `openbb-kdb/tests/test_config.py`:

```python
"""Config resolution: env vars and OpenBB credentials into one frozen object."""

import pytest

from openbb_kdb.config import KdbConfig, resolve_config


def test_defaults(monkeypatch):
    for var in ("KDB_HOST", "KDB_PORT", "KDB_EMBEDDED", "KDB_MEMORY_MB",
                "KDB_CACHE_WATERMARK", "KDB_UPSTREAM"):
        monkeypatch.delenv(var, raising=False)
    cfg = resolve_config()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 5000
    assert cfg.embedded is True
    assert cfg.memory_mb == 8192
    assert cfg.watermark == 0.75
    assert cfg.upstream == "eodhd"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("KDB_HOST", "kdb.internal")
    monkeypatch.setenv("KDB_PORT", "5010")
    monkeypatch.setenv("KDB_UPSTREAM", "yfinance")
    cfg = resolve_config()
    assert (cfg.host, cfg.port, cfg.upstream) == ("kdb.internal", 5010, "yfinance")


def test_credentials_beat_env(monkeypatch):
    monkeypatch.setenv("KDB_UPSTREAM", "yfinance")
    cfg = resolve_config({"kdb_upstream": "fmp"})
    assert cfg.upstream == "fmp"


def test_pointing_at_an_external_server_disables_spawning(monkeypatch):
    """A non-loopback host means the user brought their own kdb+."""
    monkeypatch.setenv("KDB_HOST", "kdb.internal")
    assert resolve_config().embedded is False


def test_explicit_embedded_false(monkeypatch):
    monkeypatch.setenv("KDB_EMBEDDED", "false")
    assert resolve_config().embedded is False


def test_workspace_is_25_percent_above_budget(monkeypatch):
    monkeypatch.setenv("KDB_MEMORY_MB", "8192")
    assert resolve_config().q_workspace_mb == 10240


def test_rejects_bad_port(monkeypatch):
    monkeypatch.setenv("KDB_PORT", "notanumber")
    with pytest.raises(ValueError):
        resolve_config()


def test_rejects_out_of_range_watermark(monkeypatch):
    monkeypatch.setenv("KDB_CACHE_WATERMARK", "1.5")
    with pytest.raises(ValueError):
        resolve_config()


def test_config_is_frozen():
    cfg = resolve_config()
    with pytest.raises(Exception):
        cfg.host = "elsewhere"  # type: ignore[misc]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd openbb-kdb && python -m pytest tests/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'openbb_kdb.config'`

- [ ] **Step 3: Implement `config.py`**

Create `openbb-kdb/openbb_kdb/config.py`:

```python
"""Connection and cache configuration.

Precedence: OpenBB credential > environment variable > default.
"""

import os
from dataclasses import dataclass

_DEFAULTS = {
    "host": "127.0.0.1",
    "port": 5000,
    "memory_mb": 8192,
    "watermark": 0.75,
    "upstream": "eodhd",
    "qhome": "/opt/kx",
}

# q is given headroom above the cache budget. Crossing -w kills the process
# outright (no catchable 'wsfull), so -w is containment that protects the rest
# of the container -- the real budget is enforced by eviction well below it.
_WORKSPACE_HEADROOM = 1.25


@dataclass(frozen=True)
class KdbConfig:
    """Resolved kdb+ settings."""

    host: str
    port: int
    embedded: bool
    memory_mb: int
    watermark: float
    upstream: str
    qhome: str

    @property
    def q_workspace_mb(self) -> int:
        """The `-w` value: the cache budget plus containment headroom."""
        return int(self.memory_mb * _WORKSPACE_HEADROOM)


def _pick(key: str, env: str, credentials: dict | None):
    creds = credentials or {}
    if creds.get(f"kdb_{key}"):
        return creds[f"kdb_{key}"]
    if os.getenv(env):
        return os.getenv(env)
    return None


def resolve_config(credentials: dict | None = None) -> KdbConfig:
    """Resolve configuration from credentials, environment, then defaults."""
    host = _pick("host", "KDB_HOST", credentials) or _DEFAULTS["host"]

    raw_port = _pick("port", "KDB_PORT", credentials) or _DEFAULTS["port"]
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid kdb+ port: {raw_port!r}. Must be an integer.") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"kdb+ port {port} out of range (1-65535).")

    raw_embedded = _pick("embedded", "KDB_EMBEDDED", credentials)
    if raw_embedded is None:
        # Spawning only makes sense for a q we own -- a remote host is the
        # user's own server.
        embedded = host in ("127.0.0.1", "localhost")
    else:
        embedded = str(raw_embedded).strip().lower() in ("1", "true", "yes", "on")

    raw_mem = _pick("memory_mb", "KDB_MEMORY_MB", credentials) or _DEFAULTS["memory_mb"]
    try:
        memory_mb = int(raw_mem)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid KDB_MEMORY_MB: {raw_mem!r}.") from exc
    if memory_mb < 64:
        raise ValueError(f"KDB_MEMORY_MB {memory_mb} is too small (minimum 64).")

    raw_wm = _pick("cache_watermark", "KDB_CACHE_WATERMARK", credentials) or _DEFAULTS["watermark"]
    try:
        watermark = float(raw_wm)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid KDB_CACHE_WATERMARK: {raw_wm!r}.") from exc
    if not 0.1 <= watermark <= 0.95:
        raise ValueError(f"KDB_CACHE_WATERMARK {watermark} out of range (0.1-0.95).")

    upstream = _pick("upstream", "KDB_UPSTREAM", credentials) or _DEFAULTS["upstream"]
    qhome = os.getenv("QHOME") or _DEFAULTS["qhome"]

    return KdbConfig(
        host=host, port=port, embedded=embedded, memory_mb=memory_mb,
        watermark=watermark, upstream=str(upstream), qhome=qhome,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd openbb-kdb && python -m pytest tests/test_config.py -q`
Expected: PASS, 9 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add openbb-kdb/openbb_kdb/config.py openbb-kdb/tests/test_config.py
git commit -m "feat(kdb): configuration resolution"
```

---

### Task 3: Session — owning the q process

Spawn q, connect, notice when it has died, respawn. This is the task that makes a dead q survivable.

**Files:**
- Create: `openbb-kdb/openbb_kdb/session.py`
- Create: `openbb-kdb/tests/test_session.py`

**Interfaces:**
- Consumes: `KdbConfig` from Task 2.
- Produces: `KdbSession(config: KdbConfig)` with `connection()` returning a live PyKX connection or raising `KdbUnavailable`; `is_alive() -> bool`; `close() -> None`; and the exception class `KdbUnavailable(Exception)`.

- [ ] **Step 1: Write the failing tests**

Create `openbb-kdb/tests/test_session.py`:

```python
"""Session lifecycle: spawn, reuse, notice death, respawn, give up cleanly."""

import pytest

from openbb_kdb.config import KdbConfig
from openbb_kdb.session import KdbSession, KdbUnavailable


def cfg(**kw) -> KdbConfig:
    base = dict(host="127.0.0.1", port=5000, embedded=True, memory_mb=1024,
                watermark=0.75, upstream="eodhd", qhome="/opt/kx")
    base.update(kw)
    return KdbConfig(**base)


class FakeConn:
    """Stands in for pykx.SyncQConnection."""

    def __init__(self, alive=True):
        self.alive = alive
        self.calls = []

    def __call__(self, query, *args):
        if not self.alive:
            raise RuntimeError("Attempted to use a closed IPC connection")
        self.calls.append(query)
        return 2

    def close(self):
        self.alive = False


def test_connects_and_reuses_one_connection(monkeypatch):
    made = []
    monkeypatch.setattr(KdbSession, "_spawn", lambda self: None)
    monkeypatch.setattr(KdbSession, "_connect", lambda self: made.append(1) or FakeConn())
    s = KdbSession(cfg())
    first, second = s.connection(), s.connection()
    assert first is second
    assert len(made) == 1


def test_health_check_detects_a_dead_q(monkeypatch):
    monkeypatch.setattr(KdbSession, "_spawn", lambda self: None)
    monkeypatch.setattr(KdbSession, "_connect", lambda self: FakeConn())
    s = KdbSession(cfg())
    conn = s.connection()
    assert s.is_alive() is True
    conn.alive = False
    assert s.is_alive() is False


def test_respawns_after_death(monkeypatch):
    """q dying is a normal state, not an exception -- the next call gets a new one."""
    conns = [FakeConn(), FakeConn()]
    spawns = []
    monkeypatch.setattr(KdbSession, "_spawn", lambda self: spawns.append(1))
    monkeypatch.setattr(KdbSession, "_connect", lambda self: conns.pop(0))
    s = KdbSession(cfg())
    first = s.connection()
    first.alive = False
    second = s.connection()
    assert second is not first
    assert len(spawns) == 2


def test_external_server_is_never_spawned(monkeypatch):
    spawns = []
    monkeypatch.setattr(KdbSession, "_spawn", lambda self: spawns.append(1))
    monkeypatch.setattr(KdbSession, "_connect", lambda self: FakeConn())
    KdbSession(cfg(host="kdb.internal", embedded=False)).connection()
    assert spawns == []


def test_connect_failure_raises_kdb_unavailable(monkeypatch):
    def boom(self):
        raise OSError("connection refused")

    monkeypatch.setattr(KdbSession, "_spawn", lambda self: None)
    monkeypatch.setattr(KdbSession, "_connect", boom)
    with pytest.raises(KdbUnavailable):
        KdbSession(cfg()).connection()


def test_repeated_failure_is_not_retried_every_call(monkeypatch):
    """A missing license must not cost a spawn attempt on every single request."""
    attempts = []

    def boom(self):
        attempts.append(1)
        raise OSError("connection refused")

    monkeypatch.setattr(KdbSession, "_spawn", lambda self: None)
    monkeypatch.setattr(KdbSession, "_connect", boom)
    s = KdbSession(cfg())
    for _ in range(5):
        with pytest.raises(KdbUnavailable):
            s.connection()
    assert len(attempts) == 1


def test_q_command_binds_loopback_and_sets_workspace():
    """The bind is load-bearing: 0.0.0.0 would expose an unauthenticated q."""
    argv = KdbSession(cfg(memory_mb=1024))._q_argv()
    assert argv[1] == "-p"
    assert argv[2] == "127.0.0.1:5000"
    assert "0.0.0.0" not in " ".join(argv)
    assert argv[argv.index("-w") + 1] == "1280"  # 1024 * 1.25
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd openbb-kdb && python -m pytest tests/test_session.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'openbb_kdb.session'`

- [ ] **Step 3: Implement `session.py`**

Create `openbb-kdb/openbb_kdb/session.py`:

```python
"""Ownership of the q process and its IPC connection.

q is a child of THIS container, bound to loopback. Everything in this stack
shares the tailscale container's network namespace, so a loopback bind is
reachable by every sibling service and by no tailnet peer. Binding 0.0.0.0
would publish an unauthenticated q -- which executes arbitrary q -- to the
whole tailnet.

Crossing q's -w kills the process outright (verified against kdb-x 5.0), so a
dead q is treated as an ordinary state: detect, respawn, carry on.
"""

import logging
import subprocess
import time

from openbb_kdb.config import KdbConfig

logger = logging.getLogger(__name__)


class KdbUnavailable(Exception):
    """No usable kdb+ connection. Callers degrade to upstream pass-through."""


class KdbSession:
    """Owns at most one q process and one IPC connection."""

    def __init__(self, config: KdbConfig):
        self.config = config
        self._conn = None
        self._proc: subprocess.Popen | None = None
        self._given_up = False

    def _q_argv(self) -> list[str]:
        """Argument vector for the q server."""
        cfg = self.config
        return [
            f"{cfg.qhome}/bin/q",
            "-p", f"127.0.0.1:{cfg.port}",
            "-w", str(cfg.q_workspace_mb),
            "-q",
        ]

    def _spawn(self) -> None:
        """Start q as a child process.

        stdin is a pipe we never close: q reads its console from stdin and
        exits on EOF, which in a detached container happens immediately.
        """
        import os

        if self._proc is not None and self._proc.poll() is None:
            return
        env = dict(os.environ, QHOME=self.config.qhome, QLIC=self.config.qhome)
        self._proc = subprocess.Popen(  # noqa: S603
            self._q_argv(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=self.config.qhome,
        )
        time.sleep(1.0)
        if self._proc.poll() is not None:
            out = (self._proc.stdout.read() or b"").decode(errors="replace")[:500]
            raise OSError(f"q exited immediately: {out}")

    def _connect(self):
        """Open a PyKX IPC connection (unlicensed client mode -- no license needed)."""
        import pykx as kx

        return kx.SyncQConnection(self.config.host, self.config.port)

    def is_alive(self) -> bool:
        """True when the current connection still answers."""
        if self._conn is None:
            return False
        try:
            self._conn("1+1")
            return True
        except Exception:
            return False

    def connection(self):
        """Return a live connection, spawning or respawning as needed."""
        if self._given_up:
            raise KdbUnavailable("kdb+ previously unreachable; not retrying")
        if self._conn is not None and self.is_alive():
            return self._conn
        self._conn = None
        try:
            if self.config.embedded:
                self._spawn()
            self._conn = self._connect()
        except Exception as exc:
            self._given_up = True
            logger.warning("kdb+ unavailable, falling back to upstream: %s", exc)
            raise KdbUnavailable(str(exc)) from exc
        return self._conn

    def close(self) -> None:
        """Close the connection and stop a q we started. Never raises."""
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None
        try:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            pass
        self._proc = None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd openbb-kdb && python -m pytest tests/test_session.py -q`
Expected: PASS, 7 passed

Note the deliberate asymmetry encoded in these tests: a q that **dies** is respawned, but a q that **never connected** is given up on permanently (`_given_up`). A missing license would otherwise cost a failed spawn on every single request. The cost is that a transient startup failure disables the cache until the container restarts — acceptable because the pass-through keeps serving data.

- [ ] **Step 5: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add openbb-kdb/openbb_kdb/session.py openbb-kdb/tests/test_session.py
git commit -m "feat(kdb): q process session with respawn"
```

---

### Task 4: Store — every q statement in one place

**Files:**
- Create: `openbb-kdb/openbb_kdb/store.py`
- Create: `openbb-kdb/tests/test_store.py`

**Interfaces:**
- Consumes: `KdbSession` (Task 3), `Range` (Task 1).
- Produces a `KdbStore(session)` with: `table_name(symbol, interval) -> str`; `read_bars(symbol, interval, start, end) -> DataFrame`; `write_bars(symbol, interval, df) -> None`; `read_coverage(symbol, interval) -> list[Range]`; `record_coverage(symbol, interval, r: Range) -> None`; `touch(symbol, interval) -> None`; `memory() -> dict`; `evict_until_below(budget_bytes: int) -> list[str]`; `drop(symbol, interval) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `openbb-kdb/tests/test_store.py`:

```python
"""Store: q statement construction and eviction policy, against a fake connection."""

from datetime import datetime

import pytest

from openbb_kdb.store import KdbStore

D = lambda s: datetime.fromisoformat(s)  # noqa: E731


class FakeConn:
    """Records queries; returns canned values by substring match."""

    def __init__(self, responses=None):
        self.queries = []
        self.responses = responses or {}
        self.data = {}

    def __call__(self, query, *args):
        self.queries.append(query)
        for needle, value in self.responses.items():
            if needle in query:
                return _Wrapped(value)
        return _Wrapped(None)

    def __setitem__(self, key, value):
        self.data[key] = value


class _Wrapped:
    def __init__(self, value):
        self._value = value

    def py(self):
        return self._value

    def pd(self):
        return self._value


class FakeSession:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


def store_with(responses=None):
    conn = FakeConn(responses)
    return KdbStore(FakeSession(conn)), conn


def test_table_name_is_symbol_and_interval():
    s, _ = store_with()
    assert s.table_name("AAPL", "1d") == "bars_AAPL_1d"


def test_table_name_sanitizes_punctuation():
    """BTC-USD and EUR.FOREX must not produce invalid q identifiers."""
    s, _ = store_with()
    assert s.table_name("BTC-USD", "1d") == "bars_BTC_USD_1d"
    assert s.table_name("BRK.B", "5m") == "bars_BRK_B_5m"


def test_memory_reads_heap_not_used():
    """heap is what approaches wmax and kills q; used trails it."""
    s, _ = store_with({".Q.w[]": {"used": 100, "heap": 500, "wmax": 1000}})
    assert s.memory()["heap"] == 500


def test_evict_stops_once_below_budget():
    s, conn = store_with({
        ".Q.w[]": {"used": 100, "heap": 100, "wmax": 1000},
        ".cache.lru": [("AAPL", "1d", 1.0)],
    })
    assert s.evict_until_below(500) == []
    assert not any(".Q.gc" in q for q in conn.queries)


def test_evict_drops_oldest_first_and_collects_garbage():
    """delete alone frees `used` but not `heap`; .Q.gc[] is what reclaims it."""
    calls = {"n": 0}

    class ShrinkingConn(FakeConn):
        def __call__(self, query, *args):
            self.queries.append(query)
            if ".Q.w[]" in query:
                calls["n"] += 1
                heap = 1000 if calls["n"] == 1 else 100
                return _Wrapped({"used": heap, "heap": heap, "wmax": 4000})
            if ".cache.lru" in query and "select" in query:
                return _Wrapped([("OLD", "1d", 1.0), ("NEW", "1d", 99.0)])
            return _Wrapped(None)

    conn = ShrinkingConn()
    s = KdbStore(FakeSession(conn))
    evicted = s.evict_until_below(500)
    assert evicted == ["bars_OLD_1d"]
    assert any(".Q.gc" in q for q in conn.queries)
    assert any("delete" in q and "bars_OLD_1d" in q for q in conn.queries)


def test_read_coverage_returns_ranges():
    s, _ = store_with({
        ".cache.cov": [(D("2024-01-01"), D("2024-06-30"))],
    })
    assert s.read_coverage("AAPL", "1d") == [(D("2024-01-01"), D("2024-06-30"))]


def test_read_coverage_empty_for_unknown_symbol():
    s, _ = store_with({".cache.cov": []})
    assert s.read_coverage("NOPE", "1d") == []


def test_drop_removes_table_and_coverage():
    s, conn = store_with()
    s.drop("AAPL", "1d")
    joined = " ".join(conn.queries)
    assert "bars_AAPL_1d" in joined
    assert ".cache.cov" in joined
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd openbb-kdb && python -m pytest tests/test_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'openbb_kdb.store'`

- [ ] **Step 3: Implement `store.py`**

Create `openbb-kdb/openbb_kdb/store.py`:

```python
"""Every q statement the cache issues.

Kept in one module so the q surface is auditable and mockable in one place.

Two measured facts shape this file:
  * `heap`, not `used`, is what approaches `wmax` and kills q, so eviction
    watches heap.
  * `delete` frees `used` but leaves `heap` untouched; only `.Q.gc[]` returns
    it. Every eviction therefore ends in a collect.
"""

import re
from datetime import datetime

Range = tuple[datetime, datetime]

_SAFE = re.compile(r"[^A-Za-z0-9_]")


class KdbStore:
    """Typed access to the cache's q state."""

    def __init__(self, session):
        self.session = session

    def _conn(self):
        return self.session.connection()

    @staticmethod
    def table_name(symbol: str, interval: str) -> str:
        """A valid q identifier for one (symbol, interval) pair."""
        sym = _SAFE.sub("_", str(symbol).strip().upper())
        iv = _SAFE.sub("_", str(interval).strip())
        return f"bars_{sym}_{iv}"

    def memory(self) -> dict:
        """`.Q.w[]` as a plain dict with str keys (used, heap, wmax, ...)."""
        return dict(self._conn()(".Q.w[]").py())

    def read_bars(self, symbol: str, interval: str, start: datetime, end: datetime):
        """Bars within [start, end] as a pandas DataFrame; empty if absent."""
        import pandas as pd

        name = self.table_name(symbol, interval)
        conn = self._conn()
        if not conn(f"`{name} in key `.").py():
            return pd.DataFrame()
        out = conn(
            f"select from {name} where t >= x, t <= y",
            _q_timestamp(start),
            _q_timestamp(end),
        ).pd()
        return out if out is not None else pd.DataFrame()

    def write_bars(self, symbol: str, interval: str, df) -> None:
        """Upsert bars, keeping the table sorted and free of duplicate stamps."""
        name = self.table_name(symbol, interval)
        conn = self._conn()
        conn[f"_incoming"] = df
        conn(f"{name}: `t xasc 0!(`t xkey $[`{name} in key `.; {name}; 0#_incoming]) upsert _incoming")
        conn("delete _incoming from `.")

    def read_coverage(self, symbol: str, interval: str) -> list[Range]:
        """Ranges already fetched for this (symbol, interval)."""
        rows = self._conn()(
            "select s, e from .cache.cov where sym = x, iv = y",
            _q_symbol(symbol), _q_symbol(interval),
        ).py()
        if not rows:
            return []
        if isinstance(rows, dict):  # column-oriented result
            return list(zip(rows["s"], rows["e"]))
        return [(r[0], r[1]) for r in rows]

    def record_coverage(self, symbol: str, interval: str, r: Range) -> None:
        """Append a covered range. Coalescing happens on read, in Python."""
        self._conn()(
            ".cache.cov: .cache.cov upsert (x; y; z; w)",
            _q_symbol(symbol), _q_symbol(interval),
            _q_timestamp(r[0]), _q_timestamp(r[1]),
        )

    def touch(self, symbol: str, interval: str) -> None:
        """Record an access for LRU ordering."""
        self._conn()(
            ".cache.lru: .cache.lru upsert (x; y; .z.p)",
            _q_symbol(symbol), _q_symbol(interval),
        )

    def drop(self, symbol: str, interval: str) -> None:
        """Remove a table, its coverage, and its LRU entry, then collect."""
        name = self.table_name(symbol, interval)
        conn = self._conn()
        conn(f"if[`{name} in key `.; delete {name} from `.]")
        conn(
            "delete from `.cache.cov where sym = x, iv = y",
            _q_symbol(symbol), _q_symbol(interval),
        )
        conn(
            "delete from `.cache.lru where sym = x, iv = y",
            _q_symbol(symbol), _q_symbol(interval),
        )
        conn(".Q.gc[]")

    def evict_until_below(self, budget_bytes: int) -> list[str]:
        """Drop least-recently-used entries until heap is under budget.

        Preventive by necessity: crossing q's -w does not raise, it kills the
        process. Returns the table names evicted.
        """
        evicted: list[str] = []
        if self.memory().get("heap", 0) <= budget_bytes:
            return evicted
        rows = self._conn()("select sym, iv, atime from .cache.lru").py() or []
        if isinstance(rows, dict):
            rows = list(zip(rows["sym"], rows["iv"], rows["atime"]))
        for sym, iv, _ in sorted(rows, key=lambda r: r[2]):
            sym = sym.decode() if isinstance(sym, bytes) else str(sym)
            iv = iv.decode() if isinstance(iv, bytes) else str(iv)
            self.drop(sym, iv)
            evicted.append(self.table_name(sym, iv))
            if self.memory().get("heap", 0) <= budget_bytes:
                break
        return evicted


def _q_symbol(value: str):
    import pykx as kx

    return kx.SymbolAtom(str(value))


def _q_timestamp(value: datetime):
    import pykx as kx

    return kx.TimestampAtom(value)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd openbb-kdb && python -m pytest tests/test_store.py -q`
Expected: PASS, 8 passed

The fake connection ignores the PyKX helpers, so `_q_symbol`/`_q_timestamp` are exercised for real only in Task 9's live check.

- [ ] **Step 5: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add openbb-kdb/openbb_kdb/store.py openbb-kdb/tests/test_store.py
git commit -m "feat(kdb): q store with heap-watermark eviction"
```

---

### Task 5: Upstream resolution

**Files:**
- Create: `openbb-kdb/openbb_kdb/upstream.py`
- Create: `openbb-kdb/tests/test_upstream.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `async fetch_gap(provider: str, model: str, params: dict, credentials: dict | None) -> list[dict]` and `UpstreamError(Exception)`.

`model` is an OpenBB fetcher key such as `"EquityHistorical"`.

- [ ] **Step 1: Write the failing tests**

Create `openbb-kdb/tests/test_upstream.py`:

```python
"""Upstream resolution: any registered provider, resolved by name."""

import pytest

from openbb_kdb.upstream import UpstreamError, fetch_gap


class FakeFetcher:
    last_call = None

    @classmethod
    async def fetch_data(cls, params, credentials=None, **kwargs):
        FakeFetcher.last_call = (params, credentials)
        return [{"date": "2024-01-02", "close": 1.0}]


class FakeProvider:
    def __init__(self, fetchers):
        self.fetcher_dict = fetchers


def install_registry(monkeypatch, providers):
    import openbb_kdb.upstream as up

    monkeypatch.setattr(up, "_load_registry", lambda: providers)
    up._REGISTRY_CACHE = None


@pytest.mark.asyncio
async def test_fetches_through_the_named_provider(monkeypatch):
    install_registry(monkeypatch, {"eodhd": FakeProvider({"EquityHistorical": FakeFetcher})})
    rows = await fetch_gap("eodhd", "EquityHistorical", {"symbol": "AAPL"}, {"k": "v"})
    assert rows == [{"date": "2024-01-02", "close": 1.0}]
    assert FakeFetcher.last_call == ({"symbol": "AAPL"}, {"k": "v"})


@pytest.mark.asyncio
async def test_any_provider_works_not_just_eodhd(monkeypatch):
    """KDB_UPSTREAM must accept any registered provider."""
    install_registry(monkeypatch, {"yfinance": FakeProvider({"EquityHistorical": FakeFetcher})})
    rows = await fetch_gap("yfinance", "EquityHistorical", {"symbol": "AAPL"}, None)
    assert rows


@pytest.mark.asyncio
async def test_unknown_provider_raises_upstream_error(monkeypatch):
    install_registry(monkeypatch, {"eodhd": FakeProvider({})})
    with pytest.raises(UpstreamError, match="nosuch"):
        await fetch_gap("nosuch", "EquityHistorical", {}, None)


@pytest.mark.asyncio
async def test_provider_without_the_model_raises(monkeypatch):
    install_registry(monkeypatch, {"eodhd": FakeProvider({"EquityHistorical": FakeFetcher})})
    with pytest.raises(UpstreamError, match="CryptoHistorical"):
        await fetch_gap("eodhd", "CryptoHistorical", {}, None)


@pytest.mark.asyncio
async def test_kdb_may_not_be_its_own_upstream(monkeypatch):
    """Guards against infinite recursion through the provider registry."""
    install_registry(monkeypatch, {"kdb": FakeProvider({"EquityHistorical": FakeFetcher})})
    with pytest.raises(UpstreamError, match="itself"):
        await fetch_gap("kdb", "EquityHistorical", {}, None)
```

Add `pytest-asyncio` to the dev extras in `openbb-kdb/pyproject.toml` and configure it:

```toml
[project.optional-dependencies]
dev = ["ruff", "pytest", "pytest-asyncio"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
asyncio_mode = "auto"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd openbb-kdb && python -m pytest tests/test_upstream.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'openbb_kdb.upstream'`

- [ ] **Step 3: Implement `upstream.py`**

Create `openbb-kdb/openbb_kdb/upstream.py`:

```python
"""Fetching a cache miss from whichever provider the user configured.

Providers are resolved BY NAME through OpenBB's registry rather than imported,
which is what lets KDB_UPSTREAM be any installed provider instead of hardwiring
EODHD.
"""

from typing import Any

_REGISTRY_CACHE: dict[str, Any] | None = None


class UpstreamError(Exception):
    """The configured upstream provider cannot serve this request."""


def _load_registry() -> dict[str, Any]:
    """Map provider name -> Provider object."""
    from openbb_core.provider.registry import RegistryLoader

    return RegistryLoader.from_extensions().providers


def _registry() -> dict[str, Any]:
    global _REGISTRY_CACHE  # noqa: PLW0603
    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = _load_registry()
    return _REGISTRY_CACHE


async def fetch_gap(
    provider: str, model: str, params: dict, credentials: dict | None
) -> list[dict]:
    """Fetch one missing range from the upstream provider."""
    if provider.lower() == "kdb":
        raise UpstreamError(
            "kdb cannot be its own upstream (KDB_UPSTREAM must name another provider)."
        )
    registry = _registry()
    prov = registry.get(provider)
    if prov is None:
        raise UpstreamError(
            f"Upstream provider {provider!r} is not installed. Available: "
            f"{sorted(registry)}"
        )
    fetcher = prov.fetcher_dict.get(model)
    if fetcher is None:
        raise UpstreamError(f"Provider {provider!r} does not implement {model}.")
    result = await fetcher.fetch_data(params, credentials)
    rows = getattr(result, "result", result)
    return [r if isinstance(r, dict) else r.model_dump() for r in rows]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd openbb-kdb && python -m pytest tests/test_upstream.py -q`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add openbb-kdb/openbb_kdb/upstream.py openbb-kdb/tests/test_upstream.py openbb-kdb/pyproject.toml
git commit -m "feat(kdb): resolve any upstream provider by name"
```

---

### Task 6: The read-through algorithm

Where the parts meet. This is the task the episode is about.

**Files:**
- Create: `openbb-kdb/openbb_kdb/cache.py`
- Create: `openbb-kdb/tests/test_cache.py`

**Interfaces:**
- Consumes: `ranges` (1), `KdbConfig` (2), `KdbUnavailable` (3), `KdbStore` (4), `fetch_gap` (5).
- Produces: `ReadThroughCache(store, config)` with `async get(symbol, interval, start, end, model, params, credentials) -> tuple[list[dict], dict]` returning `(rows, metadata)`; metadata keys `cache`, `rows_from_cache`, `rows_from_upstream`, `gaps_fetched`, `upstream_ms`, `kdb_ms`; and `last_complete_boundary(interval, now) -> datetime`.

- [ ] **Step 1: Write the failing tests**

Create `openbb-kdb/tests/test_cache.py`:

```python
"""The read-through algorithm: what gets fetched, what gets served, what it reports."""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from openbb_kdb.cache import ReadThroughCache, last_complete_boundary
from openbb_kdb.config import KdbConfig
from openbb_kdb.session import KdbUnavailable

D = lambda s: datetime.fromisoformat(s)  # noqa: E731


def cfg(**kw) -> KdbConfig:
    base = dict(host="127.0.0.1", port=5000, embedded=True, memory_mb=1024,
                watermark=0.75, upstream="eodhd", qhome="/opt/kx")
    base.update(kw)
    return KdbConfig(**base)


class FakeStore:
    """In-memory stand-in for KdbStore."""

    def __init__(self, coverage=None, bars=None, heap=0):
        self.coverage = coverage or {}
        self.bars = bars or {}
        self.heap = heap
        self.written = []
        self.dropped = []
        self.evicted_to = None

    def read_coverage(self, symbol, interval):
        return self.coverage.get((symbol, interval), [])

    def record_coverage(self, symbol, interval, r):
        self.coverage.setdefault((symbol, interval), []).append(r)

    def read_bars(self, symbol, interval, start, end):
        df = self.bars.get((symbol, interval), pd.DataFrame())
        if df.empty:
            return df
        return df[(df["t"] >= start) & (df["t"] <= end)]

    def write_bars(self, symbol, interval, df):
        self.written.append((symbol, interval, df))
        prior = self.bars.get((symbol, interval), pd.DataFrame())
        self.bars[(symbol, interval)] = pd.concat([prior, df]).sort_values("t")

    def touch(self, symbol, interval):
        pass

    def drop(self, symbol, interval):
        self.dropped.append((symbol, interval))
        self.coverage.pop((symbol, interval), None)
        self.bars.pop((symbol, interval), None)

    def memory(self):
        return {"used": self.heap, "heap": self.heap, "wmax": 4_000_000_000}

    def evict_until_below(self, budget):
        self.evicted_to = budget
        return []

    def table_name(self, symbol, interval):
        return f"bars_{symbol}_{interval}"


def bars_frame(dates, close=1.0):
    return pd.DataFrame({"t": [D(d) for d in dates], "close": [close] * len(dates)})


def make_cache(store, fetches, **cfg_kw):
    """Wire a cache whose upstream records calls and returns canned rows.

    Responses are returned in order; the LAST one is sticky, so a test that
    retries (the bypass path) keeps getting data instead of an empty list.
    """
    calls = []

    async def fake_fetch(provider, model, params, credentials):
        calls.append(params)
        if not fetches:
            return []
        return fetches.pop(0) if len(fetches) > 1 else fetches[0]

    cache = ReadThroughCache(store, cfg(**cfg_kw))
    cache._fetch_gap = fake_fetch
    return cache, calls


async def test_cold_cache_is_a_miss_and_fetches_everything():
    store = FakeStore()
    rows_up = [{"date": "2024-01-02", "close": 1.0}]
    cache, calls = make_cache(store, [rows_up])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-01"), D("2024-01-31"),
        "EquityHistorical", {"symbol": "AAPL"}, None,
    )
    assert meta["cache"] == "miss"
    assert meta["gaps_fetched"] == 1
    assert len(calls) == 1


async def test_full_hit_makes_no_upstream_call():
    """The second identical request must not touch the network."""
    store = FakeStore(
        coverage={("AAPL", "1d"): [(D("2024-01-01"), D("2024-12-31"))]},
        bars={("AAPL", "1d"): bars_frame(["2024-06-01", "2024-06-02"])},
    )
    cache, calls = make_cache(store, [])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-06-01"), D("2024-06-30"),
        "EquityHistorical", {"symbol": "AAPL"}, None,
    )
    assert meta["cache"] == "hit"
    assert meta["rows_from_upstream"] == 0
    assert calls == []


async def test_the_zoom_fetches_only_the_missing_years():
    """1y cached, 3y requested -> exactly one gap, and it is the missing prefix."""
    store = FakeStore(
        coverage={("AAPL", "1d"): [(D("2024-01-01"), D("2024-12-31"))]},
        bars={("AAPL", "1d"): bars_frame(["2024-06-01"])},
    )
    cache, calls = make_cache(store, [[{"date": "2022-01-03", "close": 1.0}]])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2022-01-01"), D("2024-12-31"),
        "EquityHistorical", {"symbol": "AAPL"}, None,
    )
    assert meta["cache"] == "partial"
    assert len(calls) == 1
    assert calls[0]["start_date"] == D("2022-01-01").date()
    assert calls[0]["end_date"] == D("2023-12-31").date()


async def test_coverage_never_includes_the_incomplete_tail():
    """Today's bar is still forming, so it must be refetched next time."""
    store = FakeStore()
    now = D("2025-06-10T15:00:00")
    cache, _ = make_cache(store, [[{"date": "2025-06-09", "close": 1.0}]])
    await cache.get(
        "AAPL", "1d", D("2025-01-01"), now,
        "EquityHistorical", {"symbol": "AAPL"}, None, now=now,
    )
    recorded = store.coverage[("AAPL", "1d")]
    assert all(end < now for _, end in recorded)


async def test_a_split_invalidates_the_symbol():
    """Overlapping closes disagree -> adjusted history was rewritten."""
    store = FakeStore(
        coverage={("AAPL", "1d"): [(D("2024-01-01"), D("2024-12-31"))]},
        bars={("AAPL", "1d"): bars_frame(["2024-12-30", "2024-12-31"], close=200.0)},
    )
    cache, calls = make_cache(
        store,
        [[{"date": "2024-12-30", "close": 100.0}, {"date": "2024-12-31", "close": 100.0}],
         [{"date": "2024-01-02", "close": 100.0}]],
    )
    now = D("2025-01-05")
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-01"), now,
        "EquityHistorical", {"symbol": "AAPL"}, None, now=now,
    )
    # Dropping the symbol clears its coverage, so the refill is a full miss --
    # which is exactly right: none of the old adjusted history is trustworthy.
    assert ("AAPL", "1d") in store.dropped
    assert meta["cache"] == "miss"


async def test_unreachable_kdb_passes_through():
    """The cache must never be the reason a request fails."""

    class DeadStore(FakeStore):
        def read_coverage(self, symbol, interval):
            raise KdbUnavailable("no q")

    cache, calls = make_cache(DeadStore(), [[{"date": "2024-01-02", "close": 1.0}]])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-01"), D("2024-01-31"),
        "EquityHistorical", {"symbol": "AAPL"}, None,
    )
    assert meta["cache"] == "bypass"
    assert rows == [{"date": "2024-01-02", "close": 1.0}]


async def test_dead_q_midway_still_returns_data():
    """A write failing after a successful fetch must not lose the fetched rows."""

    class FlakyStore(FakeStore):
        def write_bars(self, symbol, interval, df):
            raise RuntimeError("Attempted to use a closed IPC connection")

    cache, _ = make_cache(FlakyStore(), [[{"date": "2024-01-02", "close": 1.0}]])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-01"), D("2024-01-31"),
        "EquityHistorical", {"symbol": "AAPL"}, None,
    )
    assert rows == [{"date": "2024-01-02", "close": 1.0}]
    assert meta["cache"] == "bypass"


async def test_eviction_runs_against_the_budget_not_the_workspace():
    store = FakeStore(heap=10_000_000)
    cache, _ = make_cache(store, [[{"date": "2024-01-02", "close": 1.0}]], memory_mb=1024)
    await cache.get(
        "AAPL", "1d", D("2024-01-01"), D("2024-01-31"),
        "EquityHistorical", {"symbol": "AAPL"}, None,
    )
    assert store.evicted_to == int(1024 * 1024 * 1024 * 0.75)


async def test_concurrent_requests_for_one_symbol_fetch_once():
    """Two widgets opening the same chart must not both hit the network."""
    import asyncio

    store = FakeStore()
    cache, calls = make_cache(store, [[{"date": "2024-01-02", "close": 1.0}], []])
    args = ("AAPL", "1d", D("2024-01-01"), D("2024-01-31"),
            "EquityHistorical", {"symbol": "AAPL"}, None)
    await asyncio.gather(cache.get(*args), cache.get(*args))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "interval,now,expected",
    [
        ("1d", D("2025-06-10T15:00:00"), D("2025-06-09T23:59:59.999999")),
        ("1h", D("2025-06-10T15:30:00"), D("2025-06-10T14:59:59.999999")),
        ("5m", D("2025-06-10T15:07:00"), D("2025-06-10T15:04:59.999999")),
    ],
)
def test_last_complete_boundary(interval, now, expected):
    assert last_complete_boundary(interval, now) == expected
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd openbb-kdb && python -m pytest tests/test_cache.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'openbb_kdb.cache'`

- [ ] **Step 3: Implement `cache.py`**

Create `openbb-kdb/openbb_kdb/cache.py`:

```python
"""The read-through algorithm.

Serve what is cached, fetch only what is missing, store it, return the merge.
The cache is never allowed to be the reason a request fails: any kdb error
degrades to a straight upstream call reported as cache="bypass".
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta

from openbb_kdb.config import KdbConfig
from openbb_kdb.ranges import Range, coalesce, interval_step, subtract, trim_tail
from openbb_kdb.upstream import fetch_gap

logger = logging.getLogger(__name__)

_OVERLAP_BARS = 3  # tail bars re-fetched and compared, to catch corporate actions


def last_complete_boundary(interval: str, now: datetime) -> datetime:
    """The end of the most recent FULLY FORMED bar.

    Coverage is never recorded past this point, so the still-forming bar is
    refetched on every request rather than cached half-built.
    """
    step = interval_step(interval)
    if step >= timedelta(days=1):
        floor = datetime(now.year, now.month, now.day)
    else:
        seconds = int(step.total_seconds())
        epoch = datetime(1970, 1, 1)
        elapsed = int((now - epoch).total_seconds())
        floor = epoch + timedelta(seconds=elapsed - (elapsed % seconds))
    return floor - timedelta(microseconds=1)


class ReadThroughCache:
    """kdb-backed read-through cache over an upstream provider."""

    def __init__(self, store, config: KdbConfig):
        self.store = store
        self.config = config
        self._fetch_gap = fetch_gap
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _lock(self, symbol: str, interval: str) -> asyncio.Lock:
        key = (symbol, interval)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def get(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        model: str,
        params: dict,
        credentials: dict | None,
        now: datetime | None = None,
    ) -> tuple[list[dict], dict]:
        """Return (rows, metadata) for the requested window."""
        async with self._lock(symbol, interval):
            return await self._get(
                symbol, interval, start, end, model, params, credentials,
                now or datetime.now(),
            )

    async def _get(self, symbol, interval, start, end, model, params, credentials, now):
        meta = {
            "cache": "miss", "rows_from_cache": 0, "rows_from_upstream": 0,
            "gaps_fetched": 0, "upstream_ms": 0.0, "kdb_ms": 0.0,
        }
        try:
            return await self._read_through(
                symbol, interval, start, end, model, params, credentials, now, meta
            )
        except Exception as exc:  # noqa: BLE001 - the cache never fails a request
            logger.warning("kdb cache bypassed for %s %s: %s", symbol, interval, exc)
            rows, elapsed = await self._timed_fetch(
                model, params, credentials, symbol, start, end
            )
            meta.update(
                cache="bypass", rows_from_upstream=len(rows),
                gaps_fetched=1, upstream_ms=elapsed,
            )
            return rows, meta

    async def _read_through(
        self, symbol, interval, start, end, model, params, credentials, now, meta
    ):
        step = interval_step(interval)
        boundary = last_complete_boundary(interval, now)

        t0 = time.perf_counter()
        covered = coalesce(self.store.read_coverage(symbol, interval), step)
        meta["kdb_ms"] += (time.perf_counter() - t0) * 1000

        # The tail is never covered, so a request reaching into the present
        # always re-fetches the newest bars.
        effective = [r for r in (trim_tail(c, boundary) for c in covered) if r]
        gaps = subtract((start, end), effective, step)

        if covered and effective:
            await self._check_corporate_action(
                symbol, interval, effective, model, params, credentials, meta
            )
            t0 = time.perf_counter()
            covered = coalesce(self.store.read_coverage(symbol, interval), step)
            meta["kdb_ms"] += (time.perf_counter() - t0) * 1000
            effective = [r for r in (trim_tail(c, boundary) for c in covered) if r]
            gaps = subtract((start, end), effective, step)

        meta["cache"] = "hit" if not gaps else ("partial" if effective else "miss")

        for gap in gaps:
            rows, elapsed = await self._timed_fetch(
                model, params, credentials, symbol, gap[0], gap[1]
            )
            meta["upstream_ms"] += elapsed
            meta["rows_from_upstream"] += len(rows)
            meta["gaps_fetched"] += 1
            if not rows:
                continue
            frame = _to_frame(rows)
            t0 = time.perf_counter()
            self.store.write_bars(symbol, interval, frame)
            recorded = trim_tail(gap, boundary)
            if recorded:
                self.store.record_coverage(symbol, interval, recorded)
            meta["kdb_ms"] += (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        self.store.touch(symbol, interval)
        budget = int(self.config.memory_mb * 1024 * 1024 * self.config.watermark)
        self.store.evict_until_below(budget)
        frame = self.store.read_bars(symbol, interval, start, end)
        meta["kdb_ms"] += (time.perf_counter() - t0) * 1000

        rows = _from_frame(frame)
        meta["rows_from_cache"] = max(len(rows) - meta["rows_from_upstream"], 0)
        return rows, meta

    async def _check_corporate_action(
        self, symbol, interval, effective, model, params, credentials, meta
    ):
        """Compare re-fetched tail bars against cached ones.

        A split or dividend rewrites the whole adjusted series. The tail is
        being re-fetched anyway, so overlapping it with cached bars catches
        that for no extra traffic.
        """
        newest_end = max(e for _, e in effective)
        step = interval_step(interval)
        overlap_start = newest_end - step * _OVERLAP_BARS
        cached = self.store.read_bars(symbol, interval, overlap_start, newest_end)
        if cached is None or cached.empty:
            return
        fresh, elapsed = await self._timed_fetch(
            model, params, credentials, symbol, overlap_start, newest_end
        )
        meta["upstream_ms"] += elapsed
        if not fresh:
            return
        fresh_frame = _to_frame(fresh)
        merged = cached.merge(fresh_frame, on="t", suffixes=("_old", "_new"))
        if merged.empty:
            return
        drift = (merged["close_old"] - merged["close_new"]).abs() > 1e-6
        if bool(drift.any()):
            logger.info("adjusted history changed for %s; dropping cache", symbol)
            self.store.drop(symbol, interval)

    async def _timed_fetch(self, model, params, credentials, symbol, start, end):
        call = dict(params)
        call["symbol"] = symbol
        call["start_date"] = start.date() if isinstance(start, datetime) else start
        call["end_date"] = end.date() if isinstance(end, datetime) else end
        t0 = time.perf_counter()
        rows = await self._fetch_gap(self.config.upstream, model, call, credentials)
        return rows, (time.perf_counter() - t0) * 1000


def _to_frame(rows: list[dict]):
    import pandas as pd

    frame = pd.DataFrame(rows)
    stamp = "date" if "date" in frame.columns else frame.columns[0]
    frame = frame.rename(columns={stamp: "t"})
    frame["t"] = pd.to_datetime(frame["t"])
    return frame.sort_values("t").reset_index(drop=True)


def _from_frame(frame) -> list[dict]:
    if frame is None or getattr(frame, "empty", True):
        return []
    out = frame.rename(columns={"t": "date"})
    return out.to_dict(orient="records")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd openbb-kdb && python -m pytest tests/test_cache.py -q`
Expected: PASS, 12 passed

- [ ] **Step 5: Run the whole extension suite**

Run: `cd openbb-kdb && python -m pytest -q`
Expected: PASS, all tests

- [ ] **Step 6: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add openbb-kdb/openbb_kdb/cache.py openbb-kdb/tests/test_cache.py
git commit -m "feat(kdb): read-through cache with gap filling"
```

---

### Task 7: Provider registration

Expose the cache as `provider="kdb"` through OpenBB's fetcher interface.

**Files:**
- Create: `openbb-kdb/openbb_kdb/models/__init__.py`
- Create: `openbb-kdb/openbb_kdb/models/historical.py`
- Create: `openbb-kdb/openbb_kdb/__init__.py`
- Create: `openbb-kdb/tests/test_fetcher.py`
- Create: `openbb-kdb/README.md`

**Interfaces:**
- Consumes: `ReadThroughCache` (6), `KdbStore` (4), `KdbSession` (3), `resolve_config` (2).
- Produces: `KdbEquityHistoricalFetcher`, `KdbEtfHistoricalFetcher`, `KdbCryptoHistoricalFetcher`, `KdbCurrencyHistoricalFetcher`, `KdbIndexHistoricalFetcher`, and `kdb_provider`.

- [ ] **Step 1: Write the failing tests**

Create `openbb-kdb/tests/test_fetcher.py`:

```python
"""The fetcher: default window, cache metadata on the response, provider wiring."""

from datetime import date

from openbb_kdb.models.historical import KdbEquityHistoricalFetcher


def test_defaults_to_a_one_year_window():
    """The chart opens on 1 year; the default must match it."""
    q = KdbEquityHistoricalFetcher.transform_query({"symbol": "AAPL"})
    assert (q.end_date - q.start_date).days >= 364
    assert q.end_date <= date.today()


def test_explicit_dates_are_preserved():
    q = KdbEquityHistoricalFetcher.transform_query(
        {"symbol": "AAPL", "start_date": date(2022, 1, 1), "end_date": date(2024, 1, 1)}
    )
    assert (q.start_date, q.end_date) == (date(2022, 1, 1), date(2024, 1, 1))


def test_transform_data_attaches_cache_metadata():
    """The HUD reads this straight off extra['results_metadata']."""
    from openbb_core.provider.abstract.annotated_result import AnnotatedResult

    q = KdbEquityHistoricalFetcher.transform_query({"symbol": "AAPL"})
    rows = [{"date": date(2024, 1, 2), "open": 1.0, "high": 2.0,
             "low": 0.5, "close": 1.5, "volume": 100}]
    meta = {"cache": "hit", "rows_from_cache": 1, "rows_from_upstream": 0,
            "gaps_fetched": 0, "upstream_ms": 0.0, "kdb_ms": 1.2}
    out = KdbEquityHistoricalFetcher.transform_data(q, {"rows": rows, "meta": meta})
    assert isinstance(out, AnnotatedResult)
    assert out.metadata["cache"] == "hit"
    assert len(out.result) == 1


def test_provider_registers_all_five_models():
    from openbb_kdb import kdb_provider

    assert set(kdb_provider.fetcher_dict) == {
        "EquityHistorical", "EtfHistorical", "CryptoHistorical",
        "CurrencyHistorical", "IndexHistorical",
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd openbb-kdb && python -m pytest tests/test_fetcher.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'openbb_kdb.models'`

- [ ] **Step 3: Implement the fetchers**

Create `openbb-kdb/openbb_kdb/models/__init__.py` (empty file).

Create `openbb-kdb/openbb_kdb/models/historical.py`:

```python
"""OpenBB fetchers backed by the read-through cache.

One shared session per process: the q child process and its connection are
created once and reused by every fetcher.
"""

from datetime import date as dateType, datetime
from typing import Any

from openbb_core.provider.abstract.annotated_result import AnnotatedResult
from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.crypto_historical import (
    CryptoHistoricalData,
    CryptoHistoricalQueryParams,
)
from openbb_core.provider.standard_models.currency_historical import (
    CurrencyHistoricalData,
    CurrencyHistoricalQueryParams,
)
from openbb_core.provider.standard_models.equity_historical import (
    EquityHistoricalData,
    EquityHistoricalQueryParams,
)
from openbb_core.provider.standard_models.etf_historical import (
    EtfHistoricalData,
    EtfHistoricalQueryParams,
)
from openbb_core.provider.standard_models.index_historical import (
    IndexHistoricalData,
    IndexHistoricalQueryParams,
)

_SESSION = None
_CACHE = None


def _cache(credentials: dict | None):
    """Build (once) the shared session, store and cache."""
    global _SESSION, _CACHE  # noqa: PLW0603
    from openbb_kdb.cache import ReadThroughCache
    from openbb_kdb.config import resolve_config
    from openbb_kdb.session import KdbSession
    from openbb_kdb.store import KdbStore

    if _CACHE is None:
        config = resolve_config(credentials)
        _SESSION = KdbSession(config)
        _CACHE = ReadThroughCache(KdbStore(_SESSION), config)
    return _CACHE


def _default_window(params: dict) -> dict:
    """Default to one year -- the window the demo chart opens on."""
    from dateutil.relativedelta import relativedelta

    out = dict(params)
    today = datetime.now().date()
    if out.get("start_date") is None:
        out["start_date"] = today - relativedelta(years=1)
    if out.get("end_date") is None:
        out["end_date"] = today
    return out


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, dateType):
        return datetime(value.year, value.month, value.day)
    return datetime.fromisoformat(str(value))


async def _extract(query, credentials: dict | None, model: str) -> dict:
    """Run the read-through cache and hand back rows plus telemetry."""
    cache = _cache(credentials)
    params = query.model_dump(exclude_none=True)
    interval = getattr(query, "interval", None) or "1d"
    rows, meta = await cache.get(
        symbol=query.symbol,
        interval=interval,
        start=_as_datetime(query.start_date),
        end=_as_datetime(query.end_date),
        model=model,
        params=params,
        credentials=credentials,
    )
    return {"rows": rows, "meta": meta}


def _annotate(data: dict, data_cls) -> AnnotatedResult:
    results = [data_cls.model_validate(r) for r in data["rows"]]
    return AnnotatedResult(result=results, metadata=data["meta"])


class KdbEquityHistoricalFetcher(
    Fetcher[EquityHistoricalQueryParams, list[EquityHistoricalData]]
):
    """Equity bars served from the kdb+ cache, filled from the upstream provider."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EquityHistoricalQueryParams:
        return EquityHistoricalQueryParams(**_default_window(params))

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:
        return await _extract(query, credentials, "EquityHistorical")

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> AnnotatedResult:
        return _annotate(data, EquityHistoricalData)


class KdbEtfHistoricalFetcher(Fetcher[EtfHistoricalQueryParams, list[EtfHistoricalData]]):
    """ETF bars served from the kdb+ cache."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EtfHistoricalQueryParams:
        return EtfHistoricalQueryParams(**_default_window(params))

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:
        return await _extract(query, credentials, "EtfHistorical")

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> AnnotatedResult:
        return _annotate(data, EtfHistoricalData)


class KdbCryptoHistoricalFetcher(
    Fetcher[CryptoHistoricalQueryParams, list[CryptoHistoricalData]]
):
    """Crypto bars served from the kdb+ cache."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> CryptoHistoricalQueryParams:
        return CryptoHistoricalQueryParams(**_default_window(params))

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:
        return await _extract(query, credentials, "CryptoHistorical")

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> AnnotatedResult:
        return _annotate(data, CryptoHistoricalData)


class KdbCurrencyHistoricalFetcher(
    Fetcher[CurrencyHistoricalQueryParams, list[CurrencyHistoricalData]]
):
    """FX bars served from the kdb+ cache."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> CurrencyHistoricalQueryParams:
        return CurrencyHistoricalQueryParams(**_default_window(params))

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:
        return await _extract(query, credentials, "CurrencyHistorical")

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> AnnotatedResult:
        return _annotate(data, CurrencyHistoricalData)


class KdbIndexHistoricalFetcher(
    Fetcher[IndexHistoricalQueryParams, list[IndexHistoricalData]]
):
    """Index bars served from the kdb+ cache."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> IndexHistoricalQueryParams:
        return IndexHistoricalQueryParams(**_default_window(params))

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:
        return await _extract(query, credentials, "IndexHistorical")

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> AnnotatedResult:
        return _annotate(data, IndexHistoricalData)
```

Create `openbb-kdb/openbb_kdb/__init__.py`:

```python
"""kdb+ read-through cache provider for the OpenBB Platform."""

from openbb_core.provider.abstract.provider import Provider

from openbb_kdb.models.historical import (
    KdbCryptoHistoricalFetcher,
    KdbCurrencyHistoricalFetcher,
    KdbEquityHistoricalFetcher,
    KdbEtfHistoricalFetcher,
    KdbIndexHistoricalFetcher,
)

__all__ = ["kdb_provider"]

kdb_provider = Provider(
    name="kdb",
    website="https://kx.com",
    description=(
        "In-memory kdb+ read-through cache. Serves cached bars, fetches only the "
        "missing ranges from the upstream provider (KDB_UPSTREAM, default eodhd), "
        "and passes through when kdb+ is unavailable."
    ),
    fetcher_dict={
        "EquityHistorical": KdbEquityHistoricalFetcher,
        "EtfHistorical": KdbEtfHistoricalFetcher,
        "CryptoHistorical": KdbCryptoHistoricalFetcher,
        "CurrencyHistorical": KdbCurrencyHistoricalFetcher,
        "IndexHistorical": KdbIndexHistoricalFetcher,
    },
)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd openbb-kdb && python -m pytest tests/test_fetcher.py -q`
Expected: PASS, 4 passed

- [ ] **Step 5: Write the README**

Create `openbb-kdb/README.md`:

````markdown
# openbb-kdb

In-memory **kdb+ read-through cache** for the OpenBB Platform. Companion code
for *Adventures in OpenBB, Ep. 10*.

`provider="kdb"` serves bars it already holds, fetches only the date ranges it
is missing from an upstream provider, stores those, and returns the merged
series:

```python
obb.equity.price.historical("AAPL", provider="kdb",
                            start_date="2022-01-01", end_date="2025-01-01")
```

Every response carries cache telemetry in `extra["results_metadata"]`:

```python
{"cache": "partial", "rows_from_cache": 252, "rows_from_upstream": 504,
 "gaps_fetched": 1, "upstream_ms": 412.7, "kdb_ms": 3.1}
```

`cache` is `hit` (no upstream call), `partial` (gaps fetched), `miss` (nothing
cached) or `bypass` (kdb unavailable — served straight from upstream).

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `KDB_EMBEDDED` | `true` | Spawn q inside this container |
| `KDB_HOST` | `127.0.0.1` | Point at your own kdb+ server (disables spawning) |
| `KDB_PORT` | `5000` | |
| `KDB_MEMORY_MB` | `8192` | Cache budget; q gets `-w` 25% above it |
| `KDB_CACHE_WATERMARK` | `0.75` | Heap fraction that triggers LRU eviction |
| `KDB_UPSTREAM` | `eodhd` | Provider used for cache misses — any installed provider |

## Requirements

- A kdb+/kdb-x **license** (`kc.lic`) for the q server. PyKX itself runs as an
  unlicensed IPC client and needs none.
- Without a license or a reachable q, the provider passes through to the
  upstream and reports `cache: "bypass"`. Nothing breaks.

## Notes

- **The cache is memory-only.** A restart means a cold cache. It is a cache.
- **q binds `127.0.0.1`.** Every service in this stack shares one network
  namespace, so a `0.0.0.0` bind would publish an unauthenticated q — which
  executes arbitrary q — to every peer on the tailnet.
- **Crossing q's `-w` kills the process**; there is no catchable `'wsfull`.
  Eviction is preventive and `-w` is containment for the rest of the container.

## Test

    pip install -e .[dev] && pytest    # no kdb license or provider key needed
````

- [ ] **Step 6: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add openbb-kdb/openbb_kdb/__init__.py openbb-kdb/openbb_kdb/models/ \
        openbb-kdb/tests/test_fetcher.py openbb-kdb/README.md
git commit -m "feat(kdb): register provider=kdb with cache telemetry"
```

---

### Task 8: Image and compose wiring

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `ts-config/serve.json`
- Modify: `.gitignore`
- Modify: `scripts/verify-isolation.sh`
- Create: `kdb-license/.gitkeep`

**Interfaces:**
- Consumes: the `openbb-kdb` package (Tasks 1–7).
- Produces: an image with `q` at `/opt/kx/bin/q` and `openbb-kdb` installed; `kdb-license/` mounted read-only at `/opt/kx-license`.

- [ ] **Step 1: Read the current Dockerfile**

Run: `cd /Users/artcashin/Developer/openbb-docker && cat Dockerfile`

Identify where extensions are copied and installed (`openbb-eodhd` is the precedent) so the kdb stage matches the existing structure.

- [ ] **Step 2: Add the q runtime and the extension to the image**

Add near the top of `Dockerfile`, before the main image stage:

```dockerfile
# --- kdb-x runtime (Ep. 10) -------------------------------------------------
# The q SERVER binary only. kc.lic is deliberately NOT copied: this image is
# published, and a personal-edition license may not be redistributed. Readers
# mount their own at /opt/kx-license (see docker-compose.yml).
FROM ghcr.io/artcashin/kdb-x:latest AS kdbx
```

Then in the main stage, after the existing extension installs:

```dockerfile
# q runtime, minus the license.
COPY --from=kdbx /root/.kx /opt/kx
RUN rm -f /opt/kx/kc.lic

# kdb read-through cache provider (Ep. 10).
COPY openbb-kdb /tmp/openbb-kdb
RUN pip install --no-cache-dir /tmp/openbb-kdb && rm -rf /tmp/openbb-kdb
```

- [ ] **Step 3: Verify the license really is absent from the image**

```bash
cd /Users/artcashin/Developer/openbb-docker
docker build -t openbb-local:10.0.0 .
docker run --rm --entrypoint sh openbb-local:10.0.0 -c 'ls /opt/kx/kc.lic 2>&1; ls /opt/kx/bin/q'
```

Expected: `No such file or directory` for `kc.lic`, and `/opt/kx/bin/q` present.
**If `kc.lic` exists, stop and fix it before committing** — publishing it is a licensing violation and an identity leak.

- [ ] **Step 4: Wire compose**

In `docker-compose.yml`, under `openbb-api`, add:

```yaml
    # Ep. 10: the kdb+ read-through cache. q runs as a CHILD of this container
    # on 127.0.0.1:5000 -- never 0.0.0.0. Every service here shares the
    # tailscale network namespace, so loopback reaches all of them and no
    # tailnet peer, while a 0.0.0.0 bind would publish an unauthenticated q
    # (which executes arbitrary q) to the whole tailnet.
    #
    # Bring your own license: register with KX, drop kc.lic into
    # ./kdb-license/ (git-ignored). Without it the cache passes through to
    # the upstream provider and the stack runs uncached.
    environment:
      - KDB_EMBEDDED=true
      - KDB_MEMORY_MB=8192
      - KDB_UPSTREAM=eodhd
      - QHOME=/opt/kx
      - QLIC=/opt/kx-license
    volumes:
      - openbb-data:/root/.openbb_platform
      - ./kdb-license:/opt/kx-license:ro
    # KDB_MEMORY_MB + OpenBB's own ~2GB. Too low and the OOM killer takes the
    # API down, not just the cache.
    mem_limit: 11g
```

Keep the existing `openbb-data` volume entry — do not drop it when adding the license mount.

- [ ] **Step 5: Guard the bind in the isolation script**

Read `scripts/verify-isolation.sh`, then add a check in the same style as the existing port checks:

```bash
# Ep. 10: q must never be reachable from another tailnet device. If this
# succeeds, q is bound to 0.0.0.0 and every peer can execute arbitrary q.
check_port_closed "${HOST}" 5000 "kdb+ (q) IPC"
```

Match the helper name and argument order actually used in that file.

- [ ] **Step 6: Ignore the license directory**

Add to `.gitignore`:

```gitignore
# kdb+ license -- bring your own; never commit it.
kdb-license/*
!kdb-license/.gitkeep
```

Then: `mkdir -p kdb-license && touch kdb-license/.gitkeep`

- [ ] **Step 7: Verify the scrub gate and commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git status --short   # confirm NO kc.lic is staged
git add Dockerfile docker-compose.yml .gitignore scripts/verify-isolation.sh kdb-license/.gitkeep
git commit -m "feat(kdb): q runtime in the image, loopback-bound, BYO license"
```

---

### Task 9: Live integration check

The first point where real q, real PyKX and the real extension meet. Everything before this was mocked.

**Files:**
- Create: `openbb-kdb/scripts/live_check.py`

**Interfaces:**
- Consumes: everything from Tasks 1–8.
- Produces: a script that exits non-zero on failure. Not part of CI (it needs a license).

- [ ] **Step 1: Write the live check**

Create `openbb-kdb/scripts/live_check.py`:

```python
"""Live check: real q, real PyKX, real store. Requires a license; not in CI.

Run inside the container:
    docker compose run --rm openbb python /tmp/live_check.py
"""

import sys
from datetime import datetime, timedelta

import pandas as pd

from openbb_kdb.config import resolve_config
from openbb_kdb.session import KdbSession
from openbb_kdb.store import KdbStore

failures = []


def check(name, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}  {name} {detail}")
    if not condition:
        failures.append(name)


config = resolve_config()
session = KdbSession(config)
conn = session.connection()
store = KdbStore(session)

check("q answers", conn("1+1").py() == 2)

mem = store.memory()
check("memory keys are str", "heap" in mem and "wmax" in mem, str(mem)[:120])
check(
    "workspace is the configured headroom",
    mem["wmax"] == config.q_workspace_mb * 1024 * 1024,
    f"wmax={mem['wmax']}",
)

# Bars round-trip through the real PyKX conversions.
now = datetime(2025, 1, 10)
frame = pd.DataFrame({
    "t": [now - timedelta(days=i) for i in range(5)],
    "open": [1.0] * 5, "high": [2.0] * 5, "low": [0.5] * 5,
    "close": [1.5] * 5, "volume": [100] * 5,
})
store.write_bars("LIVECHK", "1d", frame)
back = store.read_bars("LIVECHK", "1d", now - timedelta(days=2), now)
check("bars round-trip", len(back) == 3, f"got {len(back)} rows")

# Writing twice must not duplicate rows.
store.write_bars("LIVECHK", "1d", frame)
back2 = store.read_bars("LIVECHK", "1d", now - timedelta(days=10), now)
check("upsert does not duplicate", len(back2) == 5, f"got {len(back2)} rows")

# Coverage round-trip (real timestamp conversion).
store.record_coverage("LIVECHK", "1d", (now - timedelta(days=5), now))
cov = store.read_coverage("LIVECHK", "1d")
check("coverage round-trip", len(cov) == 1, str(cov)[:120])

# Eviction genuinely returns heap -- delete alone does not.
before = store.memory()["heap"]
conn("ballast: 4000000 # 1.0")
grown = store.memory()["heap"]
conn("delete ballast from `.")
conn(".Q.gc[]")
after = store.memory()["heap"]
check("gc reclaims heap", after < grown, f"{grown} -> {after}")

store.drop("LIVECHK", "1d")
check("drop clears coverage", store.read_coverage("LIVECHK", "1d") == [])

# One shared connection used from several threads (spec risk 3).
import threading

results = {}


def worker(tag):
    try:
        results[tag] = conn(f"{tag}+1").py()
    except Exception as exc:  # noqa: BLE001
        results[tag] = f"ERR {type(exc).__name__}"


threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 4)]
[t.start() for t in threads]
[t.join() for t in threads]
ok = all(results.get(i) == i + 1 for i in range(1, 4))
check("shared connection across threads", ok, str(results))
if not ok:
    print("  -> serialize q access behind one lock, or open a connection per thread")

session.close()
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
```

- [ ] **Step 2: Run it inside the container**

```bash
cd /Users/artcashin/Developer/openbb-docker
docker compose up -d
docker compose cp openbb-kdb/scripts/live_check.py openbb-api:/tmp/live_check.py
docker compose exec openbb-api python /tmp/live_check.py
```

Expected: every line `PASS`, `FAILURES: none`, exit 0.

If **"shared connection across threads"** fails, that is spec risk 3 landing. Fix it in `session.py` by guarding `connection()` usage with a `threading.Lock` held for the duration of each q call, then re-run. Do not proceed with a known-broken concurrency story.

- [ ] **Step 3: Verify the end-to-end cache through the API**

```bash
source api-auth.env 2>/dev/null || true
BASE="http://127.0.0.1:6900/api/v1/equity/price/historical"
Q="symbol=AAPL&provider=kdb&start_date=2024-01-01&end_date=2024-12-31"

docker compose exec openbb-api sh -c \
  "curl -s -u \$OPENBB_API_USERNAME:\$OPENBB_API_PASSWORD '$BASE?$Q' | head -c 400"
```

Expected: JSON whose `extra.results_metadata.cache` is `miss` on the first call.
Run the identical command again — expected `hit` with `rows_from_upstream: 0`.
Then widen to `start_date=2022-01-01` — expected `partial` with exactly one gap.

That third result is the episode's claim, verified end to end.

- [ ] **Step 4: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add openbb-kdb/scripts/live_check.py
git commit -m "test(kdb): live integration check against real q"
```

---

### Task 10: The chart service

**Files:**
- Create: `cache-chart/pyproject.toml`, `cache-chart/Dockerfile`, `cache-chart/widgets.json`
- Create: `cache-chart/app/__init__.py`, `app/openbb_client.py`, `app/figure.py`, `app/main.py`
- Create: `cache-chart/tests/__init__.py`, `tests/test_openbb_client.py`, `tests/test_figure.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: the running OpenBB API with `provider=kdb`.
- Produces: `fetch_series(symbol, interval, start, end, provider) -> tuple[list[dict], dict]`; `build_figure(symbol, bars) -> dict`; a FastAPI `app` serving `/widgets.json`, `/chart`, `/series`, `/demo`, `/health`.

- [ ] **Step 1: Write the failing tests**

Create `cache-chart/tests/test_openbb_client.py`:

```python
"""The client: talks to the OpenBB API on loopback, surfaces cache metadata."""

import pytest

from app.openbb_client import fetch_series


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def payload():
    return {
        "results": [{"date": "2024-01-02", "open": 1.0, "high": 2.0,
                     "low": 0.5, "close": 1.5, "volume": 10}],
        "extra": {"results_metadata": {"cache": "partial", "rows_from_cache": 100,
                                       "rows_from_upstream": 5, "gaps_fetched": 1,
                                       "upstream_ms": 12.5, "kdb_ms": 0.9}},
    }


async def test_returns_bars_and_cache_metadata(monkeypatch, payload):
    async def fake_get(self, url, **kw):
        return FakeResponse(payload)

    monkeypatch.setattr("httpx.AsyncClient.get", fake_get)
    bars, meta = await fetch_series("AAPL", "1d", "2024-01-01", "2024-12-31", "kdb")
    assert len(bars) == 1
    assert meta["cache"] == "partial"


async def test_provider_is_forwarded(monkeypatch, payload):
    seen = {}

    async def fake_get(self, url, **kw):
        seen["params"] = kw.get("params")
        return FakeResponse(payload)

    monkeypatch.setattr("httpx.AsyncClient.get", fake_get)
    await fetch_series("AAPL", "1d", "2024-01-01", "2024-12-31", "eodhd")
    assert seen["params"]["provider"] == "eodhd"


async def test_missing_metadata_reports_unknown(monkeypatch):
    """A provider without the cache (eodhd direct) still renders."""

    async def fake_get(self, url, **kw):
        return FakeResponse({"results": [], "extra": {}})

    monkeypatch.setattr("httpx.AsyncClient.get", fake_get)
    bars, meta = await fetch_series("AAPL", "1d", "2024-01-01", "2024-12-31", "eodhd")
    assert meta["cache"] == "unknown"
```

Create `cache-chart/tests/test_figure.py`:

```python
"""Figure construction."""

from app.figure import build_figure

BARS = [
    {"date": "2024-01-02", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
    {"date": "2024-01-03", "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0},
]


def test_figure_has_one_trace_with_every_bar():
    fig = build_figure("AAPL", BARS)
    assert len(fig["data"]) == 1
    assert len(fig["data"][0]["x"]) == 2


def test_symbol_appears_in_the_title():
    assert "AAPL" in fig_title(build_figure("AAPL", BARS))


def fig_title(fig):
    title = fig["layout"]["title"]
    return title["text"] if isinstance(title, dict) else title


def test_empty_bars_still_produce_a_valid_figure():
    fig = build_figure("AAPL", [])
    assert fig["data"][0]["x"] == []
```

Create `cache-chart/tests/test_main.py`:

```python
"""Routes."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def stub_series(monkeypatch):
    async def fake(symbol, interval, start, end, provider):
        return (
            [{"date": "2024-01-02", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}],
            {"cache": "hit", "rows_from_cache": 1, "rows_from_upstream": 0,
             "gaps_fetched": 0, "upstream_ms": 0.0, "kdb_ms": 0.4},
        )

    monkeypatch.setattr("app.main.fetch_series", fake)


def test_widgets_json_is_served():
    r = client.get("/widgets.json")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_series_returns_bars_and_cache_block():
    r = client.get("/series", params={"symbol": "AAPL", "start": "2024-01-01",
                                      "end": "2024-12-31"})
    assert r.status_code == 200
    body = r.json()
    assert body["cache"]["cache"] == "hit"
    assert len(body["bars"]) == 1


def test_chart_returns_plotly_figure_json():
    r = client.get("/chart", params={"symbol": "AAPL"})
    assert r.status_code == 200
    assert "data" in r.json() and "layout" in r.json()


def test_demo_page_is_html_with_scroll_enabled():
    r = client.get("/demo")
    assert r.status_code == 200
    assert "scrollZoom" in r.text
    assert "plotly_relayout" in r.text


def test_health_reports_provider():
    assert client.get("/health").status_code == 200
```

- [ ] **Step 2: Create the package skeleton and run the tests to verify they fail**

```bash
cd /Users/artcashin/Developer/openbb-docker
mkdir -p cache-chart/app/static cache-chart/tests
touch cache-chart/app/__init__.py cache-chart/tests/__init__.py
```

Create `cache-chart/pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "cache-chart"
version = "0.1.0"
description = "OpenBB Workspace chart backend demonstrating the kdb+ read-through cache"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.110",
    "uvicorn>=0.29",
    "httpx>=0.27",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "httpx", "ruff"]

[tool.setuptools]
packages = ["app"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
asyncio_mode = "auto"

[tool.ruff]
target-version = "py312"
line-length = 100
```

Run: `cd cache-chart && python -m pytest -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.openbb_client'`

- [ ] **Step 3: Implement the client**

Create `cache-chart/app/openbb_client.py`:

```python
"""Client for the OpenBB API on loopback.

Going through the real API (rather than importing the provider) means the demo
exercises exactly the path any other client takes -- including the cache
metadata the Platform attaches to every response.
"""

import os

OPENBB_URL = os.getenv("OPENBB_URL", "http://127.0.0.1:6900")
USERNAME = os.getenv("OPENBB_API_USERNAME", "")
PASSWORD = os.getenv("OPENBB_API_PASSWORD", "")

_UNKNOWN = {
    "cache": "unknown", "rows_from_cache": 0, "rows_from_upstream": 0,
    "gaps_fetched": 0, "upstream_ms": 0.0, "kdb_ms": 0.0,
}


async def fetch_series(
    symbol: str, interval: str, start: str, end: str, provider: str = "kdb"
) -> tuple[list[dict], dict]:
    """Return (bars, cache_metadata) for one window."""
    import httpx

    params = {
        "symbol": symbol, "provider": provider, "interval": interval,
        "start_date": start, "end_date": end,
    }
    auth = (USERNAME, PASSWORD) if USERNAME else None
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            f"{OPENBB_URL}/api/v1/equity/price/historical", params=params, auth=auth
        )
        response.raise_for_status()
        payload = response.json()
    bars = payload.get("results") or []
    meta = (payload.get("extra") or {}).get("results_metadata") or dict(_UNKNOWN)
    return bars, meta
```

- [ ] **Step 4: Implement the figure builder**

Create `cache-chart/app/figure.py`:

```python
"""Plotly figure JSON. The client renders it -- this image has no chart backend."""


def build_figure(symbol: str, bars: list[dict]) -> dict:
    """A candlestick figure for the given bars."""
    return {
        "data": [
            {
                "type": "candlestick",
                "name": symbol,
                "x": [b.get("date") for b in bars],
                "open": [b.get("open") for b in bars],
                "high": [b.get("high") for b in bars],
                "low": [b.get("low") for b in bars],
                "close": [b.get("close") for b in bars],
            }
        ],
        "layout": {
            "title": {"text": f"{symbol} — served through the kdb+ cache"},
            "xaxis": {"rangeslider": {"visible": False}},
            "margin": {"l": 40, "r": 20, "t": 40, "b": 40},
            "template": "plotly_dark",
        },
    }
```

- [ ] **Step 5: Implement the routes**

Create `cache-chart/widgets.json`:

```json
{
  "kdb_cache_chart": {
    "name": "Cached Price Chart",
    "description": "Historical prices served through the kdb+ read-through cache",
    "category": "Equity",
    "type": "chart",
    "endpoint": "chart",
    "gridData": { "w": 20, "h": 12 },
    "params": [
      { "paramName": "symbol", "value": "AAPL", "label": "Symbol", "type": "text" },
      {
        "paramName": "interval", "value": "1d", "label": "Interval", "type": "text",
        "options": [
          { "label": "1 day", "value": "1d" },
          { "label": "1 hour", "value": "1h" },
          { "label": "5 min", "value": "5m" }
        ]
      }
    ]
  }
}
```

Create `cache-chart/app/main.py`:

```python
"""Routes for the cache demo: a Workspace widget and a standalone page."""

import json
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from app.figure import build_figure
from app.openbb_client import fetch_series

app = FastAPI(title="cache-chart")

_HERE = Path(__file__).parent
_WIDGETS = _HERE.parent / "widgets.json"
_STATIC = _HERE / "static"


def _window(start: str | None, end: str | None) -> tuple[str, str]:
    """Default to one year -- the window the demo opens on."""
    today = date.today()
    return (start or str(today - timedelta(days=365)), end or str(today))


@app.get("/widgets.json")
async def widgets():
    return JSONResponse(json.loads(_WIDGETS.read_text()))


@app.get("/series")
async def series(
    symbol: str = "AAPL",
    interval: str = "1d",
    start: str | None = None,
    end: str | None = None,
    provider: str = "kdb",
):
    """Bars plus the cache telemetry that drives the HUD."""
    s, e = _window(start, end)
    bars, meta = await fetch_series(symbol, interval, s, e, provider)
    return {"symbol": symbol, "interval": interval, "start": s, "end": e,
            "bars": bars, "cache": meta}


@app.get("/chart")
async def chart(
    symbol: str = "AAPL",
    interval: str = "1d",
    start: str | None = None,
    end: str | None = None,
    provider: str = "kdb",
):
    """Plotly figure JSON for the Workspace widget."""
    s, e = _window(start, end)
    bars, _ = await fetch_series(symbol, interval, s, e, provider)
    return JSONResponse(build_figure(symbol, bars))


@app.get("/demo", response_class=HTMLResponse)
async def demo():
    return HTMLResponse((_STATIC / "demo.html").read_text())


@app.get("/health")
async def health():
    try:
        _, meta = await fetch_series(
            "AAPL", "1d", str(date.today() - timedelta(days=5)), str(date.today()), "kdb"
        )
        return {"ok": True, "cache": meta.get("cache")}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
```

- [ ] **Step 6: Write the demo page**

Create `cache-chart/app/static/demo.html`. It loads `/static/plotly.min.js`, which the Dockerfile vendors into the image in Step 8 — there is no CDN reference at runtime.

```html
<!doctype html>
<meta charset="utf-8">
<title>kdb+ read-through cache</title>
<script src="/static/plotly.min.js"></script>
<style>
  body { background:#111; color:#ddd; font:14px/1.5 ui-monospace, monospace; margin:0; padding:16px; }
  #chart { height:60vh; }
  table { border-collapse:collapse; margin-top:12px; width:100%; }
  th, td { text-align:left; padding:4px 10px; border-bottom:1px solid #333; }
  .hit { color:#4ade80; } .partial { color:#fbbf24; }
  .miss, .bypass { color:#f87171; }
  header { display:flex; gap:16px; align-items:center; flex-wrap:wrap; }
  select, input { background:#222; color:#ddd; border:1px solid #444; padding:4px; }
</style>

<header>
  <strong>kdb+ read-through cache</strong>
  <label>symbol <input id="symbol" value="AAPL" size="6"></label>
  <label>provider
    <select id="provider">
      <option value="kdb">kdb (cached)</option>
      <option value="eodhd">eodhd (no cache)</option>
    </select>
  </label>
  <span>scroll to zoom out — only the missing range is fetched</span>
</header>

<div id="chart"></div>
<table id="hud">
  <thead><tr><th>window</th><th>cache</th><th>from cache</th><th>from upstream</th>
  <th>upstream ms</th><th>kdb ms</th><th>bytes</th></tr></thead>
  <tbody></tbody>
</table>
<p id="total"></p>

<script>
const chart = document.getElementById("chart");
let loadedStart = null, loadedEnd = null, bars = [], inflight = false, savedBytes = 0;
const iso = d => d.toISOString().slice(0, 10);
const symbol = () => document.getElementById("symbol").value.trim().toUpperCase();
const provider = () => document.getElementById("provider").value;

async function load(start, end) {
  if (inflight) return;
  inflight = true;
  const t0 = performance.now();
  const url = `/series?symbol=${symbol()}&start=${start}&end=${end}&provider=${provider()}`;
  const res = await fetch(url);
  const text = await res.text();
  const body = JSON.parse(text);
  inflight = false;
  hud(start, end, body.cache, new Blob([text]).size, performance.now() - t0);
  return body.bars;
}

function hud(start, end, cache, bytes) {
  const row = document.createElement("tr");
  const kind = cache.cache || "unknown";
  if (kind === "hit") savedBytes += bytes;
  row.innerHTML = `<td>${start} → ${end}</td>
    <td class="${kind}">${kind}</td>
    <td>${cache.rows_from_cache ?? 0}</td>
    <td>${cache.rows_from_upstream ?? 0}</td>
    <td>${(cache.upstream_ms ?? 0).toFixed(1)}</td>
    <td>${(cache.kdb_ms ?? 0).toFixed(1)}</td>
    <td>${bytes}</td>`;
  document.querySelector("#hud tbody").prepend(row);
  document.getElementById("total").textContent =
    `served without touching the network: ${savedBytes} bytes`;
}

function draw() {
  Plotly.react(chart, [{
    type: "candlestick", name: symbol(),
    x: bars.map(b => b.date), open: bars.map(b => b.open),
    high: bars.map(b => b.high), low: bars.map(b => b.low),
    close: bars.map(b => b.close),
  }], {
    title: { text: `${symbol()} — scroll to extend` },
    xaxis: { rangeslider: { visible: false } },
    template: "plotly_dark", paper_bgcolor: "#111", plot_bgcolor: "#111",
    font: { color: "#ddd" }, margin: { l: 40, r: 20, t: 40, b: 40 },
  }, { scrollZoom: true, displayModeBar: false });
}

// A continuous scroll fires many relayout events; debounce so ONE request
// goes out when the gesture settles.
let timer = null;
function onRelayout(ev) {
  const start = ev["xaxis.range[0]"];
  if (!start) return;
  clearTimeout(timer);
  timer = setTimeout(async () => {
    const wanted = new Date(start);
    const floor = new Date(loadedEnd);
    floor.setFullYear(floor.getFullYear() - 10);       // clamp
    const from = wanted < floor ? floor : wanted;
    if (from >= new Date(loadedStart)) return;          // already loaded
    const gapEnd = new Date(loadedStart);
    gapEnd.setDate(gapEnd.getDate() - 1);
    const fetched = await load(iso(from), iso(gapEnd));
    if (!fetched) return;
    bars = fetched.concat(bars);
    loadedStart = iso(from);
    draw();
    chart.on("plotly_relayout", onRelayout);
  }, 150);
}

(async function boot() {
  const end = new Date();
  const start = new Date();
  start.setFullYear(start.getFullYear() - 1);
  loadedStart = iso(start); loadedEnd = iso(end);
  bars = await load(loadedStart, loadedEnd) || [];
  draw();
  chart.on("plotly_relayout", onRelayout);
  document.getElementById("symbol").onchange =
  document.getElementById("provider").onchange = async () => {
    bars = await load(loadedStart, loadedEnd) || [];
    draw();
  };
})();
</script>
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd cache-chart && pip install -e .[dev] && python -m pytest -q`
Expected: PASS, 12 passed

- [ ] **Step 8: Write the Dockerfile with plotly vendored**

Create `cache-chart/Dockerfile`:

```dockerfile
# cache-chart: the kdb+ cache demo — Workspace widget + standalone scroll page.
# Loopback-only; Tailscale Serve publishes it (see repo compose).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

# plotly.js is vendored at build time: the page must render on a tailnet with
# no route to a CDN.
ADD https://cdn.plot.ly/plotly-2.35.2.min.js /srv/app/static/plotly.min.js

COPY pyproject.toml ./
COPY app/ app/
COPY widgets.json ./
RUN pip install . && chmod 644 app/static/plotly.min.js
CMD ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "6906"]
```

Serve the vendored library. In `cache-chart/app/main.py`, add to the imports:

```python
from fastapi.staticfiles import StaticFiles
```

and add this single line immediately **after** the `_STATIC = _HERE / "static"` assignment (it must come after, because the mount reads that path):

```python
app.mount("/static", StaticFiles(directory=_STATIC), name="static")
```

Create the directory so the mount resolves during tests: `mkdir -p cache-chart/app/static`

- [ ] **Step 9: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add cache-chart/
git commit -m "feat(cache-chart): widget + scroll demo for the kdb cache"
```

---

### Task 11: Ship it — compose, Serve, docs

**Files:**
- Modify: `docker-compose.yml`, `ts-config/serve.json`, `ts-config/serve-funnel.json`, `README.md`
- Create: `cache-chart/README.md`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Add the service to compose**

Append to `docker-compose.yml` services:

```yaml
  # cache-chart (Ep. 10): the kdb+ cache demo — a Workspace chart widget and a
  # standalone page whose scroll gesture shows only the missing range being
  # fetched. Loopback :6906; Serve publishes it on :6906 — tailnet-only,
  # NEVER funneled (it holds API credentials and has no auth of its own).
  cache-chart:
    build: ./cache-chart
    image: openbb-cache-chart:latest
    container_name: openbb-cache-chart
    restart: unless-stopped
    network_mode: service:tailscale
    depends_on:
      - tailscale
      - openbb-api
    environment:
      - OPENBB_URL=http://127.0.0.1:6900
    env_file:
      # OPENBB_API_USERNAME / PASSWORD — the API enforces Basic auth.
      - path: ./api-auth.env
        required: true
```

- [ ] **Step 2: Publish it through Serve**

Read `ts-config/serve.json` and add a `:6906` entry matching the existing
`:6903` (live-grid) block exactly in shape, pointing at `http://127.0.0.1:6906`.

Leave `ts-config/serve-funnel.json` **unchanged** — this service is never funneled.

Verify the file parses: `python -m json.tool ts-config/serve.json > /dev/null && echo OK`

- [ ] **Step 3: Write the service README**

Create `cache-chart/README.md`:

````markdown
# cache-chart

The **kdb+ read-through cache**, made visible. Companion code for
*Adventures in OpenBB, Ep. 10*.

- `GET /widgets.json` — the Workspace widget contract
- `GET /chart` — Plotly figure JSON (the widget)
- `GET /series` — `{bars, cache}` for incremental loads
- `GET /demo` — the standalone page
- `GET /health` — cache reachability

## The demo

Open `https://openbb.<your-tailnet>.ts.net:6906/demo`. The chart opens on one
year of daily bars. **Scroll out** and the page requests only the range it does
not already have; the HUD shows rows served from cache versus fetched upstream.

Scroll back in and out again: `hit`, zero rows upstream, no network traffic.
The provider toggle runs the same gesture with the cache off.

A Workspace plotly widget zooms client-side and will not refetch, so it
benefits from the cache on parameter changes rather than on scroll — which is
why the scroll demo is a page of its own.

## Test

    pip install -e .[dev] && pytest    # mocked; no key or license needed
````

- [ ] **Step 4: Bring the stack up and verify**

```bash
cd /Users/artcashin/Developer/openbb-docker
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:6906/health
```

Expected: all containers `Up`; health returns `{"ok": true, ...}`.

Then, from a tailnet device, open `https://openbb.<your-tailnet>.ts.net:6906/demo`,
scroll out, and confirm the HUD shows `partial` on the first zoom and `hit` on
the repeat.

- [ ] **Step 5: Confirm the walls still stand**

```bash
scripts/verify-isolation.sh openbb.<your-tailnet>.ts.net
```

Expected: PASS, including the new port-5000 check. **A reachable 5000 is a
release blocker** — it means an unauthenticated q is exposed to the tailnet.

- [ ] **Step 6: Update the top-level README**

Add to the release table:

```markdown
| v10.0.0 | Ep. 10 — The Cache | kdb+ read-through cache (`provider="kdb"`) + cache-chart scroll demo |
```

Add a "New in v10.0.0" section above the v9.0.0 section, in the same voice:
cover `provider="kdb"`, gap-filling, the memory-only cache, the BYO license and
pass-through behaviour, and the loopback bind. Link `openbb-kdb/README.md`,
`cache-chart/README.md` and `docs/kdb-cache-design.md`.

- [ ] **Step 7: Commit**

```bash
cd /Users/artcashin/Developer/openbb-docker
bash scripts/scrub-check.sh
git add docker-compose.yml ts-config/serve.json README.md cache-chart/README.md
git commit -m "feat: ship the kdb cache and its demo (Ep. 10)"
```

---

### Task 12: CI

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Read the existing workflow**

Run: `cat .github/workflows/ci.yml`

Note how `openbb-eodhd` and `live-grid` are tested so the new jobs match.

- [ ] **Step 2: Add the two test jobs**

Following the existing pattern exactly, add steps that run:

```bash
cd openbb-kdb && pip install -e .[dev] && pytest -q
cd cache-chart && pip install -e .[dev] && pytest -q
```

Neither needs a license or an API key — that is a property worth preserving.
If `pip install -e openbb-kdb` pulls a heavy `openbb-core`, mirror whatever
constraint mechanism `openbb-eodhd`'s job already uses
(`extension-constraints.txt`).

- [ ] **Step 3: Verify locally**

```bash
cd /Users/artcashin/Developer/openbb-docker
(cd openbb-kdb && python -m pytest -q)
(cd cache-chart && python -m pytest -q)
bash scripts/scrub-check.sh
```

Expected: both suites pass; scrub passes.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: test openbb-kdb and cache-chart"
```

---

## Verification

The episode's claim is verified when all of the following hold:

1. `cd openbb-kdb && pytest -q` — passes with no license and no key.
2. `cd cache-chart && pytest -q` — passes.
3. `docker compose exec openbb-api python /tmp/live_check.py` — `FAILURES: none`.
4. Requesting `provider=kdb` for 2024 twice reports `cache: "miss"` then
   `cache: "hit"` with `rows_from_upstream: 0`.
5. Widening that request to 2022 reports `cache: "partial"` with
   `gaps_fetched: 1` — **the claim: one gap, not a refetch**.
6. `scripts/verify-isolation.sh` passes, including port 5000.
7. `docker run --rm --entrypoint sh openbb-local:10.0.0 -c 'ls /opt/kx/kc.lic'`
   reports the file is absent.
8. Removing `kdb-license/kc.lic` and restarting leaves the stack working, with
   responses reporting `cache: "bypass"`.
9. `bash scripts/scrub-check.sh` passes.

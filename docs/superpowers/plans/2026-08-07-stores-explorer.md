# stores-explorer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `stores-explorer`, a new read-only HTTP service that lets a bdobb widget browse the shared ArcticDB and kdb+ stores — the widget's door into the vault, pairing with `stores-mcp` (the analyst's door).

**Architecture:** A single FastAPI app (`stores-explorer/app/main.py`), no app-level auth (loopback bind + Tailscale Serve is the only ingress, matching `live-grid`/`stores-mcp`). Every ArcticDB/kdb+ call is a thin wrapper over `mcp_stores`'s existing, already-tested discovery functions, imported directly (installed as a sibling package in the Docker image, exactly how `live-grid` already installs `kdb-store`). Every backend call is injected into `create_app` as a keyword argument defaulting to the real `mcp_stores` import, so the test suite never touches a real ArcticDB or kdb+.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, pytest. Reuses `mcp_stores` (already in this repo) for all ArcticDB/kdb+ client logic.

## Global Constraints

- No app-level auth on this service — topology only (design spec D1).
- CORS wide open (`allow_origins=["*"]`), matching `live-grid`, not `key-maint` (D2).
- Zero duplicated ArcticDB/kdb+ client logic — every backend call traces to an imported `mcp_stores` function, or (for `/arctic/summary` only) `mcp_stores`'s own `_arctic`/`_bounded` helpers (D3, D6).
- Two additive widgets, `type: "table"`, no new bdobb code required (D4, D5).
- Static `widgets.json` file, not an inline Python dict (D7).
- `scripts/scrub-check.sh` must stay green — no credential, bucket path, or host name in any tracked file.

Full rationale: `docs/superpowers/specs/2026-08-07-stores-explorer-design.md`.

---

### Task 1: App skeleton + ArcticDB endpoints + `arctic_explorer` widget

**Files:**
- Create: `stores-explorer/pyproject.toml`
- Create: `stores-explorer/app/__init__.py`
- Create: `stores-explorer/app/main.py`
- Create: `stores-explorer/widgets.json`
- Create: `stores-explorer/tests/__init__.py`
- Create: `stores-explorer/tests/test_main.py`

**Interfaces:**
- Produces: `create_app(*, arctic_libraries_fn=..., arctic_symbols_fn=..., arctic_read_fn=..., arctic_client_factory=..., bounded_fn=...) -> FastAPI`, module-level `app = create_app()`. Task 2 extends this signature with three more keyword arguments (`kdb_tables_fn`, `kdb_schema_fn`, `kdb_select_fn`) and adds three more routes to the same file.

- [ ] **Step 1: Confirm `mcp_stores` installs and its functions import as expected**

Run (from the worktree root):
```bash
python3 -m venv /tmp/se-check && source /tmp/se-check/bin/activate
pip install -q -e ./mcp_stores
python3 -c "from server import arctic_list_libraries, arctic_list_symbols, arctic_read, _arctic, _bounded; print('ok')"
deactivate && rm -rf /tmp/se-check
```
Expected: prints `ok`. This confirms `mcp_stores` installs as a bare top-level `server` module (its `pyproject.toml` declares `py-modules = ["server"]`) before writing any code against it.

- [ ] **Step 2: Write `stores-explorer/pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "stores-explorer"
version = "11.2.0"
description = "Read-only widget backend for browsing the shared ArcticDB/kdb+ store"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.110",
    "uvicorn>=0.29",
    "mcp-stores",
]

[project.optional-dependencies]
dev = ["pytest", "httpx", "ruff"]

[tool.setuptools]
packages = ["app"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]

[tool.ruff]
target-version = "py312"
line-length = 100
```

`mcp-stores` is not on PyPI — listing it bare here documents the dependency; it must already be installed in the environment (Task 3's Dockerfile does this, matching exactly how `live-grid/pyproject.toml` lists bare `"kdb-store"`).

- [ ] **Step 3: Write `stores-explorer/app/__init__.py`**

```python
```
(empty file — matches `key-maint/app/__init__.py` and `live-grid/app/__init__.py`)

- [ ] **Step 4: Write the failing tests**

Create `stores-explorer/tests/__init__.py` (empty), then `stores-explorer/tests/test_main.py`:

```python
"""Server-layer tests for the ArcticDB half. Every backend call is injected
-- no real ArcticDB or kdb+ is ever touched."""
from fastapi.testclient import TestClient

from app.main import create_app


class FakeColumn:
    def __init__(self, name, dtype):
        self.name = name
        self.dtype = dtype


class FakeSymbolDescription:
    def __init__(self, row_count, date_range, columns):
        self.row_count = row_count
        self.date_range = date_range
        self.columns = columns


class FakeLib:
    def __init__(self, description):
        self._description = description

    def get_description(self, symbol):
        return self._description


class FakeArctic:
    def __init__(self, libs):
        self._libs = libs  # dict: library name -> FakeLib

    def __getitem__(self, library):
        return self._libs[library]


DEFAULT_DESC = FakeSymbolDescription(
    row_count=42,
    date_range=("2026-01-01", "2026-08-07"),
    columns=[FakeColumn("close", "float64")],
)


def make_client(libraries=("openbb",), symbols_by_library=None, series_response=None):
    symbols_by_library = symbols_by_library if symbols_by_library is not None else {"openbb": ["AAPL"]}

    def arctic_libraries_fn():
        return list(libraries)

    def arctic_symbols_fn(library):
        if library not in libraries:
            raise ValueError(f"unknown library {library!r}; call arctic_list_libraries first")
        return symbols_by_library.get(library, [])

    def arctic_read_fn(library, symbol, start=None, end=None, tail_rows=1000):
        if library not in libraries:
            raise ValueError(f"unknown library {library!r}; call arctic_list_libraries first")
        if symbol not in symbols_by_library.get(library, []):
            raise ValueError(f"unknown symbol {symbol!r} in {library!r}; call arctic_list_symbols first")
        return series_response or {
            "library": library, "symbol": symbol,
            "total_rows_in_range": 1, "returned_rows": 1,
            "rows": [{"date": "2026-08-07", "close": 100.0}],
        }

    def arctic_client_factory():
        return FakeArctic({lib: FakeLib(DEFAULT_DESC) for lib in libraries})

    app = create_app(
        arctic_libraries_fn=arctic_libraries_fn,
        arctic_symbols_fn=arctic_symbols_fn,
        arctic_read_fn=arctic_read_fn,
        arctic_client_factory=arctic_client_factory,
        bounded_fn=lambda fn, *a, **kw: fn(*a, **kw),
    )
    return TestClient(app)


def test_widgets_json_declares_arctic_explorer():
    body = make_client().get("/widgets.json").json()
    w = body["arctic_explorer"]
    assert w["type"] == "table"
    assert w["endpoint"] == "arctic/series"
    assert w["dataKey"] == "rows"
    param_names = [p["paramName"] for p in w["params"]]
    assert param_names == ["library", "symbol", "start", "end"]
    symbol_param = next(p for p in w["params"] if p["paramName"] == "symbol")
    assert symbol_param["optionsEndpoint"] == "arctic/symbols"
    assert symbol_param["optionsParams"] == {"library": "$library"}


def test_arctic_libraries_lists_libraries():
    r = make_client(libraries=("openbb", "ticks"))
    assert r.get("/arctic/libraries").json() == ["openbb", "ticks"]


def test_arctic_symbols_lists_symbols_for_library():
    client = make_client(symbols_by_library={"openbb": ["AAPL", "MSFT"]})
    r = client.get("/arctic/symbols", params={"library": "openbb"})
    assert r.json() == ["AAPL", "MSFT"]


def test_arctic_symbols_unknown_library_is_404():
    r = make_client().get("/arctic/symbols", params={"library": "nope"})
    assert r.status_code == 404
    assert "unknown library" in r.json()["detail"]


def test_arctic_series_returns_rows():
    r = make_client().get("/arctic/series", params={"library": "openbb", "symbol": "AAPL"})
    assert r.status_code == 200
    assert r.json()["rows"] == [{"date": "2026-08-07", "close": 100.0}]


def test_arctic_series_unknown_symbol_is_404():
    r = make_client().get("/arctic/series", params={"library": "openbb", "symbol": "NOPE"})
    assert r.status_code == 404
    assert "unknown symbol" in r.json()["detail"]


def test_arctic_summary_returns_metadata_without_reading_rows():
    def spy_read_fn(*a, **kw):
        raise AssertionError("summary must not call arctic_read")

    app = create_app(
        arctic_libraries_fn=lambda: ["openbb"],
        arctic_symbols_fn=lambda library: ["AAPL"],
        arctic_read_fn=spy_read_fn,
        arctic_client_factory=lambda: FakeArctic({"openbb": FakeLib(DEFAULT_DESC)}),
        bounded_fn=lambda fn, *a, **kw: fn(*a, **kw),
    )
    r = TestClient(app).get("/arctic/summary", params={"library": "openbb", "symbol": "AAPL"})
    assert r.status_code == 200
    body = r.json()
    assert body["row_count"] == 42
    assert body["date_range"] == ["2026-01-01", "2026-08-07"]
    assert body["columns"] == [{"name": "close", "dtype": "float64"}]


def test_arctic_summary_unknown_symbol_is_404():
    r = make_client(symbols_by_library={"openbb": ["AAPL"]}).get(
        "/arctic/summary", params={"library": "openbb", "symbol": "NOPE"}
    )
    assert r.status_code == 404
    assert "unknown symbol" in r.json()["detail"]


def test_arctic_summary_unknown_library_is_404():
    r = make_client().get("/arctic/summary", params={"library": "nope", "symbol": "AAPL"})
    assert r.status_code == 404
    assert "unknown library" in r.json()["detail"]
```

- [ ] **Step 5: Run tests to verify they fail**

Run (after installing `mcp_stores` per Step 1, then `pip install -e ./stores-explorer[dev]` from repo root):
```bash
cd stores-explorer && python -m pytest -q
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.main'` (the app doesn't exist yet).

- [ ] **Step 6: Write `stores-explorer/widgets.json`**

```json
{
  "arctic_explorer": {
    "name": "ArcticDB Explorer",
    "description": "Browse the shared ArcticDB store: pick a library, pick a symbol, see the stored series.",
    "category": "Data",
    "type": "table",
    "endpoint": "arctic/series",
    "dataKey": "rows",
    "gridData": { "w": 30, "h": 14 },
    "params": [
      { "paramName": "library", "type": "text", "label": "Library", "value": "", "optionsEndpoint": "arctic/libraries" },
      { "paramName": "symbol", "type": "text", "label": "Symbol", "value": "", "optionsEndpoint": "arctic/symbols", "optionsParams": { "library": "$library" } },
      { "paramName": "start", "type": "date", "label": "Start", "value": "", "show": false },
      { "paramName": "end", "type": "date", "label": "End", "value": "", "show": false }
    ],
    "source": ["ArcticDB"]
  }
}
```

- [ ] **Step 7: Write `stores-explorer/app/main.py`**

```python
"""FastAPI app for stores-explorer: read-only browsing of the shared
ArcticDB and kdb+ store, for a bdobb widget (the widget's door -- stores-mcp
is the analyst's). Loopback-only; Tailscale Serve is the ingress (see the
repo compose file). No app-level auth -- matches live-grid/stores-mcp's
posture for read-only discovery of data already behind the tailnet.

Every ArcticDB/kdb+ call is a thin wrapper around mcp_stores's existing,
already-scrubbed, already-timeout-bounded functions -- this file adds no new
backend client code except /arctic/summary, which reuses mcp_stores's own
_arctic()/_bounded() helpers directly (deliberately imported despite the
leading underscore -- see the design spec's D3/D6) rather than duplicating
them. Every backend call is injectable via create_app's keyword arguments,
defaulting to the real mcp_stores imports, so the test suite never touches a
real ArcticDB or kdb+ (mirrors live-grid's seed_client/client_factory
injection pattern).
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from server import (
    _arctic,
    _bounded,
    arctic_list_libraries,
    arctic_list_symbols,
    arctic_read,
)

WIDGETS_PATH = Path(__file__).resolve().parent.parent / "widgets.json"


def create_app(
    *,
    arctic_libraries_fn=arctic_list_libraries,
    arctic_symbols_fn=arctic_list_symbols,
    arctic_read_fn=arctic_read,
    arctic_client_factory=_arctic,
    bounded_fn=_bounded,
) -> FastAPI:
    app = FastAPI()

    # No credential to protect once topology is the gate -- matches
    # live-grid's posture, not key-maint's narrow allowlist (design spec D1/D2).
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/widgets.json")
    def widgets() -> JSONResponse:
        return JSONResponse(json.loads(WIDGETS_PATH.read_text()))

    @app.get("/arctic/libraries")
    def arctic_libraries() -> list[str]:
        return arctic_libraries_fn()

    @app.get("/arctic/symbols")
    def arctic_symbols(library: str) -> list[str]:
        try:
            return arctic_symbols_fn(library)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.get("/arctic/summary")
    def arctic_summary(library: str, symbol: str) -> dict:
        try:
            symbols = arctic_symbols_fn(library)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        if symbol not in symbols:
            raise HTTPException(
                status_code=404,
                detail=f"unknown symbol {symbol!r} in {library!r}; call /arctic/symbols first",
            )
        ac = arctic_client_factory()
        lib = bounded_fn(ac.__getitem__, library)
        desc = bounded_fn(lib.get_description, symbol)
        return {
            "library": library,
            "symbol": symbol,
            "row_count": desc.row_count,
            "date_range": [str(desc.date_range[0]), str(desc.date_range[1])],
            "columns": [{"name": c.name, "dtype": str(c.dtype)} for c in desc.columns],
        }

    @app.get("/arctic/series")
    def arctic_series(
        library: str, symbol: str,
        start: str | None = None, end: str | None = None, tail_rows: int = 1000,
    ) -> dict:
        try:
            return arctic_read_fn(library, symbol, start=start, end=end, tail_rows=tail_rows)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    return app


app = create_app()
```

- [ ] **Step 8: Run tests to verify they pass**

Run:
```bash
cd stores-explorer && python -m pytest -q
```
Expected: PASS (9 tests)

- [ ] **Step 9: Commit**

```bash
git add stores-explorer/pyproject.toml stores-explorer/app/ stores-explorer/widgets.json stores-explorer/tests/
git commit -m "feat(stores-explorer): app skeleton and ArcticDB browsing endpoints

libraries/symbols/summary/series, all thin wrappers over mcp_stores's
existing discovery functions. No app-level auth, matching live-grid."
```

---

### Task 2: kdb+ endpoints + `kdb_explorer` widget

**Files:**
- Modify: `stores-explorer/app/main.py`
- Modify: `stores-explorer/widgets.json`
- Modify: `stores-explorer/tests/test_main.py`

**Interfaces:**
- Produces: `create_app` gains three more keyword arguments (`kdb_tables_fn`, `kdb_schema_fn`, `kdb_select_fn`) and the app gains three more routes. Nothing from Task 1's interface changes — existing arctic routes/tests are unaffected.

- [ ] **Step 1: Write the failing tests**

Add to `stores-explorer/tests/test_main.py`, after the existing arctic tests:

```python
def make_kdb_client(tables=("trades",), schema_by_table=None, select_response=None):
    schema_by_table = schema_by_table if schema_by_table is not None else {"trades": [{"c": "sym", "t": "s"}]}

    def kdb_tables_fn():
        return list(tables)

    def kdb_schema_fn(table):
        if table not in tables:
            raise ValueError(f"unknown table {table!r}; call kdb_tables first")
        return schema_by_table.get(table, [])

    def kdb_select_fn(table, symbol=None, start_time=None, end_time=None, limit=1000):
        if table not in tables:
            raise ValueError(f"unknown table {table!r}; call kdb_tables first")
        return select_response or {"table": table, "returned_rows": 1, "rows": [{"sym": "AAPL"}]}

    app = create_app(
        arctic_libraries_fn=lambda: [],
        arctic_symbols_fn=lambda library: [],
        arctic_read_fn=lambda *a, **kw: {},
        arctic_client_factory=lambda: None,
        bounded_fn=lambda fn, *a, **kw: fn(*a, **kw),
        kdb_tables_fn=kdb_tables_fn,
        kdb_schema_fn=kdb_schema_fn,
        kdb_select_fn=kdb_select_fn,
    )
    return TestClient(app)


def test_widgets_json_declares_kdb_explorer():
    body = make_client().get("/widgets.json").json()
    w = body["kdb_explorer"]
    assert w["type"] == "table"
    assert w["endpoint"] == "kdb/select"
    assert w["dataKey"] == "rows"
    param_names = [p["paramName"] for p in w["params"]]
    assert param_names == ["table", "symbol", "start_time", "end_time"]
    table_param = next(p for p in w["params"] if p["paramName"] == "table")
    assert table_param["optionsEndpoint"] == "kdb/tables"


def test_kdb_tables_lists_tables():
    r = make_kdb_client(tables=("trades", "quotes"))
    assert r.get("/kdb/tables").json() == ["trades", "quotes"]


def test_kdb_schema_returns_columns():
    r = make_kdb_client(schema_by_table={"trades": [{"c": "sym", "t": "s"}, {"c": "price", "t": "f"}]})
    body = r.get("/kdb/schema", params={"table": "trades"}).json()
    assert body == [{"c": "sym", "t": "s"}, {"c": "price", "t": "f"}]


def test_kdb_schema_unknown_table_is_404():
    r = make_kdb_client().get("/kdb/schema", params={"table": "nope"})
    assert r.status_code == 404
    assert "unknown table" in r.json()["detail"]


def test_kdb_select_returns_rows():
    r = make_kdb_client().get("/kdb/select", params={"table": "trades"})
    assert r.status_code == 200
    assert r.json()["rows"] == [{"sym": "AAPL"}]


def test_kdb_select_unknown_table_is_404():
    r = make_kdb_client().get("/kdb/select", params={"table": "nope"})
    assert r.status_code == 404
    assert "unknown table" in r.json()["detail"]
```

Note: `make_client` (from Task 1) still works unmodified for the `test_widgets_json_declares_kdb_explorer` test above — Task 1's `create_app` call in `make_client` doesn't pass `kdb_*_fn` arguments, so this step's Step 2 (implementation) must give them defaults that resolve to the real `mcp_stores` imports, exactly like the arctic ones already do, so a caller that doesn't override them still gets a working (if untested-by-this-call) app.

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd stores-explorer && python -m pytest -q
```
Expected: FAIL — `TypeError: create_app() got an unexpected keyword argument 'kdb_tables_fn'`

- [ ] **Step 3: Update `stores-explorer/widgets.json`**

Replace the file's full contents with:

```json
{
  "arctic_explorer": {
    "name": "ArcticDB Explorer",
    "description": "Browse the shared ArcticDB store: pick a library, pick a symbol, see the stored series.",
    "category": "Data",
    "type": "table",
    "endpoint": "arctic/series",
    "dataKey": "rows",
    "gridData": { "w": 30, "h": 14 },
    "params": [
      { "paramName": "library", "type": "text", "label": "Library", "value": "", "optionsEndpoint": "arctic/libraries" },
      { "paramName": "symbol", "type": "text", "label": "Symbol", "value": "", "optionsEndpoint": "arctic/symbols", "optionsParams": { "library": "$library" } },
      { "paramName": "start", "type": "date", "label": "Start", "value": "", "show": false },
      { "paramName": "end", "type": "date", "label": "End", "value": "", "show": false }
    ],
    "source": ["ArcticDB"]
  },
  "kdb_explorer": {
    "name": "kdb+ Explorer",
    "description": "Browse the kdb+ tables the tape writes to: pick a table, see the filtered rows.",
    "category": "Data",
    "type": "table",
    "endpoint": "kdb/select",
    "dataKey": "rows",
    "gridData": { "w": 30, "h": 14 },
    "params": [
      { "paramName": "table", "type": "text", "label": "Table", "value": "", "optionsEndpoint": "kdb/tables" },
      { "paramName": "symbol", "type": "text", "label": "Symbol", "value": "", "show": false },
      { "paramName": "start_time", "type": "date", "label": "Start", "value": "", "show": false },
      { "paramName": "end_time", "type": "date", "label": "End", "value": "", "show": false }
    ],
    "source": ["kdb+"]
  }
}
```

- [ ] **Step 4: Update `stores-explorer/app/main.py`**

Replace the `from server import (...)` block with:

```python
from server import (
    _arctic,
    _bounded,
    arctic_list_libraries,
    arctic_list_symbols,
    arctic_read,
    kdb_select,
    kdb_table_schema,
    kdb_tables,
)
```

Replace the `def create_app(` signature block with:

```python
def create_app(
    *,
    arctic_libraries_fn=arctic_list_libraries,
    arctic_symbols_fn=arctic_list_symbols,
    arctic_read_fn=arctic_read,
    arctic_client_factory=_arctic,
    bounded_fn=_bounded,
    kdb_tables_fn=kdb_tables,
    kdb_schema_fn=kdb_table_schema,
    kdb_select_fn=kdb_select,
) -> FastAPI:
```

Add three routes directly after the existing `arctic_series` route (i.e. immediately before the final `return app`):

```python
    @app.get("/kdb/tables")
    def kdb_tables_route() -> list[str]:
        return kdb_tables_fn()

    @app.get("/kdb/schema")
    def kdb_schema(table: str) -> list[dict]:
        try:
            return kdb_schema_fn(table)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.get("/kdb/select")
    def kdb_select_route(
        table: str, symbol: str | None = None,
        start_time: str | None = None, end_time: str | None = None, limit: int = 1000,
    ) -> dict:
        try:
            return kdb_select_fn(
                table, symbol=symbol, start_time=start_time, end_time=end_time, limit=limit
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
```

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
cd stores-explorer && python -m pytest -q
```
Expected: PASS (15 tests)

- [ ] **Step 6: Commit**

```bash
git add stores-explorer/app/main.py stores-explorer/widgets.json stores-explorer/tests/test_main.py
git commit -m "feat(stores-explorer): kdb+ browsing endpoints and the kdb_explorer widget

tables/schema/select, thin wrappers over mcp_stores's existing kdb+
functions. Completes both doors into the store this service opens."
```

---

### Task 3: Deployment — Dockerfile, compose service, Tailscale Serve routes, README

**Files:**
- Create: `stores-explorer/Dockerfile`
- Create: `stores-explorer/README.md`
- Modify: `docker-compose.yml`
- Modify: `ts-config/serve.json`
- Modify: `ts-config/serve-funnel.json`

**Interfaces:**
- Consumes: `stores-explorer/app/main.py`'s `app = create_app()` (Tasks 1-2), served via `uvicorn app.main:app`.
- Produces: a running container reachable at `127.0.0.1:6904` inside the `service:tailscale` network namespace, published externally at `${TS_CERT_DOMAIN}:6904`.

- [ ] **Step 1: Write `stores-explorer/Dockerfile`**

```dockerfile
# stores-explorer: read-only widget backend browsing the shared ArcticDB/kdb+
# store (Ep. 11) -- the widget's door into the vault, pairing with
# stores-mcp (the analyst's door). Loopback-only; Tailscale Serve publishes
# it (see repo compose).
#
# Built with the REPO ROOT as build context (see docker-compose.yml) --
# COPY paths below are repo-root-relative, because a Dockerfile can only
# reach paths inside its build context and mcp_stores/ is stores-explorer's
# sibling, not its descendant (same reasoning as live-grid's kdb-store COPY).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

# mcp_stores's discovery functions (arctic_list_libraries, arctic_read,
# kdb_select, ...) -- installed before stores-explorer itself, since
# stores-explorer's own pyproject.toml depends on it and it is not
# published to PyPI. Installs as a bare top-level `server` module (its
# pyproject.toml declares py-modules = ["server"]), which app/main.py
# imports from directly.
COPY mcp_stores/ /srv/mcp_stores/
RUN pip install /srv/mcp_stores

COPY stores-explorer/pyproject.toml ./
COPY stores-explorer/app/ app/
COPY stores-explorer/widgets.json ./
RUN pip install .
CMD ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "6904"]
```

- [ ] **Step 2: Attempt a local build (best-effort verification)**

Run from the repo root:
```bash
docker build -f stores-explorer/Dockerfile -t stores-explorer:test .
```
Expected: image builds successfully. `arcticdb`/`pykx` (pulled in transitively via `mcp_stores`) are native-dependency-heavy and this repo pins `platform: linux/amd64` for exactly this reason (see `docs/superpowers/specs/2026-08-05-arcticdb-minio-design.md`'s D7) — on Apple Silicon this build runs under emulation and may be slow. If the build does not complete in a reasonable time or fails specifically due to platform/emulation issues in the current environment, note this in the task report as a known limitation (matching the project's documented Apple-Silicon-emulation caveat) rather than blocking the task on it — the Python-level test suite (Tasks 1-2) and `scripts/scrub-check.sh` (Step 6 below) are the primary verification for this task.

- [ ] **Step 3: Add the `stores-explorer` service to `docker-compose.yml`**

Add a new service block, placed alongside the other `network_mode: service:tailscale` services (e.g. directly after the `stores-mcp` block):

```yaml
  stores-explorer:
    build:
      context: .
      dockerfile: stores-explorer/Dockerfile
    image: openbb-stores-explorer:11.2.0
    container_name: openbb-stores-explorer
    restart: unless-stopped
    network_mode: service:tailscale
    depends_on:
      - tailscale
    environment:
      - KX_HOST=127.0.0.1
      - KX_PORT=5000
      - PYKX_UNLICENSED=1
      - PYKX_IGNORE_QHOME=1
    env_file:
      - path: ./minio.env
        required: true
```

(Match the file's existing indentation exactly — copy the indentation style of the neighboring `stores-mcp` block rather than the 2-space shown here if it differs.)

- [ ] **Step 4: Add the Tailscale Serve route**

`stores-explorer` needs its own external port, following `live-grid`'s `:6903` pattern exactly (`stores-mcp` on `:6902` is reached only from sibling containers inside the shared network namespace, not externally — confirmed by its absence from both `ts-config/serve.json` and `ts-config/serve-funnel.json`; `stores-explorer` is different because bdobb, its caller, runs on the user's own remote machine). Port `6904` is unused (`6900`=openbb-api, `6901`=openbb-mcp, `6902`=stores-mcp/internal-only, `6903`=live-grid).

In `ts-config/serve.json`, add to the `"TCP"` object:
```json
    "6904": {
      "HTTPS": true
    }
```

and add to the `"Web"` object:
```json
    "${TS_CERT_DOMAIN}:6904": {
      "Handlers": {
        "/": {
          "Proxy": "http://127.0.0.1:6904"
        }
      }
    }
```

Make the identical two additions to `ts-config/serve-funnel.json` (its `"TCP"` and `"Web"` objects are otherwise identical to `serve.json`'s — only `"AllowFunnel"` differs, and `stores-explorer` is not added there, matching `live-grid`'s `:6903` which is also tailnet-only in both files).

- [ ] **Step 5: Write `stores-explorer/README.md`**

```markdown
# stores-explorer

Read-only widget backend for browsing the shared ArcticDB and kdb+ store —
the "explorer" widget from *Adventures in OpenBB, Ep. 11*. Design:
`../docs/superpowers/specs/2026-08-07-stores-explorer-design.md`.

Pairs with `stores-mcp`: same underlying discovery code
(`mcp_stores/server.py`), two doors — `stores-mcp` answers an agent (Rita),
this answers a widget (bdobb, over the tailnet). No app-level auth: loopback
bind + Tailscale Serve is the only ingress, matching `live-grid`.

## Endpoints

**ArcticDB:** `GET /arctic/libraries`, `GET /arctic/symbols?library=`,
`GET /arctic/summary?library=&symbol=` (row count, date range, columns —
no row data read), `GET /arctic/series?library=&symbol=&start=&end=&tail_rows=`.

**kdb+:** `GET /kdb/tables`, `GET /kdb/schema?table=`,
`GET /kdb/select?table=&symbol=&start_time=&end_time=&limit=`.

`GET /widgets.json` declares both `arctic_explorer` and `kdb_explorer`
(`type: "table"` — bdobb's existing table/chart auto-detection handles the
"plotted series" step, no bespoke renderer needed).

## Test

    pip install -e ../mcp_stores && pip install -e .[dev] && pytest

All backend calls are injected in tests (see `tests/test_main.py`'s
`make_client`/`make_kdb_client`) — no real ArcticDB or kdb+ needed.
```

- [ ] **Step 6: Run the scrub gate**

Run from the repo root:
```bash
bash scripts/scrub-check.sh
```
Expected: exits 0. If it flags anything in the new files, fix the flagged content (never add the flag to the allowlist to make a real credential pass) and re-run until clean.

- [ ] **Step 7: Run the full stores-explorer suite once more to confirm no regression**

```bash
cd stores-explorer && python -m pytest -q
```
Expected: PASS (15 tests)

- [ ] **Step 8: Commit**

```bash
git add stores-explorer/Dockerfile stores-explorer/README.md docker-compose.yml ts-config/serve.json ts-config/serve-funnel.json
git commit -m "feat(stores-explorer): deployment -- Dockerfile, compose service, Serve route

Port 6904, tailnet-only (matching live-grid's 6903), not funneled.
Scrub-check verified clean."
```

---

## After Task 3

`stores-explorer` answers over Serve, completing this episode's backend
build-order gate ("Explorer answers over Serve; Rita queries ArcticDB;
scrub-check green" — `episodes-10-12-plan.md`). Cutting a tag and building
the bdobb-side explorer widget (Phase 3) are separate, later steps.

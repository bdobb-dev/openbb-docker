<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Delta Lake Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ArcticDB with Delta Lake (delta-rs) as Ep. 11's shared store, preserving the store/provider/accessor public surface and tick-lab's standalone-client story.

**Architecture:** One Delta table per symbol at `<base>/<library>/<symbol>` where `<base>` is `s3://<bucket>` on MinIO or a local directory (replacing LMDB). Credentials travel as delta-rs `storage_options`, never in a URI. The provider fetchers read through the store's public API; tick-lab keeps its own thin wrapper over the `deltalake` package.

**Tech Stack:** Python ≥3.10, `deltalake>=1.0` (delta-rs), `pyarrow>=16`, pandas 2.x, OpenBB Platform (`openbb-core>=1.5.8`), MinIO (unchanged), Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-01-deltalake-store-design.md`

## Global Constraints

- Package renames exactly: dir `openbb-arcticdb` → `openbb-deltalake`, module `openbb_arcticdb` → `openbb_deltalake`, provider/extension name `"deltalake"`, accessor namespace `.deltalake`.
- Env vars exactly: `ARCTICDB_S3_ENDPOINT/PORT/BUCKET/SECURE/ACCESS/SECRET` → `DELTA_S3_*` (same semantics, same validation); `ARCTICDB_URI` → `DELTA_URI`; `ARCTICDB_LIBRARY` → `DELTA_LIBRARY`.
- `resolve_config` precedence unchanged: explicit arg > OpenBB credential (`deltalake_uri`/`deltalake_library`) > `DELTA_URI` > `DELTA_S3_*` parts > local default `~/.openbb_platform/deltalake` (honoring `OPENBB_HOME`).
- The `date` column convention is the index round-trip: writes turn a `DatetimeIndex` into a `date` column; reads restore a datetime `date` column as the sorted index. `normalize_index`, `parse_temporal`, `to_bounds` move over UNCHANGED (including whole-day widening of a date-shaped `end`).
- `as_of` accepts an `int` (Delta version) or a str/date/datetime (timestamp travel).
- Dropped from the public API (Arctic-only concepts): `prune_previous_versions` kwarg, `redact_uri`, `get_library`, the `_arctic_cache`, and LMDB anything.
- Error contracts in tick-lab keep their names: `LibraryNotFoundError(ValueError)`, `StoreWriteError(RuntimeError)` now wrapping `deltalake.exceptions.DeltaError`.
- Licenses/versions stay: pyprojects keep `version = "11.0.0"` and their license fields; ruff line-length 100; lazy imports inside functions (existing `PLC0415` ignore) stay the pattern.
- Commit metadata convention: a store write with `metadata=` commits with custom metadata key `openbb_meta` = `json.dumps(metadata, default=str)`; `read_metadata` returns the parsed dict from the newest commit that has it, else `None`.
- Run tests with the repo's venvs as today: `cd openbb-deltalake && pytest tests/ -x -q` and `cd tick-lab && pytest tests/ -x -q`.
- delta-rs API note: this plan targets deltalake 1.x names (`DeltaTable.load_as_version`, `DeltaTable.is_deltatable`, `CommitProperties(custom_metadata=...)`, `write_deltalake(..., schema_mode="overwrite")`). Task 1's canary test pins these; if the installed version renames one, fix the canary first and use its names everywhere after.

---

### Task 1: Rename scaffold + deltalake API canary

**Files:**
- Rename: `openbb-arcticdb/` → `openbb-deltalake/`, `openbb-arcticdb/openbb_arcticdb/` → `openbb-deltalake/openbb_deltalake/`
- Modify: `openbb-deltalake/pyproject.toml`
- Test: `openbb-deltalake/tests/test_deltalake_api.py` (new)

**Interfaces:**
- Produces: an installable `openbb-deltalake` package skeleton and a canary test that pins every delta-rs API name later tasks call.

- [ ] **Step 1: Mechanical rename**

```bash
cd ~/Developer/openbb-docker-ep11
git mv openbb-arcticdb openbb-deltalake
git mv openbb-deltalake/openbb_arcticdb openbb-deltalake/openbb_deltalake
grep -rl openbb_arcticdb openbb-deltalake | xargs sed -i '' 's/openbb_arcticdb/openbb_deltalake/g'
```

- [ ] **Step 2: Update pyproject**

In `openbb-deltalake/pyproject.toml`: name `openbb-deltalake`; description "Delta Lake integration for OpenBB: read stored bars as a provider, and persist any OBBject result to a Delta table."; dependencies become:

```toml
dependencies = [
    "openbb-core>=1.5.8",
    "deltalake>=1.0",
    "pyarrow>=16",
]
```

(remove `arcticdb` and the `protobuf<7` pin plus its comment). Entry points:

```toml
[project.entry-points."openbb_provider_extension"]
deltalake = "openbb_deltalake:deltalake_provider"

[project.entry-points."openbb_obbject_extension"]
deltalake = "openbb_deltalake:ext"
```

`packages.find` include becomes `["openbb_deltalake*"]`.

- [ ] **Step 3: Write the canary test**

`openbb-deltalake/tests/test_deltalake_api.py`:

```python
"""Pins the delta-rs API surface this package depends on.

If a deltalake upgrade renames any of these, THIS test fails first, with a
clear message, instead of the store failing obscurely.
"""

import json

import pandas as pd


def test_deltalake_api_surface(tmp_path):
    from deltalake import CommitProperties, DeltaTable, write_deltalake

    path = str(tmp_path / "lib" / "AAPL")
    df1 = pd.DataFrame({"date": pd.to_datetime(["2026-01-02"]), "close": [1.0]})
    df2 = pd.DataFrame({"date": pd.to_datetime(["2026-01-03"]), "close": [2.0]})

    props = CommitProperties(custom_metadata={"openbb_meta": json.dumps({"k": "v"})})
    write_deltalake(path, df1, mode="overwrite", schema_mode="overwrite", commit_properties=props)
    write_deltalake(path, df2, mode="overwrite", schema_mode="overwrite")

    assert DeltaTable.is_deltatable(path)
    dt = DeltaTable(path)
    assert dt.version() == 1

    # column projection + filter through a pyarrow dataset scan
    import pyarrow.dataset as ds

    table = dt.to_pyarrow_dataset().to_table(
        columns=["date", "close"],
        filter=ds.field("date") >= pd.Timestamp("2026-01-03").to_pydatetime(),
    )
    assert table.num_rows == 1

    # time travel by version, and commit metadata in history
    dt.load_as_version(0)
    assert dt.to_pyarrow_dataset().to_table().num_rows == 1
    assert any("openbb_meta" in h for h in dt.history())
```

- [ ] **Step 4: Install and run to verify it passes**

```bash
cd openbb-deltalake && pip install -e ".[dev]" && pytest tests/test_deltalake_api.py -v
```

Expected: PASS. If an AttributeError/TypeError names a missing API, adjust the canary to the installed version's name and record the change — every later task uses the canary's names.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor: rename openbb-arcticdb to openbb-deltalake, pin delta-rs API canary"
```

(Other tests are red until Tasks 2–3; the gate for this task is the canary alone.)

---

### Task 2: Config module (`utils.py` rewrite)

**Files:**
- Modify: `openbb-deltalake/openbb_deltalake/utils.py`
- Test: `openbb-deltalake/tests/test_config.py` (replaces `test_s3_config.py`), `openbb-deltalake/tests/test_utils.py`

**Interfaces:**
- Produces: `resolve_config(uri, library, credentials) -> tuple[str, str, dict[str, str]]` (base, library, storage_options); `s3_options_from_env(env=None) -> tuple[str, dict[str, str]] | None`; `default_base() -> str`; `table_path(base, library, symbol) -> str`; `fs_and_root(base, options) -> (pyarrow.fs.FileSystem, str)`. Unchanged: `normalize_index`, `parse_temporal`, `to_bounds`.

- [ ] **Step 1: Write the failing tests**

Rename `tests/test_s3_config.py` → `tests/test_config.py` with content:

```python
import pytest


def test_s3_options_from_env_complete():
    from openbb_deltalake.utils import s3_options_from_env

    env = {
        "DELTA_S3_ENDPOINT": "minio.tail.ts.net",
        "DELTA_S3_BUCKET": "openbb",
        "DELTA_S3_ACCESS": "user",
        "DELTA_S3_SECRET": "hunter2 ",  # trailing space must be stripped
        "DELTA_S3_PORT": "9000",
        "DELTA_S3_SECURE": "true",
    }
    base, opts = s3_options_from_env(env)
    assert base == "s3://openbb"
    assert opts["aws_endpoint"] == "https://minio.tail.ts.net:9000"
    assert opts["aws_secret_access_key"] == "hunter2"
    assert opts["aws_virtual_hosted_style_request"] == "false"
    assert opts["aws_conditional_put"] == "etag"
    assert "aws_allow_http" not in opts


def test_s3_options_insecure_allows_http():
    from openbb_deltalake.utils import s3_options_from_env

    env = {
        "DELTA_S3_ENDPOINT": "localhost",
        "DELTA_S3_BUCKET": "b",
        "DELTA_S3_ACCESS": "a",
        "DELTA_S3_SECRET": "s",
        "DELTA_S3_SECURE": "false",
    }
    base, opts = s3_options_from_env(env)
    assert opts["aws_endpoint"] == "http://localhost:9000"
    assert opts["aws_allow_http"] == "true"


def test_s3_options_incomplete_returns_none():
    from openbb_deltalake.utils import s3_options_from_env

    assert s3_options_from_env({"DELTA_S3_ENDPOINT": "x"}) is None


def test_s3_options_bad_port_raises():
    from openbb_deltalake.utils import s3_options_from_env

    env = {
        "DELTA_S3_ENDPOINT": "x", "DELTA_S3_BUCKET": "b",
        "DELTA_S3_ACCESS": "a", "DELTA_S3_SECRET": "s", "DELTA_S3_PORT": "nope",
    }
    with pytest.raises(ValueError):
        s3_options_from_env(env)


def test_resolve_config_explicit_arg_wins(monkeypatch, tmp_path):
    from openbb_deltalake.utils import resolve_config

    monkeypatch.setenv("DELTA_URI", "/somewhere/else")
    base, library, opts = resolve_config(str(tmp_path), "mylib", None)
    assert (base, library, opts) == (str(tmp_path), "mylib", {})


def test_resolve_config_local_default(monkeypatch, tmp_path):
    from openbb_deltalake.utils import resolve_config

    for var in ("DELTA_URI", "DELTA_LIBRARY", "DELTA_S3_ENDPOINT", "DELTA_S3_BUCKET",
                "DELTA_S3_ACCESS", "DELTA_S3_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENBB_HOME", str(tmp_path))
    base, library, opts = resolve_config()
    assert base == str(tmp_path / "deltalake")
    assert library == "openbb"
    assert opts == {}


def test_table_path():
    from openbb_deltalake.utils import table_path

    assert table_path("s3://bucket", "lib", "AAPL") == "s3://bucket/lib/AAPL"
    assert table_path("/tmp/base/", "lib", "AAPL") == "/tmp/base/lib/AAPL"
```

In `tests/test_utils.py`: delete the `redact_uri` and `get_library`/cache tests; keep the `normalize_index`/`parse_temporal`/`to_bounds` tests unchanged.

- [ ] **Step 2: Run to verify the new tests fail**

```bash
cd openbb-deltalake && pytest tests/test_config.py -v
```

Expected: FAIL — `ImportError: cannot import name 's3_options_from_env'`.

- [ ] **Step 3: Rewrite `utils.py`**

Keep the module docstring style and `normalize_index` / `parse_temporal` / `to_bounds` verbatim. Delete `default_uri`, `redact_uri`, `s3_uri_from_env`, `get_library`, `_arctic_cache`, `_arctic_cache_lock`, and the `threading` import. Add:

```python
_S3_REQUIRED = (
    "DELTA_S3_ENDPOINT",
    "DELTA_S3_BUCKET",
    "DELTA_S3_ACCESS",
    "DELTA_S3_SECRET",
)


def default_base() -> str:
    """Default local Delta store under the OpenBB home directory."""
    home = os.getenv("OPENBB_HOME") or os.path.expanduser("~/.openbb_platform")
    return os.path.join(home, "deltalake")


def s3_options_from_env(env: Any = None) -> tuple[str, dict[str, str]] | None:
    """(base_uri, storage_options) assembled from DELTA_S3_* parts.

    Returns None unless every required part is present, so a partially
    configured environment falls through to the local default. Values are
    stripped: Docker Compose's env_file parser does not strip trailing
    whitespace, and an invisible trailing space in DELTA_S3_SECRET would
    fail auth deep inside delta-rs with no hint.
    """
    e = os.environ if env is None else env
    if any(not str(e.get(k, "")).strip() for k in _S3_REQUIRED):
        return None

    endpoint = str(e["DELTA_S3_ENDPOINT"]).strip()
    bucket = str(e["DELTA_S3_BUCKET"]).strip()
    access = str(e["DELTA_S3_ACCESS"]).strip()
    secret = str(e["DELTA_S3_SECRET"]).strip()

    port_raw = str(e.get("DELTA_S3_PORT") or "9000").strip()
    if not port_raw.isdigit():
        raise ValueError(f"DELTA_S3_PORT must be a number, got {port_raw!r}")

    secure_raw = str(e.get("DELTA_S3_SECURE") or "true").strip().lower()
    if secure_raw not in ("true", "false"):
        raise ValueError(
            f"DELTA_S3_SECURE must be 'true' or 'false', got {secure_raw!r}"
        )

    scheme = "https" if secure_raw == "true" else "http"
    options = {
        "aws_endpoint": f"{scheme}://{endpoint}:{port_raw}",
        "aws_access_key_id": access,
        "aws_secret_access_key": secret,
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        # Commit atomicity on S3 without a lock service. MinIO supports
        # conditional puts; verified by the Task 6 integration test.
        "aws_conditional_put": "etag",
    }
    if scheme == "http":
        options["aws_allow_http"] = "true"
    return f"s3://{bucket}", options


def resolve_config(
    uri: str | None = None,
    library: str | None = None,
    credentials: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, str]]:
    """Resolve (base, library, storage_options).

    Precedence: explicit arg > OpenBB credential > DELTA_URI env var >
    DELTA_S3_* parts > local default. An s3:// base picks up storage
    options from DELTA_S3_* when they are set; a local path needs none.
    """
    creds = credentials or {}
    library = (
        library
        or creds.get("deltalake_library")
        or os.getenv("DELTA_LIBRARY")
        or "openbb"
    )
    s3 = s3_options_from_env()
    explicit = uri or creds.get("deltalake_uri") or os.getenv("DELTA_URI")
    if explicit:
        explicit = str(explicit).rstrip("/")
        opts = s3[1] if (explicit.startswith("s3://") and s3) else {}
        return explicit, library, opts
    if s3:
        return s3[0], library, s3[1]
    return default_base(), library, {}


def table_path(base: str, library: str, symbol: str) -> str:
    """Path of one symbol's Delta table. '/' join works for s3:// and POSIX paths."""
    return f"{base.rstrip('/')}/{library}/{symbol}"


def fs_and_root(base: str, options: dict[str, str]):
    """(pyarrow FileSystem, root path without scheme) for listing and deleting.

    delta-rs handles reads/writes itself; only list_symbols/delete need a
    filesystem handle, and pyarrow.fs covers both local and MinIO.
    """
    # pylint: disable=import-outside-toplevel
    from pyarrow import fs as pafs

    if base.startswith("s3://"):
        s3fs = pafs.S3FileSystem(
            access_key=options.get("aws_access_key_id"),
            secret_key=options.get("aws_secret_access_key"),
            endpoint_override=options.get("aws_endpoint"),
            region=options.get("aws_region", "us-east-1"),
            force_virtual_addressing=False,
        )
        return s3fs, base[len("s3://") :]
    return pafs.LocalFileSystem(), base
```

- [ ] **Step 4: Run tests**

```bash
cd openbb-deltalake && pytest tests/test_config.py tests/test_utils.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: DELTA_* config resolution and storage_options assembly"
```

---

### Task 3: `DeltaStore` (store.py rewrite)

**Files:**
- Modify: `openbb-deltalake/openbb_deltalake/store.py`
- Modify: `openbb-deltalake/tests/conftest.py`
- Test: `openbb-deltalake/tests/test_store.py`

**Interfaces:**
- Consumes: Task 2's `resolve_config`, `table_path`, `fs_and_root`, `normalize_index`, `to_bounds`.
- Produces: `class DeltaStore` with `__init__(uri=None, library=None)`, `write(key, data, *, metadata=None) -> dict`, `append(key, data) -> dict`, `read(key, *, start_date=None, end_date=None, columns=None, as_of=None, output="obbject")`, `list_symbols() -> list[str]`, `has(key) -> bool`, `delete(key) -> dict`, `read_metadata(key) -> Any`; module-level `store(uri=None, library=None) -> DeltaStore`. Attributes `base`, `library`, `storage_options`.

- [ ] **Step 1: Update conftest fixtures**

```python
"""Test fixtures: temporary local Delta store per test."""

import pytest


@pytest.fixture
def store(tmp_path):
    """Return a DeltaStore pointed at a fresh temporary directory."""
    from openbb_deltalake.store import DeltaStore

    return DeltaStore(uri=str(tmp_path), library="test")
```

(Delete `tmp_uri` and `lib` fixtures; rewrite any test that used `lib` for direct writes to go through `store.write` instead.)

- [ ] **Step 2: Port `tests/test_store.py` and add the new behaviors**

Keep every existing behavioral test (write/append/read round-trip, date-range filtering incl. whole-day end, column projection, list/has/delete, metadata, empty-write rejection), adjusting only fixture usage — plus one key rename in assertions: the return dicts now carry `"base"` instead of `"uri"` (no redaction: credentials are not in the base). Add:

```python
def test_as_of_version_time_travel(store):
    import pandas as pd

    df1 = pd.DataFrame({"date": pd.to_datetime(["2026-01-02"]), "close": [1.0]})
    df2 = pd.DataFrame({"date": pd.to_datetime(["2026-01-03"]), "close": [2.0]})
    store.write("AAPL", df1)
    store.write("AAPL", df2)
    old = store.read("AAPL", as_of=0, output="dataframe")
    new = store.read("AAPL", output="dataframe")
    assert old["close"].tolist() == [1.0]
    assert new["close"].tolist() == [2.0]


def test_write_returns_version_and_rows(store):
    import pandas as pd

    df = pd.DataFrame({"date": pd.to_datetime(["2026-01-02"]), "close": [1.0]})
    out = store.write("AAPL", df)
    assert out["version"] == 0 and out["rows"] == 1
    out = store.write("AAPL", df)
    assert out["version"] == 1


def test_datetime_index_round_trips_as_date_index(store):
    import pandas as pd

    df = pd.DataFrame(
        {"close": [1.0, 2.0]},
        index=pd.DatetimeIndex(pd.to_datetime(["2026-01-03", "2026-01-02"]), name="date"),
    )
    store.write("AAPL", df)
    back = store.read("AAPL", output="dataframe")
    assert back.index.name == "date"
    assert list(back.index) == sorted(back.index)  # sorted on read
    assert back["close"].tolist() == [2.0, 1.0]
```

- [ ] **Step 3: Run to verify the new tests fail**

```bash
cd openbb-deltalake && pytest tests/test_store.py -v
```

Expected: FAIL — `ImportError: cannot import name 'DeltaStore'`.

- [ ] **Step 4: Rewrite `store.py`**

Module docstring updates to Delta Lake wording (same structure as today). Implementation:

```python
class DeltaStore:
    """Generic Delta Lake store for arbitrary tabular data."""

    def __init__(self, uri: Optional[str] = None, library: Optional[str] = None):
        # pylint: disable=import-outside-toplevel
        from openbb_deltalake.utils import resolve_config

        self.base, self.library, self.storage_options = resolve_config(uri, library, None)

    # -- helpers ------------------------------------------------------------
    def _path(self, key: str) -> str:
        # pylint: disable=import-outside-toplevel
        from openbb_deltalake.utils import table_path

        return table_path(self.base, self.library, key)

    @staticmethod
    def _to_frame(data: Any):
        """Accept an OBBject, DataFrame, or records; return a frame with the
        DatetimeIndex (if any) moved into a 'date' column for Parquet."""
        # pylint: disable=import-outside-toplevel
        from pandas import DataFrame, DatetimeIndex, RangeIndex

        from openbb_deltalake.utils import normalize_index

        if hasattr(data, "to_dataframe"):  # OBBject
            df = data.to_dataframe()
        elif isinstance(data, DataFrame):
            df = data.copy()
        else:
            df = DataFrame(data)
        if df is None or df.empty:
            raise ValueError("No data to write to the Delta store.")
        df = normalize_index(df)
        if isinstance(df.index, DatetimeIndex):
            name = df.index.name or "date"
            df = df.reset_index().rename(columns={name: "date"})
        else:
            df = df.reset_index(drop=isinstance(df.index, RangeIndex))
        return df

    @staticmethod
    def _commit_props(metadata: Optional[dict]):
        # pylint: disable=import-outside-toplevel
        import json

        from deltalake import CommitProperties

        if not metadata:
            return None
        return CommitProperties(
            custom_metadata={"openbb_meta": json.dumps(metadata, default=str)}
        )

    def _table(self, key: str):
        # pylint: disable=import-outside-toplevel
        from deltalake import DeltaTable

        return DeltaTable(self._path(key), storage_options=self.storage_options)

    # -- write --------------------------------------------------------------
    def write(self, key: str, data: Any, *, metadata: Optional[dict] = None) -> dict[str, Any]:
        """Write any data as a new version of `key` (overwrites the symbol)."""
        # pylint: disable=import-outside-toplevel
        from deltalake import write_deltalake

        df = self._to_frame(data)
        write_deltalake(
            self._path(key),
            df,
            mode="overwrite",
            schema_mode="overwrite",
            storage_options=self.storage_options,
            commit_properties=self._commit_props(metadata),
        )
        return {
            "base": self.base,
            "library": self.library,
            "symbol": key,
            "version": self._table(key).version(),
            "rows": int(len(df)),
        }

    def append(self, key: str, data: Any) -> dict[str, Any]:
        """Append data to `key` (creates the table on first append)."""
        # pylint: disable=import-outside-toplevel
        from deltalake import write_deltalake

        df = self._to_frame(data)
        write_deltalake(
            self._path(key), df, mode="append", storage_options=self.storage_options
        )
        return {
            "base": self.base,
            "library": self.library,
            "symbol": key,
            "version": self._table(key).version(),
            "rows_appended": int(len(df)),
        }

    # -- read ---------------------------------------------------------------
    def read(
        self,
        key: str,
        *,
        start_date: Any = None,
        end_date: Any = None,
        columns: Optional[Sequence[str]] = None,
        as_of: Any = None,
        output: str = "obbject",
    ):
        """Read `key`; OBBject by default, DataFrame with output='dataframe'.

        `as_of` is an int Delta version, or a date/datetime/string for
        timestamp-based time travel.
        """
        # pylint: disable=import-outside-toplevel
        from pandas.api.types import is_datetime64_any_dtype

        from openbb_deltalake.utils import to_bounds

        if not self.has(key):
            raise FileNotFoundError(
                f"No Delta table for symbol '{key}' in library '{self.library}' "
                f"at '{self.base}'. Write some data first."
            )
        dt = self._table(key)
        if as_of is not None:
            dt.load_as_version(as_of if isinstance(as_of, int) else str(as_of))
        dataset = dt.to_pyarrow_dataset()
        start_ts, end_ts = to_bounds(start_date, end_date)
        filt = _date_filter(dataset.schema, start_ts, end_ts)
        cols = None
        if columns:
            cols = list(dict.fromkeys(["date", *columns])) \
                if "date" in dataset.schema.names else list(columns)
        df = dataset.to_table(columns=cols, filter=filt).to_pandas()
        if "date" in df.columns and is_datetime64_any_dtype(df["date"]):
            df = df.set_index("date").sort_index()
        if output == "dataframe":
            return df
        return self._to_obbject(df, key, self.read_metadata(key), self.library)

    # -- catalog ------------------------------------------------------------
    def list_symbols(self) -> list[str]:
        """List symbols (Delta tables) in the library."""
        # pylint: disable=import-outside-toplevel
        from pyarrow import fs as pafs

        from openbb_deltalake.utils import fs_and_root

        fsys, root = fs_and_root(self.base, self.storage_options)
        sel = pafs.FileSelector(f"{root.rstrip('/')}/{self.library}", allow_not_found=True)
        # ponytail: one _delta_log stat per child dir; fine for a per-episode
        # catalog, switch to a manifest table if libraries grow into thousands.
        out = []
        for info in fsys.get_file_info(sel):
            if info.type != pafs.FileType.Directory:
                continue
            log = fsys.get_file_info(f"{info.path}/_delta_log")
            if log.type != pafs.FileType.NotFound:
                out.append(info.base_name)
        return sorted(out)

    def has(self, key: str) -> bool:
        """Whether a Delta table exists for `key`."""
        # pylint: disable=import-outside-toplevel
        from deltalake import DeltaTable

        return DeltaTable.is_deltatable(self._path(key), storage_options=self.storage_options)

    def delete(self, key: str) -> dict[str, Any]:
        """Delete a symbol's table entirely."""
        # pylint: disable=import-outside-toplevel
        from openbb_deltalake.utils import fs_and_root

        fsys, root = fs_and_root(self.base, self.storage_options)
        fsys.delete_dir(f"{root.rstrip('/')}/{self.library}/{key}")
        return {"base": self.base, "library": self.library, "deleted": key}

    def read_metadata(self, key: str) -> Any:
        """Metadata from the newest commit that carries `openbb_meta`."""
        # pylint: disable=import-outside-toplevel
        import json

        for entry in self._table(key).history():
            raw = entry.get("openbb_meta")
            if raw is not None:
                return json.loads(raw)
        return None
```

`_to_obbject` moves over from the old file with `provider="deltalake"` and `extra={"symbol": key, "library": library, "metadata": metadata}` unchanged. Module-level `_date_filter`:

```python
def _date_filter(schema, start_ts, end_ts):
    """Build a pyarrow dataset filter on the 'date' column, matching its tz."""
    if "date" not in schema.names or (start_ts is None and end_ts is None):
        return None
    # pylint: disable=import-outside-toplevel
    import pyarrow.dataset as ds

    field_type = schema.field("date").type
    tz = getattr(field_type, "tz", None)

    def _bound(ts):
        if tz is None:
            return ts.tz_localize(None) if ts.tzinfo else ts
        return ts.tz_localize(tz) if ts.tzinfo is None else ts.tz_convert(tz)

    expr = None
    if start_ts is not None:
        expr = ds.field("date") >= _bound(start_ts).to_pydatetime()
    if end_ts is not None:
        e = ds.field("date") <= _bound(end_ts).to_pydatetime()
        expr = e if expr is None else expr & e
    return expr
```

Factory stays: `def store(uri=None, library=None) -> DeltaStore: return DeltaStore(uri=uri, library=library)`.

- [ ] **Step 5: Run tests**

```bash
cd openbb-deltalake && pytest tests/test_store.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: DeltaStore replaces ArcticStore, same public surface plus time travel"
```

---

### Task 4: Provider, accessor, package `__init__`

**Files:**
- Modify: `openbb-deltalake/openbb_deltalake/models/historical.py`, `openbb-deltalake/openbb_deltalake/accessor.py`, `openbb-deltalake/openbb_deltalake/__init__.py`, `openbb-deltalake/scripts/load_aapl.py`
- Test: `openbb-deltalake/tests/test_historical.py`

**Interfaces:**
- Consumes: `DeltaStore` from Task 3 (`has`, `read(..., output="dataframe")`).
- Produces: `deltalake_provider` (Provider name `"deltalake"`), `ext` (Extension name `"deltalake"`), `DeltaLakeAccessor`, fetcher classes `DeltaLake{Equity,Etf,Crypto,Currency,Index}HistoricalFetcher`. `__all__ = ["deltalake_provider", "ext", "DeltaStore", "store"]`.

- [ ] **Step 1: Port `tests/test_historical.py`**

Mechanical: fixtures now come from Task 3's conftest; any `Arctic`-prefixed fetcher class becomes the `DeltaLake`-prefixed name; query params `uri=` values become tmp-path strings instead of `lmdb://` URIs. Behavioral assertions (resampling rules, tick→OHLCV, multi-symbol, missing-symbol error message listing unknown symbols, whole-day end bound) stay identical.

- [ ] **Step 2: Run to verify failure**

```bash
cd openbb-deltalake && pytest tests/test_historical.py -v
```

Expected: FAIL — import errors for the new class names.

- [ ] **Step 3: Rewrite `_extract_bars` and rename classes**

In `models/historical.py` only `_extract_bars` changes logic — it now goes through the store's public API (the resample helpers `_resample_spec` / `_pandas_ohlcv` stay byte-identical):

```python
async def _extract_bars(query, credentials: Optional[dict]) -> list[dict]:
    """Read raw bars for one or more symbols from a Delta library."""
    # pylint: disable=import-outside-toplevel
    import asyncio

    from openbb_deltalake.store import DeltaStore
    from openbb_deltalake.utils import resolve_config, to_bounds

    uri, library, _ = resolve_config(
        getattr(query, "uri", None), getattr(query, "library", None), credentials
    )
    symbols = [s.strip().upper() for s in query.symbol.split(",")]
    multiple = len(symbols) > 1
    interval = getattr(query, "interval", None) or "1d"
    pandas_rule = _resample_spec(interval)
    pandas_anchor = bool(getattr(query, "pandas_anchor", False))
    start_ts, end_ts = to_bounds(query.start_date, query.end_date)

    def _read() -> list[dict]:
        s = DeltaStore(uri=uri, library=library)
        out: list[dict] = []
        missing: list[str] = []
        for sym in symbols:
            if not s.has(sym):
                missing.append(sym)
                continue
            df = s.read(
                sym,
                start_date=query.start_date,
                end_date=query.end_date,
                output="dataframe",
            )
            if df is None or df.empty:
                continue
            if pandas_anchor:
                ref = start_ts if start_ts is not None else (
                    df.index[0] if len(df.index) else None
                )
                origin = ref.normalize() if ref is not None else "epoch"
            else:
                origin = "epoch"
            df = _pandas_ohlcv(df, pandas_rule, origin=origin)
            if df is None or df.empty:
                continue
            df = df.reset_index()
            if "date" not in df.columns:
                df = df.rename(columns={df.columns[0]: "date"})
            records = df.to_dict("records")
            if multiple:
                for rec in records:
                    rec["symbol"] = sym
            out.extend(records)
        if not out:
            detail = f" Unknown symbols: {missing}." if missing else ""
            raise EmptyDataError(f"No data in Delta library '{library}'.{detail}")
        return out

    return await asyncio.to_thread(_read)
```

Renames in the same file: `_QP.__name__ = f"DeltaLake{label}QueryParams"`, `_Data.__name__ = f"DeltaLake{label}Data"`, `_Fetcher.__name__ = f"DeltaLake{label}Fetcher"`, the five module-level `DeltaLake*HistoricalFetcher` assignments, and the `library`/`uri` field descriptions ("Delta library to read from. Defaults to DELTA_LIBRARY or 'openbb'." / "Store base: a local path or s3://bucket. Defaults to DELTA_URI, DELTA_S3_*, or a local directory.").

- [ ] **Step 4: Rewrite `accessor.py` and `__init__.py`**

`accessor.py`: class `DeltaLakeAccessor`, docstring examples use `.deltalake`, drop the `prune_previous_versions` kwarg from `write`, `_make_store` builds `DeltaStore`. Method bodies otherwise unchanged.

`__init__.py`:

```python
"""Delta Lake integration for OpenBB (provider + OBBject accessor + generic store)."""

from openbb_core.app.model.extension import Extension
from openbb_core.provider.abstract.provider import Provider

from openbb_deltalake.accessor import DeltaLakeAccessor
from openbb_deltalake.models.historical import (
    DeltaLakeCryptoHistoricalFetcher,
    DeltaLakeCurrencyHistoricalFetcher,
    DeltaLakeEquityHistoricalFetcher,
    DeltaLakeEtfHistoricalFetcher,
    DeltaLakeIndexHistoricalFetcher,
)
from openbb_deltalake.store import DeltaStore, store

__all__ = ["deltalake_provider", "ext", "DeltaStore", "store"]

deltalake_provider = Provider(
    name="deltalake",
    website="https://delta.io",
    description=(
        "Serve bars stored in a Delta Lake library through the standard OpenBB "
        "interface (equity/etf/crypto/currency/index historical). Pair with the "
        "`.deltalake` OBBject accessor and the `openbb_deltalake.store` API to "
        "persist and read back ANY data offline."
    ),
    # No credentials: connection is configured via DELTA_URI / DELTA_S3_* /
    # DELTA_LIBRARY env vars or per-call query params.
    credentials=None,
    fetcher_dict={
        "EquityHistorical": DeltaLakeEquityHistoricalFetcher,
        "EtfHistorical": DeltaLakeEtfHistoricalFetcher,
        "CryptoHistorical": DeltaLakeCryptoHistoricalFetcher,
        "CurrencyHistorical": DeltaLakeCurrencyHistoricalFetcher,
        "IndexHistorical": DeltaLakeIndexHistoricalFetcher,
    },
    repr_name="Delta Lake",
)

ext = Extension(
    name="deltalake",
    description="Persist OBBject results to a Delta Lake library.",
)
DeltaLake = ext.obbject_accessor(DeltaLakeAccessor)
```

`scripts/load_aapl.py`: update imports/names to the new module and accessor.

- [ ] **Step 5: Run the whole package suite**

```bash
cd openbb-deltalake && pytest tests/ -v
```

Expected: ALL PASS (canary, config, utils, store, historical).

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: deltalake provider, accessor, and package wiring"
```

---

### Task 5: tick-lab on deltalake

**Files:**
- Modify: `tick-lab/tick_lab/config.py`, `tick-lab/tick_lab/store.py`, `tick-lab/tick_lab/cli.py` (comment on line ~151 mentioning ArcticDB), `tick-lab/tick_lab/__init__.py`, `tick-lab/pyproject.toml`, `tick-lab/.env.example`
- Test: `tick-lab/tests/test_config.py`, `tick-lab/tests/test_store.py`, `tick-lab/tests/test_cli.py`

**Interfaces:**
- Consumes: nothing from openbb-deltalake — tick-lab depends on the `deltalake` package directly (the chapter's point: no Platform install).
- Produces: `S3Config` with new properties `base_uri -> str` (`s3://<bucket>`) and `storage_options -> dict[str, str]` (the `.uri` property is deleted); `TickStore(cfg)` with unchanged method signatures `write(library, symbol, frame, metadata=None)`, `read(library, symbol, start=None, end=None) -> DataFrame`, `list_symbols(library) -> list[str]`, `has(library, symbol) -> bool`; errors `LibraryNotFoundError`, `StoreWriteError`.

- [ ] **Step 1: Update tests first**

`tests/test_config.py`: rename every `ARCTICDB_S3_*` env key to `DELTA_S3_*`; replace `.uri` assertions with:

```python
def test_base_uri_and_storage_options():
    cfg = S3Config(endpoint="minio.tail.ts.net", bucket="openbb",
                   access="u", secret="p", port=9000, secure=True)
    assert cfg.base_uri == "s3://openbb"
    opts = cfg.storage_options
    assert opts["aws_endpoint"] == "https://minio.tail.ts.net:9000"
    assert opts["aws_access_key_id"] == "u"
    assert "aws_allow_http" not in opts


def test_storage_options_insecure():
    cfg = S3Config(endpoint="localhost", bucket="b", access="u", secret="p", secure=False)
    assert cfg.storage_options["aws_allow_http"] == "true"
    assert cfg.storage_options["aws_endpoint"] == "http://localhost:9000"
```

`tests/test_store.py`: the store tests currently target ArcticDB semantics; keep every behavioral test (idempotent overwrite, date-range read incl. whole-day end, `LibraryNotFoundError` on missing library, `StoreWriteError` classification) but build `TickStore` against a local base for unit tests. To allow that, give `TickStore` an internal base override used only by tests: `TickStore(cfg, base=None)` where `base` defaults to `cfg.base_uri` (unit tests pass `base=str(tmp_path)` with empty storage options via a minimal cfg). `StoreWriteError` test: write a frame whose schema conflicts with an existing table via append-mode CLI path — simplest deterministic trigger is `write` to a path that exists as a plain FILE (create `tmp_path/lib/SYM` as a file first), which makes delta-rs raise `DeltaError`.

`tests/test_cli.py`: only env-var names in fixtures change.

- [ ] **Step 2: Run to verify failures**

```bash
cd tick-lab && pytest tests/test_config.py tests/test_store.py -v
```

Expected: FAIL on the new assertions/imports.

- [ ] **Step 3: Rewrite `config.py`**

`REQUIRED = ("DELTA_S3_ENDPOINT", "DELTA_S3_BUCKET", "DELTA_S3_ACCESS", "DELTA_S3_SECRET")`; `from_env` reads `DELTA_S3_PORT`/`DELTA_S3_SECURE` (same validation, same `ConfigError` messages with the new names). Replace the `uri` property:

```python
@property
def base_uri(self) -> str:
    return f"s3://{self.bucket}"

@property
def storage_options(self) -> dict[str, str]:
    scheme = "https" if self.secure else "http"
    options = {
        "aws_endpoint": f"{scheme}://{self.endpoint}:{self.port}",
        "aws_access_key_id": self.access,
        "aws_secret_access_key": self.secret,
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_conditional_put": "etag",
    }
    if not self.secure:
        options["aws_allow_http"] = "true"
    return options
```

Module docstring: "read from the same DELTA_S3_* names the container uses". Drop the now-unused `quote` import.

- [ ] **Step 4: Rewrite `store.py`**

```python
class TickStore:
    """A thin, typed wrapper over Delta tables at s3://<bucket>/<library>/<symbol>."""

    def __init__(self, cfg: S3Config, base: str | None = None):
        self._cfg = cfg
        self._base = (base or cfg.base_uri).rstrip("/")
        self._opts = {} if base else cfg.storage_options

    def _path(self, library: str, symbol: str) -> str:
        return f"{self._base}/{library}/{symbol}"

    def write(self, library, symbol, frame, metadata=None) -> None:
        """Overwrite `symbol`, so re-running a load is idempotent."""
        import json

        from deltalake import CommitProperties, write_deltalake
        from deltalake.exceptions import DeltaError

        out = frame.reset_index()
        if "date" not in out.columns:
            out = out.rename(columns={out.columns[0]: "date"})
        props = None
        if metadata:
            props = CommitProperties(
                custom_metadata={"openbb_meta": json.dumps(metadata, default=str)}
            )
        try:
            write_deltalake(
                self._path(library, symbol), out,
                mode="overwrite", schema_mode="overwrite",
                storage_options=self._opts, commit_properties=props,
            )
        except DeltaError as err:
            raise StoreWriteError(
                f"Delta rejected the write for {symbol!r} to library {library!r}: {err}"
            ) from err

    def read(self, library, symbol, start=None, end=None) -> pd.DataFrame:
        from deltalake import DeltaTable

        if not DeltaTable.is_deltatable(
            self._path(library, symbol), storage_options=self._opts
        ):
            if not self.list_symbols(library):
                raise LibraryNotFoundError(
                    f"Delta library {library!r} does not exist. Check "
                    "DELTA_LIBRARY/--library, or run `tick-lab load` first "
                    "if nothing has been written to it yet."
                )
            raise ValueError(f"symbol {symbol!r} not found in library {library!r}")
        dt = DeltaTable(self._path(library, symbol), storage_options=self._opts)
        dataset = dt.to_pyarrow_dataset()
        start_ts, end_ts = to_bounds(start, end)
        filt = _date_filter(dataset.schema, start_ts, end_ts)
        df = dataset.to_table(filter=filt).to_pandas()
        if "date" in df.columns:
            df = df.set_index("date").sort_index()
        return df

    def list_symbols(self, library: str) -> list[str]:
        from pyarrow import fs as pafs

        fsys, root = self._fs_and_root()
        sel = pafs.FileSelector(f"{root}/{library}", allow_not_found=True)
        return sorted(
            info.base_name
            for info in fsys.get_file_info(sel)
            if info.type == pafs.FileType.Directory
            and fsys.get_file_info(f"{info.path}/_delta_log").type
            != pafs.FileType.NotFound
        )

    def has(self, library: str, symbol: str) -> bool:
        return symbol in self.list_symbols(library)

    def _fs_and_root(self):
        from pyarrow import fs as pafs

        if self._base.startswith("s3://"):
            return (
                pafs.S3FileSystem(
                    access_key=self._cfg.access,
                    secret_key=self._cfg.secret,
                    endpoint_override=self._opts.get("aws_endpoint"),
                    region="us-east-1",
                    force_virtual_addressing=False,
                ),
                self._base[len("s3://") :],
            )
        return pafs.LocalFileSystem(), self._base
```

`to_bounds` stays exactly as it is in the file today. Add the same `_date_filter` helper as Task 3 Step 4 (module-level, duplicated here on purpose — tick-lab must not import openbb code). Update the `StoreWriteError` docstring to name `deltalake.exceptions.DeltaError`.

- [ ] **Step 5: Update pyproject + .env.example + cli comment**

`tick-lab/pyproject.toml` dependencies: replace `"arcticdb>=6.21"` with `"deltalake>=1.0", "pyarrow>=16"`. `.env.example`: rename all `ARCTICDB_S3_*` keys to `DELTA_S3_*`. `cli.py`: the comment "ArcticDB rejecting the frame" becomes "delta-rs rejecting the frame"; `__init__.py` docstring likewise if it names ArcticDB.

- [ ] **Step 6: Run the tick-lab suite**

```bash
cd tick-lab && pip install -e ".[dev]" && pytest tests/ -v
```

Expected: ALL PASS (golden-parity and adapter tests are store-agnostic and must pass unchanged).

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "feat: tick-lab talks Delta Lake directly, DELTA_S3_* config"
```

---

### Task 6: Repo plumbing — Docker, compose, env, constraints, CI, registry

**Files:**
- Modify: `Dockerfile`, `docker-compose.yml`, `minio.env.example`, `extension-constraints.txt`, `key-maint/app/registry.py`, `.github/workflows/ci.yml`, `scripts/scrub-allowlist.txt`, `minio/entrypoint.sh` (if it names ARCTICDB vars)

**Interfaces:**
- Consumes: the renamed package and env names from Tasks 1–5.
- Produces: an image whose sanity check asserts `'deltalake' in obb.coverage.providers`; compose with no amd64 pins; every `ARCTICDB`/`arctic` token renamed (verified by grep in Step 4).

- [ ] **Step 1: Dockerfile**

Replace the ArcticDB block (lines ~98–104): comment becomes "Delta Lake store + provider extension (Ep. 11). Bars and ticks persisted to S3/MinIO can stand in for an upstream API call via provider=\"deltalake\"."; delete the "manylinux x86_64 wheels ONLY" note; `COPY openbb-deltalake /tmp/openbb-deltalake` and `RUN pip install --no-cache-dir /tmp/openbb-deltalake && rm -rf /tmp/openbb-deltalake`. Sanity check line: `assert 'deltalake' in obb.coverage.providers, 'deltalake provider not registered'` and the print mentions `deltalake` instead of `arcticdb`.

- [ ] **Step 2: docker-compose.yml**

- Delete all `platform: linux/amd64` lines and their "ArcticDB publishes no aarch64 wheels" comments (openbb-api, openbb-mcp, and the minio service — the MinIO image is multi-arch; if the local `minio/` build fails on arm64 in Step 5, restore ONLY the minio pin with a comment saying why).
- Rename every `ARCTICDB_S3_*` reference: env comments and the minio-init shell block (`$${ARCTICDB_S3_ENDPOINT}` → `$${DELTA_S3_ENDPOINT}` etc.).
- Update the Ep. 11 comments to say Delta Lake, and the "falls back silently to a local LMDB store" comment to "falls back silently to a local directory store (see `openbb_deltalake.utils.resolve_config`)".

- [ ] **Step 3: minio.env.example, constraints, registry, CI, scrub-allowlist**

- `minio.env.example`: header comment says "the Delta Lake connection derived from them"; keys become `DELTA_S3_ENDPOINT/PORT/BUCKET/SECURE/ACCESS/SECRET`, `DELTA_LIBRARY=openbb` ("Delta library used by the provider…").
- `extension-constraints.txt`: delete the `protobuf<7` line (ArcticDB-only). KEEP `pandas<3` but fix its attribution: the comment already says pykx imports `pandas.core.indexes.numeric` — reword the block so pandas<3 is attributed to pykx (Ep. 10) alone, and remove ArcticDB from the comment.
- `key-maint/app/registry.py` (lines ~165–167): `"ARCTICDB_BUCKET"` → `"DELTA_S3_BUCKET"`, `"ARCTICDB_LIBRARY"` → `"DELTA_LIBRARY"`, `"ARCTICDB_URI"` → `"DELTA_URI"` (read the surrounding block first; keep whatever role the list plays, this is a token rename).
- `.github/workflows/ci.yml`: rename any `openbb-arcticdb` paths/job names to `openbb-deltalake`; env vars in test steps to `DELTA_*`.
- `scripts/scrub-allowlist.txt`: rename `ARCTICDB_*` entries to the `DELTA_*` equivalents.
- `minio/entrypoint.sh`: rename any `ARCTICDB_S3_*` variables it reads.

- [ ] **Step 4: Verify zero leftovers**

```bash
cd ~/Developer/openbb-docker-ep11 && grep -rn -i "arctic" --include='*' . \
  | grep -v -e .git/ -e __pycache__ -e docs/ -e '\.md:'
```

Expected: no output (docs/ intentionally keeps the history: the old spec and the evaluation).

- [ ] **Step 5: Build the image and boot the stack**

```bash
docker compose build openbb-api && docker compose up -d && sleep 20 && docker compose logs openbb-api | tail -20
```

Expected: build passes the sanity `assert 'deltalake' in obb.coverage.providers`; api healthy. Then `docker compose down`.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: image, compose, env, CI, and registry on deltalake; drop amd64 pins"
```

---

### Task 7: Integration tests — provider parity and MinIO concurrent appends

**Files:**
- Modify: `tests/integration/test_provider_parity.py`
- Create: `tests/integration/test_minio_concurrency.py`

**Interfaces:**
- Consumes: `DeltaStore` and the `DELTA_S3_*` env convention.
- Produces: the spec's verification gate — proof that MinIO conditional puts make concurrent commits safe, or a documented failure that goes back to the operator.

- [ ] **Step 1: Rename provider in the parity test**

In `tests/integration/test_provider_parity.py`, change `provider="arcticdb"` to `provider="deltalake"` and any `ARCTICDB_*` env plumbing to `DELTA_*`. No behavioral change.

- [ ] **Step 2: Write the concurrency test**

`tests/integration/test_minio_concurrency.py`:

```python
"""Spec gate: delta-rs commit atomicity on MinIO via conditional puts.

Two writers appending concurrently must BOTH land (no lost update) or fail
loudly — never silently drop a commit. Skipped without a MinIO env.
"""

import os
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DELTA_S3_ENDPOINT"), reason="needs MinIO (DELTA_S3_* unset)"
)

N_WRITERS = 8


def test_concurrent_appends_all_land():
    from openbb_deltalake.store import DeltaStore

    store = DeltaStore(library="it_concurrency")
    sym = "CONCURRENT"
    if store.has(sym):
        store.delete(sym)
    store.write(sym, pd.DataFrame({"date": pd.to_datetime(["2026-01-01"]), "n": [-1]}))

    def _append(i: int):
        df = pd.DataFrame(
            {"date": [pd.Timestamp("2026-01-02") + pd.Timedelta(minutes=i)], "n": [i]}
        )
        # delta-rs retries commit conflicts internally for blind appends;
        # a raised CommitFailedError here is a FINDING, not flake — it means
        # MinIO's conditional puts are not doing their job.
        DeltaStore(library="it_concurrency").append(sym, df)

    with ThreadPoolExecutor(max_workers=N_WRITERS) as pool:
        list(pool.map(_append, range(N_WRITERS)))

    back = store.read(sym, output="dataframe")
    assert sorted(back["n"].tolist()) == [-1, *range(N_WRITERS)]
    store.delete(sym)
```

- [ ] **Step 3: Run against MinIO**

With the stack up and `minio.env` loaded:

```bash
set -a && source minio.env && set +a && pytest tests/integration/test_minio_concurrency.py -v
```

Expected: PASS. **If it fails with CommitFailedError or a lost row: STOP — this is the spec's decision gate. Report to the operator; the fallback choice (single-writer discipline vs a lock table) is theirs.**

- [ ] **Step 4: Run parity**

```bash
pytest tests/integration/test_provider_parity.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "test: provider parity on deltalake; MinIO concurrent-append gate"
```

---

### Task 8: Docs

**Files:**
- Modify: `README.md`, `openbb-deltalake/README.md`, `tick-lab/README.md`, `docs/arcticdb-minio-design.md` (add a superseded-by note at top only), `docs/key-maint-design.md` (token rename if it names ARCTICDB vars)

**Interfaces:**
- Consumes: everything shipped in Tasks 1–7.
- Produces: docs that match the shipped stack.

- [ ] **Step 1: Update the READMEs**

- Root `README.md`: Ep. 11 section — store is Delta Lake (delta-rs, Apache 2.0); one paragraph on WHY (the licensing story is an episode beat, link `docs/arcticdb-alternatives-evaluation.md`); env var names; note Apple Silicon now runs native (amd64 pins gone); one time-travel example (`obb` read with `as_of`).
- `openbb-deltalake/README.md`: rewrite usage examples with `.deltalake` accessor, `provider="deltalake"`, `DELTA_*` vars, `store()` API including `as_of`, and the `date`-column round-trip convention.
- `tick-lab/README.md`: env var names, "the store is a directory of Parquet + a transaction log — open it with anything that reads Delta (DuckDB, Polars, pandas)".

- [ ] **Step 2: Mark the old spec superseded**

Top of `docs/arcticdb-minio-design.md`, after the title: "> **Storage superseded:** ArcticDB was replaced by Delta Lake for licensing reasons — see `docs/superpowers/specs/2026-09-01-deltalake-store-design.md`. The MinIO/tailnet design below stands." Change nothing else in that file.

- [ ] **Step 3: Full-suite final gate**

```bash
cd openbb-deltalake && pytest tests/ -q && cd ../tick-lab && pytest tests/ -q
```

Expected: ALL PASS.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "docs: READMEs and design notes on the Delta Lake stack"
```

---

## Out of scope (tracked, not in this repo)

- `load-firstratedata-tick-zip` skill (in `~/.claude`): its ArcticDB writer needs the same `write_deltalake` treatment — follow-up after this plan lands.
- GHCR image publish / release tagging: the repo's own Release workflow, run by the operator.

"""Server-layer tests for the ArcticDB half. Every backend call is injected
-- no real ArcticDB or kdb+ is ever touched."""
import server
from fastapi.testclient import TestClient

from app.main import create_app

# Design spec (Testing, bullet 5): no credential ever reaches a response
# body or error message. In production, every ValueError that reaches this
# file's routes has already passed through mcp_stores's _bounded(), which
# scrubs credentials out of the exception text before re-raising -- so the
# text a backend function raises here is realistic *already-scrubbed*
# output, produced with the real server._scrub() (not re-testing _scrub
# itself, which has its own tests in mcp_stores/test_server.py). These
# tests instead pin main.py's own exception handling: does
# `except ValueError as e: raise HTTPException(..., detail=str(e))`
# preserve that already-scrubbed text as-is, with no path that could
# reintroduce the raw credentials it was built from.
_RAW_CREDENTIAL_MESSAGE = (
    "S3 error connecting to s3://minio.example.ts.net:openbb?port=9000"
    "&access=AKIAEXAMPLE&secret=topsecret123"
)
_SCRUBBED_CREDENTIAL_MESSAGE = server._scrub(_RAW_CREDENTIAL_MESSAGE)
assert "<redacted>" in _SCRUBBED_CREDENTIAL_MESSAGE  # sanity: scrub actually did something


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


def test_arctic_symbols_error_does_not_leak_credentials():
    def raises_scrubbed_error(library):
        raise ValueError(_SCRUBBED_CREDENTIAL_MESSAGE)

    app = create_app(
        arctic_libraries_fn=lambda: ["openbb"],
        arctic_symbols_fn=raises_scrubbed_error,
        arctic_read_fn=lambda *a, **kw: {},
        arctic_client_factory=lambda: None,
        bounded_fn=lambda fn, *a, **kw: fn(*a, **kw),
    )
    r = TestClient(app).get("/arctic/symbols", params={"library": "openbb"})
    assert r.status_code == 404
    body = r.text
    assert "AKIAEXAMPLE" not in body
    assert "topsecret123" not in body
    assert "s3://minio.example.ts.net" not in body
    assert "<redacted>" in body


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


def test_kdb_schema_error_does_not_leak_credentials():
    def raises_scrubbed_error(table):
        raise ValueError(_SCRUBBED_CREDENTIAL_MESSAGE)

    app = create_app(
        arctic_libraries_fn=lambda: [],
        arctic_symbols_fn=lambda library: [],
        arctic_read_fn=lambda *a, **kw: {},
        arctic_client_factory=lambda: None,
        bounded_fn=lambda fn, *a, **kw: fn(*a, **kw),
        kdb_tables_fn=lambda: ["trades"],
        kdb_schema_fn=raises_scrubbed_error,
        kdb_select_fn=lambda *a, **kw: {},
    )
    r = TestClient(app).get("/kdb/schema", params={"table": "trades"})
    assert r.status_code == 404
    body = r.text
    assert "AKIAEXAMPLE" not in body
    assert "topsecret123" not in body
    assert "s3://minio.example.ts.net" not in body
    assert "<redacted>" in body

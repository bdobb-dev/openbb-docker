# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Server-layer tests for the Delta half. Every backend call is injected
-- no real Delta store or kdb+ is ever touched.

The ArcticDB version of this file needed FakeArctic/FakeLib/FakeSymbolDescription
because /arctic/summary reached into the client itself. delta_describe is a real
mcp_stores tool, so the route is now an injected function like every other one
and the fake store objects are gone with it."""
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


DEFAULT_DESCRIBE = {
    "library": "openbb", "symbol": "AAPL",
    "row_count": 42,
    "date_range": ["2026-01-01", "2026-08-07"],
    "columns": [{"name": "close", "dtype": "float64"}],
}


def make_client(libraries=("openbb",), symbols_by_library=None, series_response=None):
    symbols_by_library = symbols_by_library if symbols_by_library is not None else {"openbb": ["AAPL"]}

    def _check(library, symbol=None):
        if library not in libraries:
            raise ValueError(f"unknown library {library!r}; call delta_list_libraries first")
        if symbol is not None and symbol not in symbols_by_library.get(library, []):
            raise ValueError(
                f"unknown symbol {symbol!r} in {library!r}; call delta_list_symbols first"
            )

    def delta_libraries_fn():
        return list(libraries)

    def delta_symbols_fn(library):
        _check(library)
        return symbols_by_library.get(library, [])

    def delta_describe_fn(library, symbol):
        _check(library, symbol)
        return {**DEFAULT_DESCRIBE, "library": library, "symbol": symbol}

    def delta_history_fn(library, symbol):
        _check(library, symbol)
        return [{"version": 1, "timestamp": "2026-09-02T10:00:00"},
                {"version": 0, "timestamp": "2026-09-01T10:00:00"}]

    def delta_read_fn(library, symbol, start=None, end=None, tail_rows=1000, as_of=None):
        _check(library, symbol)
        return series_response or {
            "library": library, "symbol": symbol,
            "total_rows_in_range": 1, "returned_rows": 1,
            "rows": [{"date": "2026-08-07", "close": 100.0}],
        }

    return TestClient(create_app(
        delta_libraries_fn=delta_libraries_fn,
        delta_symbols_fn=delta_symbols_fn,
        delta_describe_fn=delta_describe_fn,
        delta_history_fn=delta_history_fn,
        delta_read_fn=delta_read_fn,
    ))


def test_widgets_json_declares_delta_explorer():
    body = make_client().get("/widgets.json").json()
    w = body["delta_explorer"]
    assert w["type"] == "delta_explorer"
    assert w["endpoint"] == "delta/series"
    assert w["dataKey"] == "rows"
    assert w["source"] == ["Delta Lake"]
    param_names = [p["paramName"] for p in w["params"]]
    assert param_names == ["library", "symbol", "start", "end", "as_of"]
    symbol_param = next(p for p in w["params"] if p["paramName"] == "symbol")
    assert symbol_param["optionsEndpoint"] == "delta/symbols"
    assert symbol_param["optionsParams"] == {"library": "$library"}
    assert "arctic_explorer" not in body


def test_delta_libraries_lists_libraries():
    r = make_client(libraries=("openbb", "ticks"))
    assert r.get("/delta/libraries").json() == ["openbb", "ticks"]


def test_delta_symbols_lists_symbols_for_library():
    client = make_client(symbols_by_library={"openbb": ["AAPL", "MSFT"]})
    r = client.get("/delta/symbols", params={"library": "openbb"})
    assert r.json() == ["AAPL", "MSFT"]


def test_delta_symbols_unknown_library_is_404():
    r = make_client().get("/delta/symbols", params={"library": "nope"})
    assert r.status_code == 404
    assert "unknown library" in r.json()["detail"]


def test_delta_series_returns_rows():
    r = make_client().get("/delta/series", params={"library": "openbb", "symbol": "AAPL"})
    assert r.status_code == 200
    assert r.json()["rows"] == [{"date": "2026-08-07", "close": 100.0}]


def test_delta_series_unknown_symbol_is_404():
    r = make_client().get("/delta/series", params={"library": "openbb", "symbol": "NOPE"})
    assert r.status_code == 404
    assert "unknown symbol" in r.json()["detail"]


def test_delta_series_passes_as_of_through():
    seen = {}

    def spy(library, symbol, start=None, end=None, tail_rows=1000, as_of=None):
        seen.update(as_of=as_of, tail_rows=tail_rows)
        return {"rows": []}

    client = TestClient(create_app(delta_read_fn=spy))
    client.get("/delta/series", params={"library": "openbb", "symbol": "AAPL", "as_of": "3"})
    assert seen["as_of"] == "3"


def test_delta_describe_returns_metadata_without_reading_rows():
    def spy_read_fn(*a, **kw):
        raise AssertionError("describe must not call delta_read")

    app = create_app(
        delta_describe_fn=lambda library, symbol: DEFAULT_DESCRIBE,
        delta_read_fn=spy_read_fn,
    )
    r = TestClient(app).get("/delta/describe", params={"library": "openbb", "symbol": "AAPL"})
    assert r.status_code == 200
    body = r.json()
    assert body["row_count"] == 42
    assert body["date_range"] == ["2026-01-01", "2026-08-07"]
    assert body["columns"] == [{"name": "close", "dtype": "float64"}]


def test_delta_history_lists_versions():
    r = make_client().get("/delta/history", params={"library": "openbb", "symbol": "AAPL"})
    assert r.status_code == 200
    assert [v["version"] for v in r.json()] == [1, 0]


def test_delta_describe_unknown_symbol_is_404():
    r = make_client(symbols_by_library={"openbb": ["AAPL"]}).get(
        "/delta/describe", params={"library": "openbb", "symbol": "NOPE"}
    )
    assert r.status_code == 404
    assert "unknown symbol" in r.json()["detail"]


def test_delta_describe_unknown_library_is_404():
    r = make_client().get("/delta/describe", params={"library": "nope", "symbol": "AAPL"})
    assert r.status_code == 404
    assert "unknown library" in r.json()["detail"]


def test_delta_symbols_error_does_not_leak_credentials():
    def raises_scrubbed_error(library):
        raise ValueError(_SCRUBBED_CREDENTIAL_MESSAGE)

    app = create_app(delta_symbols_fn=raises_scrubbed_error)
    r = TestClient(app).get("/delta/symbols", params={"library": "openbb"})
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "AKIAEXAMPLE" not in detail
    assert "topsecret123" not in detail
    assert "<redacted>" in detail


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

    return TestClient(create_app(
        kdb_tables_fn=kdb_tables_fn,
        kdb_schema_fn=kdb_schema_fn,
        kdb_select_fn=kdb_select_fn,
    ))


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


def test_dockerfile_installs_unpublished_siblings_before_mcp_stores():
    """mcp_stores depends on openbb-deltalake, which is not on PyPI.

    pip cannot resolve it from the index, so it must already be installed when
    `pip install /srv/mcp_stores` runs. Ordering, not presence, is the bug:
    the build failed with "No matching distribution found for openbb-deltalake".
    """
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    assert dockerfile.index("pip install /srv/openbb-deltalake") < dockerfile.index(
        "pip install /srv/mcp_stores"
    )

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for mcp_stores/server.py: a real tmp-path Delta store, pykx MOCKED.

Runs on the Mac with no kdb installed:
    cd /Users/artcashin/Developer/openbb-docker
    uv run --with fastmcp --with pytest --with pandas python -m pytest mcp_stores/test_server.py -v

deltalake needs no server, so the Delta tools are tested against a real
tmp-path store. server.py imports pykx lazily inside helper functions, so kdb
fakes still go in via sys.modules before any tool touches them.
"""
import os
import sys
import time
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


# ---------- fakes ----------

class K:
    """Stand-in for a pykx K result object."""

    def __init__(self, value):
        self.value = value

    def py(self):
        return self.value

    def pd(self):
        return self.value


class FakeConn:
    """Records every (query, args) call; answers from a canned dict."""

    def __init__(self):
        self.responses = {}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __call__(self, query, *args):
        self.calls.append((query, args))
        return K(self.responses[query])


@pytest.fixture
def delta_store(monkeypatch, tmp_path):
    """A real tmp-path Delta store.

    deltalake needs no server and no credentials, so these tests exercise the
    real read path rather than a MagicMock -- which is what lets the
    "bounds the read" assertion below mean anything.
    """
    from deltalake import write_deltalake

    monkeypatch.setenv("DELTA_URI", str(tmp_path))
    idx = pd.date_range("2026-01-01", periods=5, freq="1min")
    write_deltalake(
        f"{tmp_path}/ticks/AAPL",
        pd.DataFrame({"date": idx, "bid": [1.0, 2.0, 3.0, 4.0, 5.0]}),
        mode="overwrite",
    )
    write_deltalake(
        f"{tmp_path}/hrp_prices/SPY",
        pd.DataFrame({"date": idx, "bid": [1.0] * 5}),
        mode="overwrite",
    )
    return tmp_path


@pytest.fixture
def kdb_conn(monkeypatch):
    conn = FakeConn()
    monkeypatch.setenv("KX_PORT", "127.0.0.1:5000")
    monkeypatch.setitem(
        sys.modules,
        "pykx",
        # **kw absorbs connection_timeout/timeout (finding 4)
        types.SimpleNamespace(SyncQConnection=lambda host, port, **kw: conn),
    )
    return conn


# ---------- delta ----------

def test_delta_list_libraries_sorted(delta_store):
    assert server.delta_list_libraries() == ["hrp_prices", "ticks"]


def test_delta_list_symbols_unknown_library_raises(delta_store):
    with pytest.raises(ValueError, match="unknown library"):
        server.delta_list_symbols("nope")


def test_delta_read_rejects_unknown_library(delta_store):
    # finding 10a: delta_read has its own allowlist check, separate from
    # delta_list_symbols's -- pin it directly so removing it fails this test.
    with pytest.raises(ValueError, match="unknown library"):
        server.delta_read("nope", "AAPL")


def test_delta_read_unknown_symbol_raises(delta_store):
    with pytest.raises(ValueError, match="unknown symbol"):
        server.delta_read("ticks", "NOPE")


def test_delta_read_bounds_the_read_not_just_the_response(delta_store, monkeypatch):
    # finding 2, ported: with no start/end the read must be bounded at the
    # storage layer. ArcticDB gave Library.tail; Delta has none, so this goes
    # through read_trailing -> trailing_fragment_paths. A plain full read
    # would materialize the whole symbol.
    from openbb_deltalake.store import DeltaStore

    def explode(self, *a, **k):
        raise AssertionError("an unfiltered read must not materialize the symbol")

    monkeypatch.setattr(DeltaStore, "read", explode)

    out = server.delta_read("ticks", "AAPL", tail_rows=3)

    assert out["total_rows_in_range"] == 5   # from the log, not from rows
    assert out["returned_rows"] == 3
    assert out["rows"][0]["date"].startswith("2026-01-01T00:02")


def test_delta_read_clamps_tail_rows_to_max_rows(delta_store, monkeypatch):
    # finding 10b: pin the real clamp, not a value the frame is smaller than.
    seen = {}
    from openbb_deltalake.store import DeltaStore

    real = DeltaStore.read_trailing

    def spy(self, key, n_rows, as_of=None):
        seen["n"] = n_rows
        return real(self, key, n_rows, as_of)

    monkeypatch.setattr(DeltaStore, "read_trailing", spy)
    server.delta_read("ticks", "AAPL", tail_rows=999_999_999)
    assert seen["n"] == server.MAX_ROWS


def test_delta_read_passes_date_range(delta_store):
    out = server.delta_read(
        "ticks", "AAPL", start="2026-01-01T00:01", end="2026-01-01T00:03"
    )
    assert out["returned_rows"] == 3
    assert out["total_rows_in_range"] == 3


def test_delta_describe_reports_metadata_without_reading_rows(delta_store, monkeypatch):
    from openbb_deltalake.store import DeltaStore

    def explode(self, *a, **k):
        raise AssertionError("describe must not read rows")

    monkeypatch.setattr(DeltaStore, "read", explode)
    monkeypatch.setattr(DeltaStore, "read_trailing", explode)

    out = server.delta_describe("ticks", "AAPL")
    assert out["row_count"] == 5
    assert {c["name"] for c in out["columns"]} == {"date", "bid"}


def test_delta_history_and_as_of_return_superseded_data(delta_store):
    from deltalake import write_deltalake

    idx = pd.date_range("2026-01-01", periods=5, freq="1min")
    write_deltalake(
        f"{delta_store}/ticks/AAPL",
        pd.DataFrame({"date": idx, "bid": [9.0] * 5}),
        mode="overwrite", schema_mode="overwrite",
    )

    versions = [h["version"] for h in server.delta_history("ticks", "AAPL")]
    assert versions == sorted(versions, reverse=True)
    assert len(versions) >= 2

    assert server.delta_read("ticks", "AAPL")["rows"][-1]["bid"] == 9.0
    # as_of accepts the int version and its string form (an MCP arg is text)
    assert server.delta_read("ticks", "AAPL", as_of=0)["rows"][-1]["bid"] == 5.0
    assert server.delta_read("ticks", "AAPL", as_of="0")["rows"][-1]["bid"] == 5.0


# ---------- finding 1: credential scrubbing ----------

def test_scrub_redacts_access_and_secret_params():
    msg = (
        "S3 error connecting to s3://192.0.2.60:openbb?port=9000"
        "&access=AKIAMINIOKEY&secret=SUPERSECRET123"
    )
    out = server._scrub(msg)
    assert "AKIAMINIOKEY" not in out
    assert "SUPERSECRET123" not in out


def test_scrub_redacts_delta_storage_option_credentials():
    """The leak moved with the store: delta-rs takes credentials as
    storage_options, so they surface as `access_key_id: ...` rather than as a
    URI query param. _scrub has to cover the shape that can actually occur."""
    msg = (
        "GenericError { store: S3, source: access_key_id: AKIAMINIOKEY, "
        "secret_access_key=SUPERSECRET123 }"
    )
    out = server._scrub(msg)
    assert "AKIAMINIOKEY" not in out
    assert "SUPERSECRET123" not in out
    assert "<redacted>" in out


def test_delta_backend_error_never_leaks_credentials(monkeypatch):
    # A bad/unreachable store raises an error that (for a real S3 client)
    # echoes the credential it was given. Confirm the caller never sees it.
    def boom(uri=None, library=None):
        raise ValueError(
            "S3 error: access_key_id: AKIAMINIOKEY secret_access_key=SUPERSECRET123"
        )

    monkeypatch.setitem(
        sys.modules, "openbb_deltalake.store", types.SimpleNamespace(DeltaStore=boom)
    )

    with pytest.raises(ValueError) as exc_info:
        server.delta_list_libraries()

    msg = str(exc_info.value)
    assert "AKIAMINIOKEY" not in msg
    assert "SUPERSECRET123" not in msg
    assert "<redacted>" in msg


# ---------- finding 4: backend timeouts ----------

def test_delta_call_times_out_cleanly_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(server, "STORES_TIMEOUT_S", 0.05)

    def hang(uri=None, library=None):
        time.sleep(5)

    monkeypatch.setitem(
        sys.modules, "openbb_deltalake.store", types.SimpleNamespace(DeltaStore=hang)
    )

    start = time.monotonic()
    with pytest.raises(TimeoutError, match="timed out"):
        server.delta_list_libraries()
    assert time.monotonic() - start < 2  # returns promptly, doesn't wait for the hang


def test_kdb_conn_sets_query_and_connect_timeouts(monkeypatch):
    captured = {}

    def fake_sync_q_connection(host, port, **kwargs):
        captured.update(kwargs)
        return FakeConn()

    monkeypatch.setenv("KX_PORT", "127.0.0.1:5000")
    monkeypatch.setitem(
        sys.modules,
        "pykx",
        types.SimpleNamespace(SyncQConnection=fake_sync_q_connection),
    )

    server._kdb_conn()

    assert captured["timeout"] == server.STORES_TIMEOUT_S
    assert captured["connection_timeout"] == server.STORES_TIMEOUT_S


# ---------- finding 6: regex trailing-newline bypass ----------

def test_ident_re_rejects_trailing_newline():
    assert server._IDENT_RE.match("trade\n") is None


def test_time_re_rejects_trailing_newline():
    assert server._TIME_RE.match("2026-01-01\n") is None


# ---------- kdb ----------

def test_kdb_tables_decodes_and_sorts(kdb_conn):
    kdb_conn.responses["tables[]"] = [b"trade", b"quote"]
    assert server.kdb_tables() == ["quote", "trade"]


def test_kdb_select_rejects_unknown_table(kdb_conn):
    kdb_conn.responses["tables[]"] = [b"trade"]
    with pytest.raises(ValueError, match="unknown table"):
        server.kdb_select("evil")


def test_kdb_select_rejects_hostile_symbol_before_connecting(kdb_conn):
    with pytest.raises(ValueError, match="invalid symbol"):
        server.kdb_select("trade", symbol='AAPL"; system "ls')
    assert kdb_conn.calls == []  # rejected before any IPC


def test_kdb_select_rejects_hostile_table_before_connecting(kdb_conn):
    # finding 10c: pin _check_ident("table", ...) directly -- without it,
    # this would proceed to call tables[] over IPC instead of raising here.
    with pytest.raises(ValueError, match="invalid table"):
        server.kdb_select('trade"; system "ls')
    assert kdb_conn.calls == []


def test_kdb_select_empty_symbol_means_no_filter(kdb_conn):
    # finding 8: symbol="" (or whitespace) must behave like symbol omitted,
    # not raise "invalid symbol".
    meta = pd.DataFrame({"c": ["price"], "t": ["f"], "f": [""], "a": [""]})
    kdb_conn.responses["tables[]"] = [b"trade"]
    kdb_conn.responses[server._Q_META] = meta
    kdb_conn.responses[server._Q_SELECT] = pd.DataFrame({"price": [1.0]})

    server.kdb_select("trade", symbol="   ")

    _, args = kdb_conn.calls[-1]
    assert args[3] == b""  # no symbol filter sent -- and no raise


def test_kdb_select_is_parameterized(kdb_conn):
    meta = pd.DataFrame(
        {"c": ["time", "sym", "price"], "t": ["p", "s", "f"],
         "f": ["", "", ""], "a": ["", "", ""]}
    )
    sel = pd.DataFrame(
        {"time": pd.to_datetime(["2026-01-01 09:30:00"]),
         "sym": [b"AAPL"], "price": [1.5]}
    )
    kdb_conn.responses["tables[]"] = [b"trade"]
    kdb_conn.responses[server._Q_META] = meta
    kdb_conn.responses[server._Q_SELECT] = sel

    out = server.kdb_select(
        "trade", symbol="AAPL", start_time="2026-01-01", limit=500
    )

    query, args = kdb_conn.calls[-1]
    assert query == server._Q_SELECT          # fixed q source, never rebuilt
    assert "AAPL" not in query                # user data NOT interpolated
    assert args == (b"trade", b"sym", b"time", b"AAPL", b"2026-01-01", b"", 500)
    assert out["returned_rows"] == 1
    assert out["rows"][0]["sym"] == "AAPL"    # bytes decoded


def test_kdb_select_caps_limit(kdb_conn):
    meta = pd.DataFrame(
        {"c": ["price"], "t": ["f"], "f": [""], "a": [""]}
    )
    kdb_conn.responses["tables[]"] = [b"trade"]
    kdb_conn.responses[server._Q_META] = meta
    kdb_conn.responses[server._Q_SELECT] = pd.DataFrame({"price": [1.0]})

    server.kdb_select("trade", limit=10_000_000)

    _, args = kdb_conn.calls[-1]
    assert args[-1] == server.MAX_ROWS


def test_q_select_bounds_rows_at_select_not_after():
    # finding 3: q must limit rows AT the select (select[n]) so it never
    # copies the whole matched result before truncating.
    assert "select[n]" in server._Q_SELECT
    assert "sublist" not in server._Q_SELECT


# ---------- finding 10d: kdb_table_schema had zero coverage ----------

def test_kdb_table_schema_returns_meta_records(kdb_conn):
    meta = pd.DataFrame(
        {"c": ["time", "sym", "price"], "t": ["p", "s", "f"],
         "f": ["", "", ""], "a": ["", "", ""]}
    )
    kdb_conn.responses["tables[]"] = [b"trade"]
    kdb_conn.responses[server._Q_META] = meta

    out = server.kdb_table_schema("trade")

    assert out == [
        {"c": "time", "t": "p", "f": "", "a": ""},
        {"c": "sym", "t": "s", "f": "", "a": ""},
        {"c": "price", "t": "f", "f": "", "a": ""},
    ]


def test_kdb_table_schema_rejects_unknown_table(kdb_conn):
    kdb_conn.responses["tables[]"] = [b"trade"]
    with pytest.raises(ValueError, match="unknown table"):
        server.kdb_table_schema("nope")


def test_kdb_table_schema_rejects_hostile_table_before_connecting(kdb_conn):
    with pytest.raises(ValueError, match="invalid table"):
        server.kdb_table_schema('trade"; system "ls')
    assert kdb_conn.calls == []

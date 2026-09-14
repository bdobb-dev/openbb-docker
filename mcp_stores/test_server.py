"""Unit tests for mcp_stores/server.py with arcticdb and pykx MOCKED.

Runs on the Mac with no arcticdb/pykx/kdb installed:
    cd /Users/artcashin/Developer/openbb-docker
    uv run --with fastmcp --with pytest --with pandas python -m pytest mcp_stores/test_server.py -v

server.py imports arcticdb/pykx lazily inside helper functions, so these tests
inject fakes via sys.modules before any tool touches them.
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
def arctic_store(monkeypatch):
    monkeypatch.setenv(
        "ARCTICDB_URI", "s3://192.0.2.60:openbb?port=9000&access=x&secret=y"
    )
    store = MagicMock()
    monkeypatch.setitem(
        sys.modules, "arcticdb", types.SimpleNamespace(Arctic=lambda uri: store)
    )
    return store


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


# ---------- arctic ----------

def test_arctic_list_libraries_sorted(arctic_store):
    arctic_store.list_libraries.return_value = ["ticks", "hrp_prices"]
    assert server.arctic_list_libraries() == ["hrp_prices", "ticks"]


def test_arctic_list_symbols_unknown_library_raises(arctic_store):
    arctic_store.list_libraries.return_value = ["ticks"]
    with pytest.raises(ValueError, match="unknown library"):
        server.arctic_list_symbols("nope")


def test_arctic_read_rejects_unknown_library(arctic_store):
    # finding 10a: arctic_read has its own allowlist check, separate from
    # arctic_list_symbols's -- pin it directly so removing it fails this test.
    arctic_store.list_libraries.return_value = ["ticks"]
    with pytest.raises(ValueError, match="unknown library"):
        server.arctic_read("nope", "AAPL")


def test_arctic_read_bounds_the_read_not_just_the_response(arctic_store):
    # finding 2: with no start/end, arctic_read must use Library.tail()
    # (bounded at the backend) instead of read()+tail() (materializes all).
    idx = pd.date_range("2026-01-01", periods=3, freq="1min")
    df = pd.DataFrame({"bid": [1.0, 2.0, 3.0]}, index=idx)
    arctic_store.list_libraries.return_value = ["ticks"]
    lib = arctic_store.__getitem__.return_value
    lib.list_symbols.return_value = ["AAPL"]
    lib.tail.return_value = SimpleNamespace(data=df)
    lib.get_description.return_value = SimpleNamespace(row_count=10_000_000)

    out = server.arctic_read("ticks", "AAPL", tail_rows=3)

    lib.tail.assert_called_once_with("AAPL", n=3)
    lib.read.assert_not_called()  # never materializes the whole symbol
    assert out["total_rows_in_range"] == 10_000_000  # cheap get_description call
    assert out["returned_rows"] == 3
    assert out["rows"][0]["index"].startswith("2026-01-01T00:00")


def test_arctic_read_clamps_tail_rows_to_max_rows(arctic_store):
    # finding 10b: the existing test used tail_rows=3 on a 5-row frame, so
    # MAX_ROWS was never actually exercised. Pin the real clamp here.
    arctic_store.list_libraries.return_value = ["ticks"]
    lib = arctic_store.__getitem__.return_value
    lib.list_symbols.return_value = ["AAPL"]
    lib.tail.return_value = SimpleNamespace(data=pd.DataFrame({"bid": [1.0]}))
    lib.get_description.return_value = SimpleNamespace(row_count=1)

    server.arctic_read("ticks", "AAPL", tail_rows=999_999_999)

    lib.tail.assert_called_once_with("AAPL", n=server.MAX_ROWS)


def test_arctic_read_passes_date_range(arctic_store):
    df = pd.DataFrame({"bid": [1.0]}, index=pd.date_range("2026-01-01", periods=1))
    arctic_store.list_libraries.return_value = ["ticks"]
    lib = arctic_store.__getitem__.return_value
    lib.list_symbols.return_value = ["AAPL"]
    lib.read.return_value = SimpleNamespace(data=df)

    server.arctic_read("ticks", "AAPL", start="2026-01-01", end="2026-01-02")

    _, kwargs = lib.read.call_args
    assert kwargs["date_range"] == (
        pd.Timestamp("2026-01-01"),
        pd.Timestamp("2026-01-02"),
    )


def test_arctic_read_unknown_symbol_raises(arctic_store):
    arctic_store.list_libraries.return_value = ["ticks"]
    lib = arctic_store.__getitem__.return_value
    lib.list_symbols.return_value = ["AAPL"]
    with pytest.raises(ValueError, match="unknown symbol"):
        server.arctic_read("ticks", "MSFT")


# ---------- finding 1: credential scrubbing ----------

def test_scrub_redacts_access_and_secret_params():
    msg = (
        "S3 error connecting to s3://192.0.2.60:openbb?port=9000"
        "&access=AKIAMINIOKEY&secret=SUPERSECRET123"
    )
    out = server._scrub(msg)
    assert "AKIAMINIOKEY" not in out
    assert "SUPERSECRET123" not in out


def test_arctic_backend_error_never_leaks_credentials(monkeypatch):
    # Reproduces the finding-1 repro end to end: a bad/unreachable
    # ARCTICDB_URI raises a ValueError that (for a real S3 client) echoes
    # the full credential-bearing URI. Confirm the caller never sees it.
    secret_uri = (
        "s3://192.0.2.60:openbb?port=9000"
        "&access=AKIAMINIOKEY&secret=SUPERSECRET123"
    )
    monkeypatch.setenv("ARCTICDB_URI", secret_uri)

    def boom(uri):
        raise ValueError(f"S3 error connecting to {uri}")

    monkeypatch.setitem(sys.modules, "arcticdb", types.SimpleNamespace(Arctic=boom))

    with pytest.raises(ValueError) as exc_info:
        server.arctic_list_libraries()

    msg = str(exc_info.value)
    assert "AKIAMINIOKEY" not in msg
    assert "SUPERSECRET123" not in msg
    assert "<redacted>" in msg


# ---------- finding 4: backend timeouts ----------

def test_arctic_call_times_out_cleanly_instead_of_hanging(monkeypatch):
    monkeypatch.setenv("ARCTICDB_URI", "s3://host:bucket?port=9000")
    monkeypatch.setattr(server, "STORES_TIMEOUT_S", 0.05)

    def hang(uri):
        time.sleep(5)

    monkeypatch.setitem(sys.modules, "arcticdb", types.SimpleNamespace(Arctic=hang))

    start = time.monotonic()
    with pytest.raises(TimeoutError, match="timed out"):
        server.arctic_list_libraries()
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

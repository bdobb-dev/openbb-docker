"""Runnable (pyarrow/deltalake/pytest-free) tests for the pure parts of
`tick_vault.capture` (task-1 brief): payload path layout, gzip write +
immutability guard, sha256, `build_capture_row`'s column-filling, which
body a capture hashes/persists on success vs. failure, distinct
capture_ids across retries, and the `Transport`/`FakeTransport`/
`UrlLibTransport` retry logic.

`tick_vault.capture` itself imports bare (no pyarrow/deltalake at module
scope - those are lazy-imported inside `append_rows`/`CaptureStore.record`,
neither of which this file exercises), so it's importable and runnable
here even though pyarrow/deltalake are not installed in this sandbox.
`CaptureStore.record`'s Delta-writing path is verified separately in
`tests/deferred/test_capture.py` (pytest, real tmp_path Delta lake).
"""
import ast
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import tempfile
import urllib.error

from tick_vault.capture import (
    CaptureRecord,
    FakeTransport,
    Transport,
    UrlLibTransport,
    build_capture_row,
    payload_path,
    resolve_capture_body,
    row_count_hint,
    sha256_hex,
    write_payload,
)
from tick_vault.ids import new_id

_SCHEMAS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tick_vault",
    "schemas.py",
)


# ---------------------------------------------------------------------------
# ast-based column extraction (mirrors tests/test_schema_registry_names.py's
# helper approach: parse schemas.py's *source text*, never import it, since
# it does `import pyarrow as pa` at module top and pyarrow isn't installed
# in this sandbox).
# ---------------------------------------------------------------------------

def _read_source() -> str:
    with open(_SCHEMAS_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _find_name_assignment(tree: ast.Module, name: str) -> ast.AST:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node.value
    raise AssertionError(f"could not find module-level assignment {name!r}")


def _field_names_in_schema_call(node: ast.AST) -> list:
    names = []
    for call_node in ast.walk(node):
        if (
            isinstance(call_node, ast.Call)
            and isinstance(call_node.func, ast.Attribute)
            and call_node.func.attr == "field"
            and call_node.args
            and isinstance(call_node.args[0], ast.Constant)
            and isinstance(call_node.args[0].value, str)
        ):
            names.append(call_node.args[0].value)
    return names


def _bronze_eodhd_tick_capture_columns() -> list:
    tree = ast.parse(_read_source(), filename=_SCHEMAS_PATH)
    node = _find_name_assignment(tree, "_BRONZE_EODHD_TICK_CAPTURE")
    return _field_names_in_schema_call(node)


_OBS = dt.datetime(2026, 3, 5, 12, 0, 0, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------------
# payload path layout
# ---------------------------------------------------------------------------

def test_payload_path_layout():
    p = payload_path("/lake", "cap_abc123", dt.datetime(2026, 3, 5, tzinfo=dt.timezone.utc))
    assert p == os.path.join("/lake", "_payloads", "2026", "03", "cap_abc123.json.gz")


def test_payload_path_zero_pads_month():
    p = payload_path("/lake", "cap_x", dt.datetime(2026, 1, 9, tzinfo=dt.timezone.utc))
    assert p.endswith(os.path.join("2026", "01", "cap_x.json.gz"))


# ---------------------------------------------------------------------------
# gzip write + immutability
# ---------------------------------------------------------------------------

def test_write_payload_gzip_roundtrip():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "_payloads", "2026", "03", "cap_1.json.gz")
        body = b'[{"ts": 1, "seq": 1}]'
        write_payload(path, body)
        assert os.path.exists(path)
        with gzip.open(path, "rb") as f:
            assert f.read() == body
    finally:
        shutil.rmtree(tmp)


def test_write_payload_immutable_raises_on_second_write():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "cap_1.json.gz")
        write_payload(path, b"first")
        try:
            write_payload(path, b"second")
            raise AssertionError("FileExistsError not raised")
        except FileExistsError:
            pass
        # original content untouched
        with gzip.open(path, "rb") as f:
            assert f.read() == b"first"
    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# sha256
# ---------------------------------------------------------------------------

def test_sha256_hex_matches_stdlib():
    body = b'[{"ts": 1, "seq": 1}]'
    assert sha256_hex(body) == hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------
# resolve_capture_body: success hashes+persists payload; failure hashes
# error body but never persists (no payload_uri)
# ---------------------------------------------------------------------------

def test_resolve_capture_body_success_persists_payload():
    body, should_persist = resolve_capture_body(b"payload-bytes", None)
    assert body == b"payload-bytes"
    assert should_persist is True


def test_resolve_capture_body_failure_hashes_error_without_persisting():
    body, should_persist = resolve_capture_body(None, b"500 server error")
    assert body == b"500 server error"
    assert should_persist is False


def test_resolve_capture_body_failure_with_no_error_body():
    body, should_persist = resolve_capture_body(None, None)
    assert body == b""
    assert should_persist is False


# ---------------------------------------------------------------------------
# row_count_hint
# ---------------------------------------------------------------------------

def test_row_count_hint_counts_json_array():
    assert row_count_hint(b'[{"a": 1}, {"a": 2}, {"a": 3}]') == 3


def test_row_count_hint_none_for_non_list_or_missing():
    assert row_count_hint(b'{"a": 1}') is None
    assert row_count_hint(None) is None
    assert row_count_hint(b"not json") is None


# ---------------------------------------------------------------------------
# build_capture_row: fills every bronze.eodhd_tick_capture schema column
# ---------------------------------------------------------------------------

def test_build_capture_row_fills_every_bronze_tick_capture_column():
    columns = _bronze_eodhd_tick_capture_columns()
    assert len(columns) > 15  # sanity: ast parse actually found the schema

    row = build_capture_row(
        columns=columns,
        capture_id="cap_test123",
        ingestion_run_id="run_x",
        source_system="eodhd",
        endpoint="/api/ticks",
        parser_version="TICK_PARSER_V1",
        request_parameters_json=json.dumps({"s": "AAPL", "from": 1, "to": 2}, sort_keys=True),
        http_status=200,
        payload_uri="/lake/_payloads/2026/03/cap_test123.json.gz",
        payload_sha256="deadbeef",
        row_count_hint=1,
        observed_at=_OBS,
        committed_at=_OBS,
        created_at=_OBS,
        extra_cols={
            "request_symbol": "AAPL",
            "request_from_sec": 1,
            "request_to_sec": 2,
        },
    )

    assert set(row.keys()) == set(columns)
    assert row["capture_id"] == "cap_test123"
    assert row["ingestion_run_id"] == "run_x"
    assert row["source_system"] == "eodhd"
    assert row["endpoint"] == "/api/ticks"
    assert row["parser_version"] == "TICK_PARSER_V1"
    assert row["http_status"] == 200
    assert row["raw_payload_uri"] == "/lake/_payloads/2026/03/cap_test123.json.gz"
    assert row["raw_payload_sha256"] == "deadbeef"
    assert row["raw_row_count"] == 1
    assert row["observed_at_ts"] == _OBS
    assert row["committed_at_ts"] == _OBS
    assert row["created_at_ts"] == _OBS
    assert row["committed_date"] == _OBS.date()
    assert row["request_symbol"] == "AAPL"
    assert row["request_from_sec"] == 1
    assert row["request_to_sec"] == 2
    # columns with no source in common/extra_cols stay None
    assert row["request_cursor"] is None
    assert row["response_headers_json"] is None


def test_build_capture_row_failure_case_has_no_payload_uri():
    columns = _bronze_eodhd_tick_capture_columns()
    row = build_capture_row(
        columns=columns,
        capture_id="cap_fail1",
        ingestion_run_id="run_x",
        source_system="eodhd",
        endpoint="/api/ticks",
        parser_version="TICK_PARSER_V1",
        request_parameters_json="{}",
        http_status=500,
        payload_uri=None,
        payload_sha256=sha256_hex(b"internal server error"),
        row_count_hint=None,
        observed_at=_OBS,
        committed_at=_OBS,
        created_at=_OBS,
        extra_cols={"request_symbol": "AAPL", "request_from_sec": 1, "request_to_sec": 2},
    )
    assert row["raw_payload_uri"] is None
    assert row["http_status"] == 500
    assert row["raw_payload_sha256"] == sha256_hex(b"internal server error")


def test_build_capture_row_extra_cols_not_in_schema_are_dropped():
    columns = ["capture_id", "endpoint"]
    row = build_capture_row(
        columns=columns,
        capture_id="cap_x",
        ingestion_run_id="run_x",
        source_system="eodhd",
        endpoint="/e",
        parser_version="V1",
        request_parameters_json="{}",
        http_status=200,
        payload_uri=None,
        payload_sha256="x",
        row_count_hint=None,
        observed_at=_OBS,
        committed_at=_OBS,
        created_at=_OBS,
        extra_cols={"not_a_real_column": "should be dropped"},
    )
    assert set(row.keys()) == {"capture_id", "endpoint"}


# ---------------------------------------------------------------------------
# retries produce distinct capture_ids (the actual DB round-trip for
# test_retry_is_new_capture lives in tests/deferred/test_capture.py; this
# checks the pure precondition it depends on without needing deltalake).
# ---------------------------------------------------------------------------

def test_successive_captures_get_distinct_capture_ids():
    a = new_id("cap")
    b = new_id("cap")
    assert a != b


# ---------------------------------------------------------------------------
# CaptureRecord is a plain dataclass-ish value
# ---------------------------------------------------------------------------

def test_capture_record_fields():
    rec = CaptureRecord(capture_id="cap_1", payload_path="/p", sha256="abc", row_count_hint=3)
    assert rec.capture_id == "cap_1"
    assert rec.payload_path == "/p"
    assert rec.sha256 == "abc"
    assert rec.row_count_hint == 3


# ---------------------------------------------------------------------------
# Transport / FakeTransport
# ---------------------------------------------------------------------------

def test_fake_transport_pops_responses_in_order_and_records_urls():
    ft = FakeTransport([(200, b"first"), (500, b"second")])
    assert ft.get("http://a") == (200, b"first")
    assert ft.get("http://b") == (500, b"second")
    assert ft.requested_urls == ["http://a", "http://b"]


def test_fake_transport_raises_when_exhausted():
    ft = FakeTransport([(200, b"only")])
    ft.get("http://a")
    try:
        ft.get("http://b")
        raise AssertionError("expected AssertionError when exhausted")
    except AssertionError as e:
        assert "no more canned responses" in str(e)


def test_transport_is_a_protocol_fake_transport_satisfies_it():
    # Structural check: FakeTransport implements the get(url, timeout) shape.
    ft = FakeTransport([(200, b"x")])
    assert callable(getattr(ft, "get"))
    assert isinstance(ft, Transport)


# ---------------------------------------------------------------------------
# UrlLibTransport retry-on-5xx logic (fake opener + injected sleeper, so no
# real network access or real sleeping happens in this test)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def getcode(self):
        return self.status

    def read(self):
        return self._body


def test_urllib_transport_returns_first_success_without_retry():
    calls = []

    def opener(url, timeout):
        calls.append(url)
        return _FakeResponse(200, b"ok")

    sleeps = []
    transport = UrlLibTransport(opener=opener, sleeper=sleeps.append)
    status, body = transport.get("http://x")
    assert (status, body) == (200, b"ok")
    assert len(calls) == 1
    assert sleeps == []


def test_urllib_transport_retries_on_5xx_then_succeeds():
    responses = [_FakeResponse(500, b"err1"), _FakeResponse(502, b"err2"), _FakeResponse(200, b"ok")]

    def opener(url, timeout):
        return responses.pop(0)

    sleeps = []
    transport = UrlLibTransport(opener=opener, sleeper=sleeps.append, max_attempts=3)
    status, body = transport.get("http://x")
    assert (status, body) == (200, b"ok")
    assert sleeps == [5, 10]  # 5*attempt for attempt=1, then attempt=2


def test_urllib_transport_gives_up_after_max_attempts():
    def opener(url, timeout):
        return _FakeResponse(500, b"still down")

    sleeps = []
    transport = UrlLibTransport(opener=opener, sleeper=sleeps.append, max_attempts=3)
    status, body = transport.get("http://x")
    assert (status, body) == (500, b"still down")
    assert sleeps == [5, 10]


def test_urllib_transport_does_not_retry_on_429():
    calls = []

    def opener(url, timeout):
        calls.append(url)
        return _FakeResponse(429, b"rate limited")

    sleeps = []
    transport = UrlLibTransport(opener=opener, sleeper=sleeps.append, max_attempts=3)
    status, body = transport.get("http://x")
    assert (status, body) == (429, b"rate limited")
    assert len(calls) == 1  # no retry loop entered for 429
    assert sleeps == []


def test_urllib_transport_handles_http_error_as_status():
    def opener(url, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

    # HTTPError.read() needs a file-like fp; patch it after construction.
    def opener2(url, timeout):
        exc = urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)
        exc.read = lambda: b"not found body"
        raise exc

    sleeps = []
    transport = UrlLibTransport(opener=opener2, sleeper=sleeps.append, max_attempts=3)
    status, body = transport.get("http://x")
    assert status == 404
    assert body == b"not found body"
    assert sleeps == []  # 404 is not >=500, no retry

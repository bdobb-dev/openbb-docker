"""Deferred tests for tick_vault.capture (task-1 brief, Step 1).

Requires deltalake, pyarrow, pytest - none of which are installable in the
authoring sandbox (network blocked). Placed under tests/deferred/ so the
sandbox mini-runner (tests/run_tests.py, which os.listdir()s tests/ non-
recursively for test_*.py) does not pick these up. Run with:

    pytest tick-vault/tests

once the dependencies are available. UNVERIFIED until then.

The pure parts of tick_vault.capture (payload path layout, gzip write +
immutability guard, sha256, resolve_capture_body, build_capture_row's
column-filling, and Transport/FakeTransport/UrlLibTransport) are already
covered by the runnable tests/test_capture_core.py; this file exercises
only the Delta-backed path (CaptureStore.record -> append_rows ->
tick_vault.schemas), against a real tmp_path Delta lake, per the task-1
brief's four key-case tests.
"""
import hashlib
from pathlib import Path

import pyarrow as pa
import pytest
from deltalake import DeltaTable

from tick_vault.capture import CaptureStore, FakeTransport
from tick_vault.schemas import create_all

import datetime as dt

OBS = dt.datetime(2026, 3, 5, 12, 0, 0, tzinfo=dt.timezone.utc)


def _read_delta(root, rel_path):
    dt_ = DeltaTable(str(Path(root) / rel_path))
    return dt_.to_pandas()


def test_success_capture_writes_payload_and_row(tmp_path):
    create_all(str(tmp_path))
    store = CaptureStore(str(tmp_path))
    payload = b'[{"ts": 1, "seq": 1}]'
    rec = store.record(
        table="bronze.eodhd_tick_capture",
        endpoint="/api/ticks",
        request_params={"s": "AAPL", "from": 1, "to": 2},
        http_status=200,
        payload_bytes=payload,
        error_body=None,
        observed_at=OBS,
        ingestion_run_id="run_x",
        parser_version="TICK_PARSER_V1",
        extra_cols={"request_symbol": "AAPL", "request_from_sec": 1, "request_to_sec": 2},
    )

    assert rec.sha256 == hashlib.sha256(payload).hexdigest()
    assert Path(rec.payload_path).exists()

    df = _read_delta(tmp_path, "bronze/eodhd_tick_capture")
    assert len(df) == 1
    row = df.iloc[0]
    assert row["raw_payload_sha256"] == rec.sha256
    assert row["http_status"] == 200
    assert row["capture_id"] == rec.capture_id
    assert row["request_symbol"] == "AAPL"
    assert row["request_from_sec"] == 1
    assert row["request_to_sec"] == 2
    assert row["raw_row_count"] == 1
    assert row["raw_payload_uri"] == rec.payload_path
    # committed_date is the date component of committed_at_ts (partition column)
    assert row["committed_date"] == row["committed_at_ts"].date()


def test_failure_capture_has_no_payload_but_row(tmp_path):
    create_all(str(tmp_path))
    store = CaptureStore(str(tmp_path))
    error_body = b"internal server error"
    rec = store.record(
        table="bronze.eodhd_tick_capture",
        endpoint="/api/ticks",
        request_params={"s": "AAPL", "from": 1, "to": 2},
        http_status=500,
        payload_bytes=None,
        error_body=error_body,
        observed_at=OBS,
        ingestion_run_id="run_x",
        parser_version="TICK_PARSER_V1",
        extra_cols={"request_symbol": "AAPL", "request_from_sec": 1, "request_to_sec": 2},
    )

    assert rec.payload_path is None
    assert rec.sha256 == hashlib.sha256(error_body).hexdigest()

    df = _read_delta(tmp_path, "bronze/eodhd_tick_capture")
    assert len(df) == 1
    row = df.iloc[0]
    assert row["http_status"] == 500
    assert row["raw_payload_uri"] is None
    assert row["raw_payload_sha256"] == rec.sha256


def test_retry_is_new_capture(tmp_path):
    create_all(str(tmp_path))
    store = CaptureStore(str(tmp_path))
    kwargs = dict(
        table="bronze.eodhd_tick_capture",
        endpoint="/api/ticks",
        request_params={"s": "AAPL", "from": 1, "to": 2},
        observed_at=OBS,
        ingestion_run_id="run_x",
        parser_version="TICK_PARSER_V1",
        extra_cols={"request_symbol": "AAPL", "request_from_sec": 1, "request_to_sec": 2},
    )
    rec1 = store.record(http_status=500, payload_bytes=None, error_body=b"err", **kwargs)
    rec2 = store.record(http_status=200, payload_bytes=b"[{}]", error_body=None, **kwargs)

    assert rec1.capture_id != rec2.capture_id

    df = _read_delta(tmp_path, "bronze/eodhd_tick_capture")
    assert len(df) == 2
    assert set(df["capture_id"]) == {rec1.capture_id, rec2.capture_id}


def test_payload_immutable(tmp_path):
    create_all(str(tmp_path))
    store = CaptureStore(str(tmp_path))
    kwargs = dict(
        table="bronze.eodhd_tick_capture",
        endpoint="/api/ticks",
        request_params={"s": "AAPL", "from": 1, "to": 2},
        http_status=200,
        error_body=None,
        observed_at=OBS,
        ingestion_run_id="run_x",
        parser_version="TICK_PARSER_V1",
        extra_cols={"request_symbol": "AAPL", "request_from_sec": 1, "request_to_sec": 2},
    )
    rec = store.record(payload_bytes=b"[{}]", **kwargs)

    # A record() writing directly to rec's already-used payload path must
    # never overwrite it - simulate that at the write_payload layer, which
    # is what record() itself calls internally.
    from tick_vault.capture import write_payload

    with pytest.raises(FileExistsError):
        write_payload(rec.payload_path, b"different bytes")

    # original payload untouched
    import gzip

    with gzip.open(rec.payload_path, "rb") as f:
        assert f.read() == b"[{}]"


def test_fetch_and_capture_success_returns_payload_and_record(tmp_path):
    create_all(str(tmp_path))
    store = CaptureStore(str(tmp_path))
    transport = FakeTransport([(200, b'[{"ts": 1}]')])

    payload, rec = store.fetch_and_capture(
        transport,
        url="https://eodhd.example/api/ticks?s=AAPL",
        table="bronze.eodhd_tick_capture",
        endpoint="/api/ticks",
        request_params={"s": "AAPL", "from": 1, "to": 2},
        observed_at=OBS,
        ingestion_run_id="run_x",
        parser_version="TICK_PARSER_V1",
        extra_cols={"request_symbol": "AAPL", "request_from_sec": 1, "request_to_sec": 2},
    )

    assert payload == b'[{"ts": 1}]'
    assert transport.requested_urls == ["https://eodhd.example/api/ticks?s=AAPL"]
    df = _read_delta(tmp_path, "bronze/eodhd_tick_capture")
    assert df.iloc[0]["http_status"] == 200
    assert df.iloc[0]["capture_id"] == rec.capture_id


def test_fetch_and_capture_failure_returns_none_payload(tmp_path):
    create_all(str(tmp_path))
    store = CaptureStore(str(tmp_path))
    transport = FakeTransport([(500, b"boom")])

    payload, rec = store.fetch_and_capture(
        transport,
        url="https://eodhd.example/api/ticks?s=AAPL",
        table="bronze.eodhd_tick_capture",
        endpoint="/api/ticks",
        request_params={"s": "AAPL", "from": 1, "to": 2},
        observed_at=OBS,
        ingestion_run_id="run_x",
        parser_version="TICK_PARSER_V1",
        extra_cols={"request_symbol": "AAPL", "request_from_sec": 1, "request_to_sec": 2},
    )

    assert payload is None
    assert rec.payload_path is None
    df = _read_delta(tmp_path, "bronze/eodhd_tick_capture")
    assert df.iloc[0]["http_status"] == 500

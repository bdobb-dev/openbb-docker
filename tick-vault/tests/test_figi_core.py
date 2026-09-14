"""Runnable (pyarrow/deltalake/pytest-free) tests for `tick_vault.figi`
(task-6 brief).

`tick_vault.figi` imports bare (no pyarrow/deltalake at module scope -
those are lazy-imported inside `_default_*` seams, none of which this
file exercises), so it's importable/runnable here even though pyarrow/
deltalake are not installed in this sandbox.

Covers every pure-compute path named in the task brief: three-level
silver rows from one OpenFIGI match (with `decimal.Decimal` confidence,
never a float); CUSIP preferred over a ticker fallback when building a
mapping job; a no-match response entry keeping its queue row visible as
`NO_MATCH` (never deleted, never silently re-queued as `NEEDS_FIGI`);
batch capping at `BATCH_LIMIT` (100) jobs per OpenFIGI request; the
keyless inter-batch sleeper being called with the `60/25`-derived
constant between two batches; and the API key never leaking into the
captured request-params dict (it only ever appears in `build_headers`'
output).

The real Delta round-trip (`FigiWorker` driven by its *default* Delta
reader/writer seams, a bronze capture row actually landing, silver rows
actually landing, a real overwrite of `ops.openfigi_resolution_queue`) is
covered separately by `tests/deferred/test_figi.py` (pytest, needs
deltalake/pyarrow/a real FakeTransport-backed `CaptureStore`).
"""
import datetime as dt
import decimal
import json
from pathlib import Path

import pandas as pd

from tick_vault.capture import CaptureStore, FakeTransport
from tick_vault.figi import (
    BATCH_LIMIT,
    CONFIDENCE_MATCH,
    FIGI_LEVEL_COMPOSITE,
    FIGI_LEVEL_SHARE_CLASS,
    FIGI_LEVEL_VENUE,
    QUEUE_STATUS_NEEDS_FIGI,
    QUEUE_STATUS_NO_MATCH,
    QUEUE_STATUS_RESOLVED,
    SLEEP_SECONDS_KEYED,
    SLEEP_SECONDS_KEYLESS,
    FigiWorker,
    _ASSIGNMENT_LOOKUP_COLUMNS,
    _QUEUE_COLUMNS,
    build_figi_rows,
    build_headers,
    build_mapping_job,
    build_queue_update,
    build_request_params,
    chunk,
    matched_data,
    sleep_seconds_for,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/openfigi_response.json").read_text()
)
OBS = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


def _queue_row(**overrides):
    row = {c: None for c in _QUEUE_COLUMNS}
    row.update(
        queue_id="wrk_q1",
        status=QUEUE_STATUS_NEEDS_FIGI,
        attempts=0,
        priority=5,
        created_at_ts=OBS,
        updated_at_ts=OBS,
    )
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# job building
# ---------------------------------------------------------------------------

def test_build_mapping_job_prefers_queue_row_cusip():
    row = _queue_row(id_namespace="CUSIP", id_value="037833100")
    job = build_mapping_job(row)
    assert job == {"idType": "ID_CUSIP", "idValue": "037833100"}


def test_build_mapping_job_prefers_cusip_over_ticker_via_assignments():
    row = _queue_row(instrument_id="ins_1", id_namespace="EODHD_SYMBOL", id_value="AAPL", exchange_code="US")
    assignments = pd.DataFrame([
        {"instrument_id": "ins_1", "id_namespace": "CUSIP", "id_value": "037833100", "system_to_ts": None},
        {"instrument_id": "ins_1", "id_namespace": "ISIN", "id_value": "US0378331005", "system_to_ts": None},
    ], columns=_ASSIGNMENT_LOOKUP_COLUMNS)
    job = build_mapping_job(row, assignments)
    assert job == {"idType": "ID_CUSIP", "idValue": "037833100"}


def test_build_mapping_job_falls_back_to_isin_when_no_cusip():
    row = _queue_row(instrument_id="ins_1", id_namespace="EODHD_SYMBOL", id_value="AAPL")
    assignments = pd.DataFrame([
        {"instrument_id": "ins_1", "id_namespace": "ISIN", "id_value": "US0378331005", "system_to_ts": None},
    ], columns=_ASSIGNMENT_LOOKUP_COLUMNS)
    job = build_mapping_job(row, assignments)
    assert job == {"idType": "ID_ISIN", "idValue": "US0378331005"}


def test_build_mapping_job_falls_back_to_ticker_and_exchange():
    row = _queue_row(instrument_id="ins_1", id_namespace="EODHD_SYMBOL", id_value="AAPL", exchange_code="US")
    job = build_mapping_job(row, assignments_df=None)
    assert job == {"idType": "TICKER", "idValue": "AAPL", "exchCode": "US"}


def test_build_mapping_job_ignores_closed_assignments():
    row = _queue_row(instrument_id="ins_1", id_namespace="EODHD_SYMBOL", id_value="AAPL")
    assignments = pd.DataFrame([
        {"instrument_id": "ins_1", "id_namespace": "CUSIP", "id_value": "OLD", "system_to_ts": OBS},
    ], columns=_ASSIGNMENT_LOOKUP_COLUMNS)
    job = build_mapping_job(row, assignments)
    # CUSIP row is system-closed, so it's ignored -> falls back to ticker.
    assert job == {"idType": "TICKER", "idValue": "AAPL"}


def test_build_mapping_job_none_when_nothing_usable():
    row = _queue_row(id_namespace="EODHD_SYMBOL", id_value=None)
    assert build_mapping_job(row) is None


# ---------------------------------------------------------------------------
# request params / headers (redaction)
# ---------------------------------------------------------------------------

def test_build_request_params_holds_only_jobs():
    jobs = [{"idType": "ID_CUSIP", "idValue": "037833100"}]
    params = build_request_params(jobs)
    assert params == {"jobs": jobs}


def test_api_key_never_in_request_params_but_is_in_headers():
    api_key = "sekret-key-value"
    jobs = [{"idType": "TICKER", "idValue": "AAPL"}]
    params = build_request_params(jobs)
    headers = build_headers(api_key)

    assert api_key not in json.dumps(params)
    assert headers["X-OPENFIGI-APIKEY"] == api_key


def test_build_headers_omits_apikey_header_when_no_key():
    headers = build_headers(None)
    assert "X-OPENFIGI-APIKEY" not in headers
    assert headers["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# response -> silver rows / queue updates
# ---------------------------------------------------------------------------

def test_matched_data_returns_first_data_element():
    assert matched_data(FIXTURE[0]) == FIXTURE[0]["data"][0]


def test_matched_data_returns_none_for_error_entry():
    assert matched_data(FIXTURE[1]) is None


def test_build_figi_rows_three_levels_with_decimal_confidence():
    row = _queue_row(instrument_id="ins_1", listing_id="lst_1")
    figi_rows = build_figi_rows(matched_data(FIXTURE[0]), row, "cap_1", OBS)

    assert len(figi_rows) == 3
    levels = [r["figi_level"] for r in figi_rows]
    assert levels == [FIGI_LEVEL_VENUE, FIGI_LEVEL_COMPOSITE, FIGI_LEVEL_SHARE_CLASS]
    assert figi_rows[0]["figi"] == "BBG000B9XRY4"
    assert figi_rows[1]["figi"] == "BBG000B9XVV8"
    assert figi_rows[2]["figi"] == "BBG001S5N8V8"
    for r in figi_rows:
        assert r["instrument_id"] == "ins_1"
        assert r["listing_id"] == "lst_1"
        assert r["source_system"] == "OPENFIGI"
        assert r["source_capture_id"] == "cap_1"
        assert r["match_status"] == "MATCH"
        assert isinstance(r["confidence"], decimal.Decimal)
        assert r["confidence"] == CONFIDENCE_MATCH
        assert r["observed_at_ts"] == OBS
        assert r["available_at_ts"] == OBS
        assert r["system_from_ts"] == OBS
        assert r["system_to_ts"] is None
        assert r["figi_assignment_version_id"]


def test_build_figi_rows_empty_for_no_match():
    row = _queue_row()
    assert build_figi_rows({}, row, "cap_1", OBS) == []


def test_build_queue_update_resolved_points_at_venue_row():
    row = _queue_row()
    figi_rows = build_figi_rows(matched_data(FIXTURE[0]), row, "cap_1", OBS)
    updated = build_queue_update(row, figi_rows, OBS)

    assert updated["status"] == QUEUE_STATUS_RESOLVED
    assert updated["resolved_figi_assignment_version_id"] == figi_rows[0]["figi_assignment_version_id"]
    assert updated["attempts"] == 1
    assert updated["last_error"] is None
    assert updated["updated_at_ts"] == OBS


def test_build_queue_update_no_match_keeps_row_visible():
    row = _queue_row(attempts=2)
    updated = build_queue_update(row, [], OBS, error_message="No identifier found.")

    assert updated["status"] == QUEUE_STATUS_NO_MATCH
    assert updated["queue_id"] == row["queue_id"]
    assert updated["attempts"] == 3
    assert updated["last_error"] == "No identifier found."
    assert updated["resolved_figi_assignment_version_id"] is None


# ---------------------------------------------------------------------------
# batching / rate limiting
# ---------------------------------------------------------------------------

def test_chunk_caps_at_batch_limit():
    items = list(range(250))
    chunks = chunk(items, BATCH_LIMIT)
    assert [len(c) for c in chunks] == [100, 100, 50]
    assert sum(chunks, []) == items


def test_sleep_seconds_for_keyless_and_keyed():
    assert sleep_seconds_for(None) == 60 / 25
    assert sleep_seconds_for("") == 60 / 25
    assert sleep_seconds_for("a-key") == 60 / 250
    assert SLEEP_SECONDS_KEYLESS == 60 / 25
    assert SLEEP_SECONDS_KEYED == 60 / 250


# ---------------------------------------------------------------------------
# FigiWorker.run_batch, fully in-memory (fake queue/assignments/writer/
# sleeper + FakeTransport + CaptureStore over a tmp dir for the payload
# store side, but no Delta/pyarrow anywhere)
# ---------------------------------------------------------------------------

class _FakeCaptureStore:
    """Stands in for `tick_vault.capture.CaptureStore`: skips the real
    `record()` (which needs `tick_vault.schemas`/pyarrow), but exercises
    the exact same `transport.post` -> success/failure split
    `fetch_and_capture_post` performs, and records every call it made so
    tests can assert on `request_params`/observed_at."""

    def __init__(self):
        self.calls = []
        self._next_id = 0

    def fetch_and_capture_post(self, transport, *, url, body, headers, table, endpoint,
                                request_params, observed_at, ingestion_run_id, parser_version,
                                extra_cols=None, timeout=30, source_system="eodhd"):
        status, resp_body = transport.post(url, body, headers, timeout=timeout)
        is_success = 200 <= status < 300
        payload_bytes = resp_body if is_success else None
        self._next_id += 1
        capture_id = f"cap_{self._next_id}"
        self.calls.append({
            "url": url, "table": table, "request_params": request_params,
            "observed_at": observed_at, "capture_id": capture_id,
        })

        class _Rec:
            pass

        rec = _Rec()
        rec.capture_id = capture_id
        return payload_bytes, rec


class _FakeLake:
    def __init__(self, queue_df, assignments_df=None):
        self.queue_df = queue_df
        self.assignments_df = assignments_df if assignments_df is not None else pd.DataFrame(columns=_ASSIGNMENT_LOOKUP_COLUMNS)
        self.silver_writes = []
        self.queue_writes = []

    def queue_reader(self, root):
        return self.queue_df

    def assignments_reader(self, root):
        return self.assignments_df

    def writer(self, root, table, df):
        self.silver_writes.append((table, df))

    def queue_writer(self, root, table, df):
        self.queue_writes.append((table, df.copy()))
        self.queue_df = df


def _make_queue_df(rows):
    return pd.DataFrame(rows, columns=_QUEUE_COLUMNS)


def test_run_batch_matched_and_no_match_end_to_end():
    rows = [
        _queue_row(queue_id="wrk_q1", instrument_id="ins_1", listing_id="lst_1",
                   id_namespace="EODHD_SYMBOL", id_value="AAPL"),
        _queue_row(queue_id="wrk_q2", instrument_id="ins_2", listing_id="lst_2",
                   id_namespace="EODHD_SYMBOL", id_value="ZZZZ"),
    ]
    queue_df = _make_queue_df(rows)
    lake = _FakeLake(queue_df)
    transport = FakeTransport([(200, json.dumps(FIXTURE).encode())])
    capture_store = _FakeCaptureStore()
    sleeps = []

    worker = FigiWorker(
        transport, capture_store, "unused-root", api_key=None,
        assignments_reader=lake.assignments_reader,
        queue_reader=lake.queue_reader,
        queue_writer=lake.queue_writer,
        writer=lake.writer,
        sleeper=sleeps.append,
        now=lambda: OBS,
    )

    summary = worker.run_batch(limit=100)

    assert summary == {"considered": 2, "jobs_built": 2, "matched": 1, "no_match": 1, "batches": 1}
    assert sleeps == []  # only one batch -> no inter-batch sleep

    assert len(lake.silver_writes) == 1
    silver_table, silver_df = lake.silver_writes[0]
    assert silver_table == "silver.figi_assignment_version"
    assert len(silver_df) == 3

    assert len(lake.queue_writes) == 1
    _, updated_queue = lake.queue_writes[0]
    by_id = {r["queue_id"]: r for r in updated_queue.to_dict("records")}
    assert by_id["wrk_q1"]["status"] == QUEUE_STATUS_RESOLVED
    assert by_id["wrk_q1"]["resolved_figi_assignment_version_id"]
    assert by_id["wrk_q2"]["status"] == QUEUE_STATUS_NO_MATCH
    assert by_id["wrk_q2"]["last_error"] == "No identifier found."

    assert len(capture_store.calls) == 1
    call = capture_store.calls[0]
    assert call["table"] == "bronze.openfigi_mapping_capture"
    assert call["request_params"] == {"jobs": [
        {"idType": "TICKER", "idValue": "AAPL"},
        {"idType": "TICKER", "idValue": "ZZZZ"},
    ]}


def test_run_batch_sleeps_between_batches_keyless():
    rows = [
        _queue_row(queue_id=f"wrk_q{i}", instrument_id=f"ins_{i}",
                   id_namespace="EODHD_SYMBOL", id_value=f"SYM{i}")
        for i in range(BATCH_LIMIT + 5)
    ]
    queue_df = _make_queue_df(rows)
    lake = _FakeLake(queue_df)
    no_match_response = json.dumps([{"error": "no match"}] * BATCH_LIMIT).encode()
    small_batch_response = json.dumps([{"error": "no match"}] * 5).encode()
    transport = FakeTransport([(200, no_match_response), (200, small_batch_response)])
    capture_store = _FakeCaptureStore()
    sleeps = []

    worker = FigiWorker(
        transport, capture_store, "unused-root", api_key=None,
        assignments_reader=lake.assignments_reader,
        queue_reader=lake.queue_reader,
        queue_writer=lake.queue_writer,
        writer=lake.writer,
        sleeper=sleeps.append,
        now=lambda: OBS,
    )

    summary = worker.run_batch(limit=1000)

    assert summary["batches"] == 2
    assert summary["considered"] == BATCH_LIMIT + 5
    # Exactly one inter-batch sleep (between batch 1 and 2, none after
    # the last batch), at the keyless 60/25-derived constant.
    assert sleeps == [SLEEP_SECONDS_KEYLESS]


def test_run_batch_leaves_unbuildable_rows_untouched():
    rows = [_queue_row(queue_id="wrk_q1", id_namespace="EODHD_SYMBOL", id_value=None)]
    queue_df = _make_queue_df(rows)
    lake = _FakeLake(queue_df)
    transport = FakeTransport([])
    capture_store = _FakeCaptureStore()

    worker = FigiWorker(
        transport, capture_store, "unused-root",
        assignments_reader=lake.assignments_reader,
        queue_reader=lake.queue_reader,
        queue_writer=lake.queue_writer,
        writer=lake.writer,
        sleeper=lambda s: None,
        now=lambda: OBS,
    )

    summary = worker.run_batch(limit=100)

    assert summary == {"considered": 1, "jobs_built": 0, "matched": 0, "no_match": 0, "batches": 0}
    assert lake.silver_writes == []
    assert lake.queue_writes == []  # nothing touched -> no overwrite at all


def test_run_batch_no_needs_figi_rows_is_a_noop():
    rows = [_queue_row(queue_id="wrk_q1", status=QUEUE_STATUS_RESOLVED)]
    queue_df = _make_queue_df(rows)
    lake = _FakeLake(queue_df)
    worker = FigiWorker(
        FakeTransport([]), _FakeCaptureStore(), "unused-root",
        assignments_reader=lake.assignments_reader,
        queue_reader=lake.queue_reader,
        queue_writer=lake.queue_writer,
        writer=lake.writer,
        sleeper=lambda s: None,
        now=lambda: OBS,
    )
    summary = worker.run_batch()
    assert summary == {"considered": 0, "jobs_built": 0, "matched": 0, "no_match": 0, "batches": 0}

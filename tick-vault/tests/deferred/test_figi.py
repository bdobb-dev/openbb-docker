"""Deferred tests for `tick_vault.figi` (task-6 brief).

Requires deltalake, pyarrow, pytest - none of which are installable in
the authoring sandbox (network blocked). Placed under tests/deferred/ so
the sandbox mini-runner (tests/run_tests.py, which os.listdir()s tests/
non-recursively for test_*.py) does not pick these up. Run with:

    pytest tick-vault/tests

once the dependencies are available. UNVERIFIED until then.

The pure resolution/mapping-job/response-parsing logic is already covered
by the runnable `tests/test_figi_core.py` (a `FigiWorker` driven entirely
by fake in-memory reader/writer/sleeper seams); this file exercises only
the *default* Delta-backed seams: a real tmp_path lake seeded with one
`ops.openfigi_resolution_queue` row, `FigiWorker(...)` constructed with
NO injected reader/writer/queue_reader/queue_writer (so it uses the
lazy-imported `deltalake`-backed defaults), a `FakeTransport` standing in
only for the network call, and a real `CaptureStore` so
`bronze.openfigi_mapping_capture` actually gets a row. After
`run_batch()`, every table is re-read straight off disk via
`DeltaTable(...).to_pandas()` to confirm the rows genuinely landed (not
just held in the worker's return value).

CI gotchas exercised here (per earlier tasks' lessons):
  - `confidence` round-trips as `decimal.Decimal`, not `float`.
  - `ops.openfigi_resolution_queue` is overwritten wholesale (see
    `tick_vault.figi`'s module docstring's "Queue mutation" section) -
    this test re-reads the WHOLE table after `run_batch()` and confirms
    the touched row's new state, not just that *a* write happened.
  - the bronze capture row's `request_parameters_json` never contains the
    API key, even when one was supplied to `FigiWorker`.
"""
import datetime as dt
import decimal
import json

import pandas as pd
import pytest
from deltalake import DeltaTable

from tick_vault.capture import CaptureStore, FakeTransport, append_rows
from tick_vault.figi import (
    FIGI_LEVEL_COMPOSITE,
    FIGI_LEVEL_SHARE_CLASS,
    FIGI_LEVEL_VENUE,
    QUEUE_STATUS_NEEDS_FIGI,
    QUEUE_STATUS_NO_MATCH,
    QUEUE_STATUS_RESOLVED,
    CONFIDENCE_MATCH,
    FigiWorker,
    _QUEUE_COLUMNS,
)
from tick_vault.schemas import create_all

OBS = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


def _read(root, table):
    layer, name = table.split(".", 1)
    return DeltaTable(f"{root}/{layer}/{name}").to_pandas()


def _seed_queue_row(root, **overrides):
    row = {c: None for c in _QUEUE_COLUMNS}
    row.update(
        queue_id="wrk_q1",
        id_namespace="EODHD_SYMBOL",
        id_value="AAPL",
        status=QUEUE_STATUS_NEEDS_FIGI,
        attempts=0,
        priority=5,
        created_at_ts=OBS,
        updated_at_ts=OBS,
    )
    row.update(overrides)
    append_rows(root, "ops.openfigi_resolution_queue", pd.DataFrame([row], columns=_QUEUE_COLUMNS))


def _fixture_response():
    return [
        {
            "data": [
                {
                    "figi": "BBG000B9XRY4",
                    "compositeFIGI": "BBG000B9XVV8",
                    "shareClassFIGI": "BBG001S5N8V8",
                }
            ]
        }
    ]


def test_run_batch_writes_bronze_capture_silver_rows_and_resolves_queue(tmp_path):
    root = str(tmp_path)
    create_all(root)
    _seed_queue_row(root, instrument_id="ins_1", listing_id="lst_1")

    transport = FakeTransport([(200, json.dumps(_fixture_response()).encode())])
    capture_store = CaptureStore(root)

    worker = FigiWorker(
        transport, capture_store, root, api_key=None,
        sleeper=lambda seconds: None,
        now=lambda: OBS,
    )
    summary = worker.run_batch(limit=100)

    assert summary["matched"] == 1
    assert summary["no_match"] == 0

    bronze = _read(root, "bronze.openfigi_mapping_capture")
    assert len(bronze) == 1
    assert bronze.iloc[0]["source_system"] == "OPENFIGI"
    assert bronze.iloc[0]["endpoint"] == "/v3/mapping"
    params = json.loads(bronze.iloc[0]["request_parameters_json"])
    assert params == {"jobs": [{"idType": "TICKER", "idValue": "AAPL"}]}
    # request_id_type/request_id_value are NOT NULL on this table but
    # this worker's captures are batch-shaped, not single-identifier -
    # see tick_vault.figi.build_batch_descriptor's docstring for why
    # these are the batch-level "BATCH"/"jobs=<n>;first=<idType>:
    # <idValue>" descriptor rather than a per-job identifier.
    assert bronze.iloc[0]["request_id_type"] == "BATCH"
    assert bronze.iloc[0]["request_id_value"] == "jobs=1;first=TICKER:AAPL"
    assert pd.isna(bronze.iloc[0]["request_exchange_code"])

    silver = _read(root, "silver.figi_assignment_version")
    assert len(silver) == 3
    levels = sorted(silver["figi_level"].tolist())
    assert levels == sorted([FIGI_LEVEL_VENUE, FIGI_LEVEL_COMPOSITE, FIGI_LEVEL_SHARE_CLASS])
    for confidence in silver["confidence"]:
        assert isinstance(confidence, decimal.Decimal)
        assert confidence == CONFIDENCE_MATCH
    assert (silver["instrument_id"] == "ins_1").all()
    assert (silver["listing_id"] == "lst_1").all()
    assert (silver["system_to_ts"].isna()).all()

    queue = _read(root, "ops.openfigi_resolution_queue")
    assert len(queue) == 1
    row = queue.iloc[0]
    assert row["status"] == QUEUE_STATUS_RESOLVED
    assert row["resolved_figi_assignment_version_id"] in set(silver["figi_assignment_version_id"])
    assert row["attempts"] == 1


def test_run_batch_no_match_keeps_queue_row_visible_and_writes_no_silver_rows(tmp_path):
    root = str(tmp_path)
    create_all(root)
    _seed_queue_row(root, instrument_id="ins_2", listing_id="lst_2", id_value="ZZZZ")

    transport = FakeTransport([(200, json.dumps([{"error": "No identifier found."}]).encode())])
    capture_store = CaptureStore(root)

    worker = FigiWorker(
        transport, capture_store, root, api_key=None,
        sleeper=lambda seconds: None,
        now=lambda: OBS,
    )
    summary = worker.run_batch(limit=100)

    assert summary["matched"] == 0
    assert summary["no_match"] == 1

    silver = _read(root, "silver.figi_assignment_version")
    assert len(silver) == 0

    queue = _read(root, "ops.openfigi_resolution_queue")
    assert len(queue) == 1
    row = queue.iloc[0]
    assert row["status"] == QUEUE_STATUS_NO_MATCH
    assert row["last_error"] == "No identifier found."
    assert pd.isna(row["resolved_figi_assignment_version_id"])


def test_api_key_never_lands_in_captured_request_params(tmp_path):
    root = str(tmp_path)
    create_all(root)
    _seed_queue_row(root, instrument_id="ins_3", listing_id="lst_3", id_value="MSFT")

    api_key = "super-secret-key"
    transport = FakeTransport([(200, json.dumps(_fixture_response()).encode())])
    capture_store = CaptureStore(root)

    worker = FigiWorker(
        transport, capture_store, root, api_key=api_key,
        sleeper=lambda seconds: None,
        now=lambda: OBS,
    )
    worker.run_batch(limit=100)

    # The key must have gone out over the wire in headers...
    assert transport.posted_requests[0]["headers"]["X-OPENFIGI-APIKEY"] == api_key
    # ...but never into the captured/persisted request params.
    bronze = _read(root, "bronze.openfigi_mapping_capture")
    assert api_key not in bronze.iloc[0]["request_parameters_json"]

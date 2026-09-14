"""Deferred tests for the reconciliation gate's reference-side capture wiring
(task-9 brief, Step 1). Thin by design: `tick_vault.reconcile` itself is pure
pandas and fully covered by the runnable `tests/test_reconcile_core.py`. The
only part of task 9 that needs a real Delta lake is the two new
`ReferenceClient` getters it depends on (`get_eod`/`get_intraday`) - this
file just checks their capture rows land in the right bronze tables, the
same pattern as `tests/deferred/test_eodhd_reference.py`'s other getters.

Requires deltalake, pyarrow, pytest - none of which are installable in the
authoring sandbox (network blocked). Placed under tests/deferred/ so the
sandbox mini-runner (tests/run_tests.py, which os.listdir()s tests/ non-
recursively for test_*.py) does not pick this up. Run with:

    pytest tick-vault/tests

once the dependencies are available. UNVERIFIED until then.
"""
import datetime as dt
from pathlib import Path

import pytest
from deltalake import DeltaTable

from tick_vault.capture import CaptureStore, FakeTransport
from tick_vault.eodhd_reference import ReferenceClient
from tick_vault.schemas import create_all

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "eodhd_ref"
OBS = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.timezone.utc)


def _load_bytes(name):
    return (_FIXTURE_DIR / name).read_bytes()


def _read_delta(root, rel_path):
    return DeltaTable(str(Path(root) / rel_path)).to_pandas()


def _client(tmp_path, responses):
    create_all(str(tmp_path))
    transport = FakeTransport(responses)
    store = CaptureStore(str(tmp_path))
    client = ReferenceClient(transport, store, api_key="SECRET_KEY_123", ingestion_run_id="run_x")
    return client, transport, store


def test_get_eod_capture_lands_in_bronze_eod_capture_table(tmp_path):
    client, _, _ = _client(tmp_path, [(200, _load_bytes("eod_slice.json"))])
    df, record = client.get_eod("AAPL.US", "2026-07-01", "2026-07-02", observed_at=OBS)

    assert len(df) == 2
    bronze = _read_delta(tmp_path, "bronze/eodhd_eod_capture")
    assert len(bronze) == 1
    row = bronze.iloc[0]
    assert row["capture_id"] == record.capture_id
    assert row["request_symbol"] == "AAPL.US"


def test_get_intraday_capture_lands_in_bronze_intraday_capture_table(tmp_path):
    client, _, _ = _client(tmp_path, [(200, _load_bytes("intraday_slice.json"))])
    df, record = client.get_intraday("AAPL.US", "1m", 1751370600, 1751370660, observed_at=OBS)

    assert len(df) == 2
    bronze = _read_delta(tmp_path, "bronze/eodhd_intraday_capture")
    assert len(bronze) == 1
    row = bronze.iloc[0]
    assert row["capture_id"] == record.capture_id
    assert row["interval"] == "1m"

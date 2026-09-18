"""Deferred tests for `tick_vault.settle` (task-8 brief).

Requires deltalake, pyarrow, pytest - none installable in the authoring
sandbox (network blocked). Placed under tests/deferred/ so the sandbox
mini-runner (tests/run_tests.py) does not pick these up. Run with:

    pytest tick-vault/tests

once dependencies are available. UNVERIFIED until then.

The pure/seam-injected core (`settle_pass`/`daily_pass` against fakes,
including the REVISED/CANCELLED/LATE_ADD diff shape and the NO_CHANGES/
FAILED short-circuits) is already covered by the runnable `tests/
test_settle_core.py`; this file is a thin end-to-end smoke test on a real
tmp_path Delta lake: an initial week is written for real via `tick_vault.
tick_writer.write_tick_versions`, then `settle_pass` re-fetches a modified
payload through a real `CaptureStore` + `FakeTransport` and appends the
delta - re-reading silver afterward proves rev-2/tombstone/late-add rows
landed AND the original rows are still there (append-only, never
rewritten).

`_existing_latest_reader` below is a deliberately simple pandas stand-in
for the real seam (`settle_pass`'s `existing_latest_reader(root, item)`):
production wiring is a DuckDB `latest_ticks()` query (`tick_vault.
temporal`) filtered to the item's week - Plan-3's job, not this task's.
Here it just reads the whole `silver.us_trade_tick_version` Delta table
back with `DeltaTable.to_pandas()` and replicates "latest, non-cancelled"
in pandas: per `logical_tick_id`, keep the highest-`revision_number` row,
then drop it entirely if that highest revision is itself a cancellation
tombstone.
"""
import datetime as dt
import json

import pandas as pd
import pytest
from deltalake import DeltaTable

from tick_vault.capture import CaptureStore, FakeTransport
from tick_vault.engine import Budget, SpanMemory
from tick_vault.schemas import create_all
from tick_vault.settle import STATUS_SETTLED, settle_pass
from tick_vault.tick_parser import parse_tick_payload
from tick_vault.tick_writer import write_tick_versions

UTC = dt.timezone.utc
SEED_OBS = dt.datetime(2024, 1, 8, tzinfo=UTC)
SETTLE_NOW = dt.datetime(2024, 1, 10, tzinfo=UTC)

FROM_SEC = 1704067200  # 2024-01-01T00:00:00Z (Monday)
TO_SEC = FROM_SEC + 86399  # one day, fits in a single default window

PAYLOAD = [
    {"ts": FROM_SEC * 1000 + 100, "price": 150.25, "shares": 100, "seq": 1001,
     "sl": "@   ", "mkt": "Q", "sub_mkt": "", "ex": "US"},
    {"ts": FROM_SEC * 1000 + 200, "price": 150.26, "shares": 200, "seq": 1002,
     "sl": "@  I", "mkt": "N", "sub_mkt": "", "ex": "US"},
    {"ts": FROM_SEC * 1000 + 300, "price": 150.10, "shares": 50, "seq": 1003,
     "sl": "@ Z ", "mkt": "D", "sub_mkt": "T", "ex": "US"},
]

INCOMING_PAYLOAD = [
    {"ts": FROM_SEC * 1000 + 100, "price": 150.25, "shares": 100, "seq": 1001,
     "sl": "@   ", "mkt": "Q", "sub_mkt": "", "ex": "US"},
    # seq 1002 price revised (150.26 -> 150.27)
    {"ts": FROM_SEC * 1000 + 200, "price": 150.27, "shares": 200, "seq": 1002,
     "sl": "@  I", "mkt": "N", "sub_mkt": "", "ex": "US"},
    # seq 1003 dropped (revealed as cancelled)
    # seq 1004 appended (late add)
    {"ts": FROM_SEC * 1000 + 400, "price": 151.00, "shares": 10, "seq": 1004,
     "sl": "@   ", "mkt": "Q", "sub_mkt": "", "ex": "US"},
]

ITEM = {
    "listing_id": "lst_settle_e2e", "instrument_id": "ins_settle_e2e",
    "vendor_symbol_at_date": "SETTLE.US",
    "request_from_sec": FROM_SEC, "request_to_sec": TO_SEC,
}


def _read(root, table):
    layer, name = table.split(".", 1)
    return DeltaTable(f"{root}/{layer}/{name}").to_pandas()


def _existing_latest_reader(root, item):
    silver = _read(root, "silver.us_trade_tick_version")
    silver = silver[silver["listing_id"] == item["listing_id"]]
    if silver.empty:
        return silver
    idx = silver.groupby("logical_tick_id")["revision_number"].idxmax()
    latest = silver.loc[idx]
    latest = latest[~latest["is_cancelled"]]
    return latest.reset_index(drop=True)


def _seed_initial_week(root):
    parsed = parse_tick_payload(
        PAYLOAD, capture_id="cap_seed", listing_id=ITEM["listing_id"],
        instrument_id=ITEM["instrument_id"], observed_at=SEED_OBS,
    )
    write_tick_versions(
        root, parsed,
        vendor_request_symbol=ITEM["vendor_symbol_at_date"],
        committed_at=SEED_OBS,
    )
    return parsed


def test_settle_pass_end_to_end_lands_revision_tombstone_and_late_add(tmp_path):
    root = str(tmp_path)
    create_all(root)
    seeded = _seed_initial_week(root)

    transport = FakeTransport([(200, json.dumps(INCOMING_PAYLOAD).encode("utf-8"))])
    store = CaptureStore(root)
    span_memory = SpanMemory()
    budget = Budget(sleeper=lambda seconds: None)

    result = settle_pass(
        transport, store, root, ITEM,
        existing_latest_reader=_existing_latest_reader,
        span_memory=span_memory, budget=budget, now=SETTLE_NOW,
    )

    assert result.status == STATUS_SETTLED
    assert result.revisions == 1
    assert result.cancellations == 1
    assert result.late_adds == 1
    assert result.new_versions == 3

    silver = _read(root, "silver.us_trade_tick_version")
    silver = silver[silver["listing_id"] == ITEM["listing_id"]]

    # Append-only: all 3 originally-seeded rows are still present, byte-
    # for-byte identical tick_version_ids.
    original_ids = set(seeded["tick_version_id"])
    assert original_ids.issubset(set(silver["tick_version_id"]))
    assert len(silver) == len(seeded) + 3  # 3 new rows appended, nothing removed

    def _by_seq(seq, revision=None):
        rows = silver[silver["logical_tick_id"].str.endswith(f"|{seq}")]
        if revision is not None:
            rows = rows[rows["revision_number"] == revision]
        return rows

    rev2 = _by_seq(1002, revision=2)
    assert len(rev2) == 1
    assert rev2.iloc[0]["is_correction"]
    assert float(rev2.iloc[0]["price"]) == pytest.approx(150.27)
    assert rev2.iloc[0]["available_at_ts"].to_pydatetime().replace(tzinfo=UTC) == SETTLE_NOW

    tomb = _by_seq(1003, revision=2)
    assert len(tomb) == 1
    assert tomb.iloc[0]["is_cancelled"]

    late = _by_seq(1004, revision=1)
    assert len(late) == 1
    assert late.iloc[0]["is_late_add"]

    # Unchanged seq 1001 has no second revision.
    assert len(_by_seq(1001)) == 1

    events = _read(root, "silver.tick_correction_event")
    assert len(events) == 3
    assert sorted(events["correction_kind"]) == ["CANCELLED", "LATE_ADD", "REVISED"]
    assert set(events["revealing_capture_id"]) == {rev2.iloc[0]["source_capture_id"]}

"""Deferred tests for `tick_vault.engine` (task-7 brief, Step 1/5).

Requires deltalake, pyarrow, pytest - none installable in the authoring
sandbox (network blocked). Placed under tests/deferred/ so the sandbox
mini-runner (tests/run_tests.py) does not pick these up. Run with:

    pytest tick-vault/tests

once dependencies are available. UNVERIFIED until then.

The pure/seam-injected core (`generate_manifest`, `symbol_at`, `Budget`,
`SpanMemory`, `build_tick_url`/`request_windows`/`dedup_ticks`,
`fetch_week` against fakes) is already covered by the runnable
`tests/test_engine_core.py`; this file is a thin end-to-end smoke test on
a real tmp_path Delta lake: `ManifestBuilder` reading/writing real
`silver.index_membership_version` / `silver.identifier_assignment_version`
/ `ops.backfill_manifest` tables, and `fetch_week` using a real
`CaptureStore` + `FakeTransport` to land real bronze + silver rows for one
small week.
"""
import datetime as dt
import decimal
import json

import pandas as pd
import pytest
from deltalake import DeltaTable

from tick_vault.capture import CaptureStore, FakeTransport
from tick_vault.engine import (
    Budget,
    ManifestBuilder,
    SpanMemory,
    STATUS_COMPLETE,
    STATUS_PENDING,
    fetch_week,
)
from tick_vault.schemas import create_all

UTC = dt.timezone.utc
OBS = dt.datetime(2024, 1, 10, tzinfo=UTC)


def _read(root, table):
    layer, name = table.split(".", 1)
    return DeltaTable(f"{root}/{layer}/{name}").to_pandas()


def _seed_membership_and_identity(root, *, listing_id, instrument_id, symbol, start_date):
    membership_row = {
        "membership_version_id": "mem_1",
        "index_id": "idx_sp500",
        "index_vendor_symbol": "GSPC.INDX",
        "listing_id": listing_id,
        "instrument_id": instrument_id,
        "source_constituent_code": symbol,
        "source_exchange_code": "US",
        "source_name": "Test Co",
        "membership_effective_from": start_date,
        "membership_effective_to": None,
        "inclusion_reason": None,
        "exclusion_reason": None,
        "index_weight": None,
        "observed_at_ts": OBS,
        "available_at_ts": OBS,
        "system_from_ts": OBS,
        "system_to_ts": None,
        "source_system": "eodhd",
        "source_capture_id": "cap_seed",
        "resolution_status": "RESOLVED",
        "confidence": decimal.Decimal("0.9000"),
        "supersedes_membership_version_id": None,
    }
    assignment_row = {
        "assignment_version_id": "wrk_seed_1",
        "issuer_id": None, "registrant_id": None,
        "instrument_id": instrument_id, "listing_id": listing_id,
        "id_namespace": "EODHD_SYMBOL", "id_value": symbol,
        "normalized_id_value": symbol.upper(),
        "effective_from_ts": dt.datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC),
        "effective_to_ts": None,
        "observed_at_ts": OBS, "available_at_ts": OBS, "system_from_ts": OBS,
        "system_to_ts": None,
        "source_system": "eodhd", "source_capture_id": "cap_seed",
        "verification_status": "UNVERIFIED",
        "confidence": decimal.Decimal("0.9000"),
        "supersedes_assignment_version_id": None,
    }
    from tick_vault.capture import append_rows

    append_rows(root, "silver.index_membership_version", pd.DataFrame([membership_row]))
    append_rows(root, "silver.identifier_assignment_version", pd.DataFrame([assignment_row]))


def test_manifest_builder_generates_and_persists_real_rows(tmp_path):
    root = str(tmp_path)
    create_all(root)
    _seed_membership_and_identity(
        root, listing_id="lst_e2e", instrument_id="ins_e2e",
        symbol="E2E.US", start_date=dt.date(2023, 1, 1),
    )

    week_monday = dt.date(2024, 1, 1)
    builder = ManifestBuilder(root)
    new_rows = builder.generate(week_monday, week_monday, created_at=OBS)

    assert len(new_rows) == 1
    assert new_rows.iloc[0]["status"] == STATUS_PENDING
    assert new_rows.iloc[0]["vendor_symbol_at_date"] == "E2E.US"

    persisted = _read(root, "ops.backfill_manifest")
    assert len(persisted) == 1
    assert persisted.iloc[0]["listing_id"] == "lst_e2e"

    # Second call is idempotent: same (listing_id, week_monday) skipped.
    again = builder.generate(week_monday, week_monday, created_at=OBS)
    assert again.empty
    assert len(_read(root, "ops.backfill_manifest")) == 1


def test_fetch_week_end_to_end_lands_bronze_and_silver_rows(tmp_path):
    root = str(tmp_path)
    create_all(root)

    from_sec = 1704067200  # 2024-01-01T00:00:00Z (Monday)
    to_sec = from_sec + 86399  # one day, fits in a single default window

    payload = json.dumps([
        {"ts": from_sec * 1000 + 500, "price": 101.5, "shares": 10, "seq": 1,
         "sl": "@   ", "mkt": "Q", "sub_mkt": "", "ex": "US"},
    ]).encode("utf-8")

    transport = FakeTransport([(200, payload)])
    store = CaptureStore(root)
    span_memory = SpanMemory()
    budget = Budget(sleeper=lambda seconds: None)

    item = {
        "listing_id": "lst_e2e", "instrument_id": "ins_e2e",
        "vendor_symbol_at_date": "E2E.US",
        "request_from_sec": from_sec, "request_to_sec": to_sec,
    }

    result = fetch_week(
        transport, store, root, item,
        span_memory=span_memory, budget=budget, now=OBS,
        monotonic=lambda: 0.0,
    )

    assert result.status == STATUS_COMPLETE
    assert result.rows_written == 1
    assert result.captures == 1

    bronze = _read(root, "bronze.eodhd_tick_capture")
    assert len(bronze) == 1
    assert bronze.iloc[0]["request_symbol"] == "E2E.US"
    assert bronze.iloc[0]["http_status"] == 200

    silver = _read(root, "silver.us_trade_tick_version")
    assert len(silver) == 1
    assert silver.iloc[0]["listing_id"] == "lst_e2e"
    assert silver.iloc[0]["session_seq"] == 1

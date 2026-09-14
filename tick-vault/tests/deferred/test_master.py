"""Deferred tests for `tick_vault.master` (task-4 brief).

Requires deltalake, pyarrow, pytest - none of which are installable in
the authoring sandbox (network blocked). Placed under tests/deferred/ so
the sandbox mini-runner (tests/run_tests.py, which os.listdir()s tests/
non-recursively for test_*.py) does not pick these up. Run with:

    pytest tick-vault/tests

once the dependencies are available. UNVERIFIED until then.

The pure resolution/derivation logic (`compute_symbol_upsert`,
`compute_symbol_changes`, `compute_attach_issue_ids`,
`resolve_listing_at`, and `MasterBuilder` driven by a fake in-memory
reader/writer) is already covered by the runnable
`tests/test_master_core.py`; this file exercises only that
`MasterBuilder`'s *default* reader/writer actually round-trip through a
real Delta lake: `create_all(root)`, then `upsert_from_symbols` ->
`apply_symbol_changes` -> `resolve_symbol_at`, re-reading each table via
`DeltaTable(...).to_pandas()` to confirm the rows genuinely landed (not
just held in `MasterBuilder`'s return value).

CI gotchas exercised here (per earlier tasks' lessons):
  - `confidence` round-trips as `decimal.Decimal`, not `float`.
  - Delta `date32` (`ops.data_quality_issue.trade_date`, unused here) and
    Delta `timestamp` columns come back through pandas as `Timestamp`/
    `NaT`; comparisons below go through `MasterBuilder.resolve_symbol_at`
    (which itself normalizes via `_to_utc_ts`) rather than comparing
    `Timestamp`/`datetime` directly against a `deltalake`-read frame.
"""
import datetime as dt
import decimal

import pandas as pd
import pytest
from deltalake import DeltaTable

from tick_vault.master import AUTO_CONFIDENCE, MasterBuilder
from tick_vault.schemas import create_all


def _read(root, table):
    layer, name = table.split(".", 1)
    return DeltaTable(f"{root}/{layer}/{name}").to_pandas()


def _symbols_df(rows):
    return pd.DataFrame(rows, columns=["code", "name", "exchange", "type", "isin"])


def _changes_df(rows):
    return pd.DataFrame(rows, columns=["old", "new", "date"])


def _fundamentals_df(rows):
    return pd.DataFrame(rows, columns=["code", "cusip", "isin", "cik", "name", "type", "share_class"])


OBS1 = dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc)
OBS2 = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc)


@pytest.fixture
def root(tmp_path):
    r = str(tmp_path / "lake")
    create_all(r)
    return r


def test_upsert_writes_land_in_delta(root):
    mb = MasterBuilder(root)
    symbols = _symbols_df([
        {"code": "IBM", "name": "IBM Corp", "exchange": "US", "type": "Common Stock", "isin": "US4592001014"},
    ])
    mb.upsert_from_symbols(symbols, "cap_1", OBS1)

    instruments = _read(root, "silver.instrument")
    listings = _read(root, "silver.listing_version")
    assignments = _read(root, "silver.identifier_assignment_version")

    assert len(instruments) == 1
    assert len(listings) == 1
    assert len(assignments) == 1
    assert isinstance(assignments.iloc[0]["confidence"], decimal.Decimal)
    assert assignments.iloc[0]["confidence"] == AUTO_CONFIDENCE
    assert assignments.iloc[0]["id_value"] == "IBM"

    # idempotent re-run against the real lake
    mb.upsert_from_symbols(symbols, "cap_2", OBS2)
    assert len(_read(root, "silver.instrument")) == 1
    assert len(_read(root, "silver.identifier_assignment_version")) == 1


def test_full_upsert_change_resolve_round_trip(root):
    mb = MasterBuilder(root)
    symbols = _symbols_df([{"code": "BK", "name": "Bank of NY", "exchange": "US", "type": "Common Stock", "isin": None}])
    upsert_result = mb.upsert_from_symbols(symbols, "cap_1", OBS1)
    listing_id = upsert_result["listing_version"].iloc[0]["listing_id"]

    change_date = dt.date(2026, 6, 15)
    changes = _changes_df([{"old": "BK", "new": "BNY", "date": change_date}])
    mb.apply_symbol_changes(changes, "cap_2", OBS2)

    assignments = _read(root, "silver.identifier_assignment_version")
    assert len(assignments) == 3  # original BK + closing BK version + new BNY

    assert mb.resolve_symbol_at("BK", dt.date(2026, 5, 1)) == listing_id
    assert mb.resolve_symbol_at("BNY", dt.date(2026, 8, 1)) == listing_id
    assert mb.resolve_symbol_at("BK", dt.date(2026, 8, 1)) is None


def test_unknown_old_code_lands_in_queue_table(root):
    mb = MasterBuilder(root)
    changes = _changes_df([{"old": "GHOST", "new": "GHOST2", "date": dt.date(2026, 3, 1)}])
    mb.apply_symbol_changes(changes, "cap_1", OBS1)

    queue = _read(root, "ops.openfigi_resolution_queue")
    assert len(queue) == 1
    assert queue.iloc[0]["status"] == "UNKNOWN_OLD_CODE"
    assert queue.iloc[0]["id_value"] == "GHOST"


def test_cusip_isin_cik_attached_at_instrument_scope_in_delta(root):
    mb = MasterBuilder(root)
    symbols = _symbols_df([{"code": "IBM", "name": "IBM Corp", "exchange": "US", "type": "Common Stock", "isin": None}])
    upsert_result = mb.upsert_from_symbols(symbols, "cap_1", OBS1)
    instrument_id = upsert_result["instrument"].iloc[0]["instrument_id"]

    fundamentals = _fundamentals_df([{
        "code": "IBM", "cusip": "459200101", "isin": "US4592001014", "cik": "0000051143",
        "name": "IBM Corp", "type": "Common Stock", "share_class": None,
    }])
    mb.attach_issue_ids(fundamentals, "cap_2", OBS2)

    assignments = _read(root, "silver.identifier_assignment_version")
    issue_rows = assignments[assignments["id_namespace"].isin(["CUSIP", "ISIN", "SEC_CIK"])]
    assert len(issue_rows) == 3
    assert (issue_rows["instrument_id"] == instrument_id).all()
    assert issue_rows["listing_id"].isna().all()

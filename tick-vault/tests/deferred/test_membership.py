"""Deferred tests for `tick_vault.membership` (task-5 brief).

Requires deltalake, pyarrow, pytest - none installable in the authoring
sandbox (network blocked). Placed under tests/deferred/ so the sandbox
mini-runner (tests/run_tests.py) does not pick these up. Run with:

    pytest tick-vault/tests

once dependencies are available. UNVERIFIED until then.

The pure resolution/overlap/dedup logic (`compute_membership`,
`build_membership` driven by a fake in-memory reader/writer + fake
resolver) is already covered by the runnable
`tests/test_membership_core.py`; this file exercises only the module's
*default* reader/writer actually round-tripping through a real Delta
lake, per the "small end-to-end with fake resolver + real lake" test
convention.
"""
import datetime as dt
import decimal

import pandas as pd
import pytest
from deltalake import DeltaTable

from tick_vault.membership import (
    AUTO_CONFIDENCE,
    INDEX_ID,
    INDEX_VENDOR_SYMBOL,
    RESOLUTION_RESOLVED,
    RESOLUTION_UNRESOLVED,
    build_membership,
)
from tick_vault.schemas import create_all

OBS1 = dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc)
OBS2 = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc)


def _read(root, table):
    layer, name = table.split(".", 1)
    return DeltaTable(f"{root}/{layer}/{name}").to_pandas()


def _components_df(rows):
    return pd.DataFrame(rows, columns=["code", "name", "start_date", "end_date", "is_active", "sector"])


def _fake_resolver(mapping):
    def resolver(code, date):
        entries = mapping.get(code)
        if not entries or date is None:
            return None
        match = None
        for listing_id, instrument_id, on_or_after in entries:
            if on_or_after <= date:
                match = (listing_id, instrument_id)
        return match
    return resolver


@pytest.fixture
def root(tmp_path):
    r = str(tmp_path / "lake")
    create_all(r)
    return r


def test_build_membership_writes_land_in_delta(root):
    resolver = _fake_resolver({"AAPL": [("lst_aapl", "ins_aapl", dt.date(1990, 1, 1))]})
    components = _components_df([
        {"code": "AAPL", "name": "Apple Inc", "start_date": dt.date(2000, 1, 1),
         "end_date": None, "is_active": True, "sector": "Technology"},
    ])

    report = build_membership(root, components, resolver=resolver, capture_id="cap_1", observed_at=OBS1)

    assert report.resolved == 1
    assert report.written == 1

    rows = _read(root, "silver.index_membership_version")
    assert len(rows) == 1
    row = rows.iloc[0]
    assert row["index_id"] == INDEX_ID
    assert row["index_vendor_symbol"] == INDEX_VENDOR_SYMBOL
    assert row["listing_id"] == "lst_aapl"
    assert row["resolution_status"] == RESOLUTION_RESOLVED
    assert isinstance(row["confidence"], decimal.Decimal)
    assert row["confidence"] == AUTO_CONFIDENCE
    assert row["membership_effective_from"] == dt.date(2000, 1, 1)

    # idempotent re-run against the real lake (default memberships_reader)
    build_membership(root, components, resolver=resolver, capture_id="cap_2", observed_at=OBS2)
    assert len(_read(root, "silver.index_membership_version")) == 1


def test_unresolved_code_writes_and_does_not_touch_dq_table(root):
    resolver = _fake_resolver({})
    components = _components_df([
        {"code": "GHOST", "name": "Ghost Corp", "start_date": dt.date(2005, 1, 1),
         "end_date": None, "is_active": True, "sector": "Unknown"},
    ])

    report = build_membership(root, components, resolver=resolver, capture_id="cap_1", observed_at=OBS1)
    assert report.unresolved == 1

    rows = _read(root, "silver.index_membership_version")
    assert len(rows) == 1
    assert rows.iloc[0]["resolution_status"] == RESOLUTION_UNRESOLVED
    assert rows.iloc[0]["listing_id"] is None

    dq = _read(root, "ops.data_quality_issue")
    assert len(dq) == 0


def test_overlap_writes_quality_issue_row_in_delta(root):
    resolver = _fake_resolver({
        "OLDX": [("lst_shared", "ins_shared", dt.date(1990, 1, 1))],
        "NEWX": [("lst_shared", "ins_shared", dt.date(1990, 1, 1))],
    })
    components = _components_df([
        {"code": "OLDX", "name": "Old Co", "start_date": dt.date(2000, 1, 1),
         "end_date": dt.date(2010, 1, 1), "is_active": False, "sector": "Industrials"},
        {"code": "NEWX", "name": "New Co", "start_date": dt.date(2005, 1, 1),
         "end_date": None, "is_active": True, "sector": "Industrials"},
    ])

    report = build_membership(root, components, resolver=resolver, capture_id="cap_1", observed_at=OBS1)
    assert report.ambiguous == 1

    rows = _read(root, "silver.index_membership_version")
    assert len(rows) == 2

    dq = _read(root, "ops.data_quality_issue")
    assert len(dq) == 1
    assert dq.iloc[0]["check_name"] == "MEMBERSHIP_OVERLAP"

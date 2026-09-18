"""`cli._default_manifest_row_writer` against a real Delta lake.

Phase-0 (2026-09-17) replaced a read-modify-OVERWRITE of the whole manifest
(4-16 s per item on the real 262,345-row table, and unsafe for concurrent
writers) with a targeted `DeltaTable.update` of one row's mutable columns.
These tests pin what that must not do: touch other rows, touch the updated
row's immutable columns, or lose a row whose `work_id` is not there yet.
"""
import datetime as dt

import pandas as pd
import pytest
from deltalake import DeltaTable

from tick_vault import cli
from tick_vault.capture import append_rows
from tick_vault.engine import MANIFEST_COLUMNS
from tick_vault.schemas import create_all

NOW = dt.datetime(2026, 9, 17, 12, 0, tzinfo=dt.timezone.utc)


def _row(work_id: str, week: dt.date) -> dict:
    row = {c: None for c in MANIFEST_COLUMNS}
    row.update(
        work_id=work_id, listing_id=f"lst_{work_id}", instrument_id="ins_a",
        week_monday=week, vendor_symbol_at_date="AAPL", eodhd_exchange_code="US",
        request_from_sec=1, request_to_sec=2, membership_snapshot_id="snap",
        identity_snapshot_id="snap", availability_policy_version="v1",
        status="PENDING", attempts=0, priority=1,
        created_at_ts=NOW, updated_at_ts=NOW,
    )
    return row


@pytest.fixture
def root(tmp_path):
    r = str(tmp_path / "lake")
    create_all(r)
    append_rows(r, "ops.backfill_manifest", pd.DataFrame(
        [_row("wrk_1", dt.date(2026, 8, 31)), _row("wrk_2", dt.date(2026, 8, 24)),
         _row("wrk_3", dt.date(2026, 8, 17))]
    ))
    return r


def _read(root) -> pd.DataFrame:
    return DeltaTable(f"{root}/ops/backfill_manifest").to_pandas().set_index("work_id")


def test_updates_only_the_matching_row_and_only_mutable_columns(root):
    updated = _row("wrk_2", dt.date(2026, 8, 24))
    updated.update(status="COMPLETE", attempts=1, last_error=None,
                   updated_at_ts=NOW + dt.timedelta(hours=1),
                   completed_at_ts=NOW + dt.timedelta(hours=1))
    cli._default_manifest_row_writer(root, updated)

    df = _read(root)
    assert len(df) == 3
    assert df.loc["wrk_2", "status"] == "COMPLETE" and int(df.loc["wrk_2", "attempts"]) == 1
    assert df.loc["wrk_2", "completed_at_ts"] == pd.Timestamp(NOW + dt.timedelta(hours=1))
    # immutable columns on the updated row survive untouched
    assert df.loc["wrk_2", "week_monday"] == dt.date(2026, 8, 24)
    assert df.loc["wrk_2", "listing_id"] == "lst_wrk_2"
    # the other rows are not rewritten
    assert list(df.loc[["wrk_1", "wrk_3"], "status"]) == ["PENDING", "PENDING"]
    assert list(df.loc[["wrk_1", "wrk_3"], "attempts"]) == [0, 0]


def test_unknown_work_id_is_appended_not_dropped(root):
    new = _row("wrk_new", dt.date(2026, 9, 7))
    new.update(status="FAILED", attempts=1, last_error="HTTP 500")
    cli._default_manifest_row_writer(root, new)

    df = _read(root)
    assert len(df) == 4 and df.loc["wrk_new", "status"] == "FAILED"


def test_error_text_with_a_quote_is_escaped(root):
    updated = _row("wrk_1", dt.date(2026, 8, 31))
    updated.update(status="FAILED", attempts=1, last_error="can't reach vendor")
    cli._default_manifest_row_writer(root, updated)

    df = _read(root)
    assert df.loc["wrk_1", "last_error"] == "can't reach vendor"
    assert len(df) == 3

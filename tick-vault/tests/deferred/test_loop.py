"""Deferred tests for `tick_vault.loop` (task-10 brief).

Requires deltalake, pyarrow, pytest - none installable in the authoring
sandbox (network blocked). Placed under tests/deferred/ so the sandbox
mini-runner (tests/run_tests.py) does not pick these up. Run with:

    pytest tick-vault/tests

once dependencies are available. UNVERIFIED until then.

The pure `run_cycle`/`apply_gate_decision` core, and `open_run`/
`close_run` against a fake `ingestion_run_writer`, are already covered by
the runnable `tests/test_loop_core.py`; this file is a thin real-Delta
smoke test: `open_run`/`close_run` land real `ops.ingestion_run` rows on
a `tmp_path` lake created via `tick_vault.schemas.create_all`.
"""
import datetime as dt

import pandas as pd
import pytest
from deltalake import DeltaTable

from tick_vault.loop import (
    LoopConfig,
    LoopContext,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_RUNNING,
    close_run,
    open_run,
)
from tick_vault.schemas import create_all

UTC = dt.timezone.utc


@pytest.fixture
def lake(tmp_path):
    root = str(tmp_path)
    create_all(root)
    return root


def test_open_run_lands_real_running_row(lake):
    now = dt.datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    ctx = LoopContext(root=lake, clock=lambda: now, config=LoopConfig())

    run_id = open_run(ctx, "backfill_loop")

    df = DeltaTable(f"{lake}/ops/ingestion_run").to_pandas()
    rows = df[df["run_id"] == run_id]
    assert len(rows) == 1
    row = rows.iloc[0]
    assert row["status"] == RUN_STATUS_RUNNING
    assert row["run_type"] == "backfill_loop"
    assert pd.isna(row["completed_at_ts"])


def test_open_then_close_run_lands_two_real_rows_same_run_id(lake):
    now = dt.datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    ctx = LoopContext(root=lake, clock=lambda: now, config=LoopConfig())

    run_id = open_run(ctx, "backfill_loop")
    close_run(ctx, run_id, status=RUN_STATUS_COMPLETED)

    df = DeltaTable(f"{lake}/ops/ingestion_run").to_pandas()
    rows = df[df["run_id"] == run_id].sort_values("completed_at_ts", na_position="first")
    assert len(rows) == 2
    assert rows.iloc[0]["status"] == RUN_STATUS_RUNNING
    assert rows.iloc[1]["status"] == RUN_STATUS_COMPLETED
    assert not pd.isna(rows.iloc[1]["completed_at_ts"])


def test_open_run_with_override_note_lands_in_quality_check_summary(lake):
    now = dt.datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    ctx = LoopContext(root=lake, clock=lambda: now, config=LoopConfig())

    run_id = open_run(ctx, "GATE_OVERRIDE", note="operator override: no calibration report")

    df = DeltaTable(f"{lake}/ops/ingestion_run").to_pandas()
    row = df[df["run_id"] == run_id].iloc[0]
    assert "operator override" in row["quality_check_summary_json"]

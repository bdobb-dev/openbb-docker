"""Runnable (pyarrow/deltalake/pytest-free) tests for `tick_vault.loop`
(task-10 brief): the `run_cycle` priority table, `apply_gate_decision`,
and `open_run`/`close_run`'s row shapes against a fake `ingestion_run_writer`
(no real Delta lake - see `tests/deferred/test_loop.py` for that).
"""
import datetime as dt

import pandas as pd

from tick_vault.engine import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PENDING,
)
from tick_vault.loop import (
    CYCLE_DAILY,
    CYCLE_FETCH,
    CYCLE_RETRY,
    CYCLE_SETTLE,
    CYCLE_SLEEP,
    LoopConfig,
    LoopContext,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
    apply_gate_decision,
    close_run,
    open_run,
    run_cycle,
)
from tick_vault.reconcile import GATE_REASON_HOLD, GateDecision

UTC = dt.timezone.utc


def _row(**overrides) -> dict:
    row = {
        "work_id": "wrk_default",
        "listing_id": "lst_a",
        "week_monday": dt.date(2024, 1, 8),
        "instrument_id": "ins_a",
        "vendor_symbol_at_date": "AAPL",
        "status": STATUS_PENDING,
        "attempts": 0,
        "priority": 1,
        "completed_at_ts": None,
        "last_error": None,
        "created_at_ts": dt.datetime(2024, 1, 1, tzinfo=UTC),
        "updated_at_ts": dt.datetime(2024, 1, 1, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def _ctx(now: dt.datetime, *, config: "LoopConfig | None" = None) -> LoopContext:
    return LoopContext(root="mem://test", clock=lambda: now, config=config or LoopConfig())


def _already_ran_daily_today(ctx: LoopContext, state: dict) -> None:
    """Pre-seed `state` so priority-0's daily check is already satisfied
    for `ctx`'s clock, isolating tests of priorities 1-4."""
    from zoneinfo import ZoneInfo

    ny_now = ctx.now().astimezone(ZoneInfo("America/New_York"))
    state["last_daily_date"] = ny_now.date()


# ---------------------------------------------------------------------------
# priority 0: daily pass
# ---------------------------------------------------------------------------

def test_run_cycle_daily_due_after_threshold():
    # 2024-01-10 07:10 UTC == 02:10 America/New_York (winter, UTC-5) -
    # after the 02:05 threshold.
    now = dt.datetime(2024, 1, 10, 7, 10, tzinfo=UTC)
    ctx = _ctx(now)
    state: dict = {}
    action = run_cycle(ctx, pd.DataFrame([_row()]), state)
    assert action.kind == CYCLE_DAILY


def test_run_cycle_daily_not_due_before_threshold():
    # 2024-01-10 06:00 UTC == 01:00 America/New_York - before 02:05.
    now = dt.datetime(2024, 1, 10, 6, 0, tzinfo=UTC)
    ctx = _ctx(now)
    state: dict = {}
    action = run_cycle(ctx, pd.DataFrame(columns=list(_row().keys())), state)
    assert action.kind == CYCLE_SLEEP


def test_run_cycle_daily_only_once_per_day():
    now = dt.datetime(2024, 1, 10, 7, 10, tzinfo=UTC)
    ctx = _ctx(now)
    state: dict = {}
    _already_ran_daily_today(ctx, state)
    action = run_cycle(ctx, pd.DataFrame(columns=list(_row().keys())), state)
    assert action.kind != CYCLE_DAILY


# ---------------------------------------------------------------------------
# priority 1: newest complete unloaded week
# ---------------------------------------------------------------------------

def test_run_cycle_selects_newest_complete_pending_week():
    now = dt.datetime(2024, 2, 1, 12, 0, tzinfo=UTC)
    ctx = _ctx(now)
    state: dict = {}
    _already_ran_daily_today(ctx, state)
    manifest = pd.DataFrame([
        _row(work_id="wrk_1", week_monday=dt.date(2024, 1, 1), status=STATUS_PENDING),
        _row(work_id="wrk_2", week_monday=dt.date(2024, 1, 8), status=STATUS_PENDING),
    ])
    action = run_cycle(ctx, manifest, state)
    assert action.kind == CYCLE_FETCH
    assert action.item["work_id"] == "wrk_2"
    assert action.reason == "newest complete unloaded week"


def test_run_cycle_incomplete_week_not_selected_by_priority_1():
    # week_monday = the current week; Saturday 08:00 UTC for it hasn't
    # happened yet at `now`, so it fails the T+1 completeness gate.
    now = dt.datetime(2024, 1, 15, 12, 0, tzinfo=UTC)  # Monday
    ctx = _ctx(now)
    state: dict = {}
    _already_ran_daily_today(ctx, state)
    manifest = pd.DataFrame([
        _row(work_id="wrk_current", week_monday=dt.date(2024, 1, 15), status=STATUS_PENDING),
    ])
    action = run_cycle(ctx, manifest, state)
    # Priority 4 is ALSO gated by T+1 completeness (fix, task-10
    # fix-round) - an incomplete week is never selected by priority 1
    # OR priority 4, so this falls all the way through to SLEEP.
    assert action.kind == CYCLE_SLEEP


# ---------------------------------------------------------------------------
# priority 2: settle due (live-edge only)
# ---------------------------------------------------------------------------

def test_run_cycle_settle_due_within_horizon():
    now = dt.datetime(2024, 2, 1, 12, 0, tzinfo=UTC)
    ctx = _ctx(now, config=LoopConfig(settle_horizon_days=14))
    state: dict = {}
    _already_ran_daily_today(ctx, state)
    manifest = pd.DataFrame([
        _row(work_id="wrk_live", week_monday=dt.date(2024, 1, 29), status=STATUS_COMPLETE),
    ])
    action = run_cycle(ctx, manifest, state)
    assert action.kind == CYCLE_SETTLE
    assert action.item["work_id"] == "wrk_live"


def test_run_cycle_2019_week_never_settles():
    now = dt.datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    ctx = _ctx(now, config=LoopConfig(settle_horizon_days=14))
    state: dict = {}
    _already_ran_daily_today(ctx, state)
    manifest = pd.DataFrame([
        _row(work_id="wrk_2019", week_monday=dt.date(2019, 3, 4), status=STATUS_COMPLETE),
    ])
    action = run_cycle(ctx, manifest, state)
    # No PENDING/FAILED rows either, and the 2019 week is nowhere near
    # today - nothing to do.
    assert action.kind == CYCLE_SLEEP


def test_run_cycle_settle_already_done_today_is_skipped():
    now = dt.datetime(2024, 2, 1, 12, 0, tzinfo=UTC)
    ctx = _ctx(now, config=LoopConfig(settle_horizon_days=14))
    state: dict = {"last_settle_date_by_work_id": {"wrk_live": now.date()}}
    _already_ran_daily_today(ctx, state)
    manifest = pd.DataFrame([
        _row(work_id="wrk_live", week_monday=dt.date(2024, 1, 29), status=STATUS_COMPLETE),
    ])
    action = run_cycle(ctx, manifest, state)
    assert action.kind == CYCLE_SLEEP


def test_execute_settle_records_state_even_when_existing_latest_reader_unwired():
    """I5 regression: `_execute_settle` used to `return` immediately when
    `ctx.existing_latest_reader` is unwired (`None`, a Plan-3 concern),
    WITHOUT recording `today` against the item's `work_id` in `state[
    "last_settle_date_by_work_id"]` first. Since a skipped settle never
    changes the manifest, `run_cycle`'s priority-2 `_due` check would keep
    re-selecting the exact same SETTLE item every subsequent cycle -
    busy-spinning the loop on one item forever instead of advancing to
    SLEEP or whatever else becomes due."""
    from tick_vault.loop import _execute_action

    now = dt.datetime(2024, 2, 1, 12, 0, tzinfo=UTC)
    ctx = _ctx(now, config=LoopConfig(settle_horizon_days=14))
    state: dict = {}
    _already_ran_daily_today(ctx, state)
    manifest = pd.DataFrame([
        _row(work_id="wrk_live", week_monday=dt.date(2024, 1, 29), status=STATUS_COMPLETE),
    ])
    assert ctx.existing_latest_reader is None  # the unwired case this test targets

    action1 = run_cycle(ctx, manifest, state)
    assert action1.kind == CYCLE_SETTLE
    _execute_action(ctx, action1, state)
    assert state["last_settle_date_by_work_id"].get("wrk_live") == now.date()

    action2 = run_cycle(ctx, manifest, state)
    assert action2.kind != CYCLE_SETTLE


# ---------------------------------------------------------------------------
# priority 3: FAILED retry >24h
# ---------------------------------------------------------------------------

def test_run_cycle_retries_failed_row_after_24h():
    now = dt.datetime(2024, 2, 1, 12, 0, tzinfo=UTC)
    ctx = _ctx(now)
    state: dict = {}
    _already_ran_daily_today(ctx, state)
    manifest = pd.DataFrame([
        _row(
            work_id="wrk_failed", status=STATUS_FAILED,
            updated_at_ts=now - dt.timedelta(hours=25),
        ),
    ])
    action = run_cycle(ctx, manifest, state)
    assert action.kind == CYCLE_RETRY
    assert action.item["work_id"] == "wrk_failed"


def test_run_cycle_does_not_retry_failed_row_before_24h():
    now = dt.datetime(2024, 2, 1, 12, 0, tzinfo=UTC)
    ctx = _ctx(now)
    state: dict = {}
    _already_ran_daily_today(ctx, state)
    manifest = pd.DataFrame([
        _row(
            work_id="wrk_failed", status=STATUS_FAILED,
            updated_at_ts=now - dt.timedelta(hours=1),
        ),
    ])
    action = run_cycle(ctx, manifest, state)
    assert action.kind == CYCLE_SLEEP


# ---------------------------------------------------------------------------
# priority 4: backward walk
# ---------------------------------------------------------------------------

def test_run_cycle_backward_walk_falls_through_priority_1_when_all_incomplete():
    # Both weeks fail the T+1 completeness gate at `now` (the older one
    # is the current week's Monday itself, the newer is next week) -
    # priority 1 finds nothing, and priority 4 (also T+1-gated as of the
    # task-10 fix-round) finds nothing either, so this sleeps rather
    # than fetching an incomplete week.
    now = dt.datetime(2024, 1, 15, 12, 0, tzinfo=UTC)  # Monday, current week
    ctx = _ctx(now)
    state: dict = {}
    _already_ran_daily_today(ctx, state)
    manifest = pd.DataFrame([
        _row(work_id="wrk_this_week", week_monday=dt.date(2024, 1, 15), status=STATUS_PENDING),
        _row(work_id="wrk_next_week", week_monday=dt.date(2024, 1, 22), status=STATUS_PENDING),
    ])
    action = run_cycle(ctx, manifest, state)
    assert action.kind == CYCLE_SLEEP


def test_run_cycle_backward_walk_only_incomplete_pending():
    now = dt.datetime(2024, 1, 15, 12, 0, tzinfo=UTC)  # Monday
    ctx = _ctx(now)
    state: dict = {}
    _already_ran_daily_today(ctx, state)
    manifest = pd.DataFrame([
        _row(work_id="wrk_only", week_monday=dt.date(2024, 1, 15), status=STATUS_PENDING),
    ])
    action = run_cycle(ctx, manifest, state)
    # The only PENDING row is incomplete - neither priority 1 nor
    # priority 4 may select it, so this sleeps.
    assert action.kind == CYCLE_SLEEP


def test_run_cycle_backward_walk_mix_of_incomplete_new_and_complete_old_picks_old():
    # A newer PENDING week that is still incomplete (T+1 not yet
    # satisfied) alongside an older PENDING week that IS complete: the
    # complete-old week must be picked (via priority 1's own
    # completeness filter, which does not depend on sort order finding
    # the newest row overall - only the newest COMPLETE row), never the
    # incomplete-new one.
    now = dt.datetime(2024, 1, 15, 12, 0, tzinfo=UTC)  # Monday, current week
    ctx = _ctx(now)
    state: dict = {}
    _already_ran_daily_today(ctx, state)
    manifest = pd.DataFrame([
        _row(work_id="wrk_new_incomplete", week_monday=dt.date(2024, 1, 15), status=STATUS_PENDING),
        _row(work_id="wrk_old_complete", week_monday=dt.date(2023, 12, 4), status=STATUS_PENDING),
    ])
    action = run_cycle(ctx, manifest, state)
    assert action.kind == CYCLE_FETCH
    assert action.item["work_id"] == "wrk_old_complete"


# ---------------------------------------------------------------------------
# priority 5: sleep
# ---------------------------------------------------------------------------

def test_run_cycle_sleeps_on_empty_manifest():
    now = dt.datetime(2024, 1, 10, 12, 0, tzinfo=UTC)
    ctx = _ctx(now)
    state: dict = {}
    _already_ran_daily_today(ctx, state)
    action = run_cycle(ctx, pd.DataFrame(columns=list(_row().keys())), state)
    assert action.kind == CYCLE_SLEEP
    assert action.seconds == 900


# ---------------------------------------------------------------------------
# apply_gate_decision
# ---------------------------------------------------------------------------

def test_apply_gate_decision_hold_forces_partial():
    row = _row(status=STATUS_COMPLETE, last_error=None)
    updated = apply_gate_decision(row, GateDecision(hold=True, reason=GATE_REASON_HOLD))
    assert updated["status"] == "PARTIAL"
    assert updated["last_error"] == "RECONCILE_HOLD"
    # original untouched
    assert row["status"] == STATUS_COMPLETE


def test_apply_gate_decision_no_hold_is_passthrough():
    row = _row(status=STATUS_COMPLETE, last_error=None)
    updated = apply_gate_decision(row, GateDecision(hold=False, reason="PASS"))
    assert updated == row


# ---------------------------------------------------------------------------
# open_run / close_run row shapes (fake writer, no real Delta lake)
# ---------------------------------------------------------------------------

class _FakeIngestionRunWriter:
    def __init__(self):
        self.writes = []

    def __call__(self, root, table, df):
        assert table == "ops.ingestion_run"
        self.writes.append(df.copy())


def test_open_run_writes_running_row():
    writer = _FakeIngestionRunWriter()
    now = dt.datetime(2024, 1, 10, 12, 0, tzinfo=UTC)
    ctx = LoopContext(root="mem://test", clock=lambda: now, ingestion_run_writer=writer)
    run_id = open_run(ctx, "backfill_loop")

    assert len(writer.writes) == 1
    row = writer.writes[0].iloc[0]
    assert row["run_id"] == run_id
    assert row["run_type"] == "backfill_loop"
    assert row["status"] == RUN_STATUS_RUNNING
    assert row["started_at_ts"] == now
    assert row["completed_at_ts"] is None
    assert row["config_hash"]
    assert row["code_git_commit"]


def test_open_run_logs_override_note():
    writer = _FakeIngestionRunWriter()
    now = dt.datetime(2024, 1, 10, 12, 0, tzinfo=UTC)
    ctx = LoopContext(root="mem://test", clock=lambda: now, ingestion_run_writer=writer)
    open_run(ctx, "GATE_OVERRIDE", note="operator override")
    row = writer.writes[0].iloc[0]
    assert "operator override" in row["quality_check_summary_json"]


def test_close_run_writes_terminal_row_same_run_id():
    writer = _FakeIngestionRunWriter()
    now = dt.datetime(2024, 1, 10, 12, 0, tzinfo=UTC)
    ctx = LoopContext(root="mem://test", clock=lambda: now, ingestion_run_writer=writer)
    run_id = open_run(ctx, "backfill_loop")
    close_run(ctx, run_id, status=RUN_STATUS_COMPLETED)

    assert len(writer.writes) == 2
    closing_row = writer.writes[1].iloc[0]
    assert closing_row["run_id"] == run_id
    assert closing_row["status"] == RUN_STATUS_COMPLETED
    assert closing_row["completed_at_ts"] == now


def test_close_run_records_error_summary():
    writer = _FakeIngestionRunWriter()
    now = dt.datetime(2024, 1, 10, 12, 0, tzinfo=UTC)
    ctx = LoopContext(root="mem://test", clock=lambda: now, ingestion_run_writer=writer)
    run_id = open_run(ctx, "backfill_loop")
    close_run(ctx, run_id, status=RUN_STATUS_FAILED, error_summary="boom")
    closing_row = writer.writes[1].iloc[0]
    assert closing_row["status"] == RUN_STATUS_FAILED
    assert closing_row["error_summary"] == "boom"

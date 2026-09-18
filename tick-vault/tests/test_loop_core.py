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


# ---------------------------------------------------------------------------
# vendor-readiness probe (SPY control) - Phase-0, 2026-09-15
# ---------------------------------------------------------------------------

def test_latest_session_day_skips_weekends():
    from tick_vault.loop import _latest_session_day

    assert _latest_session_day(dt.datetime(2026, 9, 15, 12, 29, tzinfo=UTC)) == dt.date(2026, 9, 14)  # Tue -> Mon
    assert _latest_session_day(dt.datetime(2026, 9, 14, 12, 0, tzinfo=UTC)) == dt.date(2026, 9, 11)   # Mon -> Fri


def test_daily_pass_deferred_while_session_not_loaded_then_retried():
    from tick_vault.loop import CYCLE_DAILY, CycleAction, SESSION_RETRY, _execute_daily

    now = dt.datetime(2026, 9, 15, 12, 29, tzinfo=UTC)  # 08:29 ET, daily due
    probes = []
    ctx = LoopContext(root="mem://test", clock=lambda: now,
                      session_probe=lambda day: probes.append(day) or False)
    state: dict = {}
    assert run_cycle(ctx, pd.DataFrame(columns=list(_row().keys())), state).kind == CYCLE_DAILY

    _execute_daily(ctx, CycleAction(kind=CYCLE_DAILY), state)
    assert probes == [dt.date(2026, 9, 14)]
    assert state["last_daily_date"] is None                       # not marked done
    assert state["unloaded_session"] == {"day": dt.date(2026, 9, 14), "retry_at": now + SESSION_RETRY}
    assert run_cycle(ctx, pd.DataFrame(columns=list(_row().keys())), state).kind != CYCLE_DAILY

    later = LoopContext(root="mem://test", clock=lambda: now + SESSION_RETRY, session_probe=lambda day: True)
    assert run_cycle(later, pd.DataFrame(columns=list(_row().keys())), state).kind == CYCLE_DAILY
    _execute_daily(later, CycleAction(kind=CYCLE_DAILY), state)
    assert state["last_daily_date"] == dt.date(2026, 9, 15) and "unloaded_session" not in state


def test_blocked_session_skips_its_week_and_keeps_the_walk_moving():
    from tick_vault.loop import CYCLE_FETCH

    # Saturday 08:30 UTC: week 2026-09-14 passes the T+1 rule, but Friday
    # 2026-09-18 is not loaded yet - fetch the older week instead.
    now = dt.datetime(2026, 9, 19, 8, 30, tzinfo=UTC)
    ctx = _ctx(now)
    state: dict = {"unloaded_session": {"day": dt.date(2026, 9, 18), "retry_at": now + dt.timedelta(minutes=10)}}
    manifest = pd.DataFrame([
        _row(work_id="wrk_new", week_monday=dt.date(2026, 9, 14)),
        _row(work_id="wrk_old", week_monday=dt.date(2026, 9, 7)),
    ])
    action = run_cycle(ctx, manifest, state)
    assert action.kind == CYCLE_FETCH and action.item["work_id"] == "wrk_old"


def test_session_ready_never_probes_for_a_week_that_does_not_cover_the_latest_session():
    from tick_vault.loop import _session_ready

    def _boom(day):
        raise AssertionError("probe must not be called")

    ctx = LoopContext(root="mem://test", clock=lambda: dt.datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
                      session_probe=_boom)
    assert _session_ready(ctx, {}, dt.date(2026, 8, 31)) is True


# ---------------------------------------------------------------------------
# parallel walk: batch selection + parent-side application (2026-09-17)
# ---------------------------------------------------------------------------

def test_select_fetch_batch_takes_newest_complete_weeks_up_to_the_limit():
    from tick_vault.loop import select_fetch_batch

    now = dt.datetime(2026, 9, 19, 8, 30, tzinfo=UTC)   # Saturday
    manifest = pd.DataFrame([
        _row(work_id="w_new", week_monday=dt.date(2026, 9, 14)),   # complete per T+1
        _row(work_id="w_mid", week_monday=dt.date(2026, 9, 7)),
        _row(work_id="w_old", week_monday=dt.date(2026, 8, 31)),
        _row(work_id="w_future", week_monday=dt.date(2026, 9, 21)),  # not complete yet
    ])
    got = select_fetch_batch(_ctx(now), manifest, {}, 2)
    assert [i["work_id"] for i in got] == ["w_new", "w_mid"]

    # a session the vendor has not loaded blocks only the week covering it
    state = {"unloaded_session": {"day": dt.date(2026, 9, 18), "retry_at": now + dt.timedelta(minutes=10)}}
    got = select_fetch_batch(_ctx(now), manifest, state, 2)
    assert [i["work_id"] for i in got] == ["w_mid", "w_old"]


def test_run_fetch_stream_keeps_workers_fed_and_applies_results_in_the_parent():
    from concurrent.futures import Future

    from tick_vault.engine import STATUS_PARTIAL
    from tick_vault.loop import LoopConfig, run_fetch_stream

    now = dt.datetime(2026, 9, 19, 8, 30, tzinfo=UTC)   # Saturday: weeks below are complete
    weeks = [dt.date(2026, 9, 14), dt.date(2026, 9, 7), dt.date(2026, 8, 31)]
    manifest = pd.DataFrame([
        _row(work_id="w1", week_monday=weeks[0]),
        _row(work_id="w2", week_monday=weeks[1]),
        _row(work_id="w3", week_monday=weeks[2]),
    ])
    payloads = {
        "w1": {"work_id": "w1", "error": None,
               "result": {"status": STATUS_COMPLETE, "rows_written": 10, "captures": 1,
                          "wall_minutes": 0.5, "error": None},
               "report": {"status": "DIVERGENT", "diffs": [], "issues": [{"issue_id": "i1"}],
                          "null_size_count": 0}},
        "w2": {"work_id": "w2", "error": None,
               "result": {"status": STATUS_COMPLETE, "rows_written": 5, "captures": 1,
                          "wall_minutes": 0.2, "error": None},
               "report": {"status": "PASS", "diffs": [], "issues": [], "null_size_count": 0}},
        "w3": {"work_id": "w3", "error": "RuntimeError: boom", "result": None, "report": None},
    }

    class _FakeExecutor:
        """Resolves immediately; records how many jobs were in flight at once."""

        def __init__(self):
            self.jobs = []

        def submit(self, fn, job):
            self.jobs.append(job)
            future = Future()
            future.set_result(payloads[job["item"]["work_id"]])
            return future

    written, dq_writes = [], []
    executor = _FakeExecutor()
    ctx = LoopContext(
        root="mem://test", clock=lambda: now, api_token="KEY", config=LoopConfig(workers=2),
        manifest_reader=lambda root: manifest,
        manifest_row_writer=lambda root, row: written.append(row),
        dq_issue_writer=lambda root, table, df: dq_writes.append((table, len(df))),
        reconcile_step=object(),          # only read as "reconciliation is wired"
    )
    state: dict = {"gate_sequence": 0, "errors": []}

    applied = run_fetch_stream(ctx, state, executor)

    assert applied == 3
    assert [j["item"]["work_id"] for j in executor.jobs] == ["w1", "w2", "w3"]
    assert executor.jobs[0]["reconcile"] is True and executor.jobs[0]["root"] == "mem://test"
    by_id = {r["work_id"]: r for r in written}
    # w1 diverged inside the gated window -> held; w2 passed; w3's failure is recorded, not fatal
    assert by_id["w1"]["status"] == STATUS_PARTIAL and by_id["w1"]["last_error"] == GATE_REASON_HOLD
    assert by_id["w2"]["status"] == STATUS_COMPLETE
    assert "w3" not in by_id and any("w3" in e for e in state["errors"])
    assert dq_writes == [("ops.data_quality_issue", 1)]
    # sequence numbers are handed out at DISPATCH (deterministic under any
    # completion order), so every dispatched item consumes one - including w3
    assert state["gate_sequence"] == 3

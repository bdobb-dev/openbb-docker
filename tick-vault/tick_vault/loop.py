"""Engine loop: the pure priority decision (`run_cycle`) plus a thin,
impure driver (`run_loop`) that turns those decisions into real
`tick_vault.engine.fetch_week` / `tick_vault.settle.settle_pass` /
`tick_vault.reconcile.Gate` calls (task-10 brief).

Design seams (binding, matching Plan-2's established pattern - task-7's
`ManifestBuilder`, task-8's `settle_pass`, task-9's `Gate`)
-------------------------------------------------------------------------
`LoopContext` bundles every seam the loop needs: transport/store (bronze
capture), `root` (the Delta lake path), readers/writers for the manifest
and `ops.ingestion_run`, the `SpanMemory`/`Budget`/`Gate` instances the
rest of Plan-2 already defined, an injectable `clock() -> datetime` (never
`datetime.now()` called directly anywhere in this module's pure code), and
a small `LoopConfig`. None of `LoopContext`'s fields default to a real
Delta-backed callable at class-definition time - callers wire up
production readers/writers explicitly (this module has no
`_default_manifest_reader`-style lazy-pyarrow-import helpers of its own;
it is a consumer of `tick_vault.engine`/`tick_vault.settle`/
`tick_vault.reconcile`'s own seams, not a new source of them).

`run_cycle` is the ONLY place priority logic lives - pure, over a
`pandas.DataFrame` (manifest-shaped) + a `datetime` clock + a small
mutable `state` dict the caller owns (`last_daily_date`,
`last_settle_date_by_work_id`) - so the whole priority table is testable
with synthetic frames and fixed clocks, no I/O. `run_loop` is
deliberately thin: it re-reads the manifest, calls `run_cycle`, executes
exactly one `CycleAction` through the real components, and loops - all
per-cycle decision-making happens in `run_cycle`, never in `run_loop`.

Retry timestamp (binding resolution of the task brief's open question)
-------------------------------------------------------------------------
The brief flags "(3) FAILED rows not retried in 24h (needs a retryable
timestamp - manifest has updated_at_ts? read schemas.py; if absent use
attempts + a state dict...)". `tick_vault.schemas._OPS_BACKFILL_MANIFEST`
DOES declare `updated_at_ts` (`tick_vault.engine.update_manifest_row`
already stamps it on every fetch attempt, success or failure) - so this
module uses that column directly, no sidecar last-attempt state dict is
needed.

Settle-horizon due-tracking (state, not manifest columns)
-------------------------------------------------------------------------
`ops.backfill_manifest` has no "last settled at" column (settling doesn't
change a manifest row's `status` - a `COMPLETE` week stays `COMPLETE`
after being settled). "due for settle" therefore needs its own bookkeeping
outside the manifest: `state["last_settle_date_by_work_id"]` (a
`dict[work_id, date]`), owned by the caller (typically `run_loop`) and
threaded through `run_cycle` calls - a `COMPLETE` row within
`settle_horizon_days` of today is "due" iff it has not already been
settled TODAY. This is loop-local state, not persisted anywhere (a
process restart just re-settles today's live-edge weeks once more, which
is idempotent per `settle_pass`'s own diff-against-latest semantics).

Gate sequence numbering
-------------------------------------------------------------------------
`tick_vault.reconcile.Gate.check(sequence_number, report)` needs a
monotonic per-process tranche counter (task-9: "sequence numbers
0..n_gated-1, tracked by the caller's ingest loop - this class holds no
state of its own"). `run_loop` owns that counter in `state["gate_sequence"]`,
incrementing it once per tranche actually reconciled (never on a SLEEP/
DAILY/SETTLE cycle, which are not reconciled tranches).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from zoneinfo import ZoneInfo

import pandas as pd

from tick_vault.engine import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    Budget,
    SpanMemory,
    _REDACTED,
    fetch_week,
    update_manifest_row,
)
from tick_vault.ids import new_id
from tick_vault.reconcile import GATE_REASON_HOLD, Gate, GateDecision

UTC = dt.timezone.utc
_NY_TZ = ZoneInfo("America/New_York")

DEFAULT_SETTLE_HORIZON_DAYS = 14
DEFAULT_DAILY_AFTER_NY = "02:05"
DEFAULT_N_GATED = 2000
DEFAULT_SLEEP_SECONDS = 900.0
RETRY_AFTER = dt.timedelta(hours=24)

CYCLE_DAILY = "DAILY"
CYCLE_FETCH = "FETCH"
CYCLE_SETTLE = "SETTLE"
CYCLE_RETRY = "RETRY"
CYCLE_SLEEP = "SLEEP"


# ---------------------------------------------------------------------------
# small shared helpers (duplicated from tick_vault.engine by the same
# convention that module documents - see its own module docstring)
# ---------------------------------------------------------------------------

def _is_missing(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return value == ""


def _to_date(value) -> "dt.date | None":
    if _is_missing(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().date()
    if isinstance(value, dt.date):
        return value
    return value


def _to_utc_ts(value) -> "dt.datetime | None":
    if _is_missing(value):
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if hasattr(value, "to_pydatetime"):
        return _to_utc_ts(value.to_pydatetime())
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=UTC)
    return value


# ---------------------------------------------------------------------------
# LoopConfig / LoopContext
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoopConfig:
    settle_horizon_days: int = DEFAULT_SETTLE_HORIZON_DAYS
    daily_after_ny: str = DEFAULT_DAILY_AFTER_NY
    n_gated: int = DEFAULT_N_GATED
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS


@dataclass
class LoopContext:
    """Bundles every seam `run_loop` needs. Only `root` is required to be
    real; every reader/writer/component is a plain attribute a caller sets
    up (production wiring - real Delta reads/writes, a real
    `tick_vault.capture.CaptureStore`/`UrlLibTransport` - lives in the CLI
    layer, `tick_vault.cli`, not here).
    """

    transport: "object | None" = None
    store: "object | None" = None
    root: str = ""
    legacy_progress_root: "str | None" = None

    # ops.backfill_manifest
    manifest_reader: "object | None" = None  # (root) -> pandas.DataFrame
    manifest_row_writer: "object | None" = None  # (root, updated_row: dict) -> None

    # ops.ingestion_run
    ingestion_run_writer: "object | None" = None  # (root, table, df) -> None

    # fetch_week / settle_pass seams
    existing_ticks_reader: "object | None" = None
    existing_latest_reader: "object | None" = None
    tick_writer: "object | None" = None
    settle_writer: "object | None" = None
    events_writer: "object | None" = None
    verifier: "object | None" = None

    # reconcile (task-9) - a single injectable hook, kept optional: real
    # OHLCV-aggregation + ReferenceClient wiring is Plan-3's job (see
    # module docstring's "thin" requirement); when set, it is called as
    # `reconcile_step(ctx, item, fetch_result) -> GateDecision | None`
    # after every FETCH/RETRY that reached COMPLETE/PARTIAL, and `None`
    # means "no reconciliation performed for this tranche" (gate skipped).
    reconcile_step: "object | None" = None

    api_token: str = _REDACTED
    span_memory: SpanMemory = field(default_factory=SpanMemory)
    budget: Budget = field(default_factory=Budget)
    gate: Gate = field(default_factory=Gate)
    clock: "object | None" = None  # () -> datetime (tz-aware UTC)
    sleeper: "object | None" = None  # (seconds: float) -> None
    config: LoopConfig = field(default_factory=LoopConfig)

    def now(self) -> dt.datetime:
        clock = self.clock or (lambda: dt.datetime.now(UTC))
        value = clock()
        return _to_utc_ts(value)

    def sleep(self, seconds: float) -> None:
        sleeper = self.sleeper or time.sleep
        sleeper(seconds)


# ---------------------------------------------------------------------------
# CycleAction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CycleAction:
    kind: str  # DAILY | FETCH | SETTLE | RETRY | SLEEP
    item: "dict | None" = None
    reason: str = ""
    seconds: "float | None" = None


# ---------------------------------------------------------------------------
# run_cycle (pure)
# ---------------------------------------------------------------------------

def _week_is_complete(week_monday: "dt.date | None", now_utc: dt.datetime) -> bool:
    """T+1 rule: the week starting `week_monday` (Mon..Sun) is considered
    complete for fetch purposes once its Saturday 08:00 UTC has passed
    (`week_monday + 5 days`, 08:00 UTC) - one day after the Friday close,
    giving the vendor time to finalize the week's tape."""
    if week_monday is None:
        return False
    saturday_utc = dt.datetime(
        week_monday.year, week_monday.month, week_monday.day, 8, 0, tzinfo=UTC
    ) + dt.timedelta(days=5)
    return now_utc >= saturday_utc


def _parse_hh_mm(value: str) -> "tuple[int, int]":
    hh, mm = value.split(":")
    return int(hh), int(mm)


def run_cycle(ctx: LoopContext, manifest_df: "pd.DataFrame | None", state: dict) -> CycleAction:
    """Pure decision function: given the current `ops.backfill_manifest`
    frame, `ctx.clock()`/`ctx.config`, and the caller's small mutable
    `state` dict (`last_daily_date`, `last_settle_date_by_work_id`),
    return the single next `CycleAction` to execute, per this priority
    order (task-10 brief, spec section 5/sip_backfill):

    0. Daily pass, once per (NY) calendar day, after `config.daily_after_ny`.
    1. The newest `PENDING` week whose data is complete per the T+1 rule
       (Saturday 08:00 UTC after that week's Friday).
    2. A `COMPLETE` week due for settle: `week_monday` within
       `config.settle_horizon_days` of today, not yet settled today (a
       week from 2019 is never "within 14 days of today" - it never
       settles, by construction, no special-casing needed).
    3. A `FAILED` row whose `updated_at_ts` is more than 24h old (oldest
       first, so the longest-stuck item gets retried first).
    4. Otherwise, the newest remaining `PENDING` week regardless of the
       T+1 completeness gate (the historical backward walk - once the
       live-edge weeks near "today" are exhausted or not yet due, this
       keeps the manifest queue draining from newest to oldest).
    5. `CycleAction(kind='SLEEP', seconds=900)` if nothing above applies.

    `state` is read AND mutated for `last_daily_date`/
    `last_settle_date_by_work_id` defaults (`setdefault`) but this
    function never advances them past their defaults - only the caller
    executing the returned action (`run_loop`) does that, once the action
    has actually been carried out.
    """
    state.setdefault("last_daily_date", None)
    state.setdefault("last_settle_date_by_work_id", {})

    now_utc = ctx.now()
    ny_now = now_utc.astimezone(_NY_TZ)

    daily_hh, daily_mm = _parse_hh_mm(ctx.config.daily_after_ny)
    daily_threshold = ny_now.replace(hour=daily_hh, minute=daily_mm, second=0, microsecond=0)
    if ny_now >= daily_threshold and state["last_daily_date"] != ny_now.date():
        return CycleAction(kind=CYCLE_DAILY, reason=f"daily pass due for {ny_now.date().isoformat()}")

    if manifest_df is None or manifest_df.empty:
        return CycleAction(kind=CYCLE_SLEEP, seconds=ctx.config.sleep_seconds, reason="empty manifest")

    df = manifest_df
    pending = df[df["status"] == STATUS_PENDING]

    # priority 1: newest complete unloaded week
    if not pending.empty:
        complete_mask = pending["week_monday"].apply(lambda wm: _week_is_complete(_to_date(wm), now_utc))
        complete_pending = pending[complete_mask]
        if not complete_pending.empty:
            row = complete_pending.sort_values("week_monday", ascending=False).iloc[0]
            return CycleAction(kind=CYCLE_FETCH, item=row.to_dict(), reason="newest complete unloaded week")

    # priority 2: week due for settle (live-edge only)
    complete_rows = df[df["status"] == STATUS_COMPLETE]
    if not complete_rows.empty:
        today = now_utc.date()
        horizon = ctx.config.settle_horizon_days
        last_settle = state["last_settle_date_by_work_id"]

        def _due(row) -> bool:
            week_monday = _to_date(row["week_monday"])
            if week_monday is None:
                return False
            age_days = (today - week_monday).days
            if age_days < 0 or age_days > horizon:
                return False
            return last_settle.get(row["work_id"]) != today

        due_mask = complete_rows.apply(_due, axis=1)
        due = complete_rows[due_mask]
        if not due.empty:
            row = due.sort_values("week_monday", ascending=True).iloc[0]
            return CycleAction(kind=CYCLE_SETTLE, item=row.to_dict(), reason="week due for settle")

    # priority 3: FAILED rows not retried in 24h
    failed = df[df["status"] == STATUS_FAILED]
    if not failed.empty:
        def _stale(updated_at) -> bool:
            ts = _to_utc_ts(updated_at)
            if ts is None:
                return True
            return (now_utc - ts) >= RETRY_AFTER

        retryable = failed[failed["updated_at_ts"].apply(_stale)]
        if not retryable.empty:
            row = retryable.sort_values("updated_at_ts", ascending=True).iloc[0]
            return CycleAction(kind=CYCLE_RETRY, item=row.to_dict(), reason="failed retry >24h")

    # priority 4: next PENDING week backwards (no completeness gate)
    if not pending.empty:
        row = pending.sort_values("week_monday", ascending=False).iloc[0]
        return CycleAction(kind=CYCLE_FETCH, item=row.to_dict(), reason="backward historical walk")

    return CycleAction(kind=CYCLE_SLEEP, seconds=ctx.config.sleep_seconds, reason="nothing to do")


# ---------------------------------------------------------------------------
# apply_gate_decision (pure)
# ---------------------------------------------------------------------------

def apply_gate_decision(manifest_row: dict, decision: GateDecision) -> dict:
    """Apply a `tick_vault.reconcile.Gate.check(...)` ruling to one
    manifest row's mutable fields: `hold=True` forces the row to
    `PARTIAL` with `last_error='RECONCILE_HOLD'` (the row is prevented
    from reaching `COMPLETE` even though its fetch itself succeeded);
    `hold=False` leaves the row exactly as it was (whatever
    `update_manifest_row` already set from the `FetchResult`)."""
    updated = dict(manifest_row)
    if decision.hold:
        updated["status"] = STATUS_PARTIAL
        updated["last_error"] = GATE_REASON_HOLD
    return updated


# ---------------------------------------------------------------------------
# ops.ingestion_run open/close
# ---------------------------------------------------------------------------

_INGESTION_RUN_COLUMNS = [
    "run_id", "run_type", "code_git_commit", "config_hash",
    "started_at_ts", "completed_at_ts", "status",
    "source_endpoint", "source_version", "parser_version",
    "availability_policy_version", "input_manifest_range_json",
    "output_table_versions_json", "quality_check_summary_json",
    "error_summary",
]

RUN_STATUS_RUNNING = "RUNNING"
RUN_STATUS_COMPLETED = "COMPLETED"
RUN_STATUS_FAILED = "FAILED"


def _config_hash(config: LoopConfig) -> str:
    payload = json.dumps(asdict(config), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git_commit() -> str:
    import os

    return os.environ.get("GIT_COMMIT", "unknown")


def _write_ingestion_run_row(ctx: LoopContext, row: dict) -> None:
    full_row = {col: row.get(col) for col in _INGESTION_RUN_COLUMNS}
    df = pd.DataFrame([full_row], columns=_INGESTION_RUN_COLUMNS)
    writer = ctx.ingestion_run_writer
    if writer is None:
        from tick_vault.capture import append_rows as writer  # lazy default
    writer(ctx.root, "ops.ingestion_run", df)


def open_run(ctx: LoopContext, run_type: str, *, note: "str | None" = None) -> str:
    """Append a `RUNNING` `ops.ingestion_run` row and return its `run_id`.
    `note` (e.g. the `backfill --loop --i-know-what-im-doing` operator
    override) is recorded in `quality_check_summary_json` as
    `{"note": note}` - there is no dedicated free-text "note" column on
    `ops.ingestion_run` (see `tick_vault.schemas._OPS_INGESTION_RUN`), and
    that field is otherwise unused at open-time."""
    run_id = new_id("run")
    now = ctx.now()
    row = {
        "run_id": run_id,
        "run_type": run_type,
        "code_git_commit": _git_commit(),
        "config_hash": _config_hash(ctx.config),
        "started_at_ts": now,
        "completed_at_ts": None,
        "status": RUN_STATUS_RUNNING,
        "quality_check_summary_json": json.dumps({"note": note}) if note else None,
    }
    _write_ingestion_run_row(ctx, row)
    return run_id


def close_run(
    ctx: LoopContext,
    run_id: str,
    *,
    status: str = RUN_STATUS_COMPLETED,
    error_summary: "str | None" = None,
    run_type: str = "backfill_loop",
) -> None:
    """Append the closing `ops.ingestion_run` row for `run_id` (same
    `run_id`, terminal `status`, `completed_at_ts` set). `ops.
    ingestion_run` is append-only, like every other table in this
    codebase (`tick_vault.capture.append_rows`'s own contract) - a reader
    resolves "the" row for a `run_id` by taking the one with the latest
    `completed_at_ts`/`started_at_ts` (production query concern, not this
    module's)."""
    now = ctx.now()
    row = {
        "run_id": run_id,
        "run_type": run_type,
        "code_git_commit": _git_commit(),
        "config_hash": _config_hash(ctx.config),
        "started_at_ts": now,
        "completed_at_ts": now,
        "status": status,
        "error_summary": error_summary,
    }
    _write_ingestion_run_row(ctx, row)


# ---------------------------------------------------------------------------
# run_loop (thin, impure driver)
# ---------------------------------------------------------------------------

def _execute_fetch_like(ctx: LoopContext, action: CycleAction, state: dict) -> None:
    item = action.item
    result = fetch_week(
        ctx.transport,
        ctx.store,
        ctx.root,
        item,
        span_memory=ctx.span_memory,
        budget=ctx.budget,
        now=ctx.now(),
        api_token=ctx.api_token,
        writer=ctx.tick_writer,
        existing_ticks_reader=ctx.existing_ticks_reader,
        verifier=ctx.verifier,
    )
    updated_row = update_manifest_row(item, result, completed_at=ctx.now())

    if result.status in (STATUS_COMPLETE, STATUS_PARTIAL) and ctx.reconcile_step is not None:
        decision = ctx.reconcile_step(ctx, item, result)
        if decision is not None:
            state["gate_sequence"] = state.get("gate_sequence", 0) + 1
            updated_row = apply_gate_decision(updated_row, decision)

    if ctx.manifest_row_writer is not None:
        ctx.manifest_row_writer(ctx.root, updated_row)


def _execute_settle(ctx: LoopContext, action: CycleAction, state: dict) -> None:
    from tick_vault.settle import settle_pass

    item = action.item
    if ctx.existing_latest_reader is None:
        # Not wired up (Plan-3 concern) - nothing safe to do.
        return
    settle_pass(
        ctx.transport,
        ctx.store,
        ctx.root,
        item,
        existing_latest_reader=ctx.existing_latest_reader,
        span_memory=ctx.span_memory,
        budget=ctx.budget,
        token=ctx.api_token,
        writer=ctx.settle_writer,
        events_writer=ctx.events_writer,
        now=ctx.now(),
    )
    today = ctx.now().date()
    state.setdefault("last_settle_date_by_work_id", {})[item.get("work_id")] = today


def _execute_daily(ctx: LoopContext, action: CycleAction, state: dict) -> None:
    from tick_vault.settle import daily_pass

    now = ctx.now()
    state["last_daily_date"] = now.astimezone(_NY_TZ).date()

    if ctx.existing_latest_reader is None or ctx.manifest_reader is None:
        return

    manifest_df = ctx.manifest_reader(ctx.root)
    if manifest_df is None or manifest_df.empty:
        return
    horizon = ctx.config.settle_horizon_days
    today = now.date()
    complete_rows = manifest_df[manifest_df["status"] == STATUS_COMPLETE]

    def _live_edge(row) -> bool:
        week_monday = _to_date(row["week_monday"])
        if week_monday is None:
            return False
        age_days = (today - week_monday).days
        return 0 <= age_days <= horizon

    items = [
        row.to_dict()
        for _, row in complete_rows[complete_rows.apply(_live_edge, axis=1)].iterrows()
    ]
    if not items:
        return
    daily_pass(
        ctx.transport,
        ctx.store,
        ctx.root,
        items,
        existing_latest_reader=ctx.existing_latest_reader,
        span_memory=ctx.span_memory,
        budget=ctx.budget,
        token=ctx.api_token,
        writer=ctx.settle_writer,
        events_writer=ctx.events_writer,
        now=now,
    )
    for item in items:
        state.setdefault("last_settle_date_by_work_id", {})[item.get("work_id")] = today


def _execute_action(ctx: LoopContext, action: CycleAction, state: dict) -> None:
    if action.kind in (CYCLE_FETCH, CYCLE_RETRY):
        _execute_fetch_like(ctx, action, state)
    elif action.kind == CYCLE_SETTLE:
        _execute_settle(ctx, action, state)
    elif action.kind == CYCLE_DAILY:
        _execute_daily(ctx, action, state)
    elif action.kind == CYCLE_SLEEP:
        ctx.sleep(action.seconds or ctx.config.sleep_seconds)
    else:
        raise ValueError(f"unknown CycleAction.kind: {action.kind!r}")


def run_loop(ctx: LoopContext, *, max_cycles: "int | None" = None, run_type: str = "backfill_loop") -> dict:
    """Thin, impure driver: opens one `ops.ingestion_run` row, repeatedly
    reads the manifest / calls `run_cycle` / executes exactly one
    `CycleAction` through the real components (catching and logging any
    per-action exception so one stuck item never kills the whole loop),
    and closes the `ops.ingestion_run` row on exit (including on an
    uncaught exception escaping this function itself - `close_run` runs
    in a `finally`).

    `max_cycles=None` loops forever (real production use, killed
    externally); a finite `max_cycles` is for tests/one-shot CLI verbs.
    Returns a small summary dict (`{"cycles", "errors"}`) - callers that
    want more should read `ops.ingestion_run`/`ops.backfill_manifest`
    themselves; this return value is a convenience, not a contract.
    """
    state: dict = {
        "last_daily_date": None,
        "last_settle_date_by_work_id": {},
        "gate_sequence": 0,
        "errors": [],
    }
    run_id = open_run(ctx, run_type)
    status = RUN_STATUS_COMPLETED
    cycles = 0
    try:
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            manifest_df = ctx.manifest_reader(ctx.root) if ctx.manifest_reader else None
            action = run_cycle(ctx, manifest_df, state)
            try:
                _execute_action(ctx, action, state)
            except Exception as exc:  # per-action isolation, not caller-fatal
                state["errors"].append(f"{action.kind}: {type(exc).__name__}: {exc}")
    except BaseException:
        status = RUN_STATUS_FAILED
        raise
    finally:
        close_run(
            ctx, run_id, status=status,
            error_summary="; ".join(state["errors"]) if state["errors"] else None,
            run_type=run_type,
        )
    return {"cycles": cycles, "errors": state["errors"]}

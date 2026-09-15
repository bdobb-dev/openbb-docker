"""`vault` CLI entry point (task-10 brief; `[project.scripts] vault =
tick_vault.cli:main`).

Every verb builds its `tick_vault.loop.LoopContext` from the environment
AT CALL TIME (`EOD_API_KEY`, `VAULT_ROOT`, `LEGACY_ROOT` - never read at
import time, so a test can monkeypatch `os.environ` per-test without
reloading this module) via `build_ctx_from_env()`, then dispatches to a
handler function `handle_<verb>(args, ctx_factory)`. Handlers are plain
module-level functions (not closures/methods) specifically so tests can
`monkeypatch.setattr(cli, "handle_backfill", fake)` and assert `main()`
calls the replacement with the parsed `args` - `main()` looks handlers up
by name via `globals()` at call time (never binds a handler reference
into the parser at `build_parser()` time), so a monkeypatch always takes
effect regardless of when it's applied relative to parsing.

The `backfill --loop` calibration gate (binding - the ~300k-call go/no-go
protection)
-------------------------------------------------------------------------
`backfill --loop` refuses to start the historical walk unless a
calibration report file already exists at `calibration_report_path()`
(`CALIBRATION_REPORT` env var, default `DEFAULT_CALIBRATION_REPORT`) -
see `tick_vault.calibrate.write_calibration_report`, produced by `vault
calibrate`. `--i-know-what-im-doing` overrides the refusal but is itself
logged: a `GATE_OVERRIDE` `ops.ingestion_run` row (opened and immediately
closed via `tick_vault.loop.open_run`/`close_run`) records the operator's
override as a permanent audit trail before the real loop starts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import pandas as pd

from tick_vault import calibrate as calibrate_mod
from tick_vault import loop as loop_mod
from tick_vault.engine import Budget, ManifestBuilder, SpanMemory, _REDACTED, session_loaded
from tick_vault.reconcile import Gate

DEFAULT_CALIBRATION_REPORT = "docs/superpowers/verification/ep15-calibration.md"

GATE_REFUSAL_MESSAGE = (
    "backfill --loop refused: no calibration report found at {path}. "
    "Run `vault calibrate --week <YYYY-MM-DD>` first (see "
    "tick_vault.calibrate), or pass --i-know-what-im-doing to override "
    "(the override is logged to ops.ingestion_run)."
)

RECONCILE_REFUSAL_MESSAGE = (
    "backfill --loop refused: reconciliation Phase-A is not configured "
    "on this LoopContext (ctx.reconcile_step is None - typically because "
    "no EOD_API_KEY reference credential is set, see "
    "tick_vault.cli.build_ctx_from_env). Set EOD_API_KEY so the OHLCV "
    "reconciliation gate (tick_vault.reconcile.Gate) can actually run, or "
    "pass --i-know-what-im-doing to override (the override is logged to "
    "ops.ingestion_run)."
)


def calibration_report_path() -> str:
    return os.environ.get("CALIBRATION_REPORT", DEFAULT_CALIBRATION_REPORT)


# ---------------------------------------------------------------------------
# LoopContext factory (real Delta-backed wiring; lazy pyarrow/deltalake
# imports, matching the rest of this codebase's convention)
# ---------------------------------------------------------------------------

def _default_manifest_reader(root: str) -> pd.DataFrame:
    from deltalake import DeltaTable

    return DeltaTable(f"{root}/ops/backfill_manifest").to_pandas()


def _default_manifest_row_writer(root: str, updated_row: dict) -> None:
    """Production manifest-row update: read the whole `ops.
    backfill_manifest` table, replace the row matching `work_id` (the
    manifest's own single-column natural key - see `tick_vault.schemas`'s
    `_OPS_BACKFILL_MANIFEST`, `work_id` non-nullable and unique per
    `(listing_id, week_monday)` pair at generation time), and overwrite.
    `ops.backfill_manifest` is the one table in this codebase whose rows
    are mutated in place (`status`/`attempts`/... - see
    `tick_vault.engine`'s own module docstring); since `deltalake` has no
    per-row `UPDATE` primitive as simple as `append_rows`, a full-table
    overwrite is this module's (documented, Plan-3-revisitable) choice
    for a manifest small enough to fit in memory (~522 weeks x ~505
    symbols, a few hundred thousand rows).

    WARNING - concurrency: this read-modify-overwrite is safe ONLY under
    the single-CLI-invocation ops rule (one `vault backfill --loop`
    process, or one-shot verb, ever writing to a given `root` at a time).
    It is NOT optimistic-concurrency-safe: two concurrent writers can
    each read the same pre-update table, race to overwrite, and one
    writer's update silently disappears (no version check, no retry). A
    real per-row/optimistic-concurrency mechanism is deferred to Plan 3;
    until then, do not run multiple `vault` processes against the same
    `VAULT_ROOT` concurrently."""
    import pyarrow as pa
    from deltalake import write_deltalake

    from tick_vault.schemas import PARTITIONING, SCHEMAS

    table = "ops.backfill_manifest"
    schema = SCHEMAS[table]
    df = _default_manifest_reader(root)
    work_id = updated_row.get("work_id")
    mask = df["work_id"] == work_id
    if mask.any():
        for col, value in updated_row.items():
            if col in df.columns:
                df.loc[mask, col] = value
    else:
        df = pd.concat([df, pd.DataFrame([updated_row])], ignore_index=True)
    clean = df.astype(object).where(df.notna(), None)
    arrow_table = pa.Table.from_pylist(clean.to_dict("records"), schema=schema)
    write_deltalake(f"{root}/ops/backfill_manifest", arrow_table, mode="overwrite", partition_by=PARTITIONING.get(table))


def _default_existing_ticks_reader(
    root: str, listing_id: str, week_monday: "dt.date | None" = None
) -> "pd.DataFrame | None":
    """Real reader for `fetch_week`'s/reconciliation's `existing_ticks_
    reader(root, listing_id) -> pandas.DataFrame` seam: this listing's
    rows already written to `silver.us_trade_tick_version` (lazy
    deltalake import, matching this module's own convention). Returns
    `None` if the table does not exist yet (first run on a fresh
    `VAULT_ROOT`) rather than raising - `fetch_week`/`_pandas_reconcile_
    step` both already treat a `None`/empty return as "nothing written
    yet"."""
    from deltalake import DeltaTable

    try:
        table = DeltaTable(f"{root}/silver/us_trade_tick_version")
    except Exception:
        return None
    # Push the filter down. Loading the whole table and filtering in pandas
    # grew with every symbol written and got the Phase-0 calibration killed
    # (50M rows / 9.5 GB by symbol 138, read twice per symbol). `trade_date`
    # is the partition column, so the week bound also skips every other
    # day's files; Sun..Sun covers every NY session date a UTC week yields.
    filters = [("listing_id", "=", listing_id)]
    if week_monday is not None:
        filters += [
            ("trade_date", ">=", week_monday - dt.timedelta(days=1)),
            ("trade_date", "<=", week_monday + dt.timedelta(days=6)),
        ]
    return table.to_pandas(filters=filters)


def _default_splits_reader(root: str, symbol: str) -> "pd.DataFrame | None":
    """Latest captured split history for `symbol` (written by `vault
    reference --sync` into bronze.eodhd_corporate_actions_capture), parsed
    to `date, factor`; `None` when nothing was captured."""
    import gzip
    import json

    from deltalake import DeltaTable

    from tick_vault.eodhd_reference import parse_splits

    try:
        caps = DeltaTable(f"{root}/bronze/eodhd_corporate_actions_capture").to_pandas(
            filters=[("request_symbol", "=", f"{symbol}.US")]
        )
    except Exception:
        return None
    caps = caps[(caps["endpoint"] == "splits") & caps["raw_payload_uri"].notna()]
    if caps.empty:
        return None
    with gzip.open(caps.sort_values("observed_at_ts").iloc[-1]["raw_payload_uri"]) as f:
        return parse_splits(json.loads(f.read()))


def _item_get(item, key: str, default=None):
    if item is None:
        return default
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _pandas_reconcile_step(
    ctx: loop_mod.LoopContext, item, fetch_result, sequence_number: int
):
    """Minimal pandas Phase-A reconciliation hook (C2 wiring - Plan-2's
    reconciliation gate, previously defined in `tick_vault.reconcile` but
    never actually called from anywhere in production).

    Reads back this tranche's just-written daily ticks via `ctx.
    existing_ticks_reader` (real wiring: `_default_existing_ticks_
    reader` above), aggregates them to daily OHLCV bars (`tick_vault.
    reconcile.aggregate_bars`), fetches the vendor's EOD reference bars
    for the same ISO week (`ReferenceClient.get_eod`), compares the two
    (`tick_vault.reconcile.reconcile_tranche`), and rules on the tranche
    via `ctx.gate.check(sequence_number, report)`.

    Returns `None` ("no reconciliation performed for this tranche", per
    `LoopContext.reconcile_step`'s own contract - the gate sequence is
    NOT advanced for a `None` return, see `loop._execute_fetch_like`)
    when `ctx.existing_ticks_reader` is unwired, the item is missing a
    vendor symbol/week, or nothing has actually been written for this
    listing yet - all "can't reconcile, not a divergence" cases, never
    silently treated as a HOLD."""
    from tick_vault import reconcile as reconcile_mod
    from tick_vault.eodhd_reference import ReferenceClient

    if ctx.existing_ticks_reader is None:
        return None

    listing_id = _item_get(item, "listing_id")
    symbol = _item_get(item, "vendor_symbol_at_date")
    week_monday = _item_get(item, "week_monday")
    if listing_id is None or symbol is None or week_monday is None:
        return None

    ticks_df = ctx.existing_ticks_reader(ctx.root, listing_id, week_monday=week_monday)
    if ticks_df is None or len(ticks_df) == 0:
        return None

    our_daily_df, our_daily_stats = reconcile_mod.aggregate_bars(
        ticks_df, interval="1d", return_stats=True
    )
    if our_daily_df.empty:
        return None

    from_date = week_monday
    to_date = week_monday + dt.timedelta(days=6)
    client = ReferenceClient(ctx.transport, ctx.store, ctx.api_token)
    ref_eod_df, _record = client.get_eod(
        symbol, from_date, to_date, exchange=_item_get(item, "eodhd_exchange_code")
    )

    splits_reader = getattr(ctx, "splits_reader", None)
    splits_df = splits_reader(ctx.root, symbol) if splits_reader is not None else None
    report = reconcile_mod.reconcile_tranche(
        item, our_daily_df, ref_eod_df, our_daily_stats=our_daily_stats, splits_df=splits_df
    )
    # Persist the divergence rows: before this, a HOLD left no trace in
    # ops.data_quality_issue (Phase-0 calibration: 500 holds, 0 rows).
    dq_issue_writer = getattr(ctx, "dq_issue_writer", None)
    if dq_issue_writer is not None and len(report.issues_df):
        dq_issue_writer(ctx.root, "ops.data_quality_issue", report.issues_df)
    return ctx.gate.check(sequence_number, report)


def build_ctx_from_env() -> loop_mod.LoopContext:
    """Build a real, Delta-backed `LoopContext` from `EOD_API_KEY`/
    `VAULT_ROOT`/`LEGACY_ROOT`, read fresh on every call (never cached at
    import time).

    Reconciliation Phase-A wiring (C2 fix-round): `ctx.reconcile_step` is
    wired to `_pandas_reconcile_step` whenever a reference config is
    available - operationally, whenever a real `EOD_API_KEY` is set (the
    same credential `ReferenceClient.get_eod` needs; without it there is
    no reference data to reconcile against, so wiring the hook would only
    ever return `None`). `ctx.existing_ticks_reader` is wired to
    `_default_existing_ticks_reader` unconditionally - it is also the
    read-back seam `fetch_week` itself uses for idempotent re-runs, not
    only a reconciliation concern."""
    from tick_vault.capture import CaptureStore, UrlLibTransport, append_rows

    root = os.environ.get("VAULT_ROOT", "./vault_data")
    api_key = os.environ.get("EOD_API_KEY", _REDACTED)
    legacy_root = os.environ.get("LEGACY_ROOT")
    reference_configured = bool(api_key) and api_key != _REDACTED

    transport = UrlLibTransport()
    return loop_mod.LoopContext(
        transport=transport,
        store=CaptureStore(root),
        root=root,
        legacy_progress_root=legacy_root,
        manifest_reader=_default_manifest_reader,
        manifest_row_writer=_default_manifest_row_writer,
        existing_ticks_reader=_default_existing_ticks_reader,
        reconcile_step=_pandas_reconcile_step if reference_configured else None,
        dq_issue_writer=append_rows,
        splits_reader=_default_splits_reader,
        session_probe=lambda day: session_loaded(transport, day, api_key),
        api_token=api_key,
        span_memory=SpanMemory(),
        budget=Budget(),
        gate=Gate(),
        config=loop_mod.LoopConfig(),
    )


def _real_ctx() -> loop_mod.LoopContext:
    """`main`'s default ctx factory: the env-built ctx, with every table
    created on first use - on a fresh VAULT_ROOT each verb's first Delta
    read otherwise fails with TableNotFoundError."""
    from tick_vault.schemas import create_all

    ctx = build_ctx_from_env()
    create_all(ctx.root)
    return ctx


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

def run_backfill_loop(ctx: loop_mod.LoopContext) -> int:
    """Separated from `handle_backfill` so tests can monkeypatch just
    this function (a fake loop handler) while still exercising the real
    calibration-gate check in `handle_backfill` itself."""
    loop_mod.run_loop(ctx)
    return 0


def run_backfill_week(ctx: loop_mod.LoopContext, week: str) -> int:
    from tick_vault.engine import fetch_week, update_manifest_row

    manifest_df = ctx.manifest_reader(ctx.root) if ctx.manifest_reader else None
    if manifest_df is None or manifest_df.empty:
        print(f"no manifest rows for week {week}")
        return 1
    week_date = dt.date.fromisoformat(week)
    rows = manifest_df[manifest_df["week_monday"] == week_date]
    for _, row in rows.iterrows():
        item = row.to_dict()
        result = fetch_week(
            ctx.transport, ctx.store, ctx.root, item,
            span_memory=ctx.span_memory, budget=ctx.budget, now=ctx.now(),
            api_token=ctx.api_token, writer=ctx.tick_writer,
            existing_ticks_reader=ctx.existing_ticks_reader, verifier=ctx.verifier,
        )
        updated = update_manifest_row(item, result, completed_at=ctx.now())
        if ctx.manifest_row_writer is not None:
            ctx.manifest_row_writer(ctx.root, updated)
    return 0


def run_backfill_until(ctx: loop_mod.LoopContext, until: str) -> int:
    """Run `run_cycle`-driven cycles (via `run_loop`) until the backward
    historical walk reaches `until` (an ISO date) or the manifest has no
    more PENDING work at/after it. A thin convenience over `run_loop`
    with a generous `max_cycles` bound (production long-running use
    should prefer `--loop`, which runs forever)."""
    until_date = dt.date.fromisoformat(until)
    manifest_df = ctx.manifest_reader(ctx.root) if ctx.manifest_reader else None
    if manifest_df is None or manifest_df.empty:
        return 0
    pending = manifest_df[manifest_df["status"] == "PENDING"]
    remaining = pending[pending["week_monday"] >= until_date]
    max_cycles = max(len(remaining), 1)
    loop_mod.run_loop(ctx, max_cycles=max_cycles)
    return 0


def handle_backfill(args: argparse.Namespace, ctx_factory) -> int:
    if args.loop:
        report_path = calibration_report_path()
        report_exists = os.path.isfile(report_path)
        if not report_exists and not args.override:
            raise SystemExit(GATE_REFUSAL_MESSAGE.format(path=report_path))

        ctx = ctx_factory()

        # Second gate (C2 fix-round): `--loop` also refuses when
        # reconciliation Phase-A isn't wired on this ctx - running the
        # ~300k-call historical walk with no reconciliation gate at all
        # means every divergence goes completely unnoticed, which is the
        # exact risk `--i-know-what-im-doing` exists to make an operator
        # consciously accept, same as the calibration-report gate above.
        reconcile_configured = getattr(ctx, "reconcile_step", None) is not None
        if not reconcile_configured and not args.override:
            raise SystemExit(RECONCILE_REFUSAL_MESSAGE)

        missing_calibration = not report_exists
        missing_reconcile = not reconcile_configured
        if args.override and (missing_calibration or missing_reconcile):
            notes = []
            if missing_calibration:
                notes.append(f"no calibration report found at {report_path}")
            if missing_reconcile:
                notes.append("reconciliation Phase-A (ctx.reconcile_step) is not configured")
            note = "--i-know-what-im-doing override: " + "; ".join(notes)
            run_id = loop_mod.open_run(ctx, "GATE_OVERRIDE", note=note)
            loop_mod.close_run(ctx, run_id, status=loop_mod.RUN_STATUS_COMPLETED, run_type="GATE_OVERRIDE")
        return run_backfill_loop(ctx)
    if args.week:
        ctx = ctx_factory()
        return run_backfill_week(ctx, args.week)
    if args.until:
        ctx = ctx_factory()
        return run_backfill_until(ctx, args.until)
    raise SystemExit("backfill requires one of --loop, --week, or --until")


def handle_settle(args: argparse.Namespace, ctx_factory) -> int:
    from tick_vault.settle import settle_pass

    ctx = ctx_factory()
    manifest_df = ctx.manifest_reader(ctx.root) if ctx.manifest_reader else None
    if manifest_df is None or manifest_df.empty or ctx.existing_latest_reader is None:
        print(f"settle --week {args.week}: nothing to settle (no manifest rows or existing_latest_reader unconfigured)")
        return 1
    week_date = dt.date.fromisoformat(args.week)
    rows = manifest_df[manifest_df["week_monday"] == week_date]
    results = []
    for _, row in rows.iterrows():
        result = settle_pass(
            ctx.transport, ctx.store, ctx.root, row.to_dict(),
            existing_latest_reader=ctx.existing_latest_reader,
            span_memory=ctx.span_memory, budget=ctx.budget, token=ctx.api_token,
            writer=ctx.settle_writer, events_writer=ctx.events_writer, now=ctx.now(),
        )
        results.append(result)
    print(f"settle --week {args.week}: {len(results)} tranches processed")
    return 0


def handle_manifest(args: argparse.Namespace, ctx_factory) -> int:
    ctx = ctx_factory()
    if args.generate:
        first, last = args.generate
        run_id = loop_mod.open_run(ctx, "manifest_generate")
        status = loop_mod.RUN_STATUS_COMPLETED
        error_summary = None
        try:
            builder = ManifestBuilder(
                ctx.root,
                manifest_reader=ctx.manifest_reader,
            )
            new_rows = builder.generate(first, last)
        except Exception as exc:
            status = loop_mod.RUN_STATUS_FAILED
            error_summary = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            loop_mod.close_run(ctx, run_id, status=status, error_summary=error_summary, run_type="manifest_generate")
        print(f"manifest --generate {first} {last}: {len(new_rows)} new rows")
        return 0
    raise SystemExit("manifest requires --generate FIRST LAST")


def sync_reference(ctx: loop_mod.LoopContext) -> dict:
    """`vault reference --sync`'s body: capture the four reference feeds,
    build the security master and S&P 500 membership from them (the
    identity `vault manifest --generate` reads), then attach CUSIP/ISIN/CIK
    from each constituent's fundamentals. Every builder dedups, so a re-run
    only adds what changed. Separated from `handle_reference` so tests can
    monkeypatch it."""
    from tick_vault.eodhd_reference import ReferenceClient
    from tick_vault.master import MasterBuilder
    from tick_vault.membership import build_membership

    now = ctx.now()
    client = ReferenceClient(ctx.transport, ctx.store, ctx.api_token)
    symbols, symbols_rec = client.get_exchange_symbols(observed_at=now)
    delisted, delisted_rec = client.get_delisted(observed_at=now)
    changes, changes_rec = client.get_symbol_changes(observed_at=now)
    components, components_rec = client.get_index_components(observed_at=now)

    # ponytail: the master is scoped to the index universe (constituent
    # codes plus the old codes chained to them by symbol changes), not all
    # ~100k US codes; widen this when a second index lands.
    members = set(components["code"].dropna())
    changes = changes[changes["old"].isin(members) | changes["new"].isin(members)].sort_values("date")
    universe = members | set(changes["old"].dropna())

    master = MasterBuilder(ctx.root)
    master.upsert_from_symbols(symbols[symbols["code"].isin(universe)], symbols_rec.capture_id, now)
    master.upsert_from_symbols(delisted[delisted["code"].isin(universe)], delisted_rec.capture_id, now)
    master.apply_symbol_changes(changes, changes_rec.capture_id, now)
    report = build_membership(
        ctx.root, components, resolver=master.resolve_listing_and_instrument_at,
        capture_id=components_rec.capture_id, observed_at=now,
    )

    attached = with_splits = 0
    for code in sorted(members):
        fundamentals, rec = client.get_fundamentals(f"{code}.US", observed_at=now)
        fundamentals = fundamentals.dropna(subset=["code"])
        if not fundamentals.empty:
            master.attach_issue_ids(fundamentals, rec.capture_id, now)
            attached += 1
        # split history, read back by reconciliation (EODHD EOD volume is split-adjusted)
        splits, _ = client.get_splits(f"{code}.US", observed_at=now)
        with_splits += int(len(splits) > 0)
    return {
        "members": len(members), "resolved": report.resolved, "ambiguous": report.ambiguous,
        "unresolved": report.unresolved, "fundamentals": attached, "with_splits": with_splits,
    }


def handle_reference(args: argparse.Namespace, ctx_factory) -> int:
    ctx = ctx_factory()
    if args.sync:
        run_id = loop_mod.open_run(ctx, "reference_sync")
        status = loop_mod.RUN_STATUS_COMPLETED
        error_summary = None
        summary = None
        try:
            summary = sync_reference(ctx)
        except Exception as exc:
            status = loop_mod.RUN_STATUS_FAILED
            error_summary = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            loop_mod.close_run(ctx, run_id, status=status, error_summary=error_summary, run_type="reference_sync")
        print(f"reference --sync: {summary}")
        return 0
    raise SystemExit("reference requires --sync")


def handle_figi(args: argparse.Namespace, ctx_factory) -> int:
    from tick_vault.figi import FigiWorker

    ctx = ctx_factory()
    if args.drain:
        run_id = loop_mod.open_run(ctx, "figi_drain")
        status = loop_mod.RUN_STATUS_COMPLETED
        error_summary = None
        summary = None
        try:
            worker = FigiWorker(ctx.transport, ctx.store, ctx.root, ctx.api_token)
            summary = worker.run_batch()
        except Exception as exc:
            status = loop_mod.RUN_STATUS_FAILED
            error_summary = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            loop_mod.close_run(ctx, run_id, status=status, error_summary=error_summary, run_type="figi_drain")
        print(f"figi --drain: {summary}")
        return 0
    raise SystemExit("figi requires --drain")


def format_status(
    counts: dict,
    frontier_weeks: "tuple | None",
    open_dq_count: int,
    budget_state: dict,
) -> str:
    """Pure formatter: `counts` is `{status: row_count}`, `frontier_weeks`
    is `(oldest_pending_week, newest_pending_week)` or `None`,
    `budget_state` is e.g. `{"consecutive_waits": int,
    "max_consecutive_waits": int}`. Runnable/tested with plain dicts, no
    I/O."""
    lines = ["ops.backfill_manifest status:"]
    for status in sorted(counts):
        lines.append(f"  {status}: {counts[status]}")
    if frontier_weeks:
        oldest, newest = frontier_weeks
        lines.append(f"frontier (PENDING weeks): {oldest} .. {newest}")
    else:
        lines.append("frontier (PENDING weeks): none")
    lines.append(f"open data-quality issues: {open_dq_count}")
    waits = budget_state.get("consecutive_waits", 0)
    max_waits = budget_state.get("max_consecutive_waits", "?")
    lines.append(f"budget: {waits}/{max_waits} consecutive 429 waits")
    return "\n".join(lines)


def handle_status(args: argparse.Namespace, ctx_factory) -> int:
    ctx = ctx_factory()
    manifest_df = ctx.manifest_reader(ctx.root) if ctx.manifest_reader else None
    if manifest_df is None or manifest_df.empty:
        counts: dict = {}
        frontier = None
    else:
        counts = manifest_df["status"].value_counts().to_dict()
        pending = manifest_df[manifest_df["status"] == "PENDING"]
        frontier = (
            (pending["week_monday"].min(), pending["week_monday"].max())
            if not pending.empty else None
        )

    open_dq_count = 0
    dq_reader = getattr(ctx, "dq_issue_reader", None)
    if dq_reader is not None:
        dq_df = dq_reader(ctx.root)
        if dq_df is not None and not dq_df.empty:
            open_dq_count = int((dq_df["status"] == "OPEN").sum())
    else:  # nothing injects a reader in production: read the real table
        from tick_vault.status_app import _default_dq_open

        open_dq_count = _default_dq_open(ctx.root)

    budget_state = {
        "consecutive_waits": ctx.budget.consecutive_waits,
        "max_consecutive_waits": ctx.budget.max_consecutive_waits,
    }
    print(format_status(counts, frontier, open_dq_count, budget_state))
    return 0


def handle_calibrate(args: argparse.Namespace, ctx_factory) -> int:
    from tick_vault.engine import STATUS_COMPLETE, STATUS_PARTIAL, fetch_week

    ctx = ctx_factory()
    week_date = dt.date.fromisoformat(args.week)
    legacy_root = args.legacy_root or ctx.legacy_progress_root

    manifest_df = ctx.manifest_reader(ctx.root) if ctx.manifest_reader else None
    if manifest_df is None or manifest_df.empty:
        print(f"calibrate --week {args.week}: no manifest rows for that week; run `vault manifest --generate` first")
        return 1

    rows = manifest_df[manifest_df["week_monday"] == week_date]

    run_id = loop_mod.open_run(ctx, "calibrate")
    run_status = loop_mod.RUN_STATUS_COMPLETED
    run_error = None
    try:
        total_calls = 0
        total_wall_minutes = 0.0
        total_rows = 0
        symbols = 0
        # Reconciliation honesty (I3 fix-round): `calibrate` does NOT run
        # the OHLCV reconciliation gate on its own - it only calls
        # `ctx.reconcile_step` when the caller's `LoopContext` actually
        # has one wired (`tick_vault.cli.build_ctx_from_env` wires it iff
        # a reference config - EOD_API_KEY - is available, per the C2
        # fix-round). Either way, the written report says so explicitly
        # (see `calibrate_mod.write_calibration_report`'s `reconciliation`
        # argument) instead of silently implying the gate always ran.
        reconcile_configured = getattr(ctx, "reconcile_step", None) is not None
        tranches_checked = 0
        tranches_held = 0
        sequence_number = 0

        # Measured, not hardcoded: bytes_written is the real filesystem delta
        # under VAULT_ROOT across this calibration week's fetches (task-10
        # fix-round - a hardcoded 0 here made every report's projected
        # storage-TB figure always read zero). Pure `calibrate.dir_bytes`
        # does the actual counting; this handler just brackets the fetch
        # loop with before/after snapshots.
        bytes_before = calibrate_mod.dir_bytes(ctx.root)
        for _, row in rows.iterrows():
            item = row.to_dict()
            result = fetch_week(
                ctx.transport, ctx.store, ctx.root, item,
                span_memory=ctx.span_memory, budget=ctx.budget, now=ctx.now(),
                api_token=ctx.api_token, writer=ctx.tick_writer,
                existing_ticks_reader=ctx.existing_ticks_reader, verifier=ctx.verifier,
            )
            total_calls += result.captures
            total_wall_minutes += result.wall_minutes
            total_rows += result.rows_written
            symbols += 1

            if reconcile_configured and result.status in (STATUS_COMPLETE, STATUS_PARTIAL):
                decision = ctx.reconcile_step(ctx, item, result, sequence_number)
                if decision is not None:
                    sequence_number += 1
                    tranches_checked += 1
                    if decision.hold:
                        tranches_held += 1
        bytes_after = calibrate_mod.dir_bytes(ctx.root)
        bytes_written = max(bytes_after - bytes_before, 0)

        if symbols == 0:
            print(f"calibrate --week {args.week}: no symbols processed")
            run_status = loop_mod.RUN_STATUS_FAILED
            run_error = "no symbols processed"
            return 1

        measurements = calibrate_mod.CalibrationMeasurements(
            calls=total_calls,
            wall_minutes=total_wall_minutes,
            rows=total_rows,
            bytes_written=bytes_written,
            symbols=symbols,
            week=week_date,
        )
        legacy_df = calibrate_mod.read_legacy_wall_minutes(legacy_root)
        projection = calibrate_mod.calibration_projection(measurements, legacy_df=legacy_df)
        report_path = calibration_report_path()
        calibrate_mod.write_calibration_report(
            report_path, projection, measurements, legacy_df,
            reconciliation={
                "configured": reconcile_configured,
                "tranches_checked": tranches_checked,
                "held": tranches_held,
            },
        )
        print(f"calibrate --week {args.week}: report written to {report_path}")
        return 0
    except Exception as exc:
        run_status = loop_mod.RUN_STATUS_FAILED
        run_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        loop_mod.close_run(ctx, run_id, status=run_status, error_summary=run_error, run_type="calibrate")


HANDLER_NAMES = {
    "backfill": "handle_backfill",
    "settle": "handle_settle",
    "manifest": "handle_manifest",
    "reference": "handle_reference",
    "figi": "handle_figi",
    "status": "handle_status",
    "calibrate": "handle_calibrate",
}


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vault")
    sub = parser.add_subparsers(dest="command", required=True)

    p_backfill = sub.add_parser("backfill", help="run/advance the re-backfill engine")
    g = p_backfill.add_mutually_exclusive_group(required=True)
    g.add_argument("--loop", action="store_true", help="run the engine loop forever")
    g.add_argument("--week", help="backfill a single ISO week (YYYY-MM-DD Monday)")
    g.add_argument("--until", help="backfill backwards until this ISO date")
    p_backfill.add_argument(
        "--i-know-what-im-doing", dest="override", action="store_true",
        help="override the calibration-report gate for --loop (logged to ops.ingestion_run)",
    )

    p_settle = sub.add_parser("settle", help="settle one week's live-edge tranche")
    p_settle.add_argument("--week", required=True)

    p_manifest = sub.add_parser("manifest", help="generate ops.backfill_manifest rows")
    p_manifest.add_argument("--generate", nargs=2, metavar=("FIRST", "LAST"))

    p_reference = sub.add_parser("reference", help="sync EODHD reference data")
    p_reference.add_argument("--sync", action="store_true")

    p_figi = sub.add_parser("figi", help="drain the OpenFIGI resolution queue")
    p_figi.add_argument("--drain", action="store_true")

    sub.add_parser("status", help="print manifest/frontier/DQ/budget status")

    p_calibrate = sub.add_parser("calibrate", help="run one representative week and project the full walk's cost")
    p_calibrate.add_argument("--week", required=True)
    p_calibrate.add_argument("--legacy-root")

    return parser


def main(argv: "list[str] | None" = None, *, ctx_factory=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx_factory = ctx_factory or _real_ctx
    handler_name = HANDLER_NAMES[args.command]
    handler = globals()[handler_name]
    return handler(args, ctx_factory)


if __name__ == "__main__":
    sys.exit(main())

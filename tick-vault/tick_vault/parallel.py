# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Worker-process side of the parallel walk.

The walk is CPU-bound in our own code, not on the network. Profiled per
symbol-week on the calibration vault (2026-09-17): parse + silver write +
bars take 4.5 s for a small name, 17 s at the median and ~8 min for TSLA's
6.0M prints, against ~8-14 s of fetch. Threads would queue on the GIL, so
the fan-out is PROCESSES.

Division of labour, chosen to keep every invariant the serial loop has:

- A worker fetches, parses and writes its own `bronze.eodhd_tick_capture`
  and `silver.us_trade_tick_version` rows. Those tables are append-only,
  and concurrent appends were measured safe (6 processes x 10 appends x
  5,000 real rows = 300,000 rows, no failures, no lost commits).
- A worker also builds the reconciliation REPORT (the expensive part:
  reading its week back and aggregating bars), but never rules on it.
- The parent stays the only writer of `ops.backfill_manifest`, the only
  caller of `reconcile.Gate`, and the only writer of
  `ops.data_quality_issue` - so gate sequencing and hold policy are
  unchanged, and the manifest's one-writer rule still holds.

Only plain picklable data crosses the process boundary: the job dict in,
and result/report records out. Each worker builds its own transport and
store; `SpanMemory` is per-process, so span-halving knowledge is not
shared between workers (a worker re-learns a slow symbol's span itself).
"""
from __future__ import annotations

import datetime as dt


def _report_to_records(report) -> "dict | None":
    if report is None:
        return None
    return {
        "status": report.status,
        "diffs": report.diffs_df.to_dict("records"),
        "issues": report.issues_df.to_dict("records"),
        "null_size_count": report.null_size_count,
    }


def records_to_report(payload: "dict | None"):
    """Rebuild a `ReconcileReport` in the parent from a worker's records, so
    the parent's own `Gate` rules on it exactly as in the serial path."""
    if payload is None:
        return None
    import pandas as pd

    from tick_vault.reconcile import _DIFF_COLUMNS, _DQ_COLUMNS, ReconcileReport

    return ReconcileReport(
        status=payload["status"],
        diffs_df=pd.DataFrame(payload["diffs"], columns=_DIFF_COLUMNS),
        issues_df=pd.DataFrame(payload["issues"], columns=_DQ_COLUMNS),
        null_size_count=payload["null_size_count"],
    )


def fetch_job(job: dict) -> dict:
    """Run one manifest item in a worker process: fetch/parse/write, then
    build its reconciliation report when a reference credential is set.

    `job`: `{"root", "item", "api_token", "now", "reconcile"}`. Returns
    `{"work_id", "result": {...}, "report": {...} | None, "error": str | None}`
    - `error` is set only for an exception that escaped the item itself, so
    one bad item never kills the pool (the parent records it and moves on).
    """
    from tick_vault.engine import STATUS_COMPLETE, STATUS_PARTIAL

    item = job["item"]
    out: dict = {"work_id": item.get("work_id"), "result": None, "report": None, "error": None}
    try:
        from tick_vault.capture import CaptureStore, UrlLibTransport
        from tick_vault.cli import (
            _default_existing_ticks_reader,
            _default_splits_reader,
            build_reconcile_report,
        )
        from tick_vault.engine import Budget, SpanMemory, fetch_week

        root, token = job["root"], job["api_token"]
        now = dt.datetime.fromisoformat(job["now"])
        transport, store = UrlLibTransport(), CaptureStore(root)

        result = fetch_week(
            transport, store, root, item,
            span_memory=SpanMemory(), budget=Budget(), now=now, api_token=token,
            existing_ticks_reader=_default_existing_ticks_reader,
        )
        out["result"] = {
            "status": result.status, "rows_written": result.rows_written,
            "captures": result.captures, "wall_minutes": result.wall_minutes,
            "error": result.error,
        }

        if job.get("reconcile") and result.status in (STATUS_COMPLETE, STATUS_PARTIAL):
            class _WorkerCtx:  # the seams build_reconcile_report reads, nothing more
                pass

            wctx = _WorkerCtx()
            wctx.root, wctx.transport, wctx.store, wctx.api_token = root, transport, store, token
            wctx.existing_ticks_reader = _default_existing_ticks_reader
            wctx.splits_reader = _default_splits_reader
            out["report"] = _report_to_records(build_reconcile_report(wctx, item))
    except Exception as exc:  # per-item isolation: the pool must survive
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out

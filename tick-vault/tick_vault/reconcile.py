"""OHLCV reconciliation gate (Plan-2 task-9 brief).

Aggregates `silver.us_trade_tick_version`-shaped tick frames into OHLCV
bars, compares them against vendor EOD/intraday reference bars
(`tick_vault.eodhd_reference.ReferenceClient.get_eod`/`get_intraday`), and
gates a backfill work item on the outcome for the first `n_gated`
tranches processed.

Split for testability, matching the rest of Plan-2: everything in this
module is pure pandas (no DuckDB, no Delta, no pyarrow import at module
scope) so it's exercised directly by the sandbox mini-runner
(`tests/test_reconcile_core.py`). Production (Plan-3) may choose to push
`aggregate_bars`'s filtering/bucketing down to a DuckDB SQL query over
silver instead of pandas for performance - the inclusion policy encoded
here (`BAR_INCLUSION_POLICY`) is the source of truth either way.

Bar inclusion policy ("BAR_INCL_V1")
-------------------------------------
A tick row contributes to a regular-session bar iff, after resolving to
the latest revision per `logical_tick_id` and dropping cancellations:

1. Its America/New_York trade time falls in the regular session window
   `[09:30:00.000, 16:00:00.000)` (late adds are included at their
   original market time - `is_late_add` is not itself a filter).
2. None of its stored `sale_condition_flags` is in `INELIGIBLE_FLAGS`.
   `INELIGIBLE_FLAGS` is transcribed directly from
   `tick_vault.sl_conditions`'s internal decode table (the single source
   of truth for which flags mark a print bars-ineligible) - this module
   filters on the *stored* flags column, it never re-decodes
   `sale_condition_raw` itself.

Bars are analytics output, not the bitemporal system of record: OHLCV
values are plain Python floats/ints even when the input frame carries
`Decimal` price/size columns (as real silver rows do) - precision loss at
the 1e-10 level is immaterial for a reconciliation tolerance check.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd

from tick_vault.ids import new_id
from tick_vault.sl_conditions import _TABLE as _SL_TABLE

BAR_INCLUSION_POLICY = "BAR_INCL_V1"

_NY_TZ = ZoneInfo("America/New_York")

# Transcribed from tick_vault.sl_conditions._TABLE: every distinct flag
# name whose decode-table entries mark it NOT eligible for bars. Built
# from the decode table itself (not hand-copied) so the two modules can
# never drift out of sync.
INELIGIBLE_FLAGS: frozenset = frozenset(
    flag_name
    for (flag_name, bars_eligible, _out_of_seq) in _SL_TABLE.values()
    if flag_name is not None and not bars_eligible
)

BAR_COLUMNS = ["trade_date", "bucket_start", "open", "high", "low", "close", "volume"]

_DQ_COLUMNS = [
    "issue_id", "check_name", "severity", "trade_date", "listing_id",
    "instrument_id", "source_capture_id", "details_json", "status",
    "detected_at_ts", "resolved_at_ts", "ingestion_run_id",
]

DQ_CHECK_RECONCILE_DIVERGENCE = "RECONCILE_DIVERGENCE"
DQ_CHECK_RECONCILE_WAIVED = "RECONCILE_WAIVED"


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------

def _row(columns: list, **overrides) -> dict:
    row = {c: None for c in columns}
    for key, value in overrides.items():
        if key in row:
            row[key] = value
    return row


def _item_get(item, key: str, default=None):
    """Read `key` off `item`, which may be a plain dict or an
    attribute-bearing object (e.g. a backfill-manifest row/namedtuple)."""
    if item is None:
        return default
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _is_eligible_flags(flags) -> bool:
    if not flags:
        return True
    return not any(f in INELIGIBLE_FLAGS for f in flags)


def _floor_minute(ts: "pd.Timestamp", minutes: int) -> "pd.Timestamp":
    floored = (ts.minute // minutes) * minutes
    return ts.replace(minute=floored, second=0, microsecond=0, nanosecond=0)


def _session_open(ts: "pd.Timestamp") -> "pd.Timestamp":
    return ts.replace(hour=9, minute=30, second=0, microsecond=0, nanosecond=0)


def _session_close(ts: "pd.Timestamp") -> "pd.Timestamp":
    return ts.replace(hour=16, minute=0, second=0, microsecond=0, nanosecond=0)


# ---------------------------------------------------------------------------
# aggregate_bars
# ---------------------------------------------------------------------------

def aggregate_bars(
    ticks_df: pd.DataFrame,
    interval: str = "1d",
    policy: str = BAR_INCLUSION_POLICY,
) -> pd.DataFrame:
    """Aggregate a `silver.us_trade_tick_version`-shaped tick frame into
    OHLCV bars under `policy` (only `BAR_INCLUSION_POLICY` is supported
    today - the argument exists so a future policy version can be passed
    explicitly without changing the call site).

    `interval` is one of `"1d"`, `"1m"`, `"5m"`. `ticks_df` may contain
    multiple revisions of the same `logical_tick_id`; only the max
    `revision_number` per logical tick is used, and cancelled ("busted")
    prints are dropped even if they are the latest revision.

    Returns a frame with columns `BAR_COLUMNS`: `open` is the price of the
    earliest-by-`(trade_ts_ms, session_seq)` contributing row in the
    bucket, `close` the latest, `high`/`low` the extrema, `volume` the sum
    of `size`.
    """
    if policy != BAR_INCLUSION_POLICY:
        raise ValueError(f"unsupported bar inclusion policy: {policy!r}")
    if interval not in ("1d", "1m", "5m"):
        raise ValueError(f"unsupported interval: {interval!r}")

    if ticks_df is None or len(ticks_df) == 0:
        return pd.DataFrame(columns=BAR_COLUMNS)

    df = ticks_df.copy()

    # 1. Latest revision per logical_tick_id.
    df = df.sort_values(["logical_tick_id", "revision_number"])
    df = df.groupby("logical_tick_id", as_index=False, sort=False).tail(1)

    # 2. Drop cancelled prints (even if they're the latest revision).
    df = df[~df["is_cancelled"].astype(bool)]
    if df.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)

    # 3. Regular session window, America/New_York.
    ny_ts = df["trade_ts"].dt.tz_convert(_NY_TZ)
    df = df.assign(_ny_ts=ny_ts)
    in_session = df["_ny_ts"].apply(
        lambda t: _session_open(t) <= t < _session_close(t)
    )
    df = df[in_session]
    if df.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)

    # 4. Stored-flag eligibility (never re-decode sale_condition_raw).
    eligible = df["sale_condition_flags"].apply(_is_eligible_flags)
    df = df[eligible]
    if df.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)

    # 5. Bucket.
    if interval == "1d":
        bucket_start = df["_ny_ts"].apply(_session_open)
    else:
        minutes = 1 if interval == "1m" else 5
        bucket_start = df["_ny_ts"].apply(lambda t: _floor_minute(t, minutes))
    df = df.assign(_bucket_start=bucket_start)

    # Sort so first/last-in-group gives open/close by (trade_ts_ms, session_seq).
    df = df.sort_values(["trade_ts_ms", "session_seq"])

    rows = []
    for (trade_date, bucket_start), group in df.groupby(
        ["trade_date", "_bucket_start"], sort=True
    ):
        prices = group["price"].astype(float)
        sizes = group["size"].astype(float)
        rows.append({
            "trade_date": trade_date,
            "bucket_start": bucket_start,
            "open": float(prices.iloc[0]),
            "high": float(prices.max()),
            "low": float(prices.min()),
            "close": float(prices.iloc[-1]),
            "volume": float(sizes.sum()),
        })

    bars = pd.DataFrame(rows, columns=BAR_COLUMNS)
    return bars.sort_values(["trade_date", "bucket_start"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# reconcile_tranche
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReconcileTolerance:
    """Plan defaults (baseline §16.3)."""
    close_pct: float = 0.001
    volume_pct: float = 0.02
    minute_close_pct: float = 0.005


_DIFF_COLUMNS = ["trade_date", "field", "ours", "theirs", "pct", "kind"]

DIFF_KIND_DIVERGENCE = "DIVERGENCE"
DIFF_KIND_REF_MISSING = "REF_MISSING"


@dataclass
class ReconcileReport:
    status: str  # "PASS" | "DIVERGENT"
    diffs_df: pd.DataFrame
    issues_df: pd.DataFrame


def _pct_diff(ours: float, theirs) -> "float | None":
    if theirs is None or (isinstance(theirs, float) and pd.isna(theirs)):
        return None
    if theirs == 0:
        return None if ours == 0 else float("inf")
    return abs(ours - theirs) / abs(theirs)


def reconcile_tranche(
    item,
    our_daily_df: pd.DataFrame,
    ref_eod_df: pd.DataFrame,
    our_minute_df: "pd.DataFrame | None" = None,
    ref_minute_df: "pd.DataFrame | None" = None,
    *,
    tolerance: ReconcileTolerance = ReconcileTolerance(),
) -> ReconcileReport:
    """Compare our OHLCV bars (from `aggregate_bars`) against vendor
    reference bars (from `ReferenceClient.get_eod`/`get_intraday`, already
    parsed to a DataFrame) for one tranche (`item` - a backfill-manifest
    row or any dict/object carrying `listing_id`/`instrument_id`/
    `source_capture_id`/`ingestion_run_id`, used only to stamp the
    resulting `ops.data_quality_issue`-shaped rows).

    Daily close/volume are compared per `trade_date` within
    `tolerance.close_pct`/`tolerance.volume_pct`. If `our_minute_df`/
    `ref_minute_df` are both supplied, 1-minute close is compared on the
    intersection of `bucket_start` buckets within
    `tolerance.minute_close_pct`.

    A `trade_date` present in `our_daily_df` but absent from `ref_eod_df`
    produces a `REF_MISSING` diff row (a diagnostic, counted in the
    report) but does NOT by itself flip the report to `DIVERGENT`
    (baseline §16.3) - only an actual close/volume/minute-close mismatch
    beyond tolerance does that.
    """
    diff_rows: list = []
    now = dt.datetime.now(dt.timezone.utc)
    divergent = False

    ref_by_date = {}
    if ref_eod_df is not None and len(ref_eod_df):
        ref_by_date = {row["date"]: row for row in ref_eod_df.to_dict("records")}

    our_by_date = {}
    if our_daily_df is not None and len(our_daily_df):
        our_by_date = {row["trade_date"]: row for row in our_daily_df.to_dict("records")}

    for trade_date, ours in sorted(our_by_date.items()):
        theirs = ref_by_date.get(trade_date)
        if theirs is None:
            diff_rows.append({
                "trade_date": trade_date, "field": "close", "ours": ours["close"],
                "theirs": None, "pct": None, "kind": DIFF_KIND_REF_MISSING,
            })
            continue

        close_pct = _pct_diff(ours["close"], theirs.get("close"))
        if close_pct is not None and close_pct > tolerance.close_pct:
            divergent = True
            diff_rows.append({
                "trade_date": trade_date, "field": "close", "ours": ours["close"],
                "theirs": theirs.get("close"), "pct": close_pct, "kind": DIFF_KIND_DIVERGENCE,
            })

        volume_pct = _pct_diff(ours["volume"], theirs.get("volume"))
        if volume_pct is not None and volume_pct > tolerance.volume_pct:
            divergent = True
            diff_rows.append({
                "trade_date": trade_date, "field": "volume", "ours": ours["volume"],
                "theirs": theirs.get("volume"), "pct": volume_pct, "kind": DIFF_KIND_DIVERGENCE,
            })

    if (
        our_minute_df is not None
        and ref_minute_df is not None
        and len(our_minute_df)
        and len(ref_minute_df)
    ):
        our_min_by_bucket = {
            (row["trade_date"], row["bucket_start"]): row
            for row in our_minute_df.to_dict("records")
        }
        ref_min_by_bucket = {
            (row["trade_date"], row["bucket_start"]): row
            for row in ref_minute_df.to_dict("records")
        }
        common = sorted(set(our_min_by_bucket) & set(ref_min_by_bucket))
        for key in common:
            ours = our_min_by_bucket[key]
            theirs = ref_min_by_bucket[key]
            pct = _pct_diff(ours["close"], theirs.get("close"))
            if pct is not None and pct > tolerance.minute_close_pct:
                divergent = True
                diff_rows.append({
                    "trade_date": key[0], "field": "minute_close", "ours": ours["close"],
                    "theirs": theirs.get("close"), "pct": pct, "kind": DIFF_KIND_DIVERGENCE,
                })

    diffs_df = pd.DataFrame(diff_rows, columns=_DIFF_COLUMNS)

    # One ops.data_quality_issue row per trade_date that has at least one
    # real DIVERGENCE diff (REF_MISSING alone does not raise an issue row -
    # it's diagnostic-only per the docstring above).
    issue_rows = []
    divergence_dates = sorted({
        r["trade_date"] for r in diff_rows if r["kind"] == DIFF_KIND_DIVERGENCE
    })
    for trade_date in divergence_dates:
        deltas = [
            r for r in diff_rows
            if r["trade_date"] == trade_date and r["kind"] == DIFF_KIND_DIVERGENCE
        ]
        issue_rows.append(_row(
            _DQ_COLUMNS,
            issue_id=new_id("wrk"),
            check_name=DQ_CHECK_RECONCILE_DIVERGENCE,
            severity="WARN",
            trade_date=trade_date,
            listing_id=_item_get(item, "listing_id"),
            instrument_id=_item_get(item, "instrument_id"),
            source_capture_id=_item_get(item, "source_capture_id"),
            details_json=json.dumps({
                "policy": BAR_INCLUSION_POLICY,
                "trade_date": trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date),
                "deltas": [
                    {"field": d["field"], "ours": d["ours"], "theirs": d["theirs"], "pct": d["pct"]}
                    for d in deltas
                ],
            }),
            status="OPEN",
            detected_at_ts=now,
            ingestion_run_id=_item_get(item, "ingestion_run_id"),
        ))

    issues_df = pd.DataFrame(issue_rows, columns=_DQ_COLUMNS)
    status = "DIVERGENT" if divergent else "PASS"
    return ReconcileReport(status=status, diffs_df=diffs_df, issues_df=issues_df)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateDecision:
    hold: bool
    reason: str


GATE_REASON_HOLD = "RECONCILE_HOLD"
GATE_REASON_LOGGED = "LOGGED"
GATE_REASON_PASS = "PASS"


@dataclass
class Gate:
    """Reconciliation gate over a sequence of tranches.

    Within the first `n_gated` tranches processed (sequence numbers
    `0..n_gated-1`, tracked by the caller's ingest loop - this class holds
    no state of its own), a `DIVERGENT` report holds the tranche
    (`hold=True`, `reason=RECONCILE_HOLD`) rather than letting it reach
    `COMPLETE`. After `n_gated`, a `DIVERGENT` report is logged but does
    not hold (`hold=False`, `reason=LOGGED`) - the gate is a burn-in
    check, not a permanent block.
    """
    n_gated: int = 2000

    def check(self, sequence_number: int, report: ReconcileReport) -> GateDecision:
        if report.status != "DIVERGENT":
            return GateDecision(hold=False, reason=GATE_REASON_PASS)
        if sequence_number < self.n_gated:
            return GateDecision(hold=True, reason=GATE_REASON_HOLD)
        return GateDecision(hold=False, reason=GATE_REASON_LOGGED)

    def waive(self, work_id: str, reason: str) -> dict:
        """Build (but do not write) a waiver `ops.data_quality_issue`-shaped
        row for `work_id`. The caller is responsible for appending it to
        the table (this class never touches storage)."""
        now = dt.datetime.now(dt.timezone.utc)
        return _row(
            _DQ_COLUMNS,
            issue_id=new_id("wrk"),
            check_name=DQ_CHECK_RECONCILE_WAIVED,
            severity="INFO",
            details_json=json.dumps({"work_id": work_id, "reason": reason}),
            status="RESOLVED",
            detected_at_ts=now,
            resolved_at_ts=now,
        )

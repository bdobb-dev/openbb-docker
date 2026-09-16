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

Bar inclusion policy ("BAR_INCL_V2")
-------------------------------------
V2 (Phase-0, 2026-09-15) splits PRICE from VOLUME eligibility, as the
consolidated tape does, and re-decodes each print's `sale_condition_raw`
with the current `tick_vault.sl_conditions` table rather than trusting
the flags stored at write time (SL_DECODE_V2 rows flag the official-close
record `M` as a closing print). Checked against EODHD EOD bars, 30
symbols x 5 days: daily volume as defined below matches to 0.02% median,
high/low to 0.003% at p90 (V1: every tranche failed, odd lots set
bogus lows).

OPEN/HIGH/LOW/CLOSE: a tick row contributes to a regular-session bar iff,
after resolving to the latest revision per `logical_tick_id` and dropping
cancellations:

1. Its America/New_York trade time falls in the regular session window
   `[09:30:00.000, 16:00:00.000]` - INCLUSIVE at the close boundary
   (late adds are included at their original market time - `is_late_add`
   is not itself a filter). A print at exactly `16:00:00.000` is a
   regular-session print, no `CLOSING_PRINT` flag required.

   Additionally, a *closing-auction grace window*: any print whose
   re-decoded conditions include `CLOSING_PRINT` (a real `6` closing
   print, not the `M` official-close record) and whose
   America/New_York trade time falls in `(16:00:00.000, 16:10:00.000]`
   is also included - closing-auction prints are eligible by design but
   commonly stamp a few minutes after the nominal 16:00:00 close, and
   excluding them on a strict boundary would create a systematic close
   divergence against vendor EOD references. A print in that same
   `(16:00, 16:10]` window WITHOUT the `CLOSING_PRINT` flag is excluded,
   and any print (flagged or not) after `16:10:00.000` is excluded.

   For `1m`/`5m` bucketing, any included print at or after
   `16:00:00.000` (i.e. the exact-close regular print or a grace-window
   closing print) is assigned to the session's final intraday bucket
   (`15:59` for `1m`, `15:55` for `5m`) rather than floored to its own
   wall-clock minute - there is no real `16:00` trading minute, so these
   prints fold into the last real bucket and participate in that
   bucket's (and the daily bar's) close/high/low/volume.
2. Its re-decoded conditions are price-eligible (`eligible_for_bars`: no
   flag in `INELIGIBLE_FLAGS` - odd lots, extended hours, out of
   sequence, average price, official open/close records, ...).
   (`CLOSING_PRINT` is itself eligible, so a grace-window print that
   qualifies under rule 1 always passes rule 2.)

VOLUME: the daily bar's `volume` sums EVERY print of that trade date
whose re-decoded conditions are volume-eligible (`eligible_for_volume`:
all but the market-center official open/close and corrected consolidated
close records) - regular session, extended hours and odd lots alike,
which is how vendor EOD volume is defined. Intraday (`1m`/`5m`) bucket
volume sums only the volume-eligible prints inside the session window,
so intraday buckets do not add up to the daily volume.

V3 additionally collapses REPEATED BLOCK REPORTS in that sum: prints of
`BLOCK_DEDUP_MIN_SIZE` shares or more that are identical in
`_BLOCK_KEY` (date, price, size, venue, conditions) count once, earliest
kept. This is a heuristic about what a repeat means - silver keeps every
report, `BarStats.deduped_block_rows` says how many the sum dropped, and
prices are never deduped.

Bars are analytics output, not the bitemporal system of record: OHLCV
values are plain Python floats/ints even when the input frame carries
`Decimal` price/size columns (as real silver rows do) - precision loss at
the 1e-10 level is immaterial for a reconciliation tolerance check.

Null `size`: a print with a `None`/null `size` contributes 0 to bar
`volume` (rather than being silently dropped from the sum via pandas'
default `skipna=True` behavior, which would understate volume without
any visible signal). Callers that need visibility into how many such
rows were folded in at 0 can pass `return_stats=True` to `aggregate_bars`
to get a `BarStats(null_size_rows=...)` alongside the bars frame, and
thread it through to `reconcile_tranche` (`our_daily_stats`/
`our_minute_stats`) so it surfaces as `ReconcileReport.null_size_count`.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd

from tick_vault.ids import new_id
from tick_vault.sl_conditions import _TABLE as _SL_TABLE, decode_sl

BAR_INCLUSION_POLICY = "BAR_INCL_V3"

# Block dedup (V3): the same large block re-reported minutes apart, every copy
# uncancelled in the tick feed (L 2026-09-03: 387,477 sh @ 110.04 on venue D at
# 16:00:09, 16:51:39 and 16:52:54). The vendor's EOD volume counts it once.
# Only prints at or above this size are collapsed - small identical trades are
# ordinary. Measured on the calibration week: 36 of 45 over-count days fixed,
# 2 of 300 passing days broken.
BLOCK_DEDUP_MIN_SIZE = 10_000
_BLOCK_KEY = ["trade_date", "price", "size", "venue_code_raw", "sale_condition_raw"]

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


_CLOSING_GRACE = dt.timedelta(minutes=10)
_OPEN_S = 9 * 3600 + 30 * 60   # 09:30:00, seconds after NY midnight
_CLOSE_S = 16 * 3600           # 16:00:00


# ---------------------------------------------------------------------------
# aggregate_bars
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BarStats:
    """Visibility into `aggregate_bars` accounting decisions that don't
    show up in the bars frame itself. `null_size_rows` counts
    contributing rows (post session/eligibility filtering) whose `size`
    was `None`/null - those rows contribute 0 to `volume` rather than
    being silently skipped. `deduped_block_rows` counts repeated block
    reports the volume sum collapsed (see `BLOCK_DEDUP_MIN_SIZE`)."""
    null_size_rows: int = 0
    deduped_block_rows: int = 0


def aggregate_bars(
    ticks_df: pd.DataFrame,
    interval: str = "1d",
    policy: str = BAR_INCLUSION_POLICY,
    *,
    return_stats: bool = False,
):
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
    of `size` (a `None` `size` contributes 0, it is never dropped from the
    sum). If `return_stats=True`, returns `(bars_df, BarStats)` instead.
    """
    if policy != BAR_INCLUSION_POLICY:
        raise ValueError(f"unsupported bar inclusion policy: {policy!r}")
    if interval not in ("1d", "1m", "5m"):
        raise ValueError(f"unsupported interval: {interval!r}")

    def _empty():
        empty_bars = pd.DataFrame(columns=BAR_COLUMNS)
        if return_stats:
            return empty_bars, BarStats()
        return empty_bars

    if ticks_df is None or len(ticks_df) == 0:
        return _empty()

    df = ticks_df.copy()

    # 1. Latest revision per logical_tick_id.
    df = df.sort_values(["logical_tick_id", "revision_number"])
    df = df.groupby("logical_tick_id", as_index=False, sort=False).tail(1)

    # 2. Drop cancelled prints (even if they're the latest revision).
    df = df[~df["is_cancelled"].astype(bool)]
    if df.empty:
        return _empty()

    # 3. Re-decode each DISTINCT raw condition once with the current table
    #    (a week holds ~90 distinct codes, so this is cheap and vectorized).
    raw = df["sale_condition_raw"].fillna("")
    decoded = {code: decode_sl(code) for code in raw.unique()}
    price_ok = raw.map({c: d.eligible_for_bars for c, d in decoded.items()}).astype(bool)
    volume_ok = raw.map({c: d.eligible_for_volume for c, d in decoded.items()}).astype(bool)
    closing = raw.map({c: "CLOSING_PRINT" in d.flags for c, d in decoded.items()}).astype(bool)

    # 4. Regular session [09:30:00, 16:00:00] (inclusive at close) plus the
    #    closing-print grace window (16:00:00, 16:10:00], America/New_York.
    ny_ts = df["trade_ts"].dt.tz_convert(_NY_TZ)
    secs = (ny_ts - ny_ts.dt.normalize()).dt.total_seconds()
    in_window = ((secs >= _OPEN_S) & (secs <= _CLOSE_S)) | (
        (secs > _CLOSE_S) & (secs <= _CLOSE_S + _CLOSING_GRACE.total_seconds()) & closing
    )
    df = df.assign(_ny_ts=ny_ts, _secs=secs)

    price_rows = df[in_window & price_ok]
    if price_rows.empty:
        return _empty()
    vol_rows = df[volume_ok] if interval == "1d" else df[in_window & volume_ok]

    # 5. Bucket. At/after 16:00:00 (exact-close print, or a closing-grace
    #    print) folds into the session's final intraday bucket.
    def _bucket(frame):
        day = frame["_ny_ts"].dt.normalize()
        if interval == "1d":
            return day + pd.Timedelta(seconds=_OPEN_S)
        minutes = 1 if interval == "1m" else 5
        last = day + pd.Timedelta(seconds=_CLOSE_S - minutes * 60)
        return frame["_ny_ts"].dt.floor(f"{minutes}min").where(frame["_secs"] < _CLOSE_S, last)

    # Sort so first/last-in-group gives open/close by (trade_ts_ms, session_seq).
    price_rows = price_rows.assign(
        _bucket=_bucket(price_rows), _px=price_rows["price"].astype(float)
    ).sort_values(["trade_ts_ms", "session_seq"])
    g = price_rows.groupby(["trade_date", "_bucket"], sort=True)["_px"]
    bars = pd.DataFrame({"open": g.first(), "high": g.max(), "low": g.min(), "close": g.last()})

    # Block dedup: the same large block re-reported minutes apart counts once
    # in the volume sum (prices above are already computed and untouched).
    vol_rows = vol_rows.sort_values(["trade_ts_ms", "session_seq"])
    big = vol_rows[vol_rows["size"].fillna(0).astype(float) >= BLOCK_DEDUP_MIN_SIZE]
    repeats = big.index[big.duplicated(subset=_BLOCK_KEY, keep="first")]
    deduped_block_rows = len(repeats)
    if deduped_block_rows:
        vol_rows = vol_rows.drop(index=repeats)

    # Null-size accounting: a None size contributes 0 to volume rather
    # than being dropped by pandas' default skipna sum.
    sizes = vol_rows["size"]
    null_size_rows = int(sizes.isna().sum())
    sizes = sizes.where(sizes.notna(), 0).astype(float)
    if interval == "1d":
        vol = sizes.groupby(vol_rows["trade_date"]).sum()
        bars["volume"] = [float(vol.get(d, 0.0)) for d in bars.index.get_level_values(0)]
    else:
        vol = sizes.groupby([vol_rows["trade_date"], _bucket(vol_rows)]).sum()
        bars["volume"] = [float(vol.get(k, 0.0)) for k in bars.index]

    bars = bars.reset_index().rename(columns={"_bucket": "bucket_start"})[BAR_COLUMNS]
    if return_stats:
        return bars, BarStats(
            null_size_rows=null_size_rows, deduped_block_rows=deduped_block_rows
        )
    return bars


# ---------------------------------------------------------------------------
# reconcile_tranche
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReconcileTolerance:
    """Plan defaults (baseline §16.3)."""
    close_pct: float = 0.001
    high_low_pct: float = 0.001
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
    null_size_count: int = 0


def _split_factor(splits_df: "pd.DataFrame | None", trade_date) -> float:
    """Product of the split factors dated after `trade_date`: the multiplier
    the vendor applied to that session's volume (APH, 2-for-1 on 2026-09-03:
    x2 through 2026-09-02, x1 from 2026-09-03 - the split day is post-split)."""
    if splits_df is None or not len(splits_df):
        return 1.0
    later = splits_df[splits_df["date"] > trade_date]["factor"]
    return float(later.prod()) if len(later) else 1.0


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
    our_daily_stats: "BarStats | None" = None,
    our_minute_stats: "BarStats | None" = None,
    splits_df: "pd.DataFrame | None" = None,
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
    (baseline §16.3) - only an actual close/high/low/volume/minute-close mismatch
    beyond tolerance does that.

    `our_daily_stats`/`our_minute_stats` are the optional `BarStats`
    returned by `aggregate_bars(..., return_stats=True)` for
    `our_daily_df`/`our_minute_df` respectively; when supplied their
    `null_size_rows` are summed onto `ReconcileReport.null_size_count` so
    null-size accounting stays visible through the reconciliation report
    instead of being buried inside volume.
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

        for field in ("high", "low"):
            hl_pct = _pct_diff(ours[field], theirs.get(field))
            if hl_pct is not None and hl_pct > tolerance.high_low_pct:
                divergent = True
                diff_rows.append({
                    "trade_date": trade_date, "field": field, "ours": ours[field],
                    "theirs": theirs.get(field), "pct": hl_pct, "kind": DIFF_KIND_DIVERGENCE,
                })

        # EODHD EOD volume is split-adjusted (its prices are not): scale our
        # raw tick volume by every split dated after this session.
        our_volume = ours["volume"] * _split_factor(splits_df, trade_date)
        volume_pct = _pct_diff(our_volume, theirs.get("volume"))
        if volume_pct is not None and volume_pct > tolerance.volume_pct:
            divergent = True
            diff_rows.append({
                "trade_date": trade_date, "field": "volume", "ours": our_volume,
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

    null_size_count = 0
    if our_daily_stats is not None:
        null_size_count += our_daily_stats.null_size_rows
    if our_minute_stats is not None:
        null_size_count += our_minute_stats.null_size_rows

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
    return ReconcileReport(
        status=status, diffs_df=diffs_df, issues_df=issues_df,
        null_size_count=null_size_count,
    )


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

    def waive(
        self,
        work_id: str,
        reason: str,
        *,
        listing_id: "str | None" = None,
        trade_date: "dt.date | None" = None,
    ) -> dict:
        """Build (but do not write) a waiver `ops.data_quality_issue`-shaped
        row for `work_id`. The caller is responsible for appending it to
        the table (this class never touches storage).

        `listing_id`/`trade_date` are optional and, when supplied, are
        threaded into the row's fixed columns (not just `details_json`)
        so a waiver row is directly correlatable/filterable against other
        `ops.data_quality_issue` rows for the same listing/day without
        parsing JSON."""
        now = dt.datetime.now(dt.timezone.utc)
        return _row(
            _DQ_COLUMNS,
            issue_id=new_id("wrk"),
            check_name=DQ_CHECK_RECONCILE_WAIVED,
            severity="INFO",
            listing_id=listing_id,
            trade_date=trade_date,
            details_json=json.dumps({"work_id": work_id, "reason": reason}),
            status="RESOLVED",
            detected_at_ts=now,
            resolved_at_ts=now,
        )

"""Runnable (pyarrow/deltalake/pytest-free) tests for `tick_vault.reconcile`
(task-9 brief): `aggregate_bars`, `reconcile_tranche`, `Gate`.

Synthetic tick frames are built via `tick_vault.tick_parser.parse_tick_payload`
on crafted EODHD-shaped payloads (same fixture shape as
`tests/fixtures/eodhd_ticks_sample.json`), then mutated in place for the
scenarios `parse_tick_payload` itself can't produce (cancelled rows,
multi-revision rows, late adds) - mirroring how `tests/test_tick_parser.py`
and `tests/test_sl_conditions.py` build their inputs.

`tick_vault.reconcile` has no pyarrow/deltalake import at module scope (see
its own module docstring), so it's importable and runnable here even though
those deps aren't installed in this sandbox. `ReferenceClient.get_eod`/
`get_intraday`'s Delta-backed capture wiring is verified separately in
`tests/deferred/test_reconcile.py`.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from tick_vault.tick_parser import parse_tick_payload
from tick_vault.reconcile import (
    aggregate_bars,
    reconcile_tranche,
    ReconcileTolerance,
    ReconcileReport,
    Gate,
    BAR_INCLUSION_POLICY,
    INELIGIBLE_FLAGS,
    DIFF_KIND_DIVERGENCE,
    DIFF_KIND_REF_MISSING,
    GATE_REASON_HOLD,
    GATE_REASON_LOGGED,
    GATE_REASON_PASS,
    DQ_CHECK_RECONCILE_DIVERGENCE,
    DQ_CHECK_RECONCILE_WAIVED,
)

_NY_TZ = ZoneInfo("America/New_York")
OBS = dt.datetime(2026, 7, 1, 20, 0, tzinfo=dt.timezone.utc)
TRADE_DATE = dt.date(2026, 7, 1)


def _ts_ms(hour, minute, second=0, microsecond=0):
    """UTC-epoch milliseconds for `TRADE_DATE hour:minute:second.microsecond`
    America/New_York wall-clock time."""
    local = dt.datetime(
        TRADE_DATE.year, TRADE_DATE.month, TRADE_DATE.day,
        hour, minute, second, microsecond, tzinfo=_NY_TZ,
    )
    utc = local.astimezone(dt.timezone.utc)
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return (utc - epoch) // dt.timedelta(milliseconds=1)


def _tick(seq, hour, minute, second, price, shares, sl="@   ", microsecond=0):
    ts_ms = _ts_ms(hour, minute, second, microsecond)
    return {"ts": ts_ms, "price": price, "shares": shares, "seq": seq, "sl": sl,
            "mkt": "Q", "sub_mkt": "", "ex": "US"}


def _build_day_ticks():
    payload = [
        _tick(1, 9, 30, 0, 100.00, 100),                      # regular, first 1m bucket
        _tick(2, 9, 30, 30, 100.50, 50),                       # regular, first 1m bucket
        _tick(3, 9, 31, 0, 101.00, 200),                       # regular, second 1m bucket
        _tick(4, 15, 59, 59, 99.50, 150, microsecond=999000),  # regular, last 1m bucket
        _tick(5, 12, 0, 0, 999.00, 9999, sl="@ Z "),            # out-of-sequence, ineligible
        _tick(6, 8, 0, 0, 50.00, 500),                          # extended-hours (before open)
        _tick(7, 12, 30, 0, 777.00, 777),                       # to be marked cancelled
        _tick(8, 13, 0, 0, 102.00, 300),                        # late add (marked after parse)
    ]
    df = parse_tick_payload(
        payload, capture_id="cap_x", listing_id="lst_a", instrument_id="ins_a", observed_at=OBS
    )
    df = df.set_index("session_seq", drop=False)
    df.loc[7, "is_cancelled"] = True
    df.loc[8, "is_late_add"] = True
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# INELIGIBLE_FLAGS / policy constant
# ---------------------------------------------------------------------------

def test_policy_constant():
    assert BAR_INCLUSION_POLICY == "BAR_INCL_V1"


def test_ineligible_flags_built_from_decode_table():
    assert "SOLD_OUT_OF_SEQUENCE" in INELIGIBLE_FLAGS
    assert "EXTENDED_HOURS" in INELIGIBLE_FLAGS
    assert "REGULAR" not in INELIGIBLE_FLAGS
    assert "ODD_LOT" not in INELIGIBLE_FLAGS


# ---------------------------------------------------------------------------
# aggregate_bars: 1d exclusions
# ---------------------------------------------------------------------------

def test_aggregate_bars_1d_excludes_out_of_seq_extended_hours_and_cancelled():
    ticks = _build_day_ticks()
    bars = aggregate_bars(ticks, interval="1d")

    assert len(bars) == 1
    row = bars.iloc[0]
    assert row["trade_date"] == TRADE_DATE
    # Included: seq 1 (100.00), 2 (100.50), 3 (101.00), 4 (99.50), 8 (102.00, late add).
    # Excluded: seq 5 (out-of-seq @Z), 6 (extended-hours pre-open), 7 (cancelled).
    assert row["open"] == 100.00     # earliest by (trade_ts_ms, session_seq) -> seq 1
    assert row["high"] == 102.00     # late add's price is the day's high
    assert row["low"] == 99.50
    assert row["close"] == 99.50     # latest by time -> seq 4 (15:59:59.999)
    assert row["volume"] == 100 + 50 + 200 + 150 + 300  # seq 1,2,3,4,8 sizes


def test_aggregate_bars_1d_late_add_included_at_market_time():
    ticks = _build_day_ticks()
    bars = aggregate_bars(ticks, interval="1d")
    # seq 8 (is_late_add=True, price 102.00) contributed to the day's high -
    # proves late adds are not excluded, just carried at their market time.
    assert bars.iloc[0]["high"] == 102.00


# ---------------------------------------------------------------------------
# aggregate_bars: revision handling
# ---------------------------------------------------------------------------

def test_aggregate_bars_uses_latest_revision():
    payload = [{"ts": _ts_ms(10, 0, 0), "price": 200.0, "shares": 10, "seq": 500,
                "sl": "@   ", "mkt": "Q", "sub_mkt": "", "ex": "US"}]
    base = parse_tick_payload(
        payload, capture_id="cap_x", listing_id="lst_a", instrument_id="ins_a", observed_at=OBS
    )
    rev1 = base.copy()
    rev1["revision_number"] = 1
    rev2 = base.copy()
    rev2["revision_number"] = 2
    rev2["price"] = 250.0
    rev2["tick_version_id"] = "tkv_rev2"

    import pandas as pd
    ticks = pd.concat([rev1, rev2], ignore_index=True)
    bars = aggregate_bars(ticks, interval="1d")

    assert len(bars) == 1
    assert bars.iloc[0]["open"] == 250.0
    assert bars.iloc[0]["close"] == 250.0


# ---------------------------------------------------------------------------
# aggregate_bars: 1m bucketing boundaries
# ---------------------------------------------------------------------------

def test_aggregate_bars_1m_bucket_boundaries():
    ticks = _build_day_ticks()
    bars = aggregate_bars(ticks, interval="1m")

    buckets = {
        (row["trade_date"], row["bucket_start"].strftime("%H:%M")): row
        for _, row in bars.iterrows()
    }
    # 09:30:00.000 (seq 1) and 09:30:30.000 (seq 2) both land in the 09:30 bucket.
    first = buckets[(TRADE_DATE, "09:30")]
    assert first["open"] == 100.00
    assert first["close"] == 100.50
    assert first["volume"] == 150

    # seq 3 (09:31:00.000) is its own bucket.
    assert (TRADE_DATE, "09:31") in buckets

    # seq 4 at 15:59:59.999 floors into the 15:59 bucket (last bucket of the
    # session - 16:00:00.000 itself would be out of session and excluded).
    last = buckets[(TRADE_DATE, "15:59")]
    assert last["close"] == 99.50
    assert (TRADE_DATE, "16:00") not in buckets

    # seq 8 (late add, 13:00:00.000) gets its own bucket.
    assert (TRADE_DATE, "13:00") in buckets


# ---------------------------------------------------------------------------
# reconcile_tranche
# ---------------------------------------------------------------------------

def _daily_bars(close, volume, trade_date=TRADE_DATE):
    import pandas as pd
    return pd.DataFrame([{
        "trade_date": trade_date, "bucket_start": None,
        "open": close, "high": close, "low": close, "close": close, "volume": volume,
    }])


def _ref_eod(close, volume, trade_date=TRADE_DATE):
    import pandas as pd
    return pd.DataFrame([{
        "date": trade_date, "open": close, "high": close, "low": close,
        "close": close, "adjusted_close": close, "volume": volume,
    }])


def test_reconcile_tranche_matching_ref_passes():
    ours = _daily_bars(close=100.00, volume=1000)
    theirs = _ref_eod(close=100.00, volume=1000)
    report = reconcile_tranche({"listing_id": "lst_a"}, ours, theirs)

    assert isinstance(report, ReconcileReport)
    assert report.status == "PASS"
    assert len(report.diffs_df) == 0
    assert len(report.issues_df) == 0


def test_reconcile_tranche_close_off_by_more_than_tolerance_is_divergent():
    ours = _daily_bars(close=100.00, volume=1000)
    theirs = _ref_eod(close=100.20, volume=1000)  # 0.2% off > 0.1% close_pct default
    report = reconcile_tranche({"listing_id": "lst_a", "instrument_id": "ins_a"}, ours, theirs)

    assert report.status == "DIVERGENT"
    close_diffs = report.diffs_df[report.diffs_df["field"] == "close"]
    assert len(close_diffs) == 1
    assert close_diffs.iloc[0]["kind"] == DIFF_KIND_DIVERGENCE

    assert len(report.issues_df) == 1
    issue = report.issues_df.iloc[0]
    assert issue["check_name"] == DQ_CHECK_RECONCILE_DIVERGENCE
    assert issue["listing_id"] == "lst_a"
    assert issue["instrument_id"] == "ins_a"
    assert issue["status"] == "OPEN"
    import json
    details = json.loads(issue["details_json"])
    assert details["policy"] == BAR_INCLUSION_POLICY
    assert any(d["field"] == "close" for d in details["deltas"])


def test_reconcile_tranche_within_tolerance_passes():
    ours = _daily_bars(close=100.00, volume=1000)
    theirs = _ref_eod(close=100.05, volume=1010)  # 0.05% close, 1% volume - both within default tolerance
    report = reconcile_tranche({"listing_id": "lst_a"}, ours, theirs)
    assert report.status == "PASS"


def test_reconcile_tranche_ref_missing_is_diagnostic_not_failing():
    ours = _daily_bars(close=100.00, volume=1000)
    theirs = _ref_eod(close=100.00, volume=1000, trade_date=dt.date(2026, 7, 2))  # different date
    report = reconcile_tranche({"listing_id": "lst_a"}, ours, theirs)

    assert report.status == "PASS"  # REF_MISSING alone never fails the report
    missing = report.diffs_df[report.diffs_df["kind"] == DIFF_KIND_REF_MISSING]
    assert len(missing) == 1
    assert missing.iloc[0]["trade_date"] == TRADE_DATE
    assert len(report.issues_df) == 0  # diagnostic only, no DQ issue raised


def test_reconcile_tranche_minute_close_divergence():
    import pandas as pd
    bucket = dt.datetime(2026, 7, 1, 9, 30, tzinfo=_NY_TZ)
    ours_min = pd.DataFrame([{"trade_date": TRADE_DATE, "bucket_start": bucket,
                               "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 10}])
    ref_min = pd.DataFrame([{"trade_date": TRADE_DATE, "bucket_start": bucket,
                              "open": 100.0, "high": 100.0, "low": 100.0, "close": 101.0, "volume": 10}])
    ours_daily = _daily_bars(close=100.00, volume=1000)
    ref_daily = _ref_eod(close=100.00, volume=1000)  # daily side matches - only minute diverges

    report = reconcile_tranche({"listing_id": "lst_a"}, ours_daily, ref_daily, ours_min, ref_min)
    assert report.status == "DIVERGENT"
    minute_diffs = report.diffs_df[report.diffs_df["field"] == "minute_close"]
    assert len(minute_diffs) == 1


def test_reconcile_tolerance_defaults():
    tol = ReconcileTolerance()
    assert tol.close_pct == 0.001
    assert tol.volume_pct == 0.02
    assert tol.minute_close_pct == 0.005


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

def test_gate_holds_inside_n_gated():
    gate = Gate(n_gated=2)
    divergent = ReconcileReport(status="DIVERGENT", diffs_df=_daily_bars(1, 1), issues_df=_daily_bars(1, 1))

    decision = gate.check(0, divergent)
    assert decision.hold is True
    assert decision.reason == GATE_REASON_HOLD

    decision = gate.check(1, divergent)
    assert decision.hold is True
    assert decision.reason == GATE_REASON_HOLD


def test_gate_logs_after_n_gated():
    gate = Gate(n_gated=2)
    divergent = ReconcileReport(status="DIVERGENT", diffs_df=_daily_bars(1, 1), issues_df=_daily_bars(1, 1))

    decision = gate.check(2, divergent)
    assert decision.hold is False
    assert decision.reason == GATE_REASON_LOGGED


def test_gate_never_holds_a_passing_report():
    gate = Gate(n_gated=2000)
    passing = ReconcileReport(status="PASS", diffs_df=_daily_bars(1, 1).iloc[0:0], issues_df=_daily_bars(1, 1).iloc[0:0])
    decision = gate.check(0, passing)
    assert decision.hold is False
    assert decision.reason == GATE_REASON_PASS


def test_gate_waiver_row_shape():
    gate = Gate()
    row = gate.waive("wrk_123", "known vendor outage 2026-07-01")
    assert row["check_name"] == DQ_CHECK_RECONCILE_WAIVED
    assert row["status"] == "RESOLVED"
    assert row["detected_at_ts"] is not None
    assert row["resolved_at_ts"] is not None
    import json
    details = json.loads(row["details_json"])
    assert details["work_id"] == "wrk_123"
    assert details["reason"] == "known vendor outage 2026-07-01"

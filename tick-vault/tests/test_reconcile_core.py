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
    BarStats,
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
    assert BAR_INCLUSION_POLICY == "BAR_INCL_V2"


def test_ineligible_flags_built_from_decode_table():
    assert "SOLD_OUT_OF_SEQUENCE" in INELIGIBLE_FLAGS
    assert "EXTENDED_HOURS" in INELIGIBLE_FLAGS
    assert "REGULAR" not in INELIGIBLE_FLAGS
    assert "ODD_LOT" in INELIGIBLE_FLAGS  # V2: odd lots are volume-only


# ---------------------------------------------------------------------------
# aggregate_bars: 1d exclusions
# ---------------------------------------------------------------------------

def test_aggregate_bars_1d_excludes_out_of_seq_extended_hours_and_cancelled():
    ticks = _build_day_ticks()
    bars = aggregate_bars(ticks, interval="1d")

    assert len(bars) == 1
    row = bars.iloc[0]
    assert row["trade_date"] == TRADE_DATE
    # Price: seq 1 (100.00), 2 (100.50), 3 (101.00), 4 (99.50), 8 (102.00, late add).
    # Price excludes seq 5 (out-of-seq @Z), 6 (pre-open), 7 (cancelled).
    assert row["open"] == 100.00     # earliest by (trade_ts_ms, session_seq) -> seq 1
    assert row["high"] == 102.00     # late add's price is the day's high
    assert row["low"] == 99.50
    assert row["close"] == 99.50     # latest by time -> seq 4 (15:59:59.999)
    # BAR_INCL_V2 volume: every volume-eligible print of the day, any hour,
    # out-of-sequence included (vendor EOD volume) - only the cancel is out.
    assert row["volume"] == 100 + 50 + 200 + 150 + 300 + 9999 + 500


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
    # session).
    last = buckets[(TRADE_DATE, "15:59")]
    assert last["close"] == 99.50

    # There is no real 16:00 trading minute - anything at/after the close
    # folds into the 15:59 bucket (BAR_INCL_V1, see below).
    assert (TRADE_DATE, "16:00") not in buckets

    # seq 8 (late add, 13:00:00.000) gets its own bucket.
    assert (TRADE_DATE, "13:00") in buckets


# ---------------------------------------------------------------------------
# aggregate_bars: BAR_INCL_V1 inclusive close + closing-print grace window
# ---------------------------------------------------------------------------

def test_aggregate_bars_exact_close_regular_print_included_in_final_bucket():
    payload = [
        _tick(1, 15, 59, 0, 100.00, 100),
        _tick(2, 16, 0, 0, 105.00, 200),  # exact close, REGULAR, no CLOSING_PRINT flag
    ]
    df = parse_tick_payload(
        payload, capture_id="cap_x", listing_id="lst_a", instrument_id="ins_a", observed_at=OBS
    )

    bars_1m = aggregate_bars(df, interval="1m")
    buckets = {
        row["bucket_start"].strftime("%H:%M"): row for _, row in bars_1m.iterrows()
    }
    assert "16:00" not in buckets
    last = buckets["15:59"]
    assert last["close"] == 105.00  # exact-close print folded into final bucket
    assert last["volume"] == 300

    bars_1d = aggregate_bars(df, interval="1d")
    assert bars_1d.iloc[0]["close"] == 105.00


def test_aggregate_bars_closing_print_grace_window_included_as_close():
    payload = [
        _tick(1, 15, 59, 0, 100.00, 100),
        _tick(2, 16, 3, 0, 110.00, 50, sl=" 6  "),  # CLOSING_PRINT, 16:03 - within grace
    ]
    df = parse_tick_payload(
        payload, capture_id="cap_x", listing_id="lst_a", instrument_id="ins_a", observed_at=OBS
    )

    bars_1d = aggregate_bars(df, interval="1d")
    assert len(bars_1d) == 1
    assert bars_1d.iloc[0]["close"] == 110.00
    assert bars_1d.iloc[0]["volume"] == 150

    bars_5m = aggregate_bars(df, interval="5m")
    buckets = {
        row["bucket_start"].strftime("%H:%M"): row for _, row in bars_5m.iterrows()
    }
    assert "16:00" not in buckets
    assert buckets["15:55"]["close"] == 110.00


def test_aggregate_bars_regular_print_in_grace_window_still_excluded():
    payload = [
        _tick(1, 15, 59, 0, 100.00, 100),
        _tick(2, 16, 3, 0, 110.00, 50),  # 16:03, REGULAR (no CLOSING_PRINT flag) - excluded
    ]
    df = parse_tick_payload(
        payload, capture_id="cap_x", listing_id="lst_a", instrument_id="ins_a", observed_at=OBS
    )

    bars_1d = aggregate_bars(df, interval="1d")
    assert len(bars_1d) == 1
    assert bars_1d.iloc[0]["close"] == 100.00   # not a price print...
    assert bars_1d.iloc[0]["volume"] == 150     # ...but still daily volume (V2)


def test_aggregate_bars_closing_print_past_grace_window_excluded():
    payload = [
        _tick(1, 15, 59, 0, 100.00, 100),
        _tick(2, 16, 11, 0, 999.00, 50, sl=" 6  "),  # CLOSING_PRINT but past 16:10 grace
    ]
    df = parse_tick_payload(
        payload, capture_id="cap_x", listing_id="lst_a", instrument_id="ins_a", observed_at=OBS
    )

    bars_1d = aggregate_bars(df, interval="1d")
    assert len(bars_1d) == 1
    assert bars_1d.iloc[0]["close"] == 100.00   # past the grace window: no price...
    assert bars_1d.iloc[0]["volume"] == 150     # ...but still daily volume (V2)


# ---------------------------------------------------------------------------
# aggregate_bars: null size accounting
# ---------------------------------------------------------------------------

def test_aggregate_bars_null_size_contributes_zero_and_is_counted():
    payload = [
        _tick(1, 10, 0, 0, 100.00, 100),
        _tick(2, 10, 0, 30, 100.00, None),  # null size
    ]
    df = parse_tick_payload(
        payload, capture_id="cap_x", listing_id="lst_a", instrument_id="ins_a", observed_at=OBS
    )

    bars, stats = aggregate_bars(df, interval="1d", return_stats=True)
    assert len(bars) == 1
    assert bars.iloc[0]["volume"] == 100  # null size contributes 0, not dropped
    assert isinstance(stats, BarStats)
    assert stats.null_size_rows == 1


def test_aggregate_bars_return_stats_default_false():
    payload = [_tick(1, 10, 0, 0, 100.00, 100)]
    df = parse_tick_payload(
        payload, capture_id="cap_x", listing_id="lst_a", instrument_id="ins_a", observed_at=OBS
    )
    result = aggregate_bars(df, interval="1d")
    assert not isinstance(result, tuple)


def test_aggregate_bars_null_size_stats_zero_when_no_nulls():
    payload = [_tick(1, 10, 0, 0, 100.00, 100)]
    df = parse_tick_payload(
        payload, capture_id="cap_x", listing_id="lst_a", instrument_id="ins_a", observed_at=OBS
    )
    _, stats = aggregate_bars(df, interval="1d", return_stats=True)
    assert stats.null_size_rows == 0


def test_reconcile_tranche_surfaces_null_size_count_from_stats():
    payload = [
        _tick(1, 10, 0, 0, 100.00, 1000),
        _tick(2, 10, 0, 30, 100.00, None),
    ]
    df = parse_tick_payload(
        payload, capture_id="cap_x", listing_id="lst_a", instrument_id="ins_a", observed_at=OBS
    )
    ours, stats = aggregate_bars(df, interval="1d", return_stats=True)
    theirs = _ref_eod(close=100.00, volume=1000)

    report = reconcile_tranche(
        {"listing_id": "lst_a"}, ours, theirs, our_daily_stats=stats,
    )
    assert report.status == "PASS"
    assert report.null_size_count == 1


def test_reconcile_tranche_null_size_count_defaults_to_zero():
    ours = _daily_bars(close=100.00, volume=1000)
    theirs = _ref_eod(close=100.00, volume=1000)
    report = reconcile_tranche({"listing_id": "lst_a"}, ours, theirs)
    assert report.null_size_count == 0


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
    assert row["listing_id"] is None
    assert row["trade_date"] is None
    import json
    details = json.loads(row["details_json"])
    assert details["work_id"] == "wrk_123"
    assert details["reason"] == "known vendor outage 2026-07-01"


def test_gate_waiver_row_threads_listing_id_and_trade_date():
    gate = Gate()
    row = gate.waive(
        "wrk_123", "known vendor outage 2026-07-01",
        listing_id="lst_a", trade_date=TRADE_DATE,
    )
    assert row["listing_id"] == "lst_a"
    assert row["trade_date"] == TRADE_DATE
    import json
    details = json.loads(row["details_json"])
    assert details["work_id"] == "wrk_123"
    assert details["reason"] == "known vendor outage 2026-07-01"



def test_aggregate_bars_v2_odd_lots_volume_only_and_official_close_record_excluded():
    # Phase-0 (2026-09-15): off-exchange odd lots ~4% below market set AAPL's
    # daily low under V1; the official-close summary record ('M') is not a trade.
    payload = [
        _tick(1, 10, 0, 0, 100.00, 100),
        _tick(2, 11, 0, 0, 96.00, 3, sl="@  I"),     # odd lot, far below market
        _tick(3, 15, 0, 0, 101.00, 200),
        _tick(4, 16, 0, 0, 101.00, 5000, sl="   M"), # market-center official close record
    ]
    df = parse_tick_payload(
        payload, capture_id="cap_x", listing_id="lst_a", instrument_id="ins_a", observed_at=OBS
    )
    row = aggregate_bars(df, interval="1d").iloc[0]
    assert row["low"] == 100.00                 # odd lot does not set the low
    assert row["volume"] == 100 + 3 + 200       # ...but counts; the M record does not


def test_reconcile_tranche_flags_high_low_divergence():
    ours = _daily_bars(close=100.00, volume=1000)
    ours.loc[0, "low"] = 96.00                  # a bad print that close/volume cannot see
    report = reconcile_tranche({"listing_id": "lst_a"}, ours, _ref_eod(close=100.00, volume=1000))
    assert report.status == "DIVERGENT"
    assert set(report.diffs_df["field"]) == {"low"}

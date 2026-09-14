"""Comparing two bar sets: price disagreements vs coverage gaps."""

import json
import math

import pandas as pd

from tick_lab.report import compare
from tick_lab.rollup import aggregate, to_minute_bars

IDX = pd.date_range("2023-05-12 13:30:00Z", periods=3, freq="1min")


def frame(closes, index=IDX):
    n = len(closes)
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": list(closes),
            "volume": [1000] * n,
        },
        index=index[:n],
    )


def test_identical_frames_report_no_discrepancies():
    rep = compare(frame([100.0, 101.0, 102.0]), frame([100.0, 101.0, 102.0]), "MSFT", "1m")
    assert rep.bars_compared == 3
    assert rep.discrepancies == []
    assert rep.largest_close() is None


def test_differences_within_tolerance_are_not_counted():
    rep = compare(frame([100.000, 101.0, 102.0]), frame([100.005, 101.0, 102.0]), "MSFT", "1m")
    assert rep.discrepancies == []


def test_difference_beyond_tolerance_is_counted():
    rep = compare(frame([100.0, 101.0, 102.0]), frame([100.5, 101.0, 102.0]), "MSFT", "1m")
    closes = [d for d in rep.discrepancies if d.field == "close"]
    assert len(closes) == 1
    assert closes[0].diff == -0.5


def test_largest_close_discrepancy_is_reported_with_bps_and_timestamp():
    rep = compare(frame([100.0, 101.0, 90.0]), frame([100.5, 101.0, 100.0]), "MSFT", "1m")
    worst = rep.largest_close()
    assert worst.timestamp == IDX[2]
    assert worst.diff == -10.0
    assert round(worst.bps, 1) == -1000.0


def test_coverage_gaps_are_counted_separately_from_price_discrepancies():
    ours = frame([100.0, 101.0, 102.0])
    theirs = frame([100.0, 101.0], index=IDX)
    rep = compare(ours, theirs, "MSFT", "1m")
    assert rep.bars_compared == 2
    assert rep.ours_only == [IDX[2]]
    assert rep.theirs_only == []
    assert rep.discrepancies == []


def test_bars_missing_on_our_side_are_reported_too():
    ours = frame([100.0, 101.0], index=IDX)
    theirs = frame([100.0, 101.0, 102.0])
    rep = compare(ours, theirs, "MSFT", "1m")
    assert rep.theirs_only == [IDX[2]]


def test_volume_is_compared_but_never_becomes_the_largest_close():
    ours = frame([100.0])
    theirs = frame([100.0])
    theirs.loc[theirs.index[0], "volume"] = 5000
    rep = compare(ours, theirs, "MSFT", "1m")
    assert any(d.field == "volume" for d in rep.discrepancies)
    assert rep.largest_close() is None


def test_no_overlap_reports_zero_bars_compared():
    ours = frame([100.0], index=IDX)
    theirs = frame([100.0], index=pd.date_range("2024-01-02 13:30:00Z", periods=1, freq="1min"))
    rep = compare(ours, theirs, "MSFT", "1m")
    assert rep.bars_compared == 0
    assert len(rep.ours_only) == 1 and len(rep.theirs_only) == 1


def test_text_output_names_the_three_categories():
    rep = compare(frame([100.0, 101.0, 90.0]), frame([100.0, 101.0, 100.0]), "MSFT", "1m")
    text = rep.to_text()
    assert "bars compared" in text
    assert "price discrepancies" in text
    assert "coverage gaps" in text
    assert "MSFT" in text


def test_dict_output_is_json_serialisable():
    rep = compare(frame([100.0, 90.0]), frame([100.0, 100.0]), "MSFT", "1m")
    payload = json.dumps(rep.to_dict())
    assert "largest_close_discrepancy" in payload


def test_notes_are_carried_into_the_report():
    rep = compare(frame([100.0]), frame([100.0]), "MSFT", "1d", notes=["stepped down from 1m"])
    assert "stepped down from 1m" in rep.to_text()


def test_a_nan_reference_close_before_a_larger_genuine_discrepancy_does_not_latch():
    # Reference (theirs) has NOT been through our rollup's dropna, so it can
    # carry a NaN close at a shared timestamp. That NaN sorts first in
    # timestamp order. A -50.0 genuine discrepancy follows at the second bar.
    # max() over NaN comparisons never replaces the first item it saw, so an
    # unguarded largest_close() would latch onto the NaN and hide the -50.0.
    ours = frame([100.0, 100.0, 100.0])
    theirs = frame([math.nan, 150.0, 102.0])
    rep = compare(ours, theirs, "MSFT", "1m")
    worst = rep.largest_close()
    assert worst is not None
    assert worst.timestamp == IDX[1]
    assert worst.diff == -50.0


def test_a_nan_on_our_side_is_excluded_from_largest_close():
    ours = frame([math.nan, 100.0, 100.0])
    theirs = frame([102.0, 101.0, 90.0])
    rep = compare(ours, theirs, "MSFT", "1m")
    worst = rep.largest_close()
    assert worst is not None
    assert worst.timestamp == IDX[2]
    assert worst.diff == 10.0


def test_non_finite_pairs_are_counted_and_excluded_from_price_discrepancies():
    ours = frame([100.0, 100.0, 100.0])
    theirs = frame([math.nan, 150.0, 100.0])
    rep = compare(ours, theirs, "MSFT", "1m")
    # bars_compared is about shared timestamps, unaffected by NaN values.
    assert rep.bars_compared == 3
    # The NaN close pair is not a price discrepancy -- it's a data-quality
    # fact, kept in its own bucket so it can't inflate or hide in the count.
    assert len(rep.non_finite) == 1
    assert rep.non_finite[0].timestamp == IDX[0]
    assert rep.non_finite[0].field == "close"
    assert all(math.isfinite(d.diff) for d in rep.discrepancies)
    close_discrepancies = [d for d in rep.discrepancies if d.field == "close"]
    assert len(close_discrepancies) == 1
    assert close_discrepancies[0].timestamp == IDX[1]


def test_non_finite_count_is_surfaced_in_text_and_dict():
    ours = frame([100.0, 100.0])
    theirs = frame([math.nan, 102.0])
    rep = compare(ours, theirs, "MSFT", "1m")
    text = rep.to_text()
    assert "non-finite" in text
    assert "1" in text.split("non-finite")[1].splitlines()[0]

    payload = rep.to_dict()
    assert len(payload["non_finite_comparisons"]) == 1
    assert payload["non_finite_comparisons"][0]["field"] == "close"
    # Confirm json-serialisable (NaN is valid in Python's json despite not
    # being valid JSON -- this just checks no exception and no crash on nan).
    json.dumps(payload)


def test_tolerance_boundary_is_inclusive():
    # diff == tolerance exactly must NOT be counted as a discrepancy. Uses
    # 0.5/0.5 rather than 0.01/0.01 because 0.5 is exactly representable in
    # binary floating point, so abs(diff) lands on tolerance exactly rather
    # than a hair over it. A regression to strict `<` would pass every other
    # existing test but would fail this one.
    rep = compare(frame([100.0]), frame([100.5]), "MSFT", "1m", tolerance=0.5)
    assert rep.discrepancies == []


def _adapter_shaped_daily_frame(day: str, ohlcv: dict) -> pd.DataFrame:
    """Build a one-row daily frame the way both EODHD adapters do: a naive
    calendar date localized straight to UTC midnight (`reference/eodhd_api.py`
    and `reference/eodhd_local.py` both do `tz_localize("UTC")` on a naive
    `pd.to_datetime(date)` index) -- never America/New_York.
    """
    index = pd.DatetimeIndex([pd.Timestamp(day)]).tz_localize("UTC")
    return pd.DataFrame([ohlcv], index=index)


def test_daily_rollup_actually_intersects_an_adapter_shaped_daily_frame():
    """End-to-end regression for the 1d label mismatch: aggregate(bars, "1d")
    used to label a daily bar at the Eastern midnight's UTC clock time
    (04:00Z in EDT), while both EODHD adapters label a daily bar at UTC
    midnight (00:00Z) for the same calendar date. report.compare() diffs by
    intersecting the two indexes, so that mismatch meant a 1d comparison
    always reported zero bars compared -- silently, since 0 discrepancies
    also looks like a passing comparison. This is the "the report ran and
    found nothing" scenario made concrete.
    """
    ticks = pd.DataFrame(
        {"price": [310.55, 312.00, 308.97], "volume": [500, 100, 400]},
        index=pd.DatetimeIndex(
            ["2023-05-12 09:30:00", "2023-05-12 12:00:00", "2023-05-12 15:59:00"]
        )
        .tz_localize("America/New_York")
        .tz_convert("UTC"),
    )
    ours_1m = to_minute_bars(ticks, session="regular")
    ours_daily = aggregate(ours_1m, "1d")

    theirs_daily = _adapter_shaped_daily_frame(
        "2023-05-12",
        {"open": 310.55, "high": 312.00, "low": 308.97, "close": 308.97, "volume": 1000},
    )

    rep = compare(ours_daily, theirs_daily, "MSFT", "1d")
    assert rep.bars_compared == 1, (
        f"expected the single daily bar to intersect; got {rep.bars_compared} "
        f"(ours={list(ours_daily.index)}, theirs={list(theirs_daily.index)})"
    )
    assert rep.ours_only == []
    assert rep.theirs_only == []
    assert rep.discrepancies == []

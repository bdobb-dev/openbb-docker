"""Tick -> 1-minute OHLCV, and 1-minute -> coarser bars."""

import pandas as pd
import pytest

from tick_lab.rollup import BAR_COLUMNS, SessionError, aggregate, to_minute_bars


def ticks(rows):
    """rows: list of (eastern timestamp string, price, volume)."""
    idx = pd.DatetimeIndex([pd.Timestamp(t) for t, _, _ in rows]).tz_localize(
        "America/New_York"
    ).tz_convert("UTC")
    return pd.DataFrame(
        {"price": [p for _, p, _ in rows], "volume": [v for _, _, v in rows]},
        index=idx,
    )


def test_ohlcv_within_one_minute():
    df = to_minute_bars(ticks([
        ("2023-05-12 09:30:00", 310.55, 500),
        ("2023-05-12 09:30:30", 311.00, 200),
        ("2023-05-12 09:30:45", 310.00, 100),
        ("2023-05-12 09:30:59", 310.40, 300),
    ]))
    assert len(df) == 1
    bar = df.iloc[0]
    assert (bar.open, bar.high, bar.low, bar.close) == (310.55, 311.00, 310.00, 310.40)
    assert bar.volume == 1100


def test_columns_and_index_contract():
    df = to_minute_bars(ticks([("2023-05-12 09:30:00", 310.0, 100)]))
    assert list(df.columns) == BAR_COLUMNS
    assert str(df.index.tz) == "UTC"


def test_bars_are_left_labeled():
    df = to_minute_bars(ticks([("2023-05-12 09:30:45", 310.0, 100)]))
    assert df.index[0] == pd.Timestamp("2023-05-12 13:30:00", tz="UTC")


def test_empty_minutes_are_dropped_not_forward_filled():
    df = to_minute_bars(ticks([
        ("2023-05-12 09:30:00", 310.0, 100),
        ("2023-05-12 09:33:00", 311.0, 100),
    ]))
    assert len(df) == 2


def test_regular_session_excludes_premarket():
    df = to_minute_bars(ticks([
        ("2023-05-12 09:15:00", 309.5, 100),
        ("2023-05-12 09:30:00", 310.5, 100),
    ]))
    assert len(df) == 1
    assert df.index[0] == pd.Timestamp("2023-05-12 13:30:00", tz="UTC")


def test_regular_session_excludes_the_1600_print():
    """16:00:00 is exclusive, so a closing-auction print is out under 'regular'.

    This is the single biggest lever on the discrepancy count, which is why
    --session exists rather than being hard-coded.
    """
    df = to_minute_bars(ticks([
        ("2023-05-12 15:59:30", 308.90, 400),
        ("2023-05-12 16:00:00", 308.97, 10000),
    ]))
    assert len(df) == 1
    assert df.iloc[0].close == 308.90


def test_all_session_keeps_everything():
    df = to_minute_bars(
        ticks([
            ("2023-05-12 09:15:00", 309.5, 100),
            ("2023-05-12 09:30:00", 310.5, 100),
            ("2023-05-12 16:00:00", 308.97, 10000),
        ]),
        session="all",
    )
    assert len(df) == 3


def test_0930_boundary_is_inclusive():
    df = to_minute_bars(ticks([("2023-05-12 09:30:00", 310.5, 100)]))
    assert len(df) == 1


def test_unknown_session_is_rejected():
    with pytest.raises(SessionError, match="weekends"):
        to_minute_bars(ticks([("2023-05-12 09:30:00", 310.0, 100)]), session="weekends")


def test_empty_input_returns_empty_frame_with_columns():
    empty = pd.DataFrame(
        {"price": [], "volume": []},
        index=pd.DatetimeIndex([], tz="UTC"),
    )
    df = to_minute_bars(empty)
    assert df.empty and list(df.columns) == BAR_COLUMNS


def test_aggregate_to_daily_uses_eastern_calendar_days():
    bars = to_minute_bars(ticks([
        ("2023-05-12 09:30:00", 310.55, 500),
        ("2023-05-12 12:00:00", 312.00, 100),
        ("2023-05-12 15:59:00", 308.97, 400),
    ]))
    daily = aggregate(bars, "1d")
    assert len(daily) == 1
    row = daily.iloc[0]
    assert (row.open, row.high, row.low, row.close) == (310.55, 312.00, 308.97, 308.97)
    assert row.volume == 1000


def test_aggregate_to_daily_keeps_after_hours_trade_crossing_utc_midnight():
    """A 20:30 ET trade on 2023-05-12 is 00:30 UTC on 2023-05-13 -- it crosses
    UTC midnight but belongs to the same Eastern session day. Bucketing on the
    raw UTC index (no tz_convert to EASTERN) would split it into a second
    daily bar dated 2023-05-13; Eastern bucketing keeps it in one bar dated
    2023-05-12.
    """
    bars = to_minute_bars(
        ticks([
            ("2023-05-12 09:30:00", 310.55, 500),
            ("2023-05-12 12:00:00", 312.00, 100),
            ("2023-05-12 15:59:00", 308.97, 400),
            ("2023-05-12 20:30:00", 305.00, 900),
        ]),
        session="all",
    )
    daily = aggregate(bars, "1d")
    assert len(daily) == 1
    # The bucketing is unchanged (this trade still lands in the 2023-05-12
    # Eastern session day); only the label changed, from the Eastern
    # midnight's UTC clock time (04:00Z in EDT) to UTC midnight of that same
    # calendar date -- see aggregate()'s docstring.
    assert daily.index[0] == pd.Timestamp("2023-05-12 00:00:00", tz="UTC")
    row = daily.iloc[0]
    assert (row.open, row.high, row.low, row.close) == (310.55, 312.00, 305.00, 305.00)
    assert row.volume == 1900


def test_aggregate_to_five_minutes():
    bars = to_minute_bars(ticks([
        ("2023-05-12 09:30:00", 310.0, 100),
        ("2023-05-12 09:31:00", 311.0, 100),
        ("2023-05-12 09:36:00", 312.0, 100),
    ]))
    five = aggregate(bars, "5m")
    assert len(five) == 2
    assert five.iloc[0].high == 311.0


def test_aggregate_1m_is_a_passthrough():
    bars = to_minute_bars(ticks([("2023-05-12 09:30:00", 310.0, 100)]))
    assert aggregate(bars, "1m").equals(bars)


def test_aggregate_rejects_unknown_interval():
    bars = to_minute_bars(ticks([("2023-05-12 09:30:00", 310.0, 100)]))
    with pytest.raises(ValueError, match="7m"):
        aggregate(bars, "7m")

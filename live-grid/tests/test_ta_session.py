# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Tests for app.ta.session — the regular-session bar filter."""

from datetime import datetime, timedelta

from app.ta.session import SESSIONS, regular_only


def minute_bars(day: str, start_utc: str, hours: int) -> list[dict]:
    """1m bars with naive-UTC timestamps, the shape build_series returns."""
    first = datetime.fromisoformat(f"{day}T{start_utc}")
    return [{"date": (first + timedelta(minutes=i)).isoformat(),
             "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
             "volume": 100.0, "vwap": 1.0}
            for i in range(hours * 60)]


class TestRegularOnly:
    def test_a_us_day_is_cut_to_the_regular_session(self):
        # 2024-01-03 is EST (UTC-5): 04:00 ET premarket opens at 09:00 UTC,
        # and the 16 hours to 20:00 ET run to 01:00 UTC the next day.
        bars = minute_bars("2024-01-03", "09:00", 16)
        kept = regular_only(bars, "AAPL", "1m")
        assert len(kept) == 390, "09:30 to 15:59 inclusive is 390 minutes"
        assert kept[0]["date"] == "2024-01-03T14:30:00"   # 09:30 ET
        assert kept[-1]["date"] == "2024-01-03T20:59:00"  # 15:59 ET

    def test_the_close_is_exclusive(self):
        # [open, close): the 16:00 ET bar is the close auction's, not the
        # session's, and including it would double-count the closing print.
        bars = minute_bars("2024-01-03", "20:58", 1)
        assert [b["date"] for b in regular_only(bars, "AAPL", "1m")] == [
            "2024-01-03T20:58:00", "2024-01-03T20:59:00",
        ]

    def test_summer_reads_the_zone_not_a_fixed_offset(self):
        # 2024-07-03 is EDT (UTC-4): the open is 13:30 UTC, an hour earlier
        # than in January. A hardcoded offset would be wrong half the year.
        bars = minute_bars("2024-07-03", "13:00", 2)
        assert regular_only(bars, "AAPL", "1m")[0]["date"] == "2024-07-03T13:30:00"

    def test_crypto_has_no_session_and_is_untouched(self):
        bars = minute_bars("2024-01-03", "09:00", 16)
        assert regular_only(bars, "BTC-USD", "1m") == bars

    def test_forex_has_no_session_and_is_untouched(self):
        bars = minute_bars("2024-01-03", "09:00", 16)
        assert regular_only(bars, "EURUSD", "1m") == bars

    def test_daily_bars_are_never_filtered(self):
        # A daily bar IS a session; its timestamp is midnight, so a naive
        # time-of-day filter would delete the entire history.
        bars = [{"date": "2024-01-03", "close": 1.0},
                {"date": "2024-01-04", "close": 2.0}]
        assert regular_only(bars, "AAPL", "1d") == bars

    def test_coarser_than_daily_is_never_filtered(self):
        bars = [{"date": "2024-01-03", "close": 1.0}]
        assert regular_only(bars, "AAPL", "1W") == bars
        assert regular_only(bars, "AAPL", "1mo") == bars

    def test_an_unreadable_timestamp_is_kept(self):
        bars = [{"date": "not a date"}, {"date": "2024-01-03T14:30:00"}]
        assert regular_only(bars, "AAPL", "1m") == bars

    def test_a_datetime_date_works_as_well_as_a_string(self):
        # Tick-derived bars carry real timestamps, not ISO strings.
        bars = [{"date": datetime(2024, 1, 3, 14, 30)},
                {"date": datetime(2024, 1, 3, 9, 0)}]
        assert regular_only(bars, "AAPL", "1m") == bars[:1]

    def test_sessions_is_keyed_by_feed(self):
        # The table is keyed as classify() names the feed, so a later
        # per-session pass (bd windows) can look up the same way.
        assert SESSIONS["us"] == ("America/New_York", "09:30", "16:00")
        assert "crypto" not in SESSIONS and "forex" not in SESSIONS

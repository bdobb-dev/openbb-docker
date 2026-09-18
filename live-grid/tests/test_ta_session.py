# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Tests for app.ta.session — the regular-session bar filter."""

from datetime import datetime, timedelta

import polars as pl

from app.ta.payload import bars_to_frame
from app.ta.session import SESSIONS, regular_only, session_closes


def minute_bars(day: str, start_utc: str, hours: int) -> list[dict]:
    """1m bars with naive-UTC timestamps, the shape build_series returns."""
    first = datetime.fromisoformat(f"{day}T{start_utc}")
    return [{"date": (first + timedelta(minutes=i)).isoformat(),
             "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
             "volume": 100.0, "vwap": 1.0}
            for i in range(hours * 60)]


def session_bars(day: str, closes: list[float]) -> list[dict]:
    """1m bars inside one US regular session, one per close given.

    09:30 ET is 14:30 UTC in January; `bars_to_frame` reads `adjusted_close`.
    """
    first = datetime.fromisoformat(f"{day}T14:30:00")
    return [{"date": (first + timedelta(minutes=i)).isoformat(),
             "open": c, "high": c, "low": c, "close": c, "adjusted_close": c,
             "volume": 100.0, "vwap": c}
            for i, c in enumerate(closes)]


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


class TestSessionCloses:
    """`session_closes` -- the series a `bd` window is computed on."""

    def frame(self) -> pl.DataFrame:
        # Two full US sessions plus a third that is still running: three bars
        # each, 09:30-09:32 ET (14:30-14:32 UTC in January).
        bars = []
        for day, closes in (("2024-01-03", [10.0, 11.0, 12.0]),
                            ("2024-01-04", [20.0, 21.0, 22.0]),
                            ("2024-01-05", [30.0, 31.0, 32.0])):
            bars.extend(session_bars(day, closes))
        return bars_to_frame(bars)

    def test_one_row_per_session_dated_by_its_last_bar(self):
        closes = session_closes(self.frame(), "us")
        assert closes.height == 3
        assert [str(d) for d in closes["date"].to_list()] == [
            "2024-01-03 14:32:00", "2024-01-04 14:32:00", "2024-01-05 14:32:00",
        ]
        # The third session is partial (a running bar) and still counts: the
        # spec's 50bd is 49 prior closes PLUS today.
        assert closes["close"].to_list() == [12.0, 22.0, 32.0]

    def test_the_session_row_aggregates_its_bars(self):
        bars = session_bars("2024-01-03", [10.0, 14.0, 12.0])
        row = session_closes(bars_to_frame(bars), "us").row(0, named=True)
        assert row["open"] == 10.0       # first
        assert row["high"] == 14.0       # max
        assert row["low"] == 10.0        # min
        assert row["close"] == 12.0      # last
        assert row["volume"] == 300.0    # sum
        assert row["vwap"] == 12.0       # last

    def test_a_session_is_the_exchange_local_date_not_the_utc_one(self):
        # 20:30 UTC on 2024-01-03 is 15:30 ET -- the same session as 14:30 UTC,
        # and a UTC-day grouping would agree here. 01:30 UTC on 2024-01-04 is
        # 20:30 ET on the 3rd: the SAME US session, a different UTC day.
        bars = (session_bars("2024-01-03", [10.0])
                + [{"date": "2024-01-04T01:30:00", "open": 9.0, "high": 9.0,
                    "low": 9.0, "close": 9.0, "volume": 100.0, "vwap": 9.0}])
        closes = session_closes(bars_to_frame(bars), "us")
        assert closes.height == 1 and closes["close"].to_list() == [9.0]

    def test_a_feed_with_no_session_falls_back_to_utc_days(self):
        bars = (session_bars("2024-01-03", [10.0])
                + [{"date": "2024-01-04T01:30:00", "open": 9.0, "high": 9.0,
                    "low": 9.0, "close": 9.0, "volume": 100.0, "vwap": 9.0}])
        closes = session_closes(bars_to_frame(bars), "crypto")
        assert closes.height == 2, "crypto has no exchange clock: UTC days"

    def test_an_empty_frame_comes_back_empty(self):
        assert session_closes(bars_to_frame([]), "us").height == 0

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The per-day key grammar: `<base>_<YYYY_MM_DD>` is one symbol's one day.

Runs with the server tests:
    uv run --with fastmcp --with pytest --with pandas --with ./openbb-deltalake \
        python -m pytest mcp_stores -q
"""
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daykeys  # noqa: E402

LIVE = [
    "AAPL_2026_09_01", "AAPL_2026_09_02", "AAPL_2026_09_04",
    "MSFT_2026_09_01", "goog_quote_2023_05_12",
]


def test_split_reads_the_day_suffix():
    assert daykeys.split("AAPL_2026_09_11") == ("AAPL", date(2026, 9, 11))
    assert daykeys.split("goog_quote_2023_05_12") == ("goog_quote", date(2023, 5, 12))


def test_split_leaves_a_plain_key_alone():
    assert daykeys.split("AAPL") == ("AAPL", None)
    assert daykeys.split("AAPL.US") == ("AAPL.US", None)


def test_split_treats_an_impossible_day_as_part_of_the_name():
    # A suffix that looks like a day but is not one is a name, not a partition.
    assert daykeys.split("X_2026_13_40") == ("X_2026_13_40", None)


def test_bases_collapses_and_sorts():
    assert daykeys.bases(LIVE) == ["AAPL", "MSFT", "goog_quote"]
    assert daykeys.bases(["SPY", "AAPL"]) == ["AAPL", "SPY"]


def test_day_keys_lists_a_base_in_day_order():
    assert daykeys.day_keys(LIVE, "AAPL") == [
        "AAPL_2026_09_01", "AAPL_2026_09_02", "AAPL_2026_09_04",
    ]


def test_day_keys_of_a_plain_key_is_itself():
    assert daykeys.day_keys(["SPY", "AAPL_2026_09_01"], "SPY") == ["SPY"]


def test_day_keys_of_an_unknown_base_is_empty():
    assert daykeys.day_keys(LIVE, "NOPE") == []


def test_in_window_with_no_bounds_is_the_newest_day_only():
    # An unfiltered read stays bounded: one day, never the whole symbol.
    assert daykeys.in_window(LIVE, "AAPL", None, None) == ["AAPL_2026_09_04"]
    assert daykeys.in_window(LIVE, "AAPL", "", "") == ["AAPL_2026_09_04"]


def test_in_window_clips_by_calendar_day_from_timestamps():
    got = daykeys.in_window(LIVE, "AAPL", "2026-09-01 22:00:00", "2026-09-02 01:00:00")
    assert got == ["AAPL_2026_09_01", "AAPL_2026_09_02"]


def test_in_window_accepts_date_and_datetime_objects_and_open_sides():
    assert daykeys.in_window(LIVE, "AAPL", date(2026, 9, 2), None) == [
        "AAPL_2026_09_02", "AAPL_2026_09_04",
    ]
    assert daykeys.in_window(LIVE, "AAPL", None, datetime(2026, 9, 1, 12)) == ["AAPL_2026_09_01"]


def test_in_window_outside_every_day_is_empty():
    assert daykeys.in_window(LIVE, "AAPL", "2026-10-01", "2026-10-31") == []


def test_in_window_of_a_plain_key_is_itself_whatever_the_bounds():
    assert daykeys.in_window(["SPY"], "SPY", "2000-01-01", "2000-01-02") == ["SPY"]

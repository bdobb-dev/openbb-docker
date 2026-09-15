# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The computation, without the API: pandas-market-calendars in, EODHD v2 out."""
import json
from pathlib import Path

import pytest

from openbb_trading_calendar.calendar import NAMES, table_rows, trading_calendar

FIXTURE = Path(__file__).parent / "nyse-calendar-2026-2028.json"


def holidays(mic, year):
    return trading_calendar(mic, year)["data"]["ExchangeHolidays"]


def test_independence_day_observed_2026_is_a_full_closure():
    # The bundled NYSE file had this as a 13:00 early close; NYSE and pmc both
    # close the market all day. The fixture copy beside this test is corrected.
    assert holidays("XNYS", 2026)["2026-07-03"] == {"Holiday": "July 4th", "Type": "Official"}


def test_day_after_thanksgiving_2026_closes_at_13():
    assert holidays("XNYS", 2026)["2026-11-27"] == {
        "Holiday": "Black Friday",
        "Type": "EarlyClose",
        "EarlyClose": "13:00:00",
    }


def test_nyse_2026_to_2028_matches_the_corrected_fixture():
    # Dates, types and early-close times must agree exactly. Names are pmc's
    # (spec §3), which word things differently from the NYSE page.
    expected = json.loads(FIXTURE.read_text())["data"]["ExchangeHolidays"]
    got = {}
    for year in (2026, 2027, 2028):
        got.update(holidays("XNYS", year))

    def shape(entries):
        return {day: (e["Type"], e.get("EarlyClose")) for day, e in entries.items()}

    assert shape(got) == shape(expected)


def test_nyse_header_and_regular_hours():
    data = trading_calendar("XNYS", 2026)["data"]
    assert data["Name"] == "New York Stock Exchange"
    assert data["Code"] == data["OperatingMIC"] == "XNYS"
    assert data["Timezone"] == "America/New_York"
    assert data["TradingHours"] == {
        "Open": "09:30:00",
        "Close": "16:00:00",
        "WorkingDays": "Mon, Tue, Wed, Thu, Fri",
    }


def test_tokyo_carries_its_lunch_break():
    assert trading_calendar("XTKS", 2026)["data"]["TradingHours"] == {
        "Open": "09:00:00",
        "Close": "15:30:00",
        "WorkingDays": "Mon, Tue, Wed, Thu, Fri",
        "LunchBreakStart": "11:30:00",
        "LunchBreakEnd": "12:30:00",
    }


def test_regular_hours_are_the_years_usual_session_not_its_last():
    # XTKS moved its close from 15:00 to 15:30 late in 2024, so 2024's last
    # sessions close at 15:30 while most of the year closed at 15:00.
    assert trading_calendar("XTKS", 2024)["data"]["TradingHours"]["Close"] == "15:00:00"


def test_hong_kong_unnamed_dates_get_fallback_names():
    days = holidays("XHKG", 2026)
    # Lunar New Year closures are ad-hoc dates in pmc, with no rule name.
    assert days["2026-02-17"] == {"Holiday": "Market holiday", "Type": "Official"}
    # And the half day before them is an unnamed ad-hoc early close.
    assert days["2026-02-16"] == {
        "Holiday": "Early close",
        "Type": "EarlyClose",
        "EarlyClose": "12:00:00",
    }


@pytest.mark.parametrize("mic", list(NAMES))
def test_every_served_mic_computes(mic):
    data = trading_calendar(mic, 2026)["data"]
    assert data["Code"] == data["OperatingMIC"] == mic
    assert data["ExchangeHolidays"]


@pytest.mark.parametrize("mic,year", [("ZZZZ", 2026), ("XNYS", 1989), ("XNYS", 2041)])
def test_unknown_mic_or_year_out_of_range_has_no_calendar(mic, year):
    assert trading_calendar(mic, year) is None


@pytest.mark.parametrize("year", [1990, 2040])
def test_range_edges_are_served(year):
    assert trading_calendar("XNYS", year) is not None


def test_table_rows_flatten_the_holidays_in_date_order():
    rows = table_rows(trading_calendar("XNYS", 2026))
    assert rows[0] == {
        "date": "2026-01-01",
        "holiday": "New Year's Day",
        "type": "Official",
        "early_close": None,
    }
    assert {"date": "2026-11-27", "holiday": "Black Friday", "type": "EarlyClose",
            "early_close": "13:00:00"} in rows
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)

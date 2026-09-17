# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Exchange trading calendars from pandas-market-calendars, in EODHD v2 shape.

Pure computation: no OpenBB, no FastAPI. router.py is a thin wrapper that turns
a `None` from here into a 404, which is what lets the tests exercise every rule
below without booting the API. The output is the `data` object of EODHD's
exchange-details v2 response, so bdobb's one parser reads this route, the live
EODHD API and its bundled NYSE file alike."""
from __future__ import annotations

import pandas as pd
import pandas_market_calendars as mcal

# The six venues v5.3.0 serves, keyed by ISO 10383 MIC -- the code
# pandas-market-calendars accepts in get_calendar() and the code the app sends
# as `exchange`. The display names are ours: pmc's own `name` is a short alias
# ("NYSE"). Insertion order is the Trading calendar widget's dropdown order.
NAMES = {
    "XNYS": "New York Stock Exchange",
    "XLON": "London Stock Exchange",
    "XTSE": "Toronto Stock Exchange",
    "XETR": "Xetra",
    "XTKS": "Tokyo Stock Exchange",
    "XHKG": "Hong Kong Exchanges and Clearing",
}

# The app never asks outside this window (spec §2), and it bounds the work one
# request can cause.
FIRST_YEAR, LAST_YEAR = 1990, 2040

# All six trade Monday to Friday, and pmc's weekmask agrees. Weekends are
# implied by this field and never listed as holidays, as in EODHD.
WORKING_DAYS = "Mon, Tue, Wed, Thu, Fri"


def _local(column: pd.Series, tz) -> pd.Series:
    """UTC session instants -> exchange-local "HH:MM:SS" strings."""
    return column.dt.tz_convert(tz).dt.strftime("%H:%M:%S")


def _names(cal, start: str, end: str) -> dict[str, str]:
    """Date -> holiday name, from pmc's named rules.

    Regular holidays and the named early-close calendars carry rule names.
    Ad-hoc dates do not (XHKG's Lunar New Year closures, one XHKG half day), and
    the caller falls back to a generic label for those."""
    names: dict[str, str] = {}
    for rules in [cal.regular_holidays] + [c for _, c in cal.special_closes]:
        for day, name in rules.holidays(start, end, return_name=True).items():
            names.setdefault(day.strftime("%Y-%m-%d"), name)
    return names


def trading_calendar(mic: str, year: int) -> dict | None:
    """One exchange-year in EODHD v2 shape, or None when it is not served."""
    if mic not in NAMES or not FIRST_YEAR <= year <= LAST_YEAR:
        return None
    cal = mcal.get_calendar(mic)
    start, end = f"{year}-01-01", f"{year}-12-31"
    sessions = cal.schedule(start_date=start, end_date=end)
    early = cal.early_closes(sessions)

    # Regular hours are the most common open and close among full sessions, not
    # the last row's: an early close would report 13:00, and XTKS moved its
    # close from 15:00 to 15:30 late in 2024, so 2024's regular close is 15:00.
    normal = sessions.drop(early.index)
    hours = {
        "Open": _local(normal["market_open"], cal.tz).mode()[0],
        "Close": _local(normal["market_close"], cal.tz).mode()[0],
        "WorkingDays": WORKING_DAYS,
    }
    # Only XTKS and XHKG break for lunch. pmc adds break_* columns for them
    # alone, and EODHD omits the fields for venues without one.
    if "break_start" in sessions.columns:
        hours["LunchBreakStart"] = _local(normal["break_start"], cal.tz).mode()[0]
        hours["LunchBreakEnd"] = _local(normal["break_end"], cal.tz).mode()[0]

    names = _names(cal, start, end)
    holidays: dict[str, dict] = {}
    # A full closure is a weekday with no session.
    for day in pd.bdate_range(start, end).difference(sessions.index):
        key = day.strftime("%Y-%m-%d")
        holidays[key] = {"Holiday": names.get(key, "Market holiday"), "Type": "Official"}
    for day, close in _local(early["market_close"], cal.tz).items():
        key = day.strftime("%Y-%m-%d")
        holidays[key] = {
            "Holiday": names.get(key, "Early close"),
            "Type": "EarlyClose",
            "EarlyClose": close,
        }

    return {
        "data": {
            "Name": NAMES[mic],
            "Code": mic,
            "OperatingMIC": mic,
            "Timezone": str(cal.tz),
            "TradingHours": hours,
            # EODHD does not promise key order; we sort, so rows come out dated.
            "ExchangeHolidays": dict(sorted(holidays.items())),
        }
    }


def table_rows(calendar: dict) -> list[dict]:
    """The Trading calendar widget's rows: one per holiday or early close."""
    return [
        {
            "date": day,
            "holiday": entry["Holiday"],
            "type": entry["Type"],
            "early_close": entry.get("EarlyClose"),
        }
        for day, entry in calendar["data"]["ExchangeHolidays"].items()
    ]

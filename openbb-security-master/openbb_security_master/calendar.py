# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The /calendar router: the client's trading-calendar contract, security master first.

One plain GET route, `/api/v1/calendar/trading?exchange=<MIC>&year=<YYYY>`,
answering the EODHD v2 envelope (`{ data: { Timezone, TradingHours,
ExchangeHolidays } }`) that bdobb-v2's `tradingCalendar.ts` normalizes. It is
registered on `router.api_router` directly, not through `@router.command`,
because the client contract predates this endpoint and expects the raw
envelope, not an OBBject wrapping.

Resolution order:

1. security-master-api MarketCalendar, `include_closed=True`, over the year.
   Any rows at all mean the security master covers this exchange and its
   provenance (religious-authority captures, temporal evidence) wins; the
   pandas calendar is never consulted.
2. `exchange_calendars` for the MIC (XNYS, XLON, XTKS, ...). This is the
   pandas-business-day fallback the client asks about for history the
   security master has not ingested: rule calendars give every official
   closure (including Good Friday, which the U.S. federal table misses) and
   exchange-local early-close times back to each calendar's start.
3. 404. An unknown MIC in both sources is the client's designed answer for
   "no calendar": `useTradingCalendar` remembers the 404 and falls through
   to its bundled file, then to the weekday table.

The route never raises OpenBBError: anything short of an answer is a 404.
"""

from __future__ import annotations

from datetime import date as dateType
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    pass

YEAR_MIN = 1990
YEAR_MAX = 2040
_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class _CalendarRouterSentinel:
    """Import guard: the module body builds the router once, at import."""


def _hhmmss(value) -> str | None:
    """`datetime.time`/Timestamp → 'HH:MM:SS', or None when not a time."""
    t = getattr(value, "time", None)
    if callable(t):  # a pandas Timestamp carries .time(); a datetime.time is used directly
        t = t()
    else:
        t = value
    if not hasattr(t, "hour"):
        return None
    return f"{t.hour:02d}:{t.minute:02d}:{t.second:02d}"


def _modal_times(rows: list[dict]) -> tuple[str | None, str | None]:
    """Most common (open, close) clock times across ordinary open sessions.

    Ordinary means not special_open/special_close, so an early close never
    drags the regular session shorter. Seconds are kept because some venues
    genuinely open off the minute.
    """
    from collections import Counter

    counts: Counter = Counter()
    for row in rows:
        if row.get("market_effect") != "open" or row.get("session_status") != "open":
            continue
        if row.get("special_open") or row.get("special_close"):
            continue
        o, c = row.get("market_open"), row.get("market_close")
        if not o or not c:
            continue
        counts[(o[11:19], c[11:19])] += 1
    if not counts:
        return None, None
    (open_t, close_t), _ = counts.most_common(1)[0]
    return open_t, close_t


def _envelope_from_security_master(rows: list[dict]) -> dict | None:
    """Project security-master MarketCalendar rows into the EODHD v2 envelope.

    Holidays come from rows whose market effect is closed, named from
    `holiday_name` (the provenance that makes the lunar-holiday scenarios
    worth serving rather than projecting). Early closes come from rows the
    authority marks special_close: the close on that row IS the early-close
    time, already converted to the exchange zone by the projection.
    """
    tz = next((r.get("timezone") for r in rows if r.get("timezone")), None)
    open_t, close_t = _modal_times(rows)
    if tz is None or open_t is None or close_t is None:
        return None
    holidays: dict[str, dict] = {}
    for row in rows:
        day = row.get("session_date")
        if not isinstance(day, str):
            day = day.isoformat() if isinstance(day, dateType) else None
        if day is None:
            continue
        if row.get("market_effect") == "closed":
            name = row.get("holiday_name")
            holidays[day] = {"Holiday": name or "Exchange holiday", "Type": "Official"}
        elif row.get("special_close") and row.get("market_close"):
            name = row.get("holiday_name")
            entry = {"Holiday": name or "Early close", "Type": "EarlyClose",
                     "EarlyClose": row["market_close"][11:19]}
            holidays[day] = entry
    hours = {"Open": open_t, "Close": close_t,
             "WorkingDays": ", ".join(_WEEKDAY_NAMES[:5])}
    return {"data": {"Timezone": tz, "TradingHours": hours, "ExchangeHolidays": holidays}}


def _envelope_from_exchange_calendars(mic: str, year: int) -> dict | None:
    """Build the same envelope from the exchange_calendars rules for `mic`.

    Imported lazily: the dependency is declared, but a route that answers
    from the security master should still work in a test image that has not
    built it.
    """
    import exchange_calendars as xc

    start, end = f"{year}-01-01", f"{year}-12-31"
    try:
        # Bound the instance to the requested year: exchange_calendars defaults
        # its schedule to roughly "a decade around today", which leaves the
        # client's 1990–2040 window (see tradingCalendar.ts) empty.
        cal = xc.get_calendar(mic.upper(), start=start, end=end)
    except Exception:  # InvalidCalendarName and friends: the MIC is simply not known
        return None
    tz_name = str(cal.tz)
    open_time = cal.open_times[-1][1]
    close_time = cal.close_times[-1][1]
    mask = getattr(cal, "weekmask", "1111100")
    working = [name for name, flag in zip(_WEEKDAY_NAMES, str(mask)) if flag == "1"]
    holidays: dict[str, dict] = {}

    regular = cal.regular_holidays
    if regular is not None:
        named = regular.holidays(start, end, return_name=True)
        for day, name in named.items():
            holidays[day.strftime("%Y-%m-%d")] = {"Holiday": str(name), "Type": "Official"}
    for day in cal.adhoc_holidays:
        if year == day.year:
            holidays.setdefault(day.strftime("%Y-%m-%d"),
                                {"Holiday": "Special closure", "Type": "Official"})

    schedule = cal.schedule.loc[start:end]
    if schedule.empty:
        return None
    # Nameless weekday closures the rule table misses (observance shifts,
    # one-off notices): any working weekday with no session is an Official
    # closure. Saturday rules never surface here — Sat/Sun are not sessions.
    sessions = {d.date() for d in schedule.index}
    import datetime as _dt

    day = _dt.date(year, 1, 1)
    year_end = _dt.date(year, 12, 31)
    working_weekdays = {name[0] for name in zip(_WEEKDAY_NAMES, str(mask)) if name[1] == "1"}
    while day <= year_end:
        if _WEEKDAY_NAMES[day.weekday()] in working_weekdays and day not in sessions:
            holidays.setdefault(day.isoformat(), {"Holiday": "Exchange holiday", "Type": "Official"})
        day += _dt.timedelta(days=1)
    closes = cal.early_closes.intersection(schedule.index)
    for day in closes:
        local_close = schedule.loc[day, "close"].tz_convert(cal.tz)
        holidays[day.strftime("%Y-%m-%d")] = {
            "Holiday": "Early close", "Type": "EarlyClose",
            "EarlyClose": _hhmmss(local_close) or close_time.strftime("%H:%M:%S"),
        }
    hours = {
        "Open": open_time.strftime("%H:%M:%S"),
        "Close": close_time.strftime("%H:%M:%S"),
        "WorkingDays": ", ".join(working),
    }
    return {"data": {"Timezone": tz_name, "TradingHours": hours, "ExchangeHolidays": holidays}}


def trading_calendar(exchange: str, year: int) -> JSONResponse:
    """GET /api/v1/calendar/trading — the EODHD v2 envelope or a designed 404."""
    if not (YEAR_MIN <= year <= YEAR_MAX):
        return JSONResponse({"detail": f"year must be {YEAR_MIN}–{YEAR_MAX}"}, status_code=400)
    try:
        # Lazy import: an image without the security-master package at all
        # (the v5 line has no Delta Lake tier) still gets the pandas fallback
        # instead of failing at router-import time.
        from openbb_security_master.client import SecurityMasterClient

        client = SecurityMasterClient.from_env()
        out = client.odp_query(
            "MarketCalendar",
            {"mic": exchange.upper(), "start_date": f"{year}-01-01",
             "end_date": f"{year}-12-31", "include_closed": True},
            {"mode": "current_corrected"},
        )
        rows = out.get("results")
        if isinstance(rows, list) and rows:
            envelope = _envelope_from_security_master(rows)
            if envelope is not None:
                return JSONResponse(envelope)
    except Exception:
        # security-master-api unreachable: fall through to the pandas calendar
        # rather than failing the route.
        pass
    try:
        envelope = _envelope_from_exchange_calendars(exchange, year)
    except Exception:
        envelope = None
    if envelope is None:
        return JSONResponse({"detail": f"no calendar for exchange '{exchange}'"},
                            status_code=404)
    return JSONResponse(envelope)


def build_router():
    """Late-bound Router so importing the module never hard-requires openbb-core."""
    from openbb_core.app.router import Router

    router = Router(prefix="/trading", description="Trading calendar (EODHD v2 envelope)")
    router.api_router.get("")(trading_calendar)
    return router


router = build_router()

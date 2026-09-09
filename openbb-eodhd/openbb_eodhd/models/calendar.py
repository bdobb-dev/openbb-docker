# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""EODHD calendars: /calendar/earnings, /calendar/ipos, /calendar/splits,
and /economic-events.

Dedicated-endpoint fetchers (design: Phase 3 "Calendars"); nothing here
touches the shared /fundamentals bundle or its cache — calendars are
time-sensitive and stay uncached.

The SDK's calendar wrappers return the parsed JSON verbatim, which for the
equity calendars is a wrapper dict ({"earnings": [...]}, {"ipos": [...]},
{"splits": [...]}) and for /economic-events a bare list. `_unwrap` accepts
both so an SDK change in either direction cannot break us.

CalendarDividend is deliberately NOT implemented: EODHD's /calendar/dividends
requires filter[symbol] or filter[date_eq] (no open date-range sweep) and its
rows carry only {date, symbol} — no amount or pay/record dates — so the
widget would be an empty shell. Revisit if EODHD enriches the rows.
"""

import warnings
from datetime import date as dateType
from datetime import timedelta
from typing import Any

from openbb_core.app.model.abstract.error import OpenBBError
from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.calendar_earnings import (
    CalendarEarningsData, CalendarEarningsQueryParams,
)
from openbb_core.provider.standard_models.calendar_ipo import (
    CalendarIpoData, CalendarIpoQueryParams,
)
from openbb_core.provider.standard_models.calendar_splits import (
    CalendarSplitsData, CalendarSplitsQueryParams,
)
from openbb_core.provider.standard_models.economic_calendar import (
    EconomicCalendarData, EconomicCalendarQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError, UnauthorizedError
from pydantic import Field

from openbb_eodhd.economic_taxonomy import classify
from openbb_eodhd.models._client import get_client, raise_sdk_error


def _date(v):
    from pandas import isna, to_datetime
    if not v:
        return None
    ts = to_datetime(v, errors="coerce")
    return None if isna(ts) else ts.date()


def _datetime(v):
    from pandas import isna, to_datetime
    if not v:
        return None
    ts = to_datetime(v, errors="coerce")
    return None if isna(ts) else ts.to_pydatetime()


def _iso(v: dateType | None) -> str | None:
    return v.isoformat() if v else None


def _row_date(row: dict) -> str:
    """Civil date (YYYY-MM-DD) an /economic-events row falls on.

    EODHD's `date` field is `"YYYY-MM-DD HH:MM:SS"`; the civil date is
    always its first 10 characters, so this needs no parsing.
    """
    return str(row.get("date") or "")[:10]


def _prev_day(iso_date: str) -> str:
    """The civil date before `iso_date` (`YYYY-MM-DD`)."""
    return (_date(iso_date) - timedelta(days=1)).isoformat()


def _unwrap(resp: Any, key: str, context: str) -> list[dict]:
    """Rows out of an SDK calendar response — wrapper dict or bare list."""
    if isinstance(resp, dict):
        if key in resp:
            resp = resp[key]
        else:  # a dict without the rows key is an API error payload
            raise UnauthorizedError(
                f"EODHD ({context}): {resp.get('message') or resp.get('error') or resp}"
            )
    if not resp:
        raise EmptyDataError(f"EODHD returned no {context} data.")
    return resp


def _fetch(credentials, call, context: str):
    """One SDK calendar call with the extension's standard error mapping."""
    client = get_client(credentials)
    try:
        with client:
            return call(client)
    except (OpenBBError, UnauthorizedError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise_sdk_error(exc, context)


# ============================================================
# CalendarEarnings — /calendar/earnings
# ============================================================

class EODHDCalendarEarningsQueryParams(CalendarEarningsQueryParams):
    """EODHD Earnings Calendar Query.

    EODHD defaults an omitted window to today .. today+7d server-side.
    """


class EODHDCalendarEarningsData(CalendarEarningsData):
    """EODHD Earnings Calendar Data."""

    eps_actual: float | None = Field(
        default=None, description="The actual earnings per share announced."
    )
    period_ending: dateType | None = Field(
        default=None, description="The fiscal period end date the report covers."
    )
    announce_time: str | None = Field(
        default=None, description="BeforeMarket or AfterMarket."
    )
    currency: str | None = Field(default=None, description="Reporting currency.")
    surprise: float | None = Field(
        default=None, description="Actual minus estimate."
    )
    surprise_percent: float | None = Field(
        default=None, description="Surprise as a percent of the estimate."
    )


class EODHDCalendarEarningsFetcher(
    Fetcher[EODHDCalendarEarningsQueryParams, list[EODHDCalendarEarningsData]]
):
    """EODHD earnings calendar."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDCalendarEarningsQueryParams:
        return EODHDCalendarEarningsQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        def _sync():
            resp = _fetch(
                credentials,
                lambda c: c.get_upcoming_earnings_data(
                    from_date=_iso(query.start_date), to_date=_iso(query.end_date)
                ),
                "earnings calendar",
            )
            return _unwrap(resp, "earnings", "earnings calendar")

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDCalendarEarningsData]:  # pylint: disable=unused-argument
        rows = []
        for it in data:
            report_date = _date(it.get("report_date"))
            symbol = (it.get("code") or "").upper()
            if not report_date or not symbol:
                continue
            rows.append(EODHDCalendarEarningsData.model_validate({
                "report_date": report_date,
                "symbol": symbol,
                "eps_consensus": it.get("estimate"),
                "eps_actual": it.get("actual"),
                "period_ending": _date(it.get("date")),
                "announce_time": it.get("before_after_market"),
                "currency": it.get("currency"),
                "surprise": it.get("difference"),
                "surprise_percent": it.get("percent"),
            }))
        return rows


# ============================================================
# CalendarIpo — /calendar/ipos
# ============================================================

class EODHDCalendarIpoQueryParams(CalendarIpoQueryParams):
    """EODHD IPO Calendar Query.

    EODHD's endpoint takes only a date window; `symbol` and `limit` are
    applied client-side.
    """


class EODHDCalendarIpoData(CalendarIpoData):
    """EODHD IPO Calendar Data."""

    name: str | None = Field(default=None, description="Name of the entity.")
    exchange: str | None = Field(default=None, description="Listing exchange.")
    currency: str | None = Field(default=None, description="Offer currency.")
    filing_date: dateType | None = Field(default=None, description="Filing date.")
    amended_date: dateType | None = Field(default=None, description="Last amendment date.")
    price_from: float | None = Field(default=None, description="Low end of the expected price range.")
    price_to: float | None = Field(default=None, description="High end of the expected price range.")
    offer_price: float | None = Field(default=None, description="Final offer price.")
    shares: float | None = Field(default=None, description="Shares offered.")
    deal_type: str | None = Field(default=None, description="Deal status, e.g. Expected, Priced, Amended.")


class EODHDCalendarIpoFetcher(
    Fetcher[EODHDCalendarIpoQueryParams, list[EODHDCalendarIpoData]]
):
    """EODHD IPO calendar."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDCalendarIpoQueryParams:
        return EODHDCalendarIpoQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        def _sync():
            resp = _fetch(
                credentials,
                lambda c: c.get_upcoming_IPOs_data(
                    from_date=_iso(query.start_date), to_date=_iso(query.end_date)
                ),
                "IPO calendar",
            )
            return _unwrap(resp, "ipos", "IPO calendar")

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDCalendarIpoData]:  # pylint: disable=unused-argument
        want = (query.symbol or "").upper().split(".")[0] or None
        rows = []
        for it in data:
            symbol = (it.get("code") or "").upper()
            if want and symbol.split(".")[0] != want:
                continue
            rows.append(EODHDCalendarIpoData.model_validate({
                "symbol": symbol or None,
                "ipo_date": _date(it.get("start_date")),
                "name": it.get("name"),
                "exchange": it.get("exchange"),
                "currency": it.get("currency"),
                "filing_date": _date(it.get("filing_date")),
                "amended_date": _date(it.get("amended_date")),
                "price_from": it.get("price_from") or None,
                "price_to": it.get("price_to") or None,
                "offer_price": it.get("offer_price") or None,
                "shares": it.get("shares") or None,
                "deal_type": it.get("deal_type"),
            }))
        return rows[: query.limit] if query.limit else rows


# ============================================================
# CalendarSplits — /calendar/splits
# ============================================================

class EODHDCalendarSplitsQueryParams(CalendarSplitsQueryParams):
    """EODHD Splits Calendar Query."""


class EODHDCalendarSplitsData(CalendarSplitsData):
    """EODHD Splits Calendar Data.

    numerator/denominator follow the standard model's new-for-old convention:
    a 1-for-5 consolidation (EODHD old_shares=5, new_shares=1) is
    numerator=1, denominator=5.
    """

    optionable: str | None = Field(
        default=None, description="Y when listed options exist on the symbol."
    )


class EODHDCalendarSplitsFetcher(
    Fetcher[EODHDCalendarSplitsQueryParams, list[EODHDCalendarSplitsData]]
):
    """EODHD splits calendar."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDCalendarSplitsQueryParams:
        return EODHDCalendarSplitsQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        def _sync():
            resp = _fetch(
                credentials,
                lambda c: c.get_upcoming_splits_data(
                    from_date=_iso(query.start_date), to_date=_iso(query.end_date)
                ),
                "splits calendar",
            )
            return _unwrap(resp, "splits", "splits calendar")

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDCalendarSplitsData]:  # pylint: disable=unused-argument
        rows = []
        for it in data:
            split_date = _date(it.get("split_date"))
            symbol = (it.get("code") or "").upper()
            old, new = it.get("old_shares"), it.get("new_shares")
            # date/symbol/numerator/denominator are required by the standard
            # model; a row missing any of them cannot be represented.
            if not split_date or not symbol or not old or not new:
                continue
            rows.append(EODHDCalendarSplitsData.model_validate({
                "date": split_date,
                "symbol": symbol,
                "numerator": float(new),
                "denominator": float(old),
                "optionable": it.get("optionable"),
            }))
        return rows


# ============================================================
# EconomicCalendar — /economic-events
# ============================================================

class EODHDEconomicCalendarQueryParams(EconomicCalendarQueryParams):
    """EODHD Economic Calendar Query."""

    country: str | None = Field(
        default=None, description="ISO 3166 two-letter country code filter."
    )
    comparison: str | None = Field(
        default=None, description="Filter by comparison basis: mom, qoq or yoy."
    )
    limit: int | None = Field(
        default=None,
        description=(
            "Maximum number of events to return; None means every event in "
            "the range. EODHD answers at most 1,000 per request, so the "
            "range is fetched in date windows and merged."
        ),
    )


class EODHDEconomicCalendarData(EconomicCalendarData):
    """EODHD Economic Calendar Data."""

    comparison: str | None = Field(
        default=None, description="Comparison basis of the values: mom, qoq or yoy."
    )
    period: str | None = Field(
        default=None, description="The period the event's figures cover."
    )
    change: float | None = Field(
        default=None, description="Change from the previous value."
    )
    change_percentage: float | None = Field(
        default=None, description="Percent change from the previous value."
    )


class EODHDEconomicCalendarFetcher(
    Fetcher[EODHDEconomicCalendarQueryParams, list[EODHDEconomicCalendarData]]
):
    """EODHD economic events calendar."""

    # A pathological range (or an EODHD change that stops the walk from ever
    # shrinking) must not spin forever; 20 requests covers 20,000 rows at the
    # API's page cap, far past any real three-month window.
    _MAX_REQUESTS = 20
    _PAGE_LIMIT = 1000

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEconomicCalendarQueryParams:
        return EODHDEconomicCalendarQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        def _sync():
            return EODHDEconomicCalendarFetcher._fetch_windowed(query, credentials)

        return await to_thread(_sync)

    @staticmethod
    def _fetch_windowed(query, credentials) -> list[dict]:
        """Fetch every row in the range, windowing past EODHD's 1,000-row cap.

        EODHD returns each page sorted by date descending and silently drops
        anything past `limit` (max 1,000). A full page may therefore be
        truncated, so only the rows strictly newer than the page's oldest
        date are trustworthy; the oldest date is re-requested whole as the
        next window's `date_to`. That re-fetch is what makes the join exact
        without guessing at `offset` or de-duplicating by row identity — the
        boundary day is simply never taken from the truncated page.

        The one exception is a full page that is entirely one civil date:
        that single day has over 1,000 events, so it is paged by `offset`
        (verified against the live API: `offset` returns the next 1,000 with
        no overlap) until a short page closes it out, and only then does the
        date window step back a day.
        """
        date_from = _iso(query.start_date)
        cur_to = _iso(query.end_date)
        collected: list[dict] = []
        offset = 0
        paging_day: str | None = None  # set while offset-paging a single day
        requests = 0

        while True:
            if requests >= EODHDEconomicCalendarFetcher._MAX_REQUESTS:
                warnings.warn(
                    "EODHD economic calendar: stopped after "
                    f"{EODHDEconomicCalendarFetcher._MAX_REQUESTS} requests; "
                    "the requested range may be incompletely fetched.",
                    stacklevel=2,
                )
                break
            requests += 1

            def _call(c, _to=cur_to, _offset=offset):
                call_kwargs = {
                    "date_from": date_from,
                    "date_to": _to,
                    "country": query.country,
                    "comparison": query.comparison,
                    "limit": EODHDEconomicCalendarFetcher._PAGE_LIMIT,
                }
                if _offset:
                    call_kwargs["offset"] = _offset
                return c.get_economic_events_data(**call_kwargs)

            page = _fetch(credentials, _call, "economic calendar")
            if isinstance(page, dict):  # /economic-events answers a bare list
                raise UnauthorizedError(
                    f"EODHD (economic calendar): {page.get('message') or page.get('error') or page}"
                )

            if not page:
                if not collected:
                    raise EmptyDataError("EODHD returned no economic calendar data.")
                break

            if len(page) < EODHDEconomicCalendarFetcher._PAGE_LIMIT:
                collected.extend(page)
                if paging_day is None:
                    break  # short page: the window is fully covered
                # the single day just finished; the range may still hold
                # earlier days, so keep walking from the day before it
                cur_to = _prev_day(paging_day)
                offset = 0
                paging_day = None
                continue

            earliest, latest = _row_date(page[-1]), _row_date(page[0])
            if earliest == latest:  # over 1,000 events on one civil date
                collected.extend(page)
                cur_to = earliest
                offset += EODHDEconomicCalendarFetcher._PAGE_LIMIT
                paging_day = earliest
                continue

            collected.extend(r for r in page if _row_date(r) > earliest)
            cur_to = earliest
            offset = 0
            paging_day = None

        collected.sort(key=lambda r: str(r.get("date") or ""))  # ascending
        return collected[: query.limit] if query.limit else collected

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDEconomicCalendarData]:  # pylint: disable=unused-argument
        rows = []
        for it in data:
            event = it.get("type")
            source, category = classify(event)
            rows.append(EODHDEconomicCalendarData.model_validate({
                "date": _datetime(it.get("date")),
                "country": it.get("country"),
                "event": event,
                "source": source,
                "category": category,
                "consensus": it.get("estimate"),
                "previous": it.get("previous"),
                "actual": it.get("actual"),
                "comparison": it.get("comparison"),
                "period": it.get("period"),
                "change": it.get("change"),
                "change_percentage": it.get("change_percentage"),
            }))
        return rows

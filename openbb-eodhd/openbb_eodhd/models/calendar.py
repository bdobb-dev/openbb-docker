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

from datetime import date as dateType
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
    # ponytail: single page capped at the API max (1000); add offset paging if
    # a real query ever needs more than 1000 events.
    limit: int = Field(
        default=1000, description="Maximum number of events (API cap 1000)."
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

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEconomicCalendarQueryParams:
        return EODHDEconomicCalendarQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        def _sync():
            resp = _fetch(
                credentials,
                lambda c: c.get_economic_events_data(
                    date_from=_iso(query.start_date),
                    date_to=_iso(query.end_date),
                    country=query.country,
                    comparison=query.comparison,
                    limit=query.limit,
                ),
                "economic calendar",
            )
            if isinstance(resp, dict):  # /economic-events answers a bare list
                raise UnauthorizedError(
                    f"EODHD (economic calendar): {resp.get('message') or resp.get('error') or resp}"
                )
            if not resp:
                raise EmptyDataError("EODHD returned no economic calendar data.")
            return resp

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDEconomicCalendarData]:  # pylint: disable=unused-argument
        rows = []
        for it in data:
            rows.append(EODHDEconomicCalendarData.model_validate({
                "date": _datetime(it.get("date")),
                "country": it.get("country"),
                "event": it.get("type"),
                "consensus": it.get("estimate"),
                "previous": it.get("previous"),
                "actual": it.get("actual"),
                "comparison": it.get("comparison"),
                "period": it.get("period"),
                "change": it.get("change"),
                "change_percentage": it.get("change_percentage"),
            }))
        return rows

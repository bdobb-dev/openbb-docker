"""EODHD US Treasury rates (/ust/yield-rates) — TreasuryRates and YieldCurve.

The endpoint answers {"meta", "data": [{date, tenor, rate}]} in long form and
ignores date parameters entirely (verified 2026-09-01: bare from/to and
filter[date_*] both return the full history), so both fetchers pull the whole
series (~2.3k rows, starting 2026-01-02) and window client-side.
"""

from datetime import date as dateType
from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.treasury_rates import (
    TreasuryRatesData, TreasuryRatesQueryParams,
)
from openbb_core.provider.standard_models.yield_curve import (
    YieldCurveData, YieldCurveQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError, UnauthorizedError
from pydantic import Field

from openbb_eodhd.models._client import sdk_call

# EODHD tenor → standard-model maturity name. "1.5M" has no standard slot and
# is dropped; "4M" rides along as a provider extra.
TENOR_MAP = {
    "1M": "month_1", "2M": "month_2", "3M": "month_3", "4M": "month_4",
    "6M": "month_6", "1Y": "year_1", "2Y": "year_2", "3Y": "year_3",
    "5Y": "year_5", "7Y": "year_7", "10Y": "year_10", "20Y": "year_20",
    "30Y": "year_30",
}


async def _fetch_yields(credentials) -> list[dict]:
    from asyncio import to_thread

    def _sync():
        resp = sdk_call(
            credentials,
            lambda c: c.get_treasury_yield_rates(),
            "treasury yields",
        )
        rows = resp.get("data") if isinstance(resp, dict) else resp
        if rows is None and isinstance(resp, dict):
            raise UnauthorizedError(
                f"EODHD (treasury yields): {resp.get('message') or resp.get('error') or resp}"
            )
        if not rows:
            raise EmptyDataError("EODHD returned no treasury yield data.")
        return rows

    return await to_thread(_sync)


def _by_date(data: list[dict]) -> dict[str, dict[str, float]]:
    """{date: {maturity: rate}} keeping only mapped tenors.

    EODHD sends percents (3.72); the standard models store normalized
    fractions (0.0372) and let the frontend multiply — same convention as
    the fmp and federal_reserve providers.
    """
    out: dict[str, dict[str, float]] = {}
    for it in data:
        maturity = TENOR_MAP.get(it.get("tenor") or "")
        d, rate = it.get("date"), it.get("rate")
        if not maturity or not d or rate is None:
            continue
        out.setdefault(d, {})[maturity] = float(rate) / 100
    return out


class EODHDTreasuryRatesQueryParams(TreasuryRatesQueryParams):
    """EODHD Treasury Rates Query."""


class EODHDTreasuryRatesData(TreasuryRatesData):
    """EODHD Treasury Rates Data."""

    month_4: float | None = Field(
        default=None,
        description="4-month rate (EODHD tenor with no standard-model slot).",
        json_schema_extra={"x-unit_measurement": "percent", "x-frontend_multiply": 100},
    )


class EODHDTreasuryRatesFetcher(
    Fetcher[EODHDTreasuryRatesQueryParams, list[EODHDTreasuryRatesData]]
):
    """EODHD US Treasury par yield rates."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDTreasuryRatesQueryParams:
        return EODHDTreasuryRatesQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        return await _fetch_yields(credentials)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDTreasuryRatesData]:  # pylint: disable=unused-argument
        start = str(query.start_date) if query.start_date else None
        end = str(query.end_date) if query.end_date else None
        rows = []
        for d, rates in sorted(_by_date(data).items()):
            if (start and d < start) or (end and d > end):
                continue
            rows.append(EODHDTreasuryRatesData.model_validate({"date": d, **rates}))
        if not rows:
            raise EmptyDataError("No treasury rates in the requested window.")
        return rows


class EODHDYieldCurveQueryParams(YieldCurveQueryParams):
    """EODHD Yield Curve Query. `date` accepts one date or several comma-joined."""


class EODHDYieldCurveData(YieldCurveData):
    """EODHD Yield Curve Data."""

    rate: float = Field(
        description="The yield as a normalized percent.",
        json_schema_extra={"x-unit_measurement": "percent", "x-frontend_multiply": 100},
    )


class EODHDYieldCurveFetcher(
    Fetcher[EODHDYieldCurveQueryParams, list[EODHDYieldCurveData]]
):
    """EODHD US Treasury yield curve."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDYieldCurveQueryParams:
        return EODHDYieldCurveQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        return await _fetch_yields(credentials)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDYieldCurveData]:  # pylint: disable=unused-argument
        by_date = _by_date(data)
        available = sorted(by_date)
        if not available:
            raise EmptyDataError("EODHD returned no usable yield-curve tenors.")

        wanted = (
            [d.strip() for d in str(query.date).split(",") if d.strip()]
            if query.date
            else [available[-1]]
        )

        rows: list[EODHDYieldCurveData] = []
        # maturity sort order follows the curve, not the alphabet
        order = list(TENOR_MAP.values())
        for want in wanted:
            # the curve as of a date = the latest observation at or before it
            candidates = [d for d in available if d <= want]
            if not candidates:
                continue
            chosen = candidates[-1]
            for maturity in order:
                rate = by_date[chosen].get(maturity)
                if rate is None or maturity == "month_4":  # no standard slot
                    continue
                rows.append(EODHDYieldCurveData.model_validate({
                    "date": chosen, "maturity": maturity, "rate": rate,
                }))
        if not rows:
            raise EmptyDataError("No yield-curve data at or before the requested date(s).")
        return rows

"""EODHD company profile models from the shared /fundamentals bundle —
EquityInfo (General), ShareStatistics (SharesStats + Technicals) and
KeyExecutives (General.Officers).
"""

from datetime import date as dateType
from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.equity_info import (
    EquityInfoData, EquityInfoQueryParams,
)
from openbb_core.provider.standard_models.key_executives import (
    KeyExecutivesData, KeyExecutivesQueryParams,
)
from openbb_core.provider.standard_models.share_statistics import (
    ShareStatisticsData, ShareStatisticsQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError
from pydantic import Field

from openbb_eodhd.models import _fundamentals as F


def _date(v):
    from pandas import isna, to_datetime
    if not v:
        return None
    ts = to_datetime(v, errors="coerce")
    return None if isna(ts) else ts.date()


def _int(v):
    try:
        return None if v in (None, "", "NA") else int(v)
    except (TypeError, ValueError):
        return None


class EODHDEquityInfoQueryParams(EquityInfoQueryParams):
    """EODHD Equity Info Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDEquityInfoData(EquityInfoData):
    """EODHD Equity Info Data."""

    security_type: str | None = Field(default=None, description="Instrument type, e.g. Common Stock.")
    currency: str | None = Field(default=None, description="Trading currency code.")
    ipo_date: dateType | None = Field(default=None, description="Date of the IPO.")
    fiscal_year_end: str | None = Field(default=None, description="Month the fiscal year ends.")
    is_delisted: bool | None = Field(default=None, description="True when the listing is dead.")
    logo_url: str | None = Field(default=None, description="EODHD-hosted logo path.")
    updated_at: dateType | None = Field(default=None, description="When EODHD last refreshed the record.")


class EODHDEquityInfoFetcher(Fetcher[EODHDEquityInfoQueryParams, list[EODHDEquityInfoData]]):
    """EODHD company profile (General)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEquityInfoQueryParams:
        return EODHDEquityInfoQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDEquityInfoData]:  # pylint: disable=unused-argument
        g = F.general(data)
        if not g:
            raise EmptyDataError(f"EODHD has no profile for '{query.symbol}'.")
        officers = F._rows(g.get("Officers"))  # pylint: disable=protected-access
        ceo = next(
            (o.get("Name") for o in officers if "CEO" in (o.get("Title") or "").upper()),
            None,
        )
        addr = g.get("AddressData") or {}
        return [EODHDEquityInfoData.model_validate({
            "symbol": query.symbol.upper(),
            "name": g.get("Name"),
            "cik": g.get("CIK"),
            "cusip": g.get("CUSIP"),
            "isin": g.get("ISIN"),
            "lei": g.get("LEI"),
            "stock_exchange": g.get("Exchange"),
            "long_description": g.get("Description"),
            "ceo": ceo,
            "company_url": g.get("WebURL"),
            "business_address": g.get("Address"),
            "business_phone_no": g.get("Phone"),
            "hq_address1": addr.get("Street"),
            "hq_address_city": addr.get("City"),
            "hq_state": addr.get("State"),
            "hq_address_postal_code": addr.get("ZIP"),
            "hq_country": addr.get("Country"),
            "employees": _int(g.get("FullTimeEmployees")),
            "sector": g.get("Sector"),
            "industry_category": g.get("Industry"),
            "industry_group": g.get("GicGroup"),
            "security_type": g.get("Type"),
            "currency": g.get("CurrencyCode"),
            "ipo_date": _date(g.get("IPODate")),
            "fiscal_year_end": g.get("FiscalYearEnd"),
            "is_delisted": g.get("IsDelisted"),
            # LogoURL arrives as a root-relative path; the host serves it as a
            # public static PNG (no api_token), so absolutize for direct use.
            "logo_url": f"https://eodhd.com{g['LogoURL']}"
            if (g.get("LogoURL") or "").startswith("/") else g.get("LogoURL"),
            "updated_at": _date(g.get("UpdatedAt")),
        })]


class EODHDShareStatisticsQueryParams(ShareStatisticsQueryParams):
    """EODHD Share Statistics Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDShareStatisticsData(ShareStatisticsData):
    """EODHD Share Statistics Data."""

    percent_insiders: float | None = Field(
        default=None, description="Percent of shares held by insiders.",
    )
    percent_institutions: float | None = Field(
        default=None, description="Percent of shares held by institutions.",
    )
    shares_short: float | None = Field(default=None, description="Shares sold short.")
    shares_short_prior_month: float | None = Field(default=None, description="Shares short a month earlier.")
    short_ratio: float | None = Field(default=None, description="Days-to-cover short ratio.")
    short_percent_float: float | None = Field(default=None, description="Short interest as a fraction of the float.")


class EODHDShareStatisticsFetcher(
    Fetcher[EODHDShareStatisticsQueryParams, list[EODHDShareStatisticsData]]
):
    """EODHD share statistics (SharesStats + Technicals)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDShareStatisticsQueryParams:
        return EODHDShareStatisticsQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDShareStatisticsData]:  # pylint: disable=unused-argument
        stats = F.shares_stats(data)
        if not stats:
            raise EmptyDataError(f"EODHD has no share statistics for '{query.symbol}'.")
        tech = F.technicals(data)
        outstanding = stats.get("SharesOutstanding")
        floating = stats.get("SharesFloat")
        return [EODHDShareStatisticsData.model_validate({
            "symbol": query.symbol.upper(),
            "date": _date(F.general(data).get("UpdatedAt")),
            "outstanding_shares": outstanding,
            "float_shares": floating,
            # free float as a percent of shares outstanding, like fmp's
            "free_float": round(floating / outstanding * 100, 4)
            if floating and outstanding else None,
            "percent_insiders": stats.get("PercentInsiders"),
            "percent_institutions": stats.get("PercentInstitutions"),
            "shares_short": stats.get("SharesShort") or tech.get("SharesShort"),
            "shares_short_prior_month": stats.get("SharesShortPriorMonth") or tech.get("SharesShortPriorMonth"),
            "short_ratio": stats.get("ShortRatio") or tech.get("ShortRatio"),
            "short_percent_float": stats.get("ShortPercentFloat") or tech.get("ShortPercent"),
        })]


class EODHDKeyExecutivesQueryParams(KeyExecutivesQueryParams):
    """EODHD Key Executives Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDKeyExecutivesData(KeyExecutivesData):
    """EODHD Key Executives Data — no compensation figures in EODHD."""


class EODHDKeyExecutivesFetcher(
    Fetcher[EODHDKeyExecutivesQueryParams, list[EODHDKeyExecutivesData]]
):
    """EODHD key executives (General.Officers)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDKeyExecutivesQueryParams:
        return EODHDKeyExecutivesQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDKeyExecutivesData]:  # pylint: disable=unused-argument
        officers = F._rows(F.general(data).get("Officers"))  # pylint: disable=protected-access
        rows = []
        for o in officers:
            name, title = o.get("Name"), o.get("Title")
            if not name or not title:
                continue
            rows.append(EODHDKeyExecutivesData.model_validate({
                "name": name,
                "title": title,
                "year_born": _int(o.get("YearBorn")),
            }))
        if not rows:
            raise EmptyDataError(f"EODHD lists no officers for '{query.symbol}'.")
        return rows

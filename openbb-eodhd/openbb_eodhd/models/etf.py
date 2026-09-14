"""EODHD ETF models — profile, holdings, sectors, regions from the shared
/fundamentals bundle's ETF_Data section (one cached call per symbol).

EODHD reports geographic exposure as World_Regions (a mix of regions and
countries, e.g. "North America", "United Kingdom"); EtfCountries maps those
rows as-is rather than inventing a per-country split EODHD does not have.
"""

from datetime import date as dateType
from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.etf_countries import (
    EtfCountriesData, EtfCountriesQueryParams,
)
from openbb_core.provider.standard_models.etf_holdings import (
    EtfHoldingsData, EtfHoldingsQueryParams,
)
from openbb_core.provider.standard_models.etf_info import (
    EtfInfoData, EtfInfoQueryParams,
)
from openbb_core.provider.standard_models.etf_sectors import (
    EtfSectorsData, EtfSectorsQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError
from pydantic import Field

from openbb_eodhd.models import _fundamentals as F

_PCT = {"x-unit_measurement": "percent", "x-frontend_multiply": 100}


def _f(v):
    try:
        return None if v in (None, "", "NA") else float(v)
    except (TypeError, ValueError):
        return None


def _pct(v):
    f = _f(v)
    return None if f is None else f / 100


def _date(v):
    from pandas import isna, to_datetime
    if not v:
        return None
    ts = to_datetime(v, errors="coerce")
    return None if isna(ts) else ts.date()


def _etf_section(bundle: dict, symbol: str) -> dict:
    section = F.etf_data(bundle)
    if not section:
        raise EmptyDataError(
            f"EODHD has no ETF_Data for '{symbol}' — not an ETF, or not covered."
        )
    return section


class EODHDEtfInfoQueryParams(EtfInfoQueryParams):
    """EODHD ETF Info Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDEtfInfoData(EtfInfoData):
    """EODHD ETF Info Data."""

    isin: str | None = Field(default=None, description="ISIN of the fund.")
    index_name: str | None = Field(default=None, description="Benchmark index the fund tracks.")
    dividend_yield: float | None = Field(default=None, description="Fund yield.", json_schema_extra=_PCT)
    net_expense_ratio: float | None = Field(default=None, description="Net expense ratio.", json_schema_extra=_PCT)
    ongoing_charge: float | None = Field(default=None, description="Ongoing charge.", json_schema_extra=_PCT)
    holdings_turnover: float | None = Field(default=None, description="Annual holdings turnover.", json_schema_extra=_PCT)
    total_assets: float | None = Field(default=None, description="Total net assets.")
    holdings_count: int | None = Field(default=None, description="Number of holdings.")


class EODHDEtfInfoFetcher(Fetcher[EODHDEtfInfoQueryParams, list[EODHDEtfInfoData]]):
    """EODHD ETF profile (ETF_Data + General)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEtfInfoQueryParams:
        return EODHDEtfInfoQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDEtfInfoData]:  # pylint: disable=unused-argument
        symbol = query.symbol.upper()
        etf = _etf_section(data, symbol)
        general = F.general(data)
        return [EODHDEtfInfoData.model_validate({
            "symbol": symbol,
            "name": general.get("Name"),
            "issuer": etf.get("Company_Name"),
            "domicile": etf.get("Domicile"),
            "website": etf.get("ETF_URL") or etf.get("Company_URL"),
            "description": general.get("Description"),
            "inception_date": _date(etf.get("Inception_Date")),
            "isin": etf.get("ISIN"),
            "index_name": etf.get("Index_Name"),
            # EODHD mixes conventions here: Yield is a percent ('1.06'),
            # the charge/turnover figures are already fractions ('0.00030').
            "dividend_yield": _pct(etf.get("Yield")),
            "net_expense_ratio": _f(etf.get("NetExpenseRatio")),
            "ongoing_charge": _f(etf.get("Ongoing_Charge")),
            "holdings_turnover": _f(etf.get("AnnualHoldingsTurnover")),
            "total_assets": _f(etf.get("TotalAssets")),
            "holdings_count": int(etf["Holdings_Count"]) if _f(etf.get("Holdings_Count")) is not None else None,
        })]


class EODHDEtfHoldingsQueryParams(EtfHoldingsQueryParams):
    """EODHD ETF Holdings Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDEtfHoldingsData(EtfHoldingsData):
    """EODHD ETF Holdings Data."""

    weight: float | None = Field(default=None, description="Portfolio weight.", json_schema_extra=_PCT)
    sector: str | None = Field(default=None, description="Sector of the holding.")
    industry: str | None = Field(default=None, description="Industry of the holding.")
    country: str | None = Field(default=None, description="Country of the holding.")
    region: str | None = Field(default=None, description="Region of the holding.")


class EODHDEtfHoldingsFetcher(Fetcher[EODHDEtfHoldingsQueryParams, list[EODHDEtfHoldingsData]]):
    """EODHD ETF holdings (ETF_Data.Holdings)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEtfHoldingsQueryParams:
        return EODHDEtfHoldingsQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDEtfHoldingsData]:  # pylint: disable=unused-argument
        etf = _etf_section(data, query.symbol.upper())
        holdings = etf.get("Holdings") or {}
        rows = []
        for key, h in holdings.items():
            if not isinstance(h, dict):
                continue
            rows.append(EODHDEtfHoldingsData.model_validate({
                "symbol": (key or h.get("Code") or "").upper() or None,
                "name": h.get("Name"),
                "weight": _pct(h.get("Assets_%")),
                "sector": h.get("Sector"),
                "industry": h.get("Industry"),
                "country": h.get("Country"),
                "region": h.get("Region"),
            }))
        if not rows:
            raise EmptyDataError(f"EODHD lists no holdings for '{query.symbol}'.")
        rows.sort(key=lambda r: r.weight or 0, reverse=True)
        return rows


class EODHDEtfSectorsQueryParams(EtfSectorsQueryParams):
    """EODHD ETF Sectors Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDEtfSectorsData(EtfSectorsData):
    """EODHD ETF Sectors Data."""


class EODHDEtfSectorsFetcher(Fetcher[EODHDEtfSectorsQueryParams, list[EODHDEtfSectorsData]]):
    """EODHD ETF sector weights (ETF_Data.Sector_Weights)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEtfSectorsQueryParams:
        return EODHDEtfSectorsQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDEtfSectorsData]:  # pylint: disable=unused-argument
        symbol = query.symbol.upper()
        etf = _etf_section(data, symbol)
        weights = etf.get("Sector_Weights") or {}
        rows = []
        for sector, w in weights.items():
            weight = _pct((w or {}).get("Equity_%")) if isinstance(w, dict) else None
            if weight is None:
                continue
            rows.append(EODHDEtfSectorsData.model_validate({
                "symbol": symbol, "sector": sector, "weight": weight,
            }))
        if not rows:
            raise EmptyDataError(f"EODHD lists no sector weights for '{symbol}'.")
        rows.sort(key=lambda r: r.weight, reverse=True)
        return rows


class EODHDEtfCountriesQueryParams(EtfCountriesQueryParams):
    """EODHD ETF Countries Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDEtfCountriesData(EtfCountriesData):
    """EODHD ETF Countries Data — EODHD World_Regions rows verbatim."""


class EODHDEtfCountriesFetcher(Fetcher[EODHDEtfCountriesQueryParams, list[EODHDEtfCountriesData]]):
    """EODHD ETF geographic weights (ETF_Data.World_Regions)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEtfCountriesQueryParams:
        return EODHDEtfCountriesQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDEtfCountriesData]:  # pylint: disable=unused-argument
        symbol = query.symbol.upper()
        etf = _etf_section(data, symbol)
        weights = etf.get("World_Regions") or {}
        rows = []
        for region, w in weights.items():
            weight = _pct((w or {}).get("Equity_%")) if isinstance(w, dict) else None
            if weight is None:
                continue
            rows.append(EODHDEtfCountriesData.model_validate({
                "symbol": symbol, "country": region, "weight": weight,
            }))
        if not rows:
            raise EmptyDataError(f"EODHD lists no region weights for '{symbol}'.")
        rows.sort(key=lambda r: r.weight, reverse=True)
        return rows

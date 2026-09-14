"""EODHD ownership models: institutional & fund holders from /fundamentals."""

from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.institutional_ownership import (
    InstitutionalOwnershipData, InstitutionalOwnershipQueryParams,
)
from openbb_core.provider.standard_models.equity_ownership import (
    EquityOwnershipData, EquityOwnershipQueryParams,
)
from pydantic import Field

from openbb_eodhd.models import _fundamentals as F


def _date(v):
    from pandas import isna, to_datetime
    if not v:
        return None
    ts = to_datetime(v, errors="coerce")
    return None if isna(ts) else ts.date()


class EODHDInstitutionalOwnershipQueryParams(InstitutionalOwnershipQueryParams):
    """EODHD Institutional Ownership Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDInstitutionalOwnershipData(InstitutionalOwnershipData):
    """EODHD Institutional Ownership Data."""
    name: str | None = Field(default=None, description="Holder name.")
    total_shares_percent: float | None = Field(default=None, description="Holder % of shares outstanding.")
    total_assets_percent: float | None = Field(default=None, description="Holder % of its portfolio.")
    current_shares: int | None = Field(default=None, description="Shares currently held.")
    change: float | None = Field(default=None, description="Share change since prior period.")
    change_percent: float | None = Field(default=None, description="Percent share change.")


class EODHDInstitutionalOwnershipFetcher(
    Fetcher[EODHDInstitutionalOwnershipQueryParams, list[EODHDInstitutionalOwnershipData]]
):
    """EODHD institutional holders (Holders.Institutions)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDInstitutionalOwnershipQueryParams:
        return EODHDInstitutionalOwnershipQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDInstitutionalOwnershipData]:  # pylint: disable=unused-argument
        rows = []
        for h in F.holders_institutions(data):
            d = _date(h.get("date"))
            if d is None:
                continue
            rows.append(EODHDInstitutionalOwnershipData.model_validate({
                "symbol": query.symbol.upper(),
                "date": d,
                "name": h.get("name"),
                "total_shares_percent": h.get("totalShares"),
                "total_assets_percent": h.get("totalAssets"),
                "current_shares": h.get("currentShares"),
                "change": h.get("change"),
                "change_percent": h.get("change_p"),
            }))
        rows.sort(key=lambda r: (r.current_shares or 0), reverse=True)
        return rows


class EODHDEquityOwnershipQueryParams(EquityOwnershipQueryParams):
    """EODHD Equity Ownership Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDEquityOwnershipData(EquityOwnershipData):
    """EODHD Equity Ownership Data."""
    current_shares: int | None = Field(default=None, description="Shares currently held.")
    total_shares_percent: float | None = Field(default=None, description="Holder % of shares outstanding.")


class EODHDEquityOwnershipFetcher(
    Fetcher[EODHDEquityOwnershipQueryParams, list[EODHDEquityOwnershipData]]
):
    """EODHD ownership (institutional + fund holders)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEquityOwnershipQueryParams:
        return EODHDEquityOwnershipQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDEquityOwnershipData]:  # pylint: disable=unused-argument
        rows = []
        for h in F.holders_institutions(data) + F.holders_funds(data):
            d = _date(h.get("date"))
            if d is None or not h.get("name"):
                continue
            rows.append(EODHDEquityOwnershipData.model_validate({
                "investor_name": h.get("name"),
                "date": d,
                "filing_date": d,  # EODHD exposes only the position date
                "symbol": query.symbol.upper(),
                "current_shares": h.get("currentShares"),
                "total_shares_percent": h.get("totalShares"),
            }))
        rows.sort(key=lambda r: (r.current_shares or 0), reverse=True)
        return rows

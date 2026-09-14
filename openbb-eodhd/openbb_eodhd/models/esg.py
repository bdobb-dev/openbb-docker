"""EODHD ESG scores (fundamentals ESGScores section) — DEPRECATED.

EODHD's ESG feed is a stale 2019 beta (the payload's own Disclaimer says it
was slated for removal in 2020, yet it still ships). Implemented for FMP
parity per the design doc, deprecated on arrival: every row carries the
disclaimer and a 2019 rating date. Do not build anything on this.
"""

from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.esg_score import (
    EsgScoreData, EsgScoreQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError
from pydantic import Field

from openbb_eodhd.models import _fundamentals as F


class EODHDEsgScoreQueryParams(EsgScoreQueryParams):
    """EODHD ESG Score Query. DEPRECATED — stale 2019 beta data."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDEsgScoreData(EsgScoreData):
    """EODHD ESG Score Data. DEPRECATED — stale 2019 beta data."""

    total_esg_percentile: float | None = Field(default=None, description="Total ESG percentile.")
    environment_score_percentile: float | None = Field(default=None, description="Environment percentile.")
    social_score_percentile: float | None = Field(default=None, description="Social percentile.")
    governance_score_percentile: float | None = Field(default=None, description="Governance percentile.")
    controversy_level: int | None = Field(default=None, description="Controversy level.")
    disclaimer: str | None = Field(default=None, description="EODHD's own staleness disclaimer.")


class EODHDEsgScoreFetcher(Fetcher[EODHDEsgScoreQueryParams, list[EODHDEsgScoreData]]):
    """EODHD ESG scores. DEPRECATED."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEsgScoreQueryParams:
        return EODHDEsgScoreQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDEsgScoreData]:  # pylint: disable=unused-argument
        esg = F.esg(data)
        if not esg or esg.get("TotalEsg") is None:
            raise EmptyDataError(f"EODHD has no ESG scores for '{query.symbol}'.")
        return [EODHDEsgScoreData.model_validate({
            "symbol": query.symbol.upper(),
            "period_ending": esg.get("RatingDate"),
            "disclosure_date": esg.get("RatingDate"),
            "company_name": F.general(data).get("Name"),
            "esg_score": esg.get("TotalEsg"),
            "environmental_score": esg.get("EnvironmentScore"),
            "social_score": esg.get("SocialScore"),
            "governance_score": esg.get("GovernanceScore"),
            "total_esg_percentile": esg.get("TotalEsgPercentile"),
            "environment_score_percentile": esg.get("EnvironmentScorePercentile"),
            "social_score_percentile": esg.get("SocialScorePercentile"),
            "governance_score_percentile": esg.get("GovernanceScorePercentile"),
            "controversy_level": esg.get("ControversyLevel"),
            "disclaimer": esg.get("Disclaimer"),
        })]

"""EODHD earnings/estimates models from /fundamentals (History + Trend)."""

from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.analyst_estimates import (
    AnalystEstimatesData, AnalystEstimatesQueryParams,
)
from openbb_core.provider.standard_models.forward_eps_estimates import (
    ForwardEpsEstimatesData, ForwardEpsEstimatesQueryParams,
)
from openbb_core.provider.standard_models.historical_eps import (
    HistoricalEpsData, HistoricalEpsQueryParams,
)
from openbb_core.provider.standard_models.price_target_consensus import (
    PriceTargetConsensusData, PriceTargetConsensusQueryParams,
)
from pydantic import Field

from openbb_eodhd.models import _fundamentals as F


def _date(v):
    from pandas import isna, to_datetime
    if not v:
        return None
    ts = to_datetime(v, errors="coerce")
    return None if isna(ts) else ts.date()


def _f(v):
    try:
        return float(v) if v not in (None, "", "NA") else None
    except (TypeError, ValueError):
        return None


def _i(v):
    f = _f(v)
    return int(f) if f is not None else None


class EODHDHistoricalEpsQueryParams(HistoricalEpsQueryParams):
    """EODHD Historical EPS Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDHistoricalEpsData(HistoricalEpsData):
    """EODHD Historical EPS Data."""
    report_date: Any = Field(default=None, description="Announced report date.")


class EODHDHistoricalEpsFetcher(
    Fetcher[EODHDHistoricalEpsQueryParams, list[EODHDHistoricalEpsData]]
):
    """EODHD historical EPS (Earnings.History)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDHistoricalEpsQueryParams:
        return EODHDHistoricalEpsQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDHistoricalEpsData]:  # pylint: disable=unused-argument
        rows = []
        for e in F.earnings_history(data):
            d = _date(e.get("date") or e.get("reportDate"))
            if d is None:
                continue
            rows.append(EODHDHistoricalEpsData.model_validate({
                "symbol": query.symbol.upper(),
                "date": d,
                "eps_actual": _f(e.get("epsActual")),
                "eps_estimated": _f(e.get("epsEstimate")),
                "report_date": _date(e.get("reportDate")),
            }))
        rows.sort(key=lambda r: r.date)
        return rows


class EODHDAnalystEstimatesQueryParams(AnalystEstimatesQueryParams):
    """EODHD Analyst Estimates Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDAnalystEstimatesData(AnalystEstimatesData):
    """EODHD Analyst Estimates Data."""


class EODHDAnalystEstimatesFetcher(
    Fetcher[EODHDAnalystEstimatesQueryParams, list[EODHDAnalystEstimatesData]]
):
    """EODHD analyst estimates (Earnings.Trend)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDAnalystEstimatesQueryParams:
        return EODHDAnalystEstimatesQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDAnalystEstimatesData]:  # pylint: disable=unused-argument
        rows = []
        for t in F.earnings_trend(data):
            d = _date(t.get("date"))
            if d is None:
                continue
            rows.append(EODHDAnalystEstimatesData.model_validate({
                "symbol": query.symbol.upper(),
                "date": d,
                "estimated_eps_avg": _f(t.get("earningsEstimateAvg")),
                "estimated_eps_low": _f(t.get("earningsEstimateLow")),
                "estimated_eps_high": _f(t.get("earningsEstimateHigh")),
                "estimated_revenue_avg": _f(t.get("revenueEstimateAvg")),
                "estimated_revenue_low": _f(t.get("revenueEstimateLow")),
                "estimated_revenue_high": _f(t.get("revenueEstimateHigh")),
                "number_analysts_estimated_eps": _i(t.get("earningsEstimateNumberOfAnalysts")),
                "number_analyst_estimated_revenue": _i(t.get("revenueEstimateNumberOfAnalysts")),
            }))
        rows.sort(key=lambda r: r.date)
        return rows


class EODHDForwardEpsEstimatesQueryParams(ForwardEpsEstimatesQueryParams):
    """EODHD Forward EPS Estimates Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDForwardEpsEstimatesData(ForwardEpsEstimatesData):
    """EODHD Forward EPS Estimates Data."""


class EODHDForwardEpsEstimatesFetcher(
    Fetcher[EODHDForwardEpsEstimatesQueryParams, list[EODHDForwardEpsEstimatesData]]
):
    """EODHD forward EPS estimates (Earnings.Trend)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDForwardEpsEstimatesQueryParams:
        return EODHDForwardEpsEstimatesQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        symbol = query.symbol or "AAPL"
        return await F.get_bundle(symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDForwardEpsEstimatesData]:  # pylint: disable=unused-argument
        sym = (query.symbol or "AAPL").upper()
        rows = []
        for t in F.earnings_trend(data):
            d = _date(t.get("date"))
            if d is None:
                continue
            rows.append(EODHDForwardEpsEstimatesData.model_validate({
                "symbol": sym,
                "date": d,
                "fiscal_period": t.get("period"),
                "mean": _f(t.get("earningsEstimateAvg")),
                "low_estimate": _f(t.get("earningsEstimateLow")),
                "high_estimate": _f(t.get("earningsEstimateHigh")),
                "number_of_analysts": _i(t.get("earningsEstimateNumberOfAnalysts")),
            }))
        rows.sort(key=lambda r: r.date)
        return rows


class EODHDPriceTargetConsensusQueryParams(PriceTargetConsensusQueryParams):
    """EODHD Price Target Consensus Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDPriceTargetConsensusData(PriceTargetConsensusData):
    """EODHD Price Target Consensus Data."""
    rating: float | None = Field(default=None, description="EODHD analyst rating (1=strong buy .. 5=strong sell).")


class EODHDPriceTargetConsensusFetcher(
    Fetcher[EODHDPriceTargetConsensusQueryParams, list[EODHDPriceTargetConsensusData]]
):
    """EODHD price target consensus (AnalystRatings)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDPriceTargetConsensusQueryParams:
        return EODHDPriceTargetConsensusQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        symbol = query.symbol or "AAPL"
        return await F.get_bundle(symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDPriceTargetConsensusData]:  # pylint: disable=unused-argument
        ar = F.analyst_ratings(data)
        target = _f(ar.get("TargetPrice"))
        if not target:
            return []
        return [EODHDPriceTargetConsensusData.model_validate({
            "symbol": (query.symbol or "AAPL").upper(),
            "name": F.general(data).get("Name"),
            "target_consensus": target,
            "rating": _f(ar.get("Rating")),
        })]

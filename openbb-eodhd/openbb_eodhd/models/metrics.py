"""EODHD valuation metrics from the shared /fundamentals bundle —
KeyMetrics and FinancialRatios (Highlights + Valuation + Technicals).

Both are **current-snapshot only**: EODHD's bundle carries one TTM/MRQ set of
figures where FMP returns a per-period history, so each fetcher answers a
single row (`fiscal_period` = "TTM") regardless of any limit. The design doc
records this difference — don't fake a series.
"""

from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.financial_ratios import (
    FinancialRatiosData, FinancialRatiosQueryParams,
)
from openbb_core.provider.standard_models.key_metrics import (
    KeyMetricsData, KeyMetricsQueryParams,
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


class EODHDKeyMetricsQueryParams(KeyMetricsQueryParams):
    """EODHD Key Metrics Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDKeyMetricsData(KeyMetricsData):
    """EODHD Key Metrics Data (TTM/MRQ snapshot)."""

    pe_ratio: float | None = Field(default=None, description="Trailing P/E.")
    forward_pe: float | None = Field(default=None, description="Forward P/E.")
    peg_ratio: float | None = Field(default=None, description="PEG ratio.")
    eps_ttm: float | None = Field(default=None, description="Diluted EPS, trailing twelve months.")
    book_value_per_share: float | None = Field(default=None, description="Book value per share.")
    dividend_per_share: float | None = Field(default=None, description="Annual dividend per share.")
    dividend_yield: float | None = Field(default=None, description="Dividend yield as a fraction.")
    revenue_ttm: float | None = Field(default=None, description="Revenue, trailing twelve months.")
    revenue_per_share_ttm: float | None = Field(default=None, description="Revenue per share, TTM.")
    gross_profit_ttm: float | None = Field(default=None, description="Gross profit, TTM.")
    ebitda: float | None = Field(default=None, description="EBITDA.")
    profit_margin: float | None = Field(default=None, description="Net profit margin as a fraction.")
    operating_margin_ttm: float | None = Field(default=None, description="Operating margin, TTM.")
    return_on_assets_ttm: float | None = Field(default=None, description="Return on assets, TTM.")
    return_on_equity_ttm: float | None = Field(default=None, description="Return on equity, TTM.")
    quarterly_revenue_growth_yoy: float | None = Field(default=None, description="Quarterly revenue growth, YoY.")
    quarterly_earnings_growth_yoy: float | None = Field(default=None, description="Quarterly earnings growth, YoY.")
    price_to_sales_ttm: float | None = Field(default=None, description="Price to sales, TTM.")
    price_to_book_mrq: float | None = Field(default=None, description="Price to book, most recent quarter.")
    enterprise_value: float | None = Field(default=None, description="Enterprise value.")
    ev_to_revenue: float | None = Field(default=None, description="EV / revenue.")
    ev_to_ebitda: float | None = Field(default=None, description="EV / EBITDA.")
    beta: float | None = Field(default=None, description="Beta vs. the market.")


class EODHDKeyMetricsFetcher(Fetcher[EODHDKeyMetricsQueryParams, list[EODHDKeyMetricsData]]):
    """EODHD key metrics snapshot."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDKeyMetricsQueryParams:
        return EODHDKeyMetricsQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDKeyMetricsData]:  # pylint: disable=unused-argument
        h, v = F.highlights(data), F.valuation(data)
        if not h and not v:
            raise EmptyDataError(f"EODHD has no metrics for '{query.symbol}'.")
        return [EODHDKeyMetricsData.model_validate({
            "symbol": query.symbol.upper(),
            "period_ending": _date(h.get("MostRecentQuarter")),
            "fiscal_period": "TTM",
            "currency": F.general(data).get("CurrencyCode"),
            "market_cap": h.get("MarketCapitalization"),
            "pe_ratio": h.get("PERatio"),
            "forward_pe": v.get("ForwardPE"),
            "peg_ratio": h.get("PEGRatio"),
            "eps_ttm": h.get("DilutedEpsTTM") or h.get("EarningsShare"),
            "book_value_per_share": h.get("BookValue"),
            "dividend_per_share": h.get("DividendShare"),
            "dividend_yield": h.get("DividendYield"),
            "revenue_ttm": h.get("RevenueTTM"),
            "revenue_per_share_ttm": h.get("RevenuePerShareTTM"),
            "gross_profit_ttm": h.get("GrossProfitTTM"),
            "ebitda": h.get("EBITDA"),
            "profit_margin": h.get("ProfitMargin"),
            "operating_margin_ttm": h.get("OperatingMarginTTM"),
            "return_on_assets_ttm": h.get("ReturnOnAssetsTTM"),
            "return_on_equity_ttm": h.get("ReturnOnEquityTTM"),
            "quarterly_revenue_growth_yoy": h.get("QuarterlyRevenueGrowthYOY"),
            "quarterly_earnings_growth_yoy": h.get("QuarterlyEarningsGrowthYOY"),
            "price_to_sales_ttm": v.get("PriceSalesTTM"),
            "price_to_book_mrq": v.get("PriceBookMRQ"),
            "enterprise_value": v.get("EnterpriseValue"),
            "ev_to_revenue": v.get("EnterpriseValueRevenue"),
            "ev_to_ebitda": v.get("EnterpriseValueEbitda"),
            "beta": F.technicals(data).get("Beta"),
        })]


class EODHDFinancialRatiosQueryParams(FinancialRatiosQueryParams):
    """EODHD Financial Ratios Query.

    `limit` is accepted for interface parity but EODHD carries a single
    current snapshot — the answer is always one row.
    """
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDFinancialRatiosData(FinancialRatiosData):
    """EODHD Financial Ratios Data (current snapshot)."""

    pe_ratio: float | None = Field(default=None, description="Trailing P/E.")
    forward_pe: float | None = Field(default=None, description="Forward P/E.")
    peg_ratio: float | None = Field(default=None, description="PEG ratio.")
    price_to_sales: float | None = Field(default=None, description="Price to sales, TTM.")
    price_to_book: float | None = Field(default=None, description="Price to book, MRQ.")
    ev_to_revenue: float | None = Field(default=None, description="EV / revenue.")
    ev_to_ebitda: float | None = Field(default=None, description="EV / EBITDA.")
    profit_margin: float | None = Field(default=None, description="Net profit margin as a fraction.")
    operating_margin: float | None = Field(default=None, description="Operating margin, TTM.")
    return_on_assets: float | None = Field(default=None, description="Return on assets, TTM.")
    return_on_equity: float | None = Field(default=None, description="Return on equity, TTM.")
    dividend_yield: float | None = Field(default=None, description="Dividend yield as a fraction.")


class EODHDFinancialRatiosFetcher(
    Fetcher[EODHDFinancialRatiosQueryParams, list[EODHDFinancialRatiosData]]
):
    """EODHD financial ratios snapshot."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDFinancialRatiosQueryParams:
        return EODHDFinancialRatiosQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDFinancialRatiosData]:  # pylint: disable=unused-argument
        h, v = F.highlights(data), F.valuation(data)
        if not h and not v:
            raise EmptyDataError(f"EODHD has no ratios for '{query.symbol}'.")
        return [EODHDFinancialRatiosData.model_validate({
            "symbol": query.symbol.upper(),
            "period_ending": _date(h.get("MostRecentQuarter")),
            "fiscal_period": "TTM",
            "pe_ratio": h.get("PERatio"),
            "forward_pe": v.get("ForwardPE"),
            "peg_ratio": h.get("PEGRatio"),
            "price_to_sales": v.get("PriceSalesTTM"),
            "price_to_book": v.get("PriceBookMRQ"),
            "ev_to_revenue": v.get("EnterpriseValueRevenue"),
            "ev_to_ebitda": v.get("EnterpriseValueEbitda"),
            "profit_margin": h.get("ProfitMargin"),
            "operating_margin": h.get("OperatingMarginTTM"),
            "return_on_assets": h.get("ReturnOnAssetsTTM"),
            "return_on_equity": h.get("ReturnOnEquityTTM"),
            "dividend_yield": h.get("DividendYield"),
        })]

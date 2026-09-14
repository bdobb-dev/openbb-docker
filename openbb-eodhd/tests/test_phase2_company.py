"""Tests for the Phase-2 company-core fetchers.

Bundle samples are trimmed live /fundamentals sections recorded 2026-09-01.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from openbb_core.provider.utils.errors import EmptyDataError

from openbb_eodhd.models.esg import EODHDEsgScoreFetcher, EODHDEsgScoreQueryParams
from openbb_eodhd.models.metrics import (
    EODHDFinancialRatiosFetcher, EODHDFinancialRatiosQueryParams,
    EODHDKeyMetricsFetcher, EODHDKeyMetricsQueryParams,
)
from openbb_eodhd.models.news import (
    EODHDCompanyNewsFetcher, EODHDCompanyNewsQueryParams,
)
from openbb_eodhd.models.profile import (
    EODHDEquityInfoFetcher, EODHDEquityInfoQueryParams,
    EODHDKeyExecutivesFetcher, EODHDKeyExecutivesQueryParams,
    EODHDShareStatisticsFetcher, EODHDShareStatisticsQueryParams,
)
from openbb_eodhd.models.quote import (
    EODHDEquityQuoteFetcher, EODHDEquityQuoteQueryParams,
)

from tests.conftest import run_async

CREDS = {"eodhd_api_key": "test_key_123"}

BUNDLE = {
    "General": {
        "Code": "AAPL", "Type": "Common Stock", "Name": "Apple Inc.",
        "Exchange": "NASDAQ", "CurrencyCode": "USD", "CountryName": "USA",
        "ISIN": "US0378331005", "CUSIP": "037833100", "CIK": "0000320193",
        "LEI": "HWUPKR0MPOU8FGXBT394", "IPODate": "1980-12-12",
        "Sector": "Technology", "Industry": "Consumer Electronics",
        "GicGroup": "Technology Hardware & Equipment",
        "Description": "Apple designs smartphones.",
        "Address": "One Apple Park Way, Cupertino, CA, United States, 95014",
        "AddressData": {"Street": "One Apple Park Way", "City": "Cupertino",
                        "State": "CA", "Country": "United States", "ZIP": "95014"},
        "Phone": "(408) 996-1010", "WebURL": "https://www.apple.com",
        "LogoURL": "/img/logos/US/aapl.png", "FullTimeEmployees": 150000,
        "FiscalYearEnd": "September", "IsDelisted": False,
        "UpdatedAt": "2026-08-31",
        "Officers": {
            "0": {"Name": "Mr. Timothy D. Cook", "Title": "CEO & Director", "YearBorn": "1961"},
            "1": {"Name": "Mr. Kevan Parekh", "Title": "Senior VP & CFO", "YearBorn": "1972"},
        },
    },
    "Highlights": {
        "MarketCapitalization": 4624166158336, "EBITDA": 167959003136,
        "PERatio": 36.6301, "PEGRatio": 2.5365, "BookValue": 7.36,
        "DividendShare": 1.05, "DividendYield": 0.0033, "EarningsShare": 8.65,
        "MostRecentQuarter": "2026-06-30", "ProfitMargin": 0.2762,
        "OperatingMarginTTM": 0.3262, "ReturnOnAssetsTTM": 0.2708,
        "ReturnOnEquityTTM": 1.4875, "RevenueTTM": 466822987776,
        "RevenuePerShareTTM": 31.707, "QuarterlyRevenueGrowthYOY": 0.164,
        "GrossProfitTTM": 227123003392, "DilutedEpsTTM": 8.65,
        "QuarterlyEarningsGrowthYOY": 0.287,
    },
    "Valuation": {
        "TrailingPE": 36.6301, "ForwardPE": 33.0033, "PriceSalesTTM": 9.9056,
        "PriceBookMRQ": 42.6994, "EnterpriseValue": 4612982144400,
        "EnterpriseValueRevenue": 9.8817, "EnterpriseValueEbitda": 27.4649,
    },
    "SharesStats": {
        "SharesOutstanding": 14594180000, "SharesFloat": 14569223952,
        "PercentInsiders": 1.648, "PercentInstitutions": 66.398,
        "SharesShort": None, "ShortRatio": None, "ShortPercentFloat": 0.008,
    },
    "Technicals": {
        "Beta": 1.086, "SharesShort": 116327753,
        "SharesShortPriorMonth": 141606163, "ShortRatio": 2.19,
        "ShortPercent": 0.008,
    },
    "ESGScores": {
        "Disclaimer": "The ESG data currently in a very early Beta stage...",
        "RatingDate": "2019-01-01", "TotalEsg": 26.15, "TotalEsgPercentile": 36.8,
        "EnvironmentScore": 0.99, "SocialScore": 13.98, "GovernanceScore": 11.18,
        "ControversyLevel": 3,
    },
}


class TestEquityInfo:
    def test_transform(self):
        q = EODHDEquityInfoQueryParams(symbol="AAPL")
        r = EODHDEquityInfoFetcher.transform_data(q, BUNDLE)[0]
        assert r.symbol == "AAPL"
        assert r.ceo == "Mr. Timothy D. Cook"
        assert r.hq_address_city == "Cupertino"
        assert r.employees == 150000
        assert r.ipo_date == date(1980, 12, 12)
        assert r.industry_group == "Technology Hardware & Equipment"
        # relative LogoURL absolutized to the public static host
        assert r.logo_url == "https://eodhd.com/img/logos/US/aapl.png"

    def test_empty_bundle(self):
        q = EODHDEquityInfoQueryParams(symbol="AAPL")
        with pytest.raises(EmptyDataError):
            EODHDEquityInfoFetcher.transform_data(q, {})


class TestShareStatistics:
    def test_transform(self):
        q = EODHDShareStatisticsQueryParams(symbol="AAPL")
        r = EODHDShareStatisticsFetcher.transform_data(q, BUNDLE)[0]
        assert r.outstanding_shares == 14594180000
        assert r.float_shares == 14569223952
        assert r.free_float == pytest.approx(99.829, abs=0.001)
        # SharesStats nulls fall back to Technicals
        assert r.shares_short == 116327753
        assert r.short_ratio == 2.19
        assert r.date == date(2026, 8, 31)


class TestKeyExecutives:
    def test_transform(self):
        q = EODHDKeyExecutivesQueryParams(symbol="AAPL")
        rows = EODHDKeyExecutivesFetcher.transform_data(q, BUNDLE)
        assert [r.name for r in rows][:1] == ["Mr. Timothy D. Cook"]
        assert rows[0].year_born == 1961


class TestKeyMetrics:
    def test_transform(self):
        q = EODHDKeyMetricsQueryParams(symbol="AAPL")
        r = EODHDKeyMetricsFetcher.transform_data(q, BUNDLE)[0]
        assert r.market_cap == 4624166158336
        assert r.period_ending == date(2026, 6, 30)
        assert r.fiscal_period == "TTM"
        assert r.forward_pe == 33.0033
        assert r.beta == 1.086
        assert r.currency == "USD"


class TestFinancialRatios:
    def test_transform_single_row(self):
        q = EODHDFinancialRatiosQueryParams(symbol="AAPL", limit=12)
        rows = EODHDFinancialRatiosFetcher.transform_data(q, BUNDLE)
        assert len(rows) == 1  # snapshot only, regardless of limit
        assert rows[0].return_on_equity == 1.4875
        assert rows[0].price_to_book == 42.6994


class TestEsgScore:
    def test_transform_deprecated_feed(self):
        q = EODHDEsgScoreQueryParams(symbol="AAPL")
        r = EODHDEsgScoreFetcher.transform_data(q, BUNDLE)[0]
        assert r.esg_score == 26.15
        assert r.period_ending == date(2019, 1, 1)  # the feed is that stale
        assert r.disclaimer.startswith("The ESG data")

    def test_missing_section(self):
        q = EODHDEsgScoreQueryParams(symbol="AAPL")
        with pytest.raises(EmptyDataError):
            EODHDEsgScoreFetcher.transform_data(q, {"General": {}})


QUOTE_ROWS = [
    {"code": "AAPL.US", "timestamp": 1788272100, "open": 316.98, "high": 323.515,
     "low": 314.73, "close": 323.515, "volume": 11399844,
     "previousClose": 316.85, "change": 6.665, "change_p": 2.1035},
]


class TestEquityQuote:
    def test_transform(self):
        q = EODHDEquityQuoteQueryParams(symbol="AAPL")
        r = EODHDEquityQuoteFetcher.transform_data(q, QUOTE_ROWS)[0]
        assert r.symbol == "AAPL.US"
        assert r.last_price == 323.515
        assert r.change_percent == 2.1035
        assert r.last_timestamp.year == 2026

    def test_multi_symbol_request(self):
        q = EODHDEquityQuoteQueryParams(symbol="AAPL,MSFT")
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.get_live_stock_prices.return_value = QUOTE_ROWS
        with patch("openbb_eodhd.models._client.get_client", return_value=client):
            run_async(EODHDEquityQuoteFetcher.aextract_data, q, CREDS)
        client.get_live_stock_prices.assert_called_once_with(
            ticker="AAPL.US", s="MSFT.US"
        )


NEWS_ROW = {
    "date": "2026-09-01T13:23:07+00:00", "title": "Apple headline",
    "content": "Body.", "link": "https://example.com/a",
    "symbols": ["AAPL.US", "MSFT.US"], "tags": ["TECHNOLOGY"],
    "sentiment": {"polarity": 0.9},
}


class TestCompanyNews:
    def test_transform(self):
        q = EODHDCompanyNewsQueryParams(symbol="AAPL")
        rows = EODHDCompanyNewsFetcher.transform_data(q, [NEWS_ROW, {"date": "x"}])
        assert len(rows) == 1
        assert rows[0].symbols == "AAPL.US,MSFT.US"
        assert rows[0].url == "https://example.com/a"

    def test_requires_symbol(self):
        from openbb_core.app.model.abstract.error import OpenBBError

        q = EODHDCompanyNewsQueryParams()
        with pytest.raises(OpenBBError):
            run_async(EODHDCompanyNewsFetcher.aextract_data, q, CREDS)

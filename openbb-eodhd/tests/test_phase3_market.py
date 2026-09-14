"""Tests for the Phase-3 market/reference fetchers.

Samples are trimmed live responses recorded 2026-09-01 with the account key.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from openbb_core.provider.utils.errors import EmptyDataError

from openbb_eodhd.models.currency import (
    EODHDCurrencyPairsFetcher, EODHDCurrencyPairsQueryParams,
    EODHDCurrencySnapshotsFetcher, EODHDCurrencySnapshotsQueryParams,
)
from openbb_eodhd.models.dividend_yield import (
    EODHDTrailingDivYieldFetcher, EODHDTrailingDivYieldQueryParams,
)
from openbb_eodhd.models.etf import (
    EODHDEtfCountriesFetcher, EODHDEtfCountriesQueryParams,
    EODHDEtfHoldingsFetcher, EODHDEtfHoldingsQueryParams,
    EODHDEtfInfoFetcher, EODHDEtfInfoQueryParams,
    EODHDEtfSectorsFetcher, EODHDEtfSectorsQueryParams,
)
from openbb_eodhd.models.government import (
    EODHDGovernmentTradesFetcher, EODHDGovernmentTradesQueryParams,
)
from openbb_eodhd.models.index import (
    EODHDAvailableIndicesFetcher, EODHDAvailableIndicesQueryParams,
    _qualify_index,
)
from openbb_eodhd.models.market_cap import (
    EODHDHistoricalMarketCapFetcher, EODHDHistoricalMarketCapQueryParams,
)
from openbb_eodhd.models.news import EODHDWorldNewsFetcher, EODHDWorldNewsQueryParams
from openbb_eodhd.models.screener import (
    EODHDEquityScreenerFetcher, EODHDEquityScreenerQueryParams,
)
from openbb_eodhd.models.search import EODHDEquitySearchFetcher, EODHDEquitySearchQueryParams
from openbb_eodhd.models.treasury import (
    EODHDTreasuryRatesFetcher, EODHDTreasuryRatesQueryParams,
    EODHDYieldCurveFetcher, EODHDYieldCurveQueryParams,
)

from tests.conftest import run_async

CREDS = {"eodhd_api_key": "test_key_123"}


# ============================================================
# Search
# ============================================================

SEARCH_ROWS = [
    {"Code": "AAPL", "Exchange": "US", "Name": "Apple Inc.", "Type": "Common Stock",
     "Country": "USA", "Currency": "USD", "ISIN": "US0378331005", "isPrimary": True,
     "previousClose": 316.85, "previousCloseDate": "2026-08-31"},
]


class TestSearch:
    def test_transform(self):
        query = EODHDEquitySearchQueryParams(query="apple")
        rows = EODHDEquitySearchFetcher.transform_data(query, SEARCH_ROWS)
        r = rows[0]
        assert r.symbol == "AAPL.US"
        assert r.isin == "US0378331005"
        assert r.is_primary is True
        assert r.previous_close_date == date(2026, 8, 31)

    def test_empty_query_raises(self):
        from openbb_core.app.model.abstract.error import OpenBBError

        query = EODHDEquitySearchQueryParams(query=" ")
        with pytest.raises(OpenBBError):
            run_async(EODHDEquitySearchFetcher.aextract_data, query, CREDS)


# ============================================================
# Screener
# ============================================================

SCREENER_ROW = {
    "code": "AAPL", "name": "Apple Inc", "last_day_data_date": "2026-08-28",
    "adjusted_close": 316.85, "exchange": "US", "currency_symbol": "$",
    "market_capitalization": 4.7e12, "earnings_share": 7.4, "dividend_yield": 0.004,
    "sector": "Technology", "industry": "Consumer Electronics",
    "avgvol_1d": 4.4e7, "avgvol_200d": 5.0e7,
}


class TestScreener:
    def test_filters_built(self):
        q = EODHDEquityScreenerQueryParams(
            exchange="us", market_cap_min=1e9, price_max=50, dividend_yield_min=0.03
        )
        assert q.to_filters() == [
            ["exchange", "=", "us"],
            ["market_capitalization", ">=", 1e9],
            ["adjusted_close", "<=", 50],
            ["dividend_yield", ">=", 0.03],
        ]

    def test_transform(self):
        q = EODHDEquityScreenerQueryParams()
        rows = EODHDEquityScreenerFetcher.transform_data(q, [SCREENER_ROW])
        r = rows[0]
        assert r.symbol == "AAPL.US"
        assert r.market_cap == 4.7e12
        assert r.last_day_data_date == date(2026, 8, 28)


# ============================================================
# Currency
# ============================================================

FOREX_LIST = [
    {"Code": "EURUSD", "Name": "EUR/USD", "Country": "Unknown", "Exchange": "FOREX",
     "Currency": "NA", "Type": "Currency", "Isin": None},
    {"Code": "AEDCAD", "Name": "UAE Dirham/Canadian Dollar", "Country": "Unknown",
     "Exchange": "FOREX", "Currency": "NA", "Type": "Currency", "Isin": None},
]

RT_FOREX = [
    {"code": "EURUSD.FOREX", "timestamp": 1788272040, "open": 1.1621, "high": 1.1628,
     "low": 1.1592, "close": 1.161, "volume": 0, "previousClose": 1.1618,
     "change": -0.0008, "change_p": -0.0689},
]


class TestCurrency:
    def test_pairs_query_filter(self):
        q = EODHDCurrencyPairsQueryParams(query="eurusd")
        rows = EODHDCurrencyPairsFetcher.transform_data(q, FOREX_LIST)
        assert [r.symbol for r in rows] == ["EURUSD"]
        assert rows[0].currency is None  # 'NA' normalized away

    def test_snapshot_direct(self):
        q = EODHDCurrencySnapshotsQueryParams(base="USD", quote_type="direct")
        rows = EODHDCurrencySnapshotsFetcher.transform_data(q, RT_FOREX)
        r = rows[0]
        assert r.base_currency == "USD" and r.counter_currency == "EUR"
        assert r.last_rate == 1.161
        assert r.last_rate_time.year == 2026

    def test_snapshot_indirect_counter(self):
        q = EODHDCurrencySnapshotsQueryParams(base="EUR", quote_type="indirect")
        rows = EODHDCurrencySnapshotsFetcher.transform_data(q, RT_FOREX)
        assert rows[0].counter_currency == "USD"


# ============================================================
# Index
# ============================================================

INDX_LIST = [
    {"Code": "GSPC", "Name": "S&P 500 Index", "Country": "USA", "Exchange": "INDX",
     "Currency": "USD", "Type": "INDEX", "Isin": None},
]


class TestIndex:
    def test_qualify(self):
        assert _qualify_index("^GSPC, dji") == ["GSPC.INDX", "DJI.INDX"]
        assert _qualify_index("GSPC.INDX") == ["GSPC.INDX"]

    def test_available_transform(self):
        q = EODHDAvailableIndicesQueryParams(query="s&p 500")
        rows = EODHDAvailableIndicesFetcher.transform_data(q, INDX_LIST)
        assert rows[0].symbol == "GSPC.INDX"
        assert rows[0].country == "USA"


# ============================================================
# Market cap
# ============================================================

class TestMarketCap:
    def test_transform_sorted(self):
        q = EODHDHistoricalMarketCapQueryParams(symbol="AAPL")
        rows = EODHDHistoricalMarketCapFetcher.transform_data(q, [
            {"date": "2021-08-13", "value": 2.4e12},
            {"date": "2021-08-06", "value": 2.3e12},
        ])
        assert [r.date for r in rows] == [date(2021, 8, 6), date(2021, 8, 13)]
        assert rows[0].symbol == "AAPL"
        assert rows[0].market_cap == 2.3e12


# ============================================================
# News
# ============================================================

NEWS_ROW = {
    "date": "2026-09-01T13:23:07+00:00", "title": "Headline", "content": "Body text.",
    "link": "https://example.com/x", "symbols": ["CPNG.US"],
    "tags": ["TECHNOLOGY"], "sentiment": {"polarity": 0.997},
}


class TestNews:
    def test_transform(self):
        q = EODHDWorldNewsQueryParams()
        rows = EODHDWorldNewsFetcher.transform_data(q, [NEWS_ROW, {"date": None}])
        assert len(rows) == 1
        r = rows[0]
        assert r.url == "https://example.com/x"
        assert r.symbols == ["CPNG.US"]
        assert r.body == "Body text."


# ============================================================
# Treasury
# ============================================================

UST_ROWS = [
    {"date": "2026-01-02", "tenor": "1M", "rate": 3.72},
    {"date": "2026-01-02", "tenor": "1.5M", "rate": 3.71},   # no standard slot
    {"date": "2026-01-02", "tenor": "10Y", "rate": 4.19},
    {"date": "2026-01-05", "tenor": "1M", "rate": 3.70},
    {"date": "2026-01-05", "tenor": "10Y", "rate": 4.21},
]


class TestTreasury:
    def test_rates_pivot_and_normalize(self):
        q = EODHDTreasuryRatesQueryParams()
        rows = EODHDTreasuryRatesFetcher.transform_data(q, UST_ROWS)
        assert len(rows) == 2
        assert rows[0].date == date(2026, 1, 2)
        assert rows[0].month_1 == pytest.approx(0.0372)
        assert rows[0].year_10 == pytest.approx(0.0419)

    def test_rates_window(self):
        q = EODHDTreasuryRatesQueryParams(start_date=date(2026, 1, 5))
        rows = EODHDTreasuryRatesFetcher.transform_data(q, UST_ROWS)
        assert [r.date for r in rows] == [date(2026, 1, 5)]

    def test_yield_curve_latest_default(self):
        q = EODHDYieldCurveQueryParams()
        rows = EODHDYieldCurveFetcher.transform_data(q, UST_ROWS)
        assert {r.maturity for r in rows} == {"month_1", "year_10"}
        assert all(r.date == date(2026, 1, 5) for r in rows)

    def test_yield_curve_asof(self):
        q = EODHDYieldCurveQueryParams(date="2026-01-03")
        rows = EODHDYieldCurveFetcher.transform_data(q, UST_ROWS)
        assert all(r.date == date(2026, 1, 2) for r in rows)


# ============================================================
# Government trades
# ============================================================

CONGRESS_ROW = {
    "chamber": "house",
    "member": {"full_name": "Pete Sessions", "state": "TX", "party": None, "district": 32},
    "asset": {"symbol": "IBM", "description": "IBM", "asset_type": "Stock"},
    "transaction": {"type": "purchase", "transaction_date": "3031-04-30",
                    "disclosure_date": "2021-05-03", "owner": "Spouse",
                    "amount_range": "$1,001 - $15,000", "amount_low": 1001,
                    "amount_high": 15000, "days_to_disclose": 368891, "is_late": True},
    "source": {"filing_url": "https://example.gov/f.pdf"},
}


class TestGovernmentTrades:
    def test_transform_garbage_txn_date(self):
        q = EODHDGovernmentTradesQueryParams()
        rows = EODHDGovernmentTradesFetcher.transform_data(q, [CONGRESS_ROW])
        r = rows[0]
        assert r.date == date(2021, 5, 3)
        # year-3031 garbage from the filing parses through pandas as-is or
        # to None — either way the row survives on its disclosure date
        assert r.symbol == "IBM"
        assert r.representative == "Pete Sessions"
        assert r.amount_high == 15000
        assert r.is_late is True


# ============================================================
# ETF (bundle-derived)
# ============================================================

ETF_BUNDLE = {
    "General": {"Name": "Vanguard Total Stock Market ETF", "Description": "Broad US equity."},
    "ETF_Data": {
        "ISIN": "US9229087690", "Company_Name": "Vanguard", "Domicile": "United States",
        "ETF_URL": "https://vanguard.com/vti", "Inception_Date": "2001-05-24",
        "Yield": "1.060000", "NetExpenseRatio": "0.00030", "Ongoing_Charge": "0.0000",
        "AnnualHoldingsTurnover": "0.02000", "TotalAssets": "692530687580.00",
        "Holdings_Count": 3467,
        "Holdings": {
            "NVDA.US": {"Code": "NVDA", "Name": "NVIDIA Corporation",
                        "Sector": "Technology", "Country": "United States",
                        "Region": "North America", "Assets_%": 6.32},
            "AAPL.US": {"Code": "AAPL", "Name": "Apple Inc.",
                        "Sector": "Technology", "Country": "United States",
                        "Region": "North America", "Assets_%": 5.84},
        },
        "Sector_Weights": {
            "Technology": {"Equity_%": "30.5", "Relative_to_Category": "29"},
            "Utilities": {"Equity_%": "2.4", "Relative_to_Category": "2.6"},
        },
        "World_Regions": {
            "North America": {"Equity_%": "99.474", "Relative_to_Category": "97.9"},
        },
    },
}


class TestEtf:
    def test_info(self):
        q = EODHDEtfInfoQueryParams(symbol="VTI")
        r = EODHDEtfInfoFetcher.transform_data(q, ETF_BUNDLE)[0]
        assert r.symbol == "VTI"
        assert r.issuer == "Vanguard"
        assert r.inception_date == date(2001, 5, 24)
        assert r.dividend_yield == pytest.approx(0.0106)      # percent → fraction
        assert r.net_expense_ratio == pytest.approx(0.0003)   # already a fraction
        assert r.holdings_count == 3467

    def test_holdings_sorted(self):
        q = EODHDEtfHoldingsQueryParams(symbol="VTI")
        rows = EODHDEtfHoldingsFetcher.transform_data(q, ETF_BUNDLE)
        assert [r.symbol for r in rows] == ["NVDA.US", "AAPL.US"]
        assert rows[0].weight == pytest.approx(0.0632)

    def test_sectors(self):
        q = EODHDEtfSectorsQueryParams(symbol="VTI")
        rows = EODHDEtfSectorsFetcher.transform_data(q, ETF_BUNDLE)
        assert rows[0].sector == "Technology"
        assert rows[0].weight == pytest.approx(0.305)

    def test_countries(self):
        q = EODHDEtfCountriesQueryParams(symbol="VTI")
        rows = EODHDEtfCountriesFetcher.transform_data(q, ETF_BUNDLE)
        assert rows[0].country == "North America"

    def test_not_an_etf(self):
        q = EODHDEtfInfoQueryParams(symbol="AAPL")
        with pytest.raises(EmptyDataError):
            EODHDEtfInfoFetcher.transform_data(q, {"General": {"Name": "Apple"}})


# ============================================================
# Trailing dividend yield
# ============================================================

class TestTrailingDivYield:
    def test_computation(self):
        q = EODHDTrailingDivYieldQueryParams(symbol="AAPL", limit=2)
        data = {
            "dividends": [
                {"date": "2025-11-10", "value": 0.25},
                {"date": "2026-02-10", "value": 0.25},
                {"date": "2026-05-12", "value": 0.26},
                {"date": "2026-08-11", "value": 0.26},
                {"date": "2025-08-01", "value": 0.24},  # > 365d before 2026-08-31
            ],
            "prices": [
                {"date": "2026-08-28", "close": 300.0},
                {"date": "2026-08-31", "close": 320.0},
            ],
        }
        rows = EODHDTrailingDivYieldFetcher.transform_data(q, data)
        assert len(rows) == 2
        # ttm on 2026-08-31 = 0.25+0.25+0.26+0.26 = 1.02
        assert rows[-1].trailing_dividend_yield == pytest.approx(1.02 / 320.0)

    def test_limit_window(self):
        q = EODHDTrailingDivYieldQueryParams(symbol="AAPL", limit=1)
        data = {"dividends": [], "prices": [
            {"date": "2026-08-28", "close": 300.0},
            {"date": "2026-08-31", "close": 320.0},
        ]}
        rows = EODHDTrailingDivYieldFetcher.transform_data(q, data)
        assert len(rows) == 1 and rows[0].date == date(2026, 8, 31)
        assert rows[0].trailing_dividend_yield == 0.0

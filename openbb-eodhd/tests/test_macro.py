"""Tests for the Phase-4 macro cluster.

Samples are trimmed live /macro-indicator responses recorded 2026-09-01.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from openbb_core.app.model.abstract.error import OpenBBError
from openbb_core.provider.utils.errors import UnauthorizedError

from openbb_eodhd.models.macro import (
    EODHDConsumerPriceIndexFetcher, EODHDConsumerPriceIndexQueryParams,
    EODHDCountryProfileFetcher, EODHDCountryProfileQueryParams,
    EODHDEconomicIndicatorsFetcher, EODHDEconomicIndicatorsQueryParams,
    EODHDGdpNominalFetcher, EODHDGdpNominalQueryParams,
    EODHDGdpRealFetcher, EODHDGdpRealQueryParams,
    EODHDUnemploymentFetcher, EODHDUnemploymentQueryParams,
    resolve_country,
)

from tests.conftest import run_async

CREDS = {"eodhd_api_key": "test_key_123"}


def _row(indicator, d, value):
    return {"CountryCode": "USA", "CountryName": "United States",
            "Indicator": indicator, "Date": d, "Period": "Annual", "Value": value}


SERIES = {
    "gdp_growth_annual": [
        _row("GDP growth (annual %)", "2025-12-31", 2.1614),
        _row("GDP growth (annual %)", "2024-12-31", 2.7932),
    ],
    "gdp_current_usd": [
        _row("GDP (current US$)", "2025-12-31", 30769700000000),
        _row("GDP (current US$)", "2024-12-31", 29298013000000),
    ],
    "inflation_consumer_prices_annual": [
        _row("Inflation, consumer prices (annual %)", "2024-12-31", 2.9495),
    ],
    "consumer_price_index": [
        _row("Consumer price index (2010 = 100)", "2024-12-31", 143.8573),
    ],
    "unemployment_total_percent": [
        _row("Unemployment, total (%)", "2025-12-31", 4.282),
        {"CountryCode": "USA", "CountryName": "United States",
         "Indicator": "x", "Date": "2023-12-31", "Value": None},  # dropped
    ],
    "population_total": [_row("Population, total", "2025-12-31", 341784857)],
    "debt_percent_gdp": [_row("Central government debt (% of GDP)", "2024-12-31", 115.7684)],
}


def _client():
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get_macro_indicators_data.side_effect = (
        lambda country, indicator=None: SERIES.get(indicator, [])
    )
    return client


def _patched(fetcher, query):
    client = _client()
    with patch("openbb_eodhd.models._client.get_client", return_value=client):
        raw = run_async(fetcher.aextract_data, query, CREDS)
    return client, fetcher.transform_data(query, raw)


class TestResolveCountry:
    def test_forms(self):
        assert resolve_country("united_states") == "USA"
        assert resolve_country("US") == "USA"
        assert resolve_country("usa") == "USA"
        assert resolve_country("DEU") == "DEU"
        assert resolve_country("narnia") == "NARNIA"  # pass-through


class TestGdp:
    def test_real_is_growth(self):
        client, rows = _patched(EODHDGdpRealFetcher, EODHDGdpRealQueryParams())
        client.get_macro_indicators_data.assert_called_with(
            country="USA", indicator="gdp_growth_annual"
        )
        assert rows[-1].value == 2.1614
        assert rows[-1].date == date(2025, 12, 31)

    def test_nominal_is_level(self):
        _, rows = _patched(EODHDGdpNominalFetcher, EODHDGdpNominalQueryParams(country="US"))
        assert rows[-1].value == 30769700000000

    def test_window(self):
        _, rows = _patched(
            EODHDGdpRealFetcher,
            EODHDGdpRealQueryParams(start_date=date(2025, 1, 1)),
        )
        assert [r.date for r in rows] == [date(2025, 12, 31)]


class TestCpi:
    def test_yoy(self):
        client, rows = _patched(
            EODHDConsumerPriceIndexFetcher, EODHDConsumerPriceIndexQueryParams()
        )
        client.get_macro_indicators_data.assert_called_with(
            country="USA", indicator="inflation_consumer_prices_annual"
        )
        assert rows[0].value == 2.9495

    def test_index(self):
        _, rows = _patched(
            EODHDConsumerPriceIndexFetcher,
            EODHDConsumerPriceIndexQueryParams(transform="index"),
        )
        assert rows[0].value == 143.8573

    def test_unsupported_transform(self):
        q = EODHDConsumerPriceIndexQueryParams(transform="period")
        with pytest.raises(OpenBBError):
            run_async(EODHDConsumerPriceIndexFetcher.aextract_data, q, CREDS)


class TestUnemployment:
    def test_null_values_dropped(self):
        _, rows = _patched(EODHDUnemploymentFetcher, EODHDUnemploymentQueryParams())
        assert len(rows) == 1
        assert rows[0].value == 4.282


class TestEconomicIndicators:
    def test_multi(self):
        q = EODHDEconomicIndicatorsQueryParams(
            symbol="gdp_current_usd,population_total", country="united_states"
        )
        _, rows = _patched(EODHDEconomicIndicatorsFetcher, q)
        assert {r.symbol for r in rows} == {"gdp_current_usd", "population_total"}
        pop = [r for r in rows if r.symbol == "population_total"][0]
        assert pop.indicator_name == "Population, total"
        assert pop.country == "United States"


class TestCountryProfile:
    def test_assembled(self):
        q = EODHDCountryProfileQueryParams(country="united_states")
        _, rows = _patched(EODHDCountryProfileFetcher, q)
        r = rows[0]
        assert r.country == "United States"
        assert r.population == 341784857
        assert r.gdp_usd == 30769700000000
        assert r.gdp_yoy == 2.1614
        assert r.cpi_yoy == 2.9495
        assert r.jobless_rate == 4.282
        assert r.govt_debt_gdp == 115.7684
        assert r.policy_rate is None  # not in the World-Bank set

    def test_auth_error_propagates(self):
        q = EODHDCountryProfileQueryParams(country="united_states")
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.get_macro_indicators_data.return_value = {"message": "denied"}
        with patch("openbb_eodhd.models._client.get_client", return_value=client):
            with pytest.raises(UnauthorizedError):
                run_async(EODHDCountryProfileFetcher.aextract_data, q, CREDS)

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Tests for openbb_eodhd.models.currency_historical."""

from datetime import date


from openbb_eodhd.models.currency_historical import (
    _qualify_forex,
    EODHDCurrencyHistoricalData,
    EODHDCurrencyHistoricalFetcher,
    EODHDCurrencyHistoricalQueryParams,
)


# ============================================================
# _qualify_forex
# ============================================================

class TestQualifyForex:
    def test_concatenated_format(self):
        assert _qualify_forex("EURUSD") == ["EURUSD.FOREX"]

    def test_slash_format(self):
        assert _qualify_forex("EUR/USD") == ["EURUSD.FOREX"]

    def test_already_qualified(self):
        assert _qualify_forex("EURUSD.FOREX") == ["EURUSD.FOREX"]

    def test_multiple_pairs(self):
        result = _qualify_forex("EURUSD,GBPUSD")
        assert result == ["EURUSD.FOREX", "GBPUSD.FOREX"]

    def test_lowercase_input(self):
        assert _qualify_forex("eurusd") == ["EURUSD.FOREX"]

    def test_whitespace_stripped(self):
        assert _qualify_forex("  EURUSD , GBPUSD  ") == ["EURUSD.FOREX", "GBPUSD.FOREX"]

    def test_empty_segment_skipped(self):
        assert _qualify_forex("EURUSD,,") == ["EURUSD.FOREX"]

    def test_empty_string(self):
        assert _qualify_forex("") == []


# ============================================================
# QueryParams
# ============================================================

class TestQueryParams:
    def test_default_interval(self):
        qp = EODHDCurrencyHistoricalQueryParams(symbol="EURUSD")
        assert qp.interval == "1d"

    def test_json_schema_extra_exposed_to_core(self):
        # OpenBB core reads __json_schema_extra__ (not model_config) for these.
        extra = EODHDCurrencyHistoricalQueryParams.__json_schema_extra__
        assert extra["symbol"]["multiple_items_allowed"] is True
        assert "interval" in extra


# ============================================================
# Data model
# ============================================================

class TestDataModel:
    def test_creation(self):
        d = EODHDCurrencyHistoricalData.model_validate({
            "date": "2026-01-02", "open": 1.05, "high": 1.06,
            "low": 1.04, "close": 1.055,
        })
        assert d.open == 1.05
        assert d.close == 1.055

    def test_adjusted_close_optional(self):
        d = EODHDCurrencyHistoricalData.model_validate({
            "date": "2026-01-02", "open": 1.05, "high": 1.06,
            "low": 1.04, "close": 1.055,
        })
        assert d.adjusted_close is None


# ============================================================
# Fetcher
# ============================================================

class TestFetcher:
    def test_transform_query_defaults(self):
        params = {"symbol": "EURUSD"}
        qp = EODHDCurrencyHistoricalFetcher.transform_query(params)
        assert qp.symbol == "EURUSD"
        assert qp.start_date is not None
        assert qp.end_date is not None

    def test_transform_query_preserves_dates(self):
        params = {"symbol": "EURUSD", "start_date": date(2024, 1, 1), "end_date": date(2024, 1, 31)}
        qp = EODHDCurrencyHistoricalFetcher.transform_query(params)
        assert qp.start_date == date(2024, 1, 1)
        assert qp.end_date == date(2024, 1, 31)

    def test_transform_data(self, eod_bar_data):
        qp = EODHDCurrencyHistoricalQueryParams(symbol="EURUSD")
        results = EODHDCurrencyHistoricalFetcher.transform_data(qp, eod_bar_data)
        assert len(results) == 2
        for r in results:
            assert isinstance(r, EODHDCurrencyHistoricalData)

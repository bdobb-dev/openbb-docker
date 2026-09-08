# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Tests for openbb_eodhd.models.equity_historical."""

from datetime import date


from openbb_eodhd.models.equity_historical import (
    _qualify_equity,
    EODHDEquityHistoricalData,
    EODHDEquityHistoricalFetcher,
    EODHDEquityHistoricalQueryParams,
)


# ============================================================
# _qualify_equity
# ============================================================

class TestQualifyEquity:
    def test_bare_symbol_default_exchange(self):
        assert _qualify_equity("AAPL", "US") == ["AAPL.US"]

    def test_bare_symbol_custom_exchange(self):
        assert _qualify_equity("AAPL", "LSE") == ["AAPL.LSE"]

    def test_already_qualified(self):
        assert _qualify_equity("AAPL.US", "US") == ["AAPL.US"]

    def test_multiple_symbols(self):
        result = _qualify_equity("AAPL,MSFT", "US")
        assert result == ["AAPL.US", "MSFT.US"]

    def test_mixed_qualified_and_bare(self):
        result = _qualify_equity("AAPL.US,MSFT", "US")
        assert result == ["AAPL.US", "MSFT.US"]

    def test_lowercase_input(self):
        assert _qualify_equity("aapl", "US") == ["AAPL.US"]

    def test_whitespace_stripped(self):
        assert _qualify_equity("  AAPL , MSFT  ", "US") == ["AAPL.US", "MSFT.US"]

    def test_empty_segment_skipped(self):
        assert _qualify_equity("AAPL,,", "US") == ["AAPL.US"]

    def test_empty_string(self):
        assert _qualify_equity("", "US") == []

    def test_exchange_upper_cased(self):
        assert _qualify_equity("AAPL", "uk") == ["AAPL.UK"]


# ============================================================
# QueryParams
# ============================================================

class TestQueryParams:
    def test_default_interval(self):
        qp = EODHDEquityHistoricalQueryParams(symbol="AAPL")
        assert qp.interval == "1d"

    def test_default_exchange(self):
        qp = EODHDEquityHistoricalQueryParams(symbol="AAPL")
        assert qp.exchange == "US"

    def test_custom_exchange(self):
        qp = EODHDEquityHistoricalQueryParams(symbol="AAPL", exchange="LSE")
        assert qp.exchange == "LSE"

    def test_json_schema_extra_exposed_to_core(self):
        # OpenBB core reads __json_schema_extra__ (not model_config) for these.
        extra = EODHDEquityHistoricalQueryParams.__json_schema_extra__
        assert extra["symbol"]["multiple_items_allowed"] is True
        assert "interval" in extra


# ============================================================
# Data model
# ============================================================

class TestDataModel:
    def test_creation(self):
        d = EODHDEquityHistoricalData.model_validate({
            "date": "2026-01-02", "open": 150.0, "high": 153.0,
            "low": 149.0, "close": 152.0,
        })
        assert d.open == 150.0
        assert d.close == 152.0
        assert d.high == 153.0
        assert d.low == 149.0

    def test_adjusted_close_optional(self):
        d = EODHDEquityHistoricalData.model_validate({
            "date": "2026-01-02", "open": 150.0, "high": 153.0,
            "low": 149.0, "close": 152.0,
        })
        assert d.adjusted_close is None

    def test_adjusted_close_present(self):
        d = EODHDEquityHistoricalData.model_validate({
            "date": "2026-01-02", "open": 150.0, "high": 153.0,
            "low": 149.0, "close": 152.0, "adjusted_close": 151.5,
        })
        assert d.adjusted_close == 151.5


# ============================================================
# Fetcher
# ============================================================

class TestFetcher:
    def test_transform_query_defaults(self):
        params = {"symbol": "AAPL"}
        qp = EODHDEquityHistoricalFetcher.transform_query(params)
        assert qp.symbol == "AAPL"
        assert qp.exchange == "US"
        assert qp.start_date is not None
        assert qp.end_date is not None

    def test_transform_query_preserves_dates(self):
        params = {"symbol": "AAPL", "start_date": date(2024, 1, 1), "end_date": date(2024, 1, 31)}
        qp = EODHDEquityHistoricalFetcher.transform_query(params)
        assert qp.start_date == date(2024, 1, 1)
        assert qp.end_date == date(2024, 1, 31)

    def test_transform_query_custom_exchange(self):
        params = {"symbol": "AAPL", "exchange": "LSE"}
        qp = EODHDEquityHistoricalFetcher.transform_query(params)
        assert qp.exchange == "LSE"

    def test_transform_data_single_symbol(self, eod_bar_data):
        qp = EODHDEquityHistoricalQueryParams(symbol="AAPL")
        results = EODHDEquityHistoricalFetcher.transform_data(qp, eod_bar_data)
        assert len(results) == 2
        for r in results:
            assert isinstance(r, EODHDEquityHistoricalData)

    def test_transform_data_multi_symbol(self):
        qp = EODHDEquityHistoricalQueryParams(symbol="AAPL,MSFT", interval="1d")
        rows = [
            {"date": "2024-01-02", "open": 150.0, "high": 153.0, "low": 149.0,
             "close": 152.0, "volume": 1000000, "adjusted_close": 151.5, "_symbol": "AAPL.US"},
            {"date": "2024-01-02", "open": 300.0, "high": 305.0, "low": 299.0,
             "close": 302.0, "volume": 2000000, "adjusted_close": 301.0, "_symbol": "MSFT.US"},
        ]
        results = EODHDEquityHistoricalFetcher.transform_data(qp, rows)
        assert len(results) == 2
        assert results[0].symbol == "AAPL.US"
        assert results[1].symbol == "MSFT.US"

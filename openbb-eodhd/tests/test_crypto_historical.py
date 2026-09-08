# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Tests for openbb_eodhd.models.crypto_historical."""

from datetime import date


from openbb_eodhd.models.crypto_historical import (
    _qualify_crypto,
    EODHDCryptoHistoricalData,
    EODHDCryptoHistoricalFetcher,
    EODHDCryptoHistoricalQueryParams,
)


# ============================================================
# _qualify_crypto
# ============================================================

class TestQualifyCrypto:
    def test_dash_format(self):
        assert _qualify_crypto("BTC-USD") == ["BTC-USD.CC"]

    def test_slash_format(self):
        assert _qualify_crypto("BTC/USD") == ["BTC-USD.CC"]

    def test_concat_format(self):
        assert _qualify_crypto("BTCUSD") == ["BTC-USD.CC"]

    def test_already_qualified(self):
        assert _qualify_crypto("BTC-USD.CC") == ["BTC-USD.CC"]

    def test_multiple_symbols(self):
        result = _qualify_crypto("BTC-USD,ETH-USD")
        assert result == ["BTC-USD.CC", "ETH-USD.CC"]

    def test_lowercase_input(self):
        assert _qualify_crypto("btc-usd") == ["BTC-USD.CC"]

    def test_whitespace_stripped(self):
        assert _qualify_crypto("  BTC-USD , ETH-USD  ") == ["BTC-USD.CC", "ETH-USD.CC"]

    def test_empty_segment_skipped(self):
        assert _qualify_crypto("BTC-USD,,") == ["BTC-USD.CC"]

    def test_empty_string(self):
        assert _qualify_crypto("") == []

    def test_non_usd_pair(self):
        assert _qualify_crypto("ETH-BTC") == ["ETH-BTC.CC"]

    def test_usdt_suffix_untouched(self):
        # BTCUSDT -> strip "USDT" suffix? No, the logic only checks .endswith("USD") and len > 3
        # BTCUSDT ends with "USDT" which doesn't match "USD", so it stays as-is
        assert _qualify_crypto("BTCUSDT") == ["BTCUSDT.CC"]


# ============================================================
# QueryParams
# ============================================================

class TestQueryParams:
    def test_default_interval(self):
        qp = EODHDCryptoHistoricalQueryParams(symbol="BTC-USD")
        assert qp.interval == "1d"

    def test_custom_interval(self):
        qp = EODHDCryptoHistoricalQueryParams(symbol="BTC-USD", interval="1m")
        assert qp.interval == "1m"

    def test_json_schema_extra_exposed_to_core(self):
        # OpenBB core reads __json_schema_extra__ (not model_config) for these.
        extra = EODHDCryptoHistoricalQueryParams.__json_schema_extra__
        assert extra["symbol"]["multiple_items_allowed"] is True
        assert "interval" in extra


# ============================================================
# Data model
# ============================================================

class TestDataModel:
    def test_creation(self):
        d = EODHDCryptoHistoricalData.model_validate({
            "date": "2026-01-02", "open": 150.0, "high": 153.0,
            "low": 149.0, "close": 152.0,
        })
        assert d.open == 150.0
        assert d.close == 152.0

    def test_adjusted_close_optional(self):
        d = EODHDCryptoHistoricalData.model_validate({
            "date": "2026-01-02", "open": 150.0, "high": 153.0,
            "low": 149.0, "close": 152.0,
        })
        assert d.adjusted_close is None

    def test_adjusted_close_present(self):
        d = EODHDCryptoHistoricalData.model_validate({
            "date": "2026-01-02", "open": 150.0, "high": 153.0,
            "low": 149.0, "close": 152.0, "adjusted_close": 151.5,
        })
        assert d.adjusted_close == 151.5


# ============================================================
# Fetcher
# ============================================================

class TestFetcher:
    def test_transform_query_defaults(self):
        params = {"symbol": "BTC-USD"}
        qp = EODHDCryptoHistoricalFetcher.transform_query(params)
        assert qp.symbol == "BTC-USD"
        assert qp.interval == "1d"
        assert qp.start_date is not None
        assert qp.end_date is not None

    def test_transform_query_preserves_dates(self):
        params = {"symbol": "BTC-USD", "start_date": date(2024, 1, 1), "end_date": date(2024, 1, 31)}
        qp = EODHDCryptoHistoricalFetcher.transform_query(params)
        assert qp.start_date == date(2024, 1, 1)
        assert qp.end_date == date(2024, 1, 31)

    def test_transform_data_single_symbol(self, eod_bar_data):
        qp = EODHDCryptoHistoricalQueryParams(symbol="BTC-USD")
        results = EODHDCryptoHistoricalFetcher.transform_data(qp, eod_bar_data)
        assert len(results) == 2
        for r in results:
            assert isinstance(r, EODHDCryptoHistoricalData)

    def test_transform_data_multi_symbol(self):
        qp = EODHDCryptoHistoricalQueryParams(symbol="BTC-USD,ETH-USD", interval="1d")
        rows = [
            {"date": "2024-01-02", "open": 150.0, "high": 153.0, "low": 149.0,
             "close": 152.0, "volume": 1000000, "adjusted_close": 151.5, "_symbol": "BTC-USD"},
            {"date": "2024-01-02", "open": 300.0, "high": 305.0, "low": 299.0,
             "close": 302.0, "volume": 2000000, "adjusted_close": 301.0, "_symbol": "ETH-USD"},
        ]
        results = EODHDCryptoHistoricalFetcher.transform_data(qp, rows)
        assert len(results) == 2
        assert results[0].symbol == "BTC-USD"
        assert results[1].symbol == "ETH-USD"

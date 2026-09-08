# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Tests for openbb_eodhd.models.corporate_actions."""

from datetime import date


from openbb_eodhd.models.corporate_actions import (
    _qualify,
    EODHDHistoricalDividendsData,
    EODHDHistoricalDividendsFetcher,
    EODHDHistoricalDividendsQueryParams,
    EODHDHistoricalSplitsData,
    EODHDHistoricalSplitsFetcher,
    EODHDHistoricalSplitsQueryParams,
)


# ============================================================
# _qualify
# ============================================================

class TestQualify:
    def test_bare_symbol(self):
        assert _qualify("AAPL", "US") == "AAPL.US"

    def test_custom_exchange(self):
        assert _qualify("AAPL", "LSE") == "AAPL.LSE"

    def test_already_qualified(self):
        assert _qualify("AAPL.US", "US") == "AAPL.US"

    def test_strips_whitespace(self):
        assert _qualify("  AAPL  ", "US") == "AAPL.US"

    def test_upper_cases(self):
        assert _qualify("aapl", "us") == "AAPL.US"

    def test_exchange_upper_cased(self):
        assert _qualify("AAPL", "uk") == "AAPL.UK"


# ============================================================
# Dividends
# ============================================================

class TestDividendsQueryParams:
    def test_default_exchange(self):
        qp = EODHDHistoricalDividendsQueryParams(symbol="AAPL")
        assert qp.exchange == "US"

    def test_custom_exchange(self):
        qp = EODHDHistoricalDividendsQueryParams(symbol="AAPL", exchange="LSE")
        assert qp.exchange == "LSE"


class TestDividendsData:
    def test_creation(self):
        d = EODHDHistoricalDividendsData.model_validate({
            "ex_dividend_date": "2024-01-02",
            "amount": 0.24,
        })
        assert d.amount == 0.24

    def test_optional_fields(self):
        d = EODHDHistoricalDividendsData.model_validate({
            "ex_dividend_date": "2024-01-02",
            "amount": 0.24,
            "declaration_date": "2023-12-15",
            "record_date": "2024-01-05",
            "payment_date": "2024-02-01",
            "frequency": "quarterly",
            "unadjusted_amount": 0.25,
            "currency": "USD",
        })
        assert d.currency == "USD"
        assert d.frequency == "quarterly"


class TestDividendsFetcher:
    def test_transform_query(self):
        params = {"symbol": "AAPL"}
        qp = EODHDHistoricalDividendsFetcher.transform_query(params)
        assert qp.symbol == "AAPL"

    def test_transform_data(self):
        qp = EODHDHistoricalDividendsQueryParams(symbol="AAPL")
        raw = [
            {
                "date": "2024-02-08",
                "value": 0.24,
                "declarationDate": "2024-01-15",
                "recordDate": "2024-02-12",
                "paymentDate": "2024-02-22",
                "period": "quarterly",
                "unadjustedValue": 0.25,
                "currency": "USD",
            },
        ]
        results = EODHDHistoricalDividendsFetcher.transform_data(qp, raw)
        assert len(results) == 1
        r = results[0]
        assert r.ex_dividend_date == date(2024, 2, 8)
        assert r.amount == 0.24
        assert r.declaration_date == date(2024, 1, 15)
        assert r.record_date == date(2024, 2, 12)
        assert r.payment_date == date(2024, 2, 22)
        assert r.frequency == "quarterly"
        assert r.unadjusted_amount == 0.25
        assert r.currency == "USD"

    def test_transform_data_sorted(self):
        qp = EODHDHistoricalDividendsQueryParams(symbol="AAPL")
        raw = [
            {"date": "2024-06-01", "value": 0.25},
            {"date": "2024-02-01", "value": 0.24},
            {"date": "2024-10-01", "value": 0.26},
        ]
        results = EODHDHistoricalDividendsFetcher.transform_data(qp, raw)
        dates = [r.ex_dividend_date for r in results]
        assert dates == [date(2024, 2, 1), date(2024, 6, 1), date(2024, 10, 1)]

    def test_transform_data_missing_date(self):
        qp = EODHDHistoricalDividendsQueryParams(symbol="AAPL")
        raw = [{"value": 0.24, "date": "2024-02-08"}]
        results = EODHDHistoricalDividendsFetcher.transform_data(qp, raw)
        assert len(results) == 1
        assert results[0].ex_dividend_date == date(2024, 2, 8)


# ============================================================
# Splits
# ============================================================

class TestSplitsQueryParams:
    def test_default_exchange(self):
        qp = EODHDHistoricalSplitsQueryParams(symbol="AAPL")
        assert qp.exchange == "US"


class TestSplitsData:
    def test_creation(self):
        d = EODHDHistoricalSplitsData.model_validate({
            "date": "2024-01-02",
        })
        assert d.date is not None

    def test_split_ratio_passthrough(self):
        d = EODHDHistoricalSplitsData.model_validate({
            "date": "2024-01-02", "split_ratio": "4:1",
            "numerator": 4, "denominator": 1,
        })
        assert d.split_ratio == "4:1"
        assert d.numerator == 4
        assert d.denominator == 1


class TestSplitsFetcher:
    def test_transform_query(self):
        params = {"symbol": "AAPL"}
        qp = EODHDHistoricalSplitsFetcher.transform_query(params)
        assert qp.symbol == "AAPL"

    def test_transform_data(self):
        qp = EODHDHistoricalSplitsQueryParams(symbol="AAPL")
        raw = [
            {"date": "2024-08-28", "split": "4.000000/1.000000"},
        ]
        results = EODHDHistoricalSplitsFetcher.transform_data(qp, raw)
        assert len(results) == 1
        r = results[0]
        assert r.date == date(2024, 8, 28)
        assert r.numerator == 4.0
        assert r.denominator == 1.0
        assert r.split_ratio == "4:1"

    def test_transform_data_reverse_split(self):
        qp = EODHDHistoricalSplitsQueryParams(symbol="AAPL")
        raw = [
            {"date": "2024-01-01", "split": "1.000000/10.000000"},
        ]
        results = EODHDHistoricalSplitsFetcher.transform_data(qp, raw)
        assert len(results) == 1
        r = results[0]
        assert r.numerator == 1.0
        assert r.denominator == 10.0
        assert r.split_ratio == "1:10"

    def test_transform_data_invalid_split_skipped(self):
        qp = EODHDHistoricalSplitsQueryParams(symbol="AAPL")
        raw = [
            {"date": "2024-01-01", "split": "invalid"},
        ]
        results = EODHDHistoricalSplitsFetcher.transform_data(qp, raw)
        assert len(results) == 0

    def test_transform_data_sorted(self):
        qp = EODHDHistoricalSplitsQueryParams(symbol="AAPL")
        raw = [
            {"date": "2024-06-01", "split": "4/1"},
            {"date": "2024-02-01", "split": "2/1"},
        ]
        results = EODHDHistoricalSplitsFetcher.transform_data(qp, raw)
        dates = [r.date for r in results]
        assert dates == [date(2024, 2, 1), date(2024, 6, 1)]

    def test_transform_data_missing_date_skipped(self):
        qp = EODHDHistoricalSplitsQueryParams(symbol="AAPL")
        raw = [
            {"date": "2024-01-01", "split": "4/1"},
            {"date": "", "split": "2/1"},  # empty date should be skipped
        ]
        results = EODHDHistoricalSplitsFetcher.transform_data(qp, raw)
        assert len(results) == 1

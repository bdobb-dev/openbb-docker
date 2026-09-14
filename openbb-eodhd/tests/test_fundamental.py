"""Tests for openbb_eodhd.models.fundamental."""

import asyncio
from datetime import date

import pytest

from openbb_eodhd.models.fundamental import (
    _snake,
    _num,
    _transform,
    INCOME_MAP,
    BALANCE_MAP,
    CASHFLOW_MAP,
    EODHDBalanceSheetFetcher,
    EODHDBalanceSheetQueryParams,
    EODHDCashFlowStatementFetcher,
    EODHDCashFlowStatementQueryParams,
    EODHDIncomeStatementFetcher,
    EODHDIncomeStatementQueryParams,
)


# ============================================================
# _snake
# ============================================================

class TestSnake:
    def test_simple_camel(self):
        assert _snake("totalRevenue") == "total_revenue"

    def test_multi_word(self):
        assert _snake("sellingGeneralAdministrative") == "selling_general_administrative"

    def test_already_snake(self):
        assert _snake("already_snake") == "already_snake"

    def test_single_word(self):
        assert _snake("revenue") == "revenue"

    def test_acronyms(self):
        assert _snake("EBIT") == "e_b_i_t"


# ============================================================
# _num
# ============================================================

class TestNum:
    def test_none(self):
        assert _num(None) is None

    def test_empty_string(self):
        assert _num("") is None

    def test_integer_string(self):
        assert _num("1234") == 1234.0

    def test_float_string(self):
        assert _num("1234.56") == 1234.56

    def test_negative(self):
        assert _num("-500") == -500.0

    def test_float_value(self):
        assert _num(1234.56) == 1234.56

    def test_int_value(self):
        assert _num(1234) == 1234.0

    def test_non_numeric_string_passthrough(self):
        assert _num("N/A") == "N/A"

    def test_zero(self):
        assert _num("0") == 0.0

    def test_none_string_not_none(self):
        # literal "None" string is no longer special-cased
        assert _num("None") == "None"


# ============================================================
# _transform
# ============================================================

class TestTransform:
    def test_annual_period(self):
        section_data = {
            "yearly": {
                "2024-12-31": {
                    "date": "2024-12-31",
                    "totalRevenue": "1000000000",
                    "netIncome": "200000000",
                }
            }
        }
        rows = _transform(section_data, "annual", None, INCOME_MAP)
        assert len(rows) == 1
        assert rows[0]["period_ending"] == date(2024, 12, 31)
        assert rows[0]["fiscal_year"] == 2024
        assert rows[0]["fiscal_period"] == "FY"
        assert rows[0]["revenue"] == 1000000000.0
        assert rows[0]["net_income"] == 200000000.0

    def test_quarterly_period(self):
        section_data = {
            "quarterly": {
                "2024-03-31": {
                    "date": "2024-03-31",
                    "totalRevenue": "250000000",
                }
            }
        }
        rows = _transform(section_data, "quarter", None, INCOME_MAP)
        assert len(rows) == 1
        assert rows[0]["fiscal_period"] == "Q1"

    def test_limit(self):
        section_data = {
            "yearly": {
                "2024-12-31": {"date": "2024-12-31", "totalRevenue": "1000000000"},
                "2023-12-31": {"date": "2023-12-31", "totalRevenue": "900000000"},
                "2022-12-31": {"date": "2022-12-31", "totalRevenue": "800000000"},
            }
        }
        rows = _transform(section_data, "annual", 2, INCOME_MAP)
        assert len(rows) == 2

    def test_empty_section_raises(self):
        with pytest.raises(Exception, match="The request was returned empty"):
            _transform({}, "annual", None, INCOME_MAP)

    def test_no_matching_bucket_raises(self):
        section_data = {"yearly": {}}
        with pytest.raises(Exception, match="The request was returned empty"):
            _transform(section_data, "annual", None, INCOME_MAP)

    def test_meta_keys_excluded(self):
        section_data = {
            "yearly": {
                "2024-12-31": {
                    "date": "2024-12-31",
                    "filing_date": "2025-02-01",
                    "currency_symbol": "USD",
                    "totalRevenue": "1000000000",
                }
            }
        }
        rows = _transform(section_data, "annual", None, INCOME_MAP)
        # date, filing_date, currency_symbol should not appear in output
        # (date only excluded from passthrough, but period_ending is set explicitly)
        row = rows[0]
        assert "currency_symbol" not in row
        assert "filing_date" not in row

    def test_unmapped_field_snake_cased(self):
        section_data = {
            "yearly": {
                "2024-12-31": {
                    "date": "2024-12-31",
                    "extraField": "123",
                }
            }
        }
        rows = _transform(section_data, "annual", None, {})
        assert rows[0]["extra_field"] == 123.0


# ============================================================
# Field maps
# ============================================================

class TestFieldMaps:
    def test_income_map_has_expected_keys(self):
        assert "totalRevenue" in INCOME_MAP
        assert "netIncome" in INCOME_MAP
        assert "ebitda" in INCOME_MAP

    def test_balance_map_has_expected_keys(self):
        assert "totalAssets" in BALANCE_MAP
        assert "totalLiab" in BALANCE_MAP
        assert "retainedEarnings" in BALANCE_MAP

    def test_cashflow_map_has_expected_keys(self):
        assert "freeCashFlow" in CASHFLOW_MAP
        assert "capitalExpenditures" in CASHFLOW_MAP
        assert "netIncome" in CASHFLOW_MAP


# ============================================================
# QueryParams
# ============================================================

class TestQueryParams:
    def test_income_default_period(self):
        qp = EODHDIncomeStatementQueryParams(symbol="AAPL")
        assert qp.period == "annual"

    def test_income_default_exchange(self):
        qp = EODHDIncomeStatementQueryParams(symbol="AAPL")
        assert qp.exchange == "US"

    def test_balance_default_period(self):
        qp = EODHDBalanceSheetQueryParams(symbol="AAPL")
        assert qp.period == "annual"

    def test_cashflow_default_period(self):
        qp = EODHDCashFlowStatementQueryParams(symbol="AAPL")
        assert qp.period == "annual"

    def test_period_choices_exposed_to_core(self):
        # OpenBB core reads __json_schema_extra__ (not model_config) for choices.
        for cls in [EODHDIncomeStatementQueryParams, EODHDBalanceSheetQueryParams,
                     EODHDCashFlowStatementQueryParams]:
            assert cls.__json_schema_extra__["period"]["choices"] == ["annual", "quarter"]


# ============================================================
# Fetchers
# ============================================================

class TestIncomeStatementFetcher:
    def test_transform_query(self):
        params = {"symbol": "AAPL"}
        qp = EODHDIncomeStatementFetcher.transform_query(params)
        assert qp.symbol == "AAPL"

    def test_transform_data(self):
        qp = EODHDIncomeStatementQueryParams(symbol="AAPL")
        rows = [
            {
                "period_ending": date(2024, 12, 31),
                "fiscal_year": 2024,
                "fiscal_period": "FY",
                "revenue": 1000000000.0,
                "net_income": 200000000.0,
            }
        ]
        results = EODHDIncomeStatementFetcher.transform_data(qp, rows)
        assert len(results) == 1
        assert results[0].revenue == 1000000000.0


class TestBalanceSheetFetcher:
    def test_transform_query(self):
        params = {"symbol": "AAPL"}
        qp = EODHDBalanceSheetFetcher.transform_query(params)
        assert qp.symbol == "AAPL"

    def test_transform_data(self):
        qp = EODHDBalanceSheetQueryParams(symbol="AAPL")
        rows = [
            {
                "period_ending": date(2024, 12, 31),
                "fiscal_year": 2024,
                "fiscal_period": "FY",
                "total_assets": 5000000000.0,
                "total_liabilities": 2000000000.0,
            }
        ]
        results = EODHDBalanceSheetFetcher.transform_data(qp, rows)
        assert len(results) == 1
        assert results[0].total_assets == 5000000000.0


class TestCashFlowFetcher:
    def test_transform_query(self):
        params = {"symbol": "AAPL"}
        qp = EODHDCashFlowStatementFetcher.transform_query(params)
        assert qp.symbol == "AAPL"

    def test_transform_data(self):
        qp = EODHDCashFlowStatementQueryParams(symbol="AAPL")
        rows = [
            {
                "period_ending": date(2024, 12, 31),
                "fiscal_year": 2024,
                "fiscal_period": "FY",
                "free_cash_flow": 500000000.0,
            }
        ]
        results = EODHDCashFlowStatementFetcher.transform_data(qp, rows)
        assert len(results) == 1
        assert results[0].free_cash_flow == 500000000.0


# ============================================================
# Bundle coalescing (Task 2)
# ============================================================

def test_three_statements_one_symbol_share_one_fetch(monkeypatch):
    """Income+Balance+CashFlow for one symbol -> ONE /fundamentals fetch."""
    from openbb_eodhd.models import _fundamentals as F

    F._reset_cache_for_tests()
    calls = {"n": 0}
    bundle = {"Financials": {
        "Income_Statement": {"yearly": {"2025-09-30": {"date": "2025-09-30", "totalRevenue": 1}}, "quarterly": {}},
        "Balance_Sheet": {"yearly": {"2025-09-30": {"date": "2025-09-30", "totalAssets": 2}}, "quarterly": {}},
        "Cash_Flow": {"yearly": {"2025-09-30": {"date": "2025-09-30", "netIncome": 3}}, "quarterly": {}},
    }}
    monkeypatch.setattr(F, "_fetch_sync", lambda s, c: (calls.__setitem__("n", calls["n"] + 1) or bundle))
    monkeypatch.setattr(F, "_l2_get", lambda sym: None)   # isolate L1 + fetch from real ArcticDB
    monkeypatch.setattr(F, "_l2_put", lambda sym, b: None)
    creds = {"eodhd_api_key": "k"}

    async def go():
        for Fetcher, QP in [
            (EODHDIncomeStatementFetcher, EODHDIncomeStatementQueryParams),
            (EODHDBalanceSheetFetcher, EODHDBalanceSheetQueryParams),
            (EODHDCashFlowStatementFetcher, EODHDCashFlowStatementQueryParams),
        ]:
            q = QP(symbol="AAPL")
            data = await Fetcher.aextract_data(q, creds)
            Fetcher.transform_data(q, data)

    asyncio.run(go())
    assert calls["n"] == 1

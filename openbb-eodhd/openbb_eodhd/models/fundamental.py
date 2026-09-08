# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""EODHD Fundamental (financial statement) models.

Maps EODHD's `/api/fundamentals` payload onto OpenBB's three standard statement
models: income statement, balance sheet, and cash-flow statement. Well-known
line items are mapped to OpenBB's conventional field names; every other EODHD
field is passed through snake_cased (the standard models allow extra fields).
"""

import re
from typing import Any, Literal

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.balance_sheet import (
    BalanceSheetData,
    BalanceSheetQueryParams,
)
from openbb_core.provider.standard_models.cash_flow import (
    CashFlowStatementData,
    CashFlowStatementQueryParams,
)
from openbb_core.provider.standard_models.income_statement import (
    IncomeStatementData,
    IncomeStatementQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError
from pydantic import Field

# EODHD field -> OpenBB conventional field name. Unmapped fields pass through
# snake_cased, so these maps only need the commonly-consumed line items.
INCOME_MAP = {
    "totalRevenue": "revenue",
    "costOfRevenue": "cost_of_revenue",
    "grossProfit": "gross_profit",
    "researchDevelopment": "research_and_development_expense",
    "sellingGeneralAdministrative": "selling_general_and_administrative_expense",
    "totalOperatingExpenses": "total_operating_expenses",
    "operatingIncome": "operating_income",
    "ebit": "ebit",
    "ebitda": "ebitda",
    "depreciationAndAmortization": "depreciation_and_amortization",
    "interestExpense": "interest_expense",
    "interestIncome": "interest_income",
    "incomeBeforeTax": "income_before_tax",
    "incomeTaxExpense": "income_tax_expense",
    "netIncome": "net_income",
    "netIncomeApplicableToCommonShares": "net_income_to_common",
}
BALANCE_MAP = {
    "totalAssets": "total_assets",
    "totalCurrentAssets": "total_current_assets",
    "cashAndEquivalents": "cash_and_cash_equivalents",
    "cashAndShortTermInvestments": "cash_and_short_term_investments",
    "netReceivables": "net_receivables",
    "inventory": "inventory",
    "propertyPlantAndEquipmentNet": "property_plant_equipment_net",
    "goodWill": "goodwill",
    "intangibleAssets": "intangible_assets",
    "totalLiab": "total_liabilities",
    "totalCurrentLiabilities": "total_current_liabilities",
    "accountsPayable": "accounts_payable",
    "shortTermDebt": "short_term_debt",
    "longTermDebt": "long_term_debt",
    "shortLongTermDebtTotal": "total_debt",
    "netDebt": "net_debt",
    "totalStockholderEquity": "total_shareholders_equity",
    "retainedEarnings": "retained_earnings",
    "commonStock": "common_stock",
    "commonStockSharesOutstanding": "common_shares_outstanding",
}
CASHFLOW_MAP = {
    "totalCashFromOperatingActivities": "net_cash_from_operating_activities",
    "totalCashflowsFromInvestingActivities": "net_cash_from_investing_activities",
    "totalCashFromFinancingActivities": "net_cash_from_financing_activities",
    "capitalExpenditures": "capital_expenditure",
    "freeCashFlow": "free_cash_flow",
    "depreciation": "depreciation_and_amortization",
    "stockBasedCompensation": "stock_based_compensation",
    "dividendsPaid": "dividends_paid",
    "netIncome": "net_income",
    "changeInCash": "net_change_in_cash",
    "beginPeriodCashFlow": "cash_at_beginning_of_period",
    "endPeriodCashFlow": "cash_at_end_of_period",
}

# Keys handled explicitly (date/meta) — excluded from numeric passthrough.
_META_KEYS = {"date", "filing_date", "currency_symbol"}
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def _snake(name: str) -> str:
    """camelCase -> snake_case."""
    return _CAMEL.sub("_", name).lower()


def _num(value: Any) -> Any:
    """Coerce EODHD's (often string) numerics to float, or None."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return value  # non-numeric passthrough (kept as-is)


def _transform(section_data: dict, period: str, limit: int | None, field_map: dict) -> list[dict]:
    """Turn one EODHD statement section into standard-model row dicts."""
    # pylint: disable=import-outside-toplevel
    from pandas import isna, to_datetime

    bucket = section_data.get("yearly" if period == "annual" else "quarterly") or {}
    # EODHD keys the entries by period-ending date; newest first.
    entries = sorted(bucket.values(), key=lambda e: e.get("date", ""), reverse=True)
    if limit:
        entries = entries[:limit]

    rows: list[dict] = []
    for entry in entries:
        end_ts = to_datetime(entry.get("date"), errors="coerce")
        # Skip entries whose period-ending date is missing/unparseable.
        if isna(end_ts):
            continue
        end = end_ts.date()
        quarter = (end.month - 1) // 3 + 1
        row: dict[str, Any] = {
            "period_ending": end,
            "fiscal_year": end.year,
            "fiscal_period": "FY" if period == "annual" else f"Q{quarter}",
        }
        for key, value in entry.items():
            if key in _META_KEYS:
                continue
            row[field_map.get(key, _snake(key))] = _num(value)
        rows.append(row)
    if not rows:
        raise EmptyDataError("The request was returned empty.")
    return rows


async def _extract(section: str, field_map: dict, query, credentials) -> list[dict]:
    """Shared extract: read the section from the shared /fundamentals bundle, then map to standard rows."""
    # pylint: disable=import-outside-toplevel
    from openbb_eodhd.models._fundamentals import get_bundle

    bundle = await get_bundle(query.symbol, query.exchange, credentials)
    financials = bundle.get("Financials") or {}
    data = financials.get(section) or {}
    return _transform(data, query.period, query.limit, field_map)


# --- Query params: add period + exchange to each standard statement query ------
class EODHDIncomeStatementQueryParams(IncomeStatementQueryParams):
    """EODHD Income Statement Query."""

    # Core reads this dunder directly for choices; keep it (not model_config).
    __json_schema_extra__ = {"period": {"choices": ["annual", "quarter"]}}
    period: Literal["annual", "quarter"] = Field(default="annual", description="Reporting period.")
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols (e.g. 'US').")


class EODHDBalanceSheetQueryParams(BalanceSheetQueryParams):
    """EODHD Balance Sheet Query."""

    # Core reads this dunder directly for choices; keep it (not model_config).
    __json_schema_extra__ = {"period": {"choices": ["annual", "quarter"]}}
    period: Literal["annual", "quarter"] = Field(default="annual", description="Reporting period.")
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols (e.g. 'US').")


class EODHDCashFlowStatementQueryParams(CashFlowStatementQueryParams):
    """EODHD Cash Flow Statement Query."""

    # Core reads this dunder directly for choices; keep it (not model_config).
    __json_schema_extra__ = {"period": {"choices": ["annual", "quarter"]}}
    period: Literal["annual", "quarter"] = Field(default="annual", description="Reporting period.")
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols (e.g. 'US').")


# --- Fetchers ------------------------------------------------------------------
class EODHDIncomeStatementFetcher(
    Fetcher[EODHDIncomeStatementQueryParams, list[IncomeStatementData]]
):
    """EODHD income statement."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDIncomeStatementQueryParams:  # pylint: disable=unused-argument
        return EODHDIncomeStatementQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        return await _extract("Income_Statement", INCOME_MAP, query, credentials)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[IncomeStatementData]:  # pylint: disable=unused-argument
        return [IncomeStatementData.model_validate(r) for r in data]


class EODHDBalanceSheetFetcher(
    Fetcher[EODHDBalanceSheetQueryParams, list[BalanceSheetData]]
):
    """EODHD balance sheet."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDBalanceSheetQueryParams:  # pylint: disable=unused-argument
        return EODHDBalanceSheetQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        return await _extract("Balance_Sheet", BALANCE_MAP, query, credentials)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[BalanceSheetData]:  # pylint: disable=unused-argument
        return [BalanceSheetData.model_validate(r) for r in data]


class EODHDCashFlowStatementFetcher(
    Fetcher[EODHDCashFlowStatementQueryParams, list[CashFlowStatementData]]
):
    """EODHD cash-flow statement."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDCashFlowStatementQueryParams:  # pylint: disable=unused-argument
        return EODHDCashFlowStatementQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        return await _extract("Cash_Flow", CASHFLOW_MAP, query, credentials)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[CashFlowStatementData]:  # pylint: disable=unused-argument
        return [CashFlowStatementData.model_validate(r) for r in data]

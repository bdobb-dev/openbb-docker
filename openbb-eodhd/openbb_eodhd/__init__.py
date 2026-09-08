# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""EODHD provider module for OpenBB."""

from openbb_core.provider.abstract.provider import Provider

from openbb_eodhd.models.corporate_actions import (
    EODHDHistoricalDividendsFetcher,
    EODHDHistoricalSplitsFetcher,
)
from openbb_eodhd.models.crypto_historical import EODHDCryptoHistoricalFetcher
from openbb_eodhd.models.currency_historical import EODHDCurrencyHistoricalFetcher
from openbb_eodhd.models.equity_historical import EODHDEquityHistoricalFetcher
from openbb_eodhd.models.estimates import (
    EODHDAnalystEstimatesFetcher,
    EODHDForwardEpsEstimatesFetcher,
    EODHDHistoricalEpsFetcher,
    EODHDPriceTargetConsensusFetcher,
)
from openbb_eodhd.models.fundamental import (
    EODHDBalanceSheetFetcher,
    EODHDCashFlowStatementFetcher,
    EODHDIncomeStatementFetcher,
)
from openbb_eodhd.models.insider import EODHDInsiderTradingFetcher
from openbb_eodhd.models.ownership import (
    EODHDEquityOwnershipFetcher,
    EODHDInstitutionalOwnershipFetcher,
)

__all__ = [
    "EODHDEquityHistoricalFetcher",
    "EODHDCryptoHistoricalFetcher",
    "EODHDCurrencyHistoricalFetcher",
    "EODHDIncomeStatementFetcher",
    "EODHDBalanceSheetFetcher",
    "EODHDCashFlowStatementFetcher",
    "EODHDHistoricalDividendsFetcher",
    "EODHDHistoricalSplitsFetcher",
    "EODHDInstitutionalOwnershipFetcher",
    "EODHDEquityOwnershipFetcher",
    "EODHDInsiderTradingFetcher",
    "EODHDHistoricalEpsFetcher",
    "EODHDAnalystEstimatesFetcher",
    "EODHDForwardEpsEstimatesFetcher",
    "EODHDPriceTargetConsensusFetcher",
    "eodhd_provider",
]

eodhd_provider = Provider(
    name="eodhd",
    website="https://eodhd.com",
    description=(
        "EOD Historical Data (EODHD) APIs. Historical pricing for equities/ETFs, "
        "crypto, and forex (end-of-day + intraday); fundamentals (income, balance "
        "sheet, cash flow); corporate actions (dividends, splits); ownership "
        "(institutional and fund holders) and insider transactions; and analyst "
        "estimates (historical EPS, forward estimates, price-target consensus)."
    ),
    # Becomes the credential field `eodhd_api_key` (env: EODHD_API_KEY).
    credentials=["api_key"],
    fetcher_dict={
        "EquityHistorical": EODHDEquityHistoricalFetcher,
        "EtfHistorical": EODHDEquityHistoricalFetcher,
        "CryptoHistorical": EODHDCryptoHistoricalFetcher,
        "CurrencyHistorical": EODHDCurrencyHistoricalFetcher,
        "IncomeStatement": EODHDIncomeStatementFetcher,
        "BalanceSheet": EODHDBalanceSheetFetcher,
        "CashFlowStatement": EODHDCashFlowStatementFetcher,
        "HistoricalDividends": EODHDHistoricalDividendsFetcher,
        "HistoricalSplits": EODHDHistoricalSplitsFetcher,
        "InstitutionalOwnership": EODHDInstitutionalOwnershipFetcher,
        "EquityOwnership": EODHDEquityOwnershipFetcher,
        "InsiderTrading": EODHDInsiderTradingFetcher,
        "HistoricalEps": EODHDHistoricalEpsFetcher,
        "AnalystEstimates": EODHDAnalystEstimatesFetcher,
        "ForwardEpsEstimates": EODHDForwardEpsEstimatesFetcher,
        "PriceTargetConsensus": EODHDPriceTargetConsensusFetcher,
    },
    repr_name="EOD Historical Data (EODHD)",
)

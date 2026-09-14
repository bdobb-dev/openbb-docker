from openbb_eodhd import eodhd_provider


def test_phase1_models_registered():
    for key in [
        "InstitutionalOwnership", "EquityOwnership", "InsiderTrading",
        "HistoricalEps", "AnalystEstimates", "ForwardEpsEstimates",
        "PriceTargetConsensus",
    ]:
        assert key in eodhd_provider.fetcher_dict, f"missing {key}"


def test_phase3_calendar_models_registered():
    for key in [
        "CalendarEarnings", "CalendarIpo", "CalendarSplits", "EconomicCalendar",
    ]:
        assert key in eodhd_provider.fetcher_dict, f"missing {key}"


def test_phase3_market_models_registered():
    for key in [
        "EquitySearch", "EtfSearch", "CryptoSearch", "EquityScreener",
        "CurrencyPairs", "CurrencySnapshots", "AvailableIndices",
        "IndexHistorical", "HistoricalMarketCap", "WorldNews",
        "TreasuryRates", "YieldCurve", "GovernmentTrades",
        "EtfInfo", "EtfHoldings", "EtfSectors", "EtfCountries",
        "TrailingDividendYield",
    ]:
        assert key in eodhd_provider.fetcher_dict, f"missing {key}"


def test_phase2_company_models_registered():
    for key in [
        "EquityInfo", "EquityQuote", "KeyMetrics", "FinancialRatios",
        "ShareStatistics", "KeyExecutives", "CompanyNews", "EsgScore",
    ]:
        assert key in eodhd_provider.fetcher_dict, f"missing {key}"


def test_phase4_macro_models_registered():
    for key in [
        "EconomicIndicators", "GdpReal", "GdpNominal",
        "ConsumerPriceIndex", "Unemployment", "CountryProfile",
    ]:
        assert key in eodhd_provider.fetcher_dict, f"missing {key}"

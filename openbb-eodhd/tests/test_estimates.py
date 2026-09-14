from openbb_eodhd.models.estimates import EODHDAnalystEstimatesFetcher as AeFetcher
from openbb_eodhd.models.estimates import EODHDAnalystEstimatesQueryParams as AeQP
from openbb_eodhd.models.estimates import EODHDForwardEpsEstimatesFetcher as FeFetcher
from openbb_eodhd.models.estimates import EODHDForwardEpsEstimatesQueryParams as FeQP
from openbb_eodhd.models.estimates import EODHDHistoricalEpsFetcher as EpsFetcher
from openbb_eodhd.models.estimates import EODHDHistoricalEpsQueryParams as EpsQP
from openbb_eodhd.models.estimates import EODHDPriceTargetConsensusFetcher as PtcFetcher
from openbb_eodhd.models.estimates import EODHDPriceTargetConsensusQueryParams as PtcQP

BUNDLE = {"Earnings": {"History": {
    "0": {"reportDate": "2026-10-29", "date": "2026-09-30", "epsActual": None, "epsEstimate": 1.98},
    "1": {"reportDate": "2026-07-31", "date": "2026-06-30", "epsActual": 1.57, "epsEstimate": 1.43},
}, "Trend": {
    "0": {"date": "2027-09-30", "period": "+1y", "earningsEstimateAvg": "9.53",
          "earningsEstimateLow": "8.24", "earningsEstimateHigh": "10.67",
          "earningsEstimateNumberOfAnalysts": "39.0",
          "revenueEstimateAvg": "525003468150.00", "revenueEstimateLow": "483496000000.00",
          "revenueEstimateHigh": "594863000000.00", "revenueEstimateNumberOfAnalysts": "39.0"},
}}}

def test_historical_eps_maps_actual_and_estimate():
    q = EpsQP(symbol="AAPL")
    rows = EpsFetcher.transform_data(q, BUNDLE)
    assert len(rows) == 2
    r = [x for x in rows if str(x.date) == "2026-06-30"][0]
    assert r.symbol == "AAPL"
    assert r.eps_actual == 1.57
    assert r.eps_estimated == 1.43

def test_analyst_estimates_maps_trend():
    q = AeQP(symbol="AAPL")
    rows = AeFetcher.transform_data(q, BUNDLE)
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "AAPL"
    assert str(r.date) == "2027-09-30"
    assert r.estimated_eps_avg == 9.53
    assert r.estimated_eps_low == 8.24
    assert r.estimated_eps_high == 10.67
    assert r.estimated_revenue_avg == 525003468150.0
    assert r.number_analysts_estimated_eps == 39

def test_forward_eps_maps_trend():
    q = FeQP(symbol="AAPL")
    rows = FeFetcher.transform_data(q, BUNDLE)
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "AAPL"
    assert str(r.date) == "2027-09-30"
    assert r.fiscal_period == "+1y"
    assert r.mean == 9.53
    assert r.low_estimate == 8.24
    assert r.high_estimate == 10.67
    assert r.number_of_analysts == 39

BUNDLE_RATINGS = {
    "General": {"Name": "Apple Inc."},
    "AnalystRatings": {"Rating": 4.04, "TargetPrice": 324.45,
                       "StrongBuy": 23, "Buy": 7, "Hold": 16, "Sell": 1, "StrongSell": 1},
}

def test_price_target_consensus():
    q = PtcQP(symbol="AAPL")
    rows = PtcFetcher.transform_data(q, BUNDLE_RATINGS)
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "AAPL"
    assert r.name == "Apple Inc."
    assert r.target_consensus == 324.45

def test_price_target_consensus_empty_when_no_target():
    q = PtcQP(symbol="AAPL")
    assert PtcFetcher.transform_data(q, {"AnalystRatings": {"TargetPrice": 0}}) == []

def test_forward_eps_labels_default_symbol_when_omitted():
    q = FeQP()
    rows = FeFetcher.transform_data(q, BUNDLE)
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"

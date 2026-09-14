# tests/test_ownership.py
from openbb_eodhd.models.ownership import EODHDInstitutionalOwnershipFetcher as Fetcher
from openbb_eodhd.models.ownership import EODHDInstitutionalOwnershipQueryParams as QP
from openbb_eodhd.models.ownership import EODHDEquityOwnershipFetcher as OFetcher
from openbb_eodhd.models.ownership import EODHDEquityOwnershipQueryParams as OQP

BUNDLE = {"Holders": {"Institutions": {
    "0": {"name": "BlackRock Inc", "date": "2026-03-31", "totalShares": 7.83,
          "totalAssets": 5.07, "currentShares": 2000000000, "change": -9970306, "change_p": -0.86},
    "1": {"name": "Vanguard Group Inc", "date": "2026-03-31", "totalShares": 8.9,
          "totalAssets": 6.1, "currentShares": 1300000000, "change": 100, "change_p": 0.01},
}}}

def test_maps_institutional_holders():
    q = QP(symbol="AAPL")
    rows = Fetcher.transform_data(q, BUNDLE)
    assert len(rows) == 2
    top = rows[0]
    assert top.symbol == "AAPL"
    assert str(top.date) == "2026-03-31"
    assert top.name == "BlackRock Inc"
    assert top.current_shares == 2000000000

def test_empty_holders_returns_empty_list():
    q = QP(symbol="AAPL")
    assert Fetcher.transform_data(q, {"Holders": {"Institutions": {}}}) == []

def test_equity_ownership_uses_investor_name_and_filing_date():
    q = OQP(symbol="AAPL")
    rows = OFetcher.transform_data(q, BUNDLE)
    assert len(rows) == 2
    r = rows[0]
    assert r.investor_name == "BlackRock Inc"
    assert r.symbol == "AAPL"
    assert str(r.date) == "2026-03-31"
    assert str(r.filing_date) == "2026-03-31"  # EODHD has no separate filing date; mirror `date`

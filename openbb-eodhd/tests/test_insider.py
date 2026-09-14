from openbb_eodhd.models.insider import EODHDInsiderTradingFetcher as Fetcher
from openbb_eodhd.models.insider import EODHDInsiderTradingQueryParams as QP

RAW = [
    {"date": "2026-08-01", "code": "AAPL", "ownerName": "COOK TIMOTHY D",
     "ownerRelationship": "CEO", "transactionDate": "2026-07-30", "transactionCode": "S",
     "transactionAmount": 50000, "transactionPrice": 315.0,
     "transactionAcquiredDisposed": "D", "postTransactionAmount": 3200000,
     "secLink": "https://www.sec.gov/x"},
]

def test_maps_insider_rows():
    q = QP(symbol="AAPL")
    rows = Fetcher.transform_data(q, RAW)
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "AAPL"
    assert r.owner_name == "COOK TIMOTHY D"
    assert r.owner_title == "CEO"
    assert str(r.transaction_date) == "2026-07-30"
    assert r.transaction_type == "S"
    assert r.securities_transacted == 50000
    assert r.transaction_price == 315.0
    assert r.acquisition_or_disposition == "D"
    assert r.filing_url == "https://www.sec.gov/x"

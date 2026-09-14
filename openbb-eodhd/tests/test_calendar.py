"""Tests for openbb_eodhd.models.calendar.

Sample rows are trimmed live responses recorded 2026-09-01 against the
account key (calendar endpoints are core-API, no marketplace add-on).
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from openbb_core.provider.utils.errors import EmptyDataError, UnauthorizedError

from openbb_eodhd.models.calendar import (
    EODHDCalendarEarningsFetcher,
    EODHDCalendarEarningsQueryParams,
    EODHDCalendarIpoFetcher,
    EODHDCalendarIpoQueryParams,
    EODHDCalendarSplitsFetcher,
    EODHDCalendarSplitsQueryParams,
    EODHDEconomicCalendarFetcher,
    EODHDEconomicCalendarQueryParams,
    _unwrap,
)

from tests.conftest import run_async


EARNINGS_RESP = {
    "type": "Earnings",
    "from": "2026-09-01",
    "to": "2026-09-03",
    "earnings": [
        {"code": "SLHN.SW", "report_date": "2026-09-01", "date": "2026-06-30",
         "before_after_market": "AfterMarket", "currency": None,
         "actual": None, "estimate": None, "difference": 0, "percent": None},
        {"code": "AAPL.US", "report_date": "2026-09-02", "date": "2026-06-30",
         "before_after_market": "BeforeMarket", "currency": "USD",
         "actual": 1.99, "estimate": 1.98, "difference": 0.01, "percent": 0.51},
        {"code": "", "report_date": "2026-09-02"},          # dropped: no symbol
        {"code": "X.US", "report_date": None},              # dropped: no date
    ],
}

IPOS_RESP = {
    "type": "IPOs",
    "ipos": [
        {"code": "SGLD.US", "name": "Scorpio Gold Corporation", "exchange": "NASDAQ",
         "currency": None, "start_date": "2026-09-01", "filing_date": "2026-09-01",
         "amended_date": "2026-09-01", "price_from": 0, "price_to": 0,
         "offer_price": 0, "shares": 0, "deal_type": "Expected"},
        {"code": "PTT.US", "name": "Ptt PCL", "exchange": "NASDAQ",
         "currency": None, "start_date": "2026-09-08", "filing_date": "2026-09-08",
         "amended_date": "2026-09-08", "price_from": 10.0, "price_to": 12.0,
         "offer_price": 0, "shares": 6112327, "deal_type": "Expected"},
    ],
}

SPLITS_RESP = {
    "type": "Splits",
    "splits": [
        {"code": "011370.KQ", "split_date": "2026-09-01", "optionable": "N",
         "old_shares": 5, "new_shares": 1},
        {"code": "3135.TW", "split_date": "2026-09-01", "optionable": "N",
         "old_shares": 1000, "new_shares": 1047},
        {"code": "BAD.US", "split_date": "2026-09-01", "optionable": "N",
         "old_shares": 0, "new_shares": 4},                 # dropped: zero shares
    ],
}

ECON_RESP = [
    {"type": "Import Prices", "comparison": "qoq", "period": "Q2", "country": "NZ",
     "date": "2026-09-02 22:45:00", "actual": None, "previous": -0.7,
     "estimate": 1.1, "change": None, "change_percentage": None},
    {"type": "Foreign Bond Investment", "comparison": None, "period": "Aug/29",
     "country": "JP", "date": "2026-09-02 23:50:00", "actual": None,
     "previous": -1978.4, "estimate": None, "change": None, "change_percentage": None},
]

CREDS = {"eodhd_api_key": "test_key_123"}


def _client(method: str, response):
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    getattr(client, method).return_value = response
    return client


# ============================================================
# _unwrap
# ============================================================

class TestUnwrap:
    def test_wrapper_dict(self):
        assert _unwrap({"earnings": [{"a": 1}]}, "earnings", "x") == [{"a": 1}]

    def test_bare_list(self):
        assert _unwrap([{"a": 1}], "earnings", "x") == [{"a": 1}]

    def test_error_dict(self):
        with pytest.raises(UnauthorizedError):
            _unwrap({"message": "Invalid API key"}, "earnings", "x")

    def test_empty(self):
        with pytest.raises(EmptyDataError):
            _unwrap({"earnings": []}, "earnings", "x")


# ============================================================
# Earnings
# ============================================================

class TestCalendarEarnings:
    def test_transform(self):
        query = EODHDCalendarEarningsQueryParams()
        rows = EODHDCalendarEarningsFetcher.transform_data(
            query, EARNINGS_RESP["earnings"]
        )
        assert [r.symbol for r in rows] == ["SLHN.SW", "AAPL.US"]
        aapl = rows[1]
        assert aapl.report_date == date(2026, 9, 2)
        assert aapl.eps_consensus == 1.98
        assert aapl.eps_actual == 1.99
        assert aapl.period_ending == date(2026, 6, 30)
        assert aapl.announce_time == "BeforeMarket"
        assert aapl.surprise == 0.01

    def test_extract_passes_window(self):
        query = EODHDCalendarEarningsQueryParams(
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 3)
        )
        client = _client("get_upcoming_earnings_data", EARNINGS_RESP)
        with patch("openbb_eodhd.models.calendar.get_client", return_value=client):
            rows = run_async(
                EODHDCalendarEarningsFetcher.aextract_data, query, CREDS
            )
        assert len(rows) == 4
        client.get_upcoming_earnings_data.assert_called_once_with(
            from_date="2026-09-01", to_date="2026-09-03"
        )


# ============================================================
# IPOs
# ============================================================

class TestCalendarIpo:
    def test_transform(self):
        query = EODHDCalendarIpoQueryParams()
        rows = EODHDCalendarIpoFetcher.transform_data(query, IPOS_RESP["ipos"])
        assert len(rows) == 2
        ptt = rows[1]
        assert ptt.symbol == "PTT.US"
        assert ptt.ipo_date == date(2026, 9, 8)
        assert ptt.price_from == 10.0
        assert ptt.offer_price is None  # zero placeholder → None
        assert ptt.shares == 6112327

    def test_symbol_filter_and_limit(self):
        query = EODHDCalendarIpoQueryParams(symbol="ptt")
        rows = EODHDCalendarIpoFetcher.transform_data(query, IPOS_RESP["ipos"])
        assert [r.symbol for r in rows] == ["PTT.US"]
        query = EODHDCalendarIpoQueryParams(limit=1)
        rows = EODHDCalendarIpoFetcher.transform_data(query, IPOS_RESP["ipos"])
        assert len(rows) == 1


# ============================================================
# Splits
# ============================================================

class TestCalendarSplits:
    def test_transform(self):
        query = EODHDCalendarSplitsQueryParams()
        rows = EODHDCalendarSplitsFetcher.transform_data(
            query, SPLITS_RESP["splits"]
        )
        assert [r.symbol for r in rows] == ["011370.KQ", "3135.TW"]
        # 5 old → 1 new consolidation: numerator=new, denominator=old
        assert rows[0].numerator == 1.0
        assert rows[0].denominator == 5.0
        assert rows[0].date == date(2026, 9, 1)
        assert rows[0].optionable == "N"


# ============================================================
# Economic calendar
# ============================================================

class TestEconomicCalendar:
    def test_transform(self):
        query = EODHDEconomicCalendarQueryParams()
        rows = EODHDEconomicCalendarFetcher.transform_data(query, ECON_RESP)
        nz = rows[0]
        assert nz.event == "Import Prices"
        assert nz.country == "NZ"
        assert nz.consensus == 1.1
        assert nz.previous == -0.7
        assert nz.comparison == "qoq"
        assert nz.date.year == 2026 and nz.date.hour == 22

    def test_extract_passes_filters(self):
        query = EODHDEconomicCalendarQueryParams(country="US", comparison="yoy")
        client = _client("get_economic_events_data", ECON_RESP)
        with patch("openbb_eodhd.models.calendar.get_client", return_value=client):
            rows = run_async(
                EODHDEconomicCalendarFetcher.aextract_data, query, CREDS
            )
        assert len(rows) == 2
        client.get_economic_events_data.assert_called_once_with(
            date_from=None, date_to=None, country="US", comparison="yoy", limit=1000
        )

    def test_error_dict_raises(self):
        query = EODHDEconomicCalendarQueryParams()
        client = _client("get_economic_events_data", {"message": "denied"})
        with patch("openbb_eodhd.models.calendar.get_client", return_value=client):
            with pytest.raises(UnauthorizedError):
                run_async(EODHDEconomicCalendarFetcher.aextract_data, query, CREDS)

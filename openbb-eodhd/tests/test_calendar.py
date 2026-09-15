# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

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
    {"type": "Made Up Event", "comparison": None, "period": None,
     "country": "XX", "date": "2026-09-02 23:55:00", "actual": None,
     "previous": None, "estimate": None, "change": None, "change_percentage": None},
]

CREDS = {"eodhd_api_key": "test_key_123"}


def _client(method: str, response):
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    getattr(client, method).return_value = response
    return client


def _client_series(method: str, responses):
    """Fake SDK client whose `method` answers one scripted page per call.

    `responses` is a list (one page per call) or a callable(**kwargs)->page,
    for the window-walk tests where a single `aextract_data` call makes
    several SDK requests.
    """
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    getattr(client, method).side_effect = responses
    return client


def _econ_page(day_counts: list[tuple]) -> list[dict]:
    """A synthetic EODHD /economic-events page: `[(date, row_count), ...]`.

    `day_counts` must already be newest-day-first, matching how EODHD orders
    a real page, since the window-walk reads the boundary date off the
    page's first/last rows.
    """
    return [
        {"type": "Test Event", "comparison": None, "period": None,
         "country": "US", "date": f"{d} 12:00:00", "actual": None,
         "previous": None, "estimate": None, "change": None,
         "change_percentage": None}
        for d, count in day_counts
        for _ in range(count)
    ]


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

    def test_transform_sets_source_and_category(self):
        query = EODHDEconomicCalendarQueryParams()
        rows = EODHDEconomicCalendarFetcher.transform_data(query, ECON_RESP)
        jp = rows[1]
        assert jp.event == "Foreign Bond Investment"
        assert jp.source == "Treasury"
        assert jp.category == "TIC"
        unknown = rows[2]
        assert unknown.event == "Made Up Event"
        assert unknown.source is None
        assert unknown.category is None

    def test_extract_passes_filters(self):
        query = EODHDEconomicCalendarQueryParams(country="US", comparison="yoy")
        client = _client("get_economic_events_data", ECON_RESP)
        with patch("openbb_eodhd.models.calendar.get_client", return_value=client):
            rows = run_async(
                EODHDEconomicCalendarFetcher.aextract_data, query, CREDS
            )
        assert len(rows) == 3
        client.get_economic_events_data.assert_called_once_with(
            date_from=None, date_to=None, country="US", comparison="yoy", limit=1000
        )

    def test_error_dict_raises(self):
        query = EODHDEconomicCalendarQueryParams()
        client = _client("get_economic_events_data", {"message": "denied"})
        with patch("openbb_eodhd.models.calendar.get_client", return_value=client):
            with pytest.raises(UnauthorizedError):
                run_async(EODHDEconomicCalendarFetcher.aextract_data, query, CREDS)

    # --------------------------------------------------------------
    # Window walk: EODHD caps /economic-events at limit=1000/request, so a
    # full page has to be windowed forward in date instead of trusted whole.
    # --------------------------------------------------------------

    def test_short_page_is_one_call(self):
        """Fewer than 1,000 rows back means the range is already complete."""
        query = EODHDEconomicCalendarQueryParams()
        client = _client_series("get_economic_events_data", [ECON_RESP])
        with patch("openbb_eodhd.models.calendar.get_client", return_value=client):
            rows = run_async(EODHDEconomicCalendarFetcher.aextract_data, query, CREDS)
        assert rows == ECON_RESP
        assert client.get_economic_events_data.call_count == 1

    def test_full_page_across_days_windows_on_boundary_date(self):
        # Page 1: 1,000 rows, 100/day across ten days, newest first.
        days = [f"2026-09-{n:02d}" for n in range(10, 0, -1)]
        page1 = _econ_page([(d, 100) for d in days])
        # Page 2: the boundary day (09-01) re-fetched whole, complete at 50.
        page2 = _econ_page([("2026-09-01", 50)])
        client = _client_series("get_economic_events_data", [page1, page2])
        query = EODHDEconomicCalendarQueryParams()
        with patch("openbb_eodhd.models.calendar.get_client", return_value=client):
            rows = run_async(EODHDEconomicCalendarFetcher.aextract_data, query, CREDS)

        assert client.get_economic_events_data.call_count == 2
        second_kwargs = client.get_economic_events_data.call_args_list[1].kwargs
        assert second_kwargs["date_to"] == "2026-09-01"
        assert "offset" not in second_kwargs

        # 900 complete rows from page 1 (days 09-02..09-10) plus the 50
        # authoritative rows for 09-01 from page 2; no duplicates.
        assert len(rows) == 950
        assert sum(1 for r in rows if r["date"].startswith("2026-09-01")) == 50
        dates = [r["date"] for r in rows]
        assert dates == sorted(dates)  # ascending

    def test_full_page_on_one_day_pages_by_offset(self):
        page1 = _econ_page([("2026-09-01", 1000)])
        page2 = _econ_page([("2026-09-01", 50)])
        page3: list[dict] = []  # nothing before 09-01: walk ends
        client = _client_series(
            "get_economic_events_data", [page1, page2, page3]
        )
        query = EODHDEconomicCalendarQueryParams()
        with patch("openbb_eodhd.models.calendar.get_client", return_value=client):
            rows = run_async(EODHDEconomicCalendarFetcher.aextract_data, query, CREDS)

        assert client.get_economic_events_data.call_count == 3
        second_kwargs = client.get_economic_events_data.call_args_list[1].kwargs
        assert second_kwargs["offset"] == 1000
        assert second_kwargs["date_to"] == "2026-09-01"
        third_kwargs = client.get_economic_events_data.call_args_list[2].kwargs
        assert third_kwargs["date_to"] == "2026-08-31"
        assert "offset" not in third_kwargs
        assert len(rows) == 1050

    def test_limit_truncates_after_the_full_walk(self):
        days = [f"2026-09-{n:02d}" for n in range(10, 0, -1)]
        page1 = _econ_page([(d, 100) for d in days])
        page2 = _econ_page([("2026-09-01", 200)])  # walk totals 1,100
        client = _client_series("get_economic_events_data", [page1, page2])
        query = EODHDEconomicCalendarQueryParams(limit=50)
        with patch("openbb_eodhd.models.calendar.get_client", return_value=client):
            rows = run_async(EODHDEconomicCalendarFetcher.aextract_data, query, CREDS)
        assert len(rows) == 50

    def test_request_ceiling_stops_and_warns(self):
        # Every page is full and spans the same days, so the walk never
        # naturally terminates; the ceiling must cut it off.
        days = [f"2026-09-{n:02d}" for n in range(10, 0, -1)]
        page = _econ_page([(d, 100) for d in days])
        client = _client_series(
            "get_economic_events_data", lambda **kw: page
        )
        query = EODHDEconomicCalendarQueryParams()
        with patch("openbb_eodhd.models.calendar.get_client", return_value=client):
            with pytest.warns(UserWarning):
                run_async(EODHDEconomicCalendarFetcher.aextract_data, query, CREDS)
        assert client.get_economic_events_data.call_count == 20

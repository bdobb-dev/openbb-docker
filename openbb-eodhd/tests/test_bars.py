# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Tests for openbb_eodhd.models._bars."""

from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from openbb_eodhd.models._bars import (
    INTRADAY_MAP,
    EOD_PERIOD_MAP,
    INTERVAL_CHOICES,
    to_unix,
    fetch_bars,
    rows_from_bars,
)
from tests.conftest import run_async


# ============================================================
# Constants
# ============================================================

class TestConstants:
    def test_intraday_map(self):
        assert INTRADAY_MAP == {"1m": "1m", "5m": "5m", "1h": "1h"}

    def test_eod_period_map(self):
        assert EOD_PERIOD_MAP == {"1d": "d", "1W": "w", "1M": "m"}

    def test_interval_choices(self):
        assert INTERVAL_CHOICES == ["1m", "5m", "1h", "1d", "1W", "1M"]


# ============================================================
# to_unix
# ============================================================

class TestToUnix:
    def test_start_of_day(self):
        d = date(2024, 1, 2)
        ts = to_unix(d, end_of_day=False)
        expected = int(datetime(2024, 1, 2, 0, 0, 0, tzinfo=timezone.utc).timestamp())
        assert ts == expected

    def test_end_of_day(self):
        d = date(2024, 1, 2)
        ts = to_unix(d, end_of_day=True)
        expected = int(datetime(2024, 1, 2, 23, 59, 59, 999999, tzinfo=timezone.utc).timestamp())
        assert ts == expected

    def test_different_date(self):
        d = date(2023, 6, 15)
        ts_start = to_unix(d, end_of_day=False)
        ts_end = to_unix(d, end_of_day=True)
        assert ts_start < ts_end


# ============================================================
# rows_from_bars
# ============================================================

class TestRowsFromBars:
    def test_eod_returns_date_objects(self, eod_bar_data):
        rows = rows_from_bars("1d", False, eod_bar_data)
        assert len(rows) == 2
        assert rows[0]["date"] == date(2024, 1, 2)
        assert rows[1]["date"] == date(2024, 1, 3)

    def test_eod_preserves_ohlcv(self, eod_bar_data):
        rows = rows_from_bars("1d", False, eod_bar_data)
        assert rows[0]["open"] == 150.0
        assert rows[0]["high"] == 153.0
        assert rows[0]["low"] == 149.0
        assert rows[0]["close"] == 152.0
        assert rows[0]["volume"] == 1000000

    def test_eod_includes_adjusted_close(self, eod_bar_data):
        rows = rows_from_bars("1d", False, eod_bar_data)
        assert rows[0]["adjusted_close"] == 151.5

    def test_intraday_returns_datetime(self, intraday_bar_data):
        rows = rows_from_bars("1m", False, intraday_bar_data)
        assert len(rows) == 2
        for r in rows:
            assert hasattr(r["date"], "tzinfo")

    def test_intraday_utc_tz(self, intraday_bar_data):
        rows = rows_from_bars("1m", False, intraday_bar_data)
        for r in rows:
            assert r["date"].tzinfo is not None

    def test_null_close_bars_dropped(self, null_close_bar_data):
        rows = rows_from_bars("1d", False, null_close_bar_data)
        assert len(rows) == 1
        assert rows[0]["date"] == date(2024, 1, 2)

    def test_sorted_by_date(self, eod_bar_data):
        reversed_data = list(reversed(eod_bar_data))
        rows = rows_from_bars("1d", False, reversed_data)
        assert rows[0]["date"] == date(2024, 1, 2)
        assert rows[1]["date"] == date(2024, 1, 3)

    def test_multi_symbol_includes_symbol(self, multi_symbol_bar_data):
        rows = rows_from_bars("1d", True, multi_symbol_bar_data)
        assert len(rows) == 2
        assert {r["symbol"] for r in rows} == {"AAPL", "MSFT"}

    def test_multi_symbol_sorted(self, multi_symbol_bar_data):
        rows = rows_from_bars("1d", True, multi_symbol_bar_data)
        assert rows[0]["symbol"] == "AAPL"
        assert rows[1]["symbol"] == "MSFT"

    def test_single_symbol_no_symbol_field(self, eod_bar_data):
        rows = rows_from_bars("1d", False, eod_bar_data)
        for r in rows:
            assert "symbol" not in r

    def test_empty_data_returns_empty(self):
        rows = rows_from_bars("1d", False, [])
        assert rows == []

    def test_intraday_ohlcv(self, intraday_bar_data):
        rows = rows_from_bars("1m", False, intraday_bar_data)
        assert rows[0]["open"] == 150.0
        assert rows[0]["close"] == 150.5

    def test_eod_bar_missing_date_skipped(self, eod_bar_data):
        from pandas import isna

        bad = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10}
        rows = rows_from_bars("1d", False, eod_bar_data + [bad])
        # The dateless bar is dropped; no NaT leaks into the series.
        assert len(rows) == len(eod_bar_data)
        assert not any(isna(r["date"]) for r in rows)

    def test_intraday_bar_missing_timestamp_skipped(self, intraday_bar_data):
        from pandas import isna

        bad = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10}
        rows = rows_from_bars("1m", False, intraday_bar_data + [bad])
        assert len(rows) == len(intraday_bar_data)
        assert not any(isna(r["date"]) for r in rows)


# ============================================================
# fetch_bars (async — mocked official-SDK client)
# ============================================================

PATCH_TARGET = "openbb_eodhd.models._bars.get_client"


class TestFetchBars:
    def test_missing_credentials_raises(self):
        with pytest.raises(Exception, match="Missing EODHD credential"):
            run_async(fetch_bars, "1d", ["AAPL.US"], date(2024, 1, 1), date(2024, 1, 5), None)

    def test_empty_credentials_raises(self):
        with pytest.raises(Exception, match="Missing EODHD credential"):
            run_async(fetch_bars, "1d", ["AAPL.US"], date(2024, 1, 1), date(2024, 1, 5), {})

    def test_single_symbol_eod(self, eodhd_credentials, eod_bar_data, mock_eodhd_client):
        client = mock_eodhd_client(eod_bar_data)
        with patch(PATCH_TARGET, return_value=client):
            results = run_async(
                fetch_bars, "1d", ["AAPL.US"],
                date(2024, 1, 1), date(2024, 1, 5), eodhd_credentials,
            )
        assert len(results) == 2
        assert "_symbol" not in results[0]
        client.get_eod_historical_stock_market_data.assert_called_once()
        assert client.get_eod_historical_stock_market_data.call_args.kwargs["symbol"] == "AAPL.US"
        client.get_intraday_historical_data.assert_not_called()

    def test_multi_symbol_eod(self, eodhd_credentials, eod_bar_data, mock_eodhd_client):
        client = mock_eodhd_client(lambda *a, **k: [dict(b) for b in eod_bar_data])
        with patch(PATCH_TARGET, return_value=client):
            results = run_async(
                fetch_bars, "1d", ["AAPL.US", "MSFT.US"],
                date(2024, 1, 1), date(2024, 1, 5), eodhd_credentials,
            )
        assert len(results) == 4  # 2 symbols x 2 bars each
        assert client.get_eod_historical_stock_market_data.call_count == 2

    def test_multi_symbol_sets_underscore_symbol(
        self, eodhd_credentials, eod_bar_data, mock_eodhd_client
    ):
        # Return fresh copies so _symbol mutations don't alias
        client = mock_eodhd_client(lambda *a, **k: [dict(b) for b in eod_bar_data])
        with patch(PATCH_TARGET, return_value=client):
            results = run_async(
                fetch_bars, "1d", ["AAPL.US", "MSFT.US"],
                date(2024, 1, 1), date(2024, 1, 5), eodhd_credentials,
            )
        assert client.get_eod_historical_stock_market_data.call_count == 2
        symbols = [r["_symbol"] for r in results]
        assert symbols[0] == "AAPL.US"
        assert symbols[1] == "AAPL.US"
        assert symbols[2] == "MSFT.US"
        assert symbols[3] == "MSFT.US"

    def test_empty_response_raises(self, eodhd_credentials, mock_eodhd_client):
        client = mock_eodhd_client([])
        with patch(PATCH_TARGET, return_value=client):
            with pytest.raises(Exception, match="The request was returned empty"):
                run_async(
                    fetch_bars, "1d", ["AAPL.US"],
                    date(2024, 1, 1), date(2024, 1, 5), eodhd_credentials,
                )

    def test_error_dict_response_raises(self, eodhd_credentials, mock_eodhd_client):
        client = mock_eodhd_client({"errors": "Invalid API token"})
        with patch(PATCH_TARGET, return_value=client):
            with pytest.raises(Exception, match="Invalid API token"):
                run_async(
                    fetch_bars, "1d", ["AAPL.US"],
                    date(2024, 1, 1), date(2024, 1, 5), eodhd_credentials,
                )

    def test_sdk_http_error_maps_to_unauthorized(self, eodhd_credentials, mock_eodhd_client):
        class FakeHTTPError(Exception):
            status_code = 401

        def boom(*a, **k):
            raise FakeHTTPError("unauthorized")

        client = mock_eodhd_client(boom)
        with patch(PATCH_TARGET, return_value=client):
            with pytest.raises(Exception, match="HTTP 401"):
                run_async(
                    fetch_bars, "1d", ["AAPL.US"],
                    date(2024, 1, 1), date(2024, 1, 5), eodhd_credentials,
                )

    def test_sdk_other_error_maps_to_openbb_error(self, eodhd_credentials, mock_eodhd_client):
        def boom(*a, **k):
            raise RuntimeError("connection reset")

        client = mock_eodhd_client(boom)
        with patch(PATCH_TARGET, return_value=client):
            with pytest.raises(Exception, match="connection reset"):
                run_async(
                    fetch_bars, "1d", ["AAPL.US"],
                    date(2024, 1, 1), date(2024, 1, 5), eodhd_credentials,
                )

    def test_intraday_uses_timestamps(
        self, eodhd_credentials, intraday_bar_data, mock_eodhd_client
    ):
        client = mock_eodhd_client(intraday_bar_data)
        with patch(PATCH_TARGET, return_value=client):
            results = run_async(
                fetch_bars, "1m", ["AAPL.US"],
                date(2024, 1, 1), date(2024, 1, 5), eodhd_credentials,
            )
        assert len(results) == 2
        client.get_intraday_historical_data.assert_called_once()
        client.get_eod_historical_stock_market_data.assert_not_called()

    def test_eod_call_params(self, eodhd_credentials, eod_bar_data, mock_eodhd_client):
        client = mock_eodhd_client(eod_bar_data)
        with patch(PATCH_TARGET, return_value=client):
            run_async(
                fetch_bars, "1d", ["AAPL.US"],
                date(2024, 1, 1), date(2024, 1, 5), eodhd_credentials,
            )
        kwargs = client.get_eod_historical_stock_market_data.call_args.kwargs
        assert kwargs["period"] == "d"
        assert kwargs["order"] == "a"
        assert kwargs["from_date"] == "2024-01-01"
        assert kwargs["to_date"] == "2024-01-05"

    def test_intraday_call_params(
        self, eodhd_credentials, intraday_bar_data, mock_eodhd_client
    ):
        client = mock_eodhd_client(intraday_bar_data)
        with patch(PATCH_TARGET, return_value=client):
            run_async(
                fetch_bars, "1m", ["AAPL.US"],
                date(2024, 1, 1), date(2024, 1, 5), eodhd_credentials,
            )
        kwargs = client.get_intraday_historical_data.call_args.kwargs
        assert kwargs["interval"] == "1m"
        assert isinstance(kwargs["from_unix_time"], int)
        assert isinstance(kwargs["to_unix_time"], int)
        assert kwargs["from_unix_time"] < kwargs["to_unix_time"]

"""Tests for app.symbol_meta."""

from unittest.mock import MagicMock

from app import symbol_meta
from app.symbol_meta import get_meta, _reset_cache_for_tests


RESP = {
    "General::LogoURL": "/img/logos/US/MSFT.png",
    "General::Name": "Microsoft Corporation",
    "Technicals::52WeekHigh": 549.2013,
    "Technicals::52WeekLow": 348.5438,
}


def _client(resp=RESP):
    client = MagicMock()
    client.get_fundamentals_data.return_value = resp
    return client


class TestGetMeta:
    def setup_method(self):
        _reset_cache_for_tests()

    def test_maps_and_absolutizes(self):
        rows = get_meta(["MSFT"], _client())
        r = rows[0]
        assert r["symbol"] == "MSFT"
        assert r["logo_url"] == "https://eodhd.com/img/logos/US/MSFT.png"
        assert r["name"] == "Microsoft Corporation"
        assert r["week52_high"] == 549.2013
        assert r["week52_low"] == 348.5438

    def test_uses_qualified_ticker_and_filter(self):
        client = _client()
        get_meta(["MSFT"], client)
        client.get_fundamentals_data.assert_called_once_with(
            ticker="MSFT.US", filter=symbol_meta._FILTER
        )

    def test_cached_second_call_no_fetch(self):
        client = _client()
        get_meta(["MSFT"], client)
        get_meta(["MSFT"], client)
        assert client.get_fundamentals_data.call_count == 1

    def test_failure_yields_blank_row(self):
        client = MagicMock()
        client.get_fundamentals_data.side_effect = RuntimeError("boom")
        rows = get_meta(["FAIL"], client)
        assert rows[0] == {"symbol": "FAIL", "name": None, "logo_url": None,
                           "week52_high": None, "week52_low": None}

    def test_na_values_coerce_to_none(self):
        rows = get_meta(["X"], _client({
            "General::LogoURL": None, "General::Name": "",
            "Technicals::52WeekHigh": "NA", "Technicals::52WeekLow": None,
        }))
        r = rows[0]
        assert r["logo_url"] is None and r["name"] is None
        assert r["week52_high"] is None and r["week52_low"] is None

    def test_forex_literal_na_logo(self):
        # EURUSD answers the literal string "NA" for LogoURL (live, 2026-09-01)
        rows = get_meta(["EURUSD"], _client({
            "General::LogoURL": "NA", "General::Name": "EUR/USD",
            "Technicals::52WeekHigh": None, "Technicals::52WeekLow": None,
        }))
        assert rows[0]["logo_url"] is None
        assert rows[0]["name"] == "EUR/USD"

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Tests for app.quotes.QuoteTable."""

from unittest.mock import MagicMock

import pytest

from app.quotes import QuoteTable


def make_rest(snapshots: dict):
    """MagicMock APIClient whose get_live_stock_prices returns per-ticker dicts."""
    client = MagicMock()
    client.get_live_stock_prices.side_effect = (
        lambda ticker=None, **kw: snapshots.get(ticker)
    )
    return client


class TestSeed:
    def test_seeds_price_and_change_from_snapshot(self):
        q = QuoteTable()
        rest = make_rest({"AAPL.US": {"close": 100.0, "previousClose": 80.0, "volume": 5000}})
        rows = q.seed(["AAPL"], rest)
        assert rows[0]["symbol"] == "AAPL"
        assert rows[0]["price"] == 100.0
        assert rows[0]["change"] == 20.0
        assert abs(rows[0]["change_percent"] - 0.25) < 1e-9
        assert rows[0]["volume"] == 5000.0

    def test_uses_exchange_qualified_ticker(self):
        q = QuoteTable()
        rest = make_rest({})
        q.seed(["BTC-USD", "EURUSD"], rest)
        tickers = [c.kwargs["ticker"] for c in rest.get_live_stock_prices.call_args_list]
        assert tickers == ["BTC-USD.CC", "EURUSD.FOREX"]

    def test_na_values_coerce_to_none(self):
        q = QuoteTable()
        rest = make_rest(
            {"AAPL.US": {"close": "NA", "previousClose": "NA", "change": "NA", "change_p": "NA"}}
        )
        rows = q.seed(["AAPL"], rest)
        assert rows[0]["price"] is None
        assert rows[0]["change"] is None
        assert rows[0]["change_percent"] is None

    def test_snapshot_failure_degrades_to_blank_row(self):
        q = QuoteTable()
        rest = MagicMock()
        rest.get_live_stock_prices.side_effect = RuntimeError("boom")
        rows = q.seed(["AAPL"], rest)
        assert rows[0]["symbol"] == "AAPL"
        assert rows[0]["price"] is None

    def test_list_wrapped_snapshot_unwrapped(self):
        # Multi-ticker REST responses come back as a list.
        q = QuoteTable()
        rest = make_rest({"AAPL.US": [{"close": 50.0, "previousClose": 40.0}]})
        rows = q.seed(["AAPL"], rest)
        assert rows[0]["price"] == 50.0

    def test_change_p_fallback_is_fraction(self):
        # No close price -> falls back to snapshot change_p, converted to fraction.
        q = QuoteTable()
        rest = make_rest({"AAPL.US": {"previousClose": 80.0, "change": 1.5, "change_p": 1.5}})
        rows = q.seed(["AAPL"], rest)
        assert rows[0]["change"] == 1.5
        assert abs(rows[0]["change_percent"] - 0.015) < 1e-9


class TestApplyTick:
    def seeded(self):
        q = QuoteTable()
        rest = make_rest({"AAPL.US": {"close": 100.0, "previousClose": 80.0}})
        q.seed(["AAPL"], rest)
        return q

    def test_trade_tick_updates_price_and_change(self):
        q = self.seeded()
        sym = q.apply_tick("us", {"s": "AAPL", "p": 90.0, "q": 25, "t": 1700000000000})
        assert sym == "AAPL"
        row = q.rows["AAPL"]
        assert row["price"] == 90.0
        assert row["change"] == 10.0
        assert abs(row["change_percent"] - 0.125) < 1e-9
        assert row["last_size"] == 25.0
        assert row["updated_at"] == "22:13:20"  # 1700000000000 ms = 2023-11-14T22:13:20Z

    def test_forex_tick_uses_mid(self):
        q = QuoteTable()
        sym = q.apply_tick("forex", {"s": "EURUSD", "a": 1.10, "b": 1.08, "t": 1700000000000})
        assert sym == "EURUSD"
        row = q.rows["EURUSD"]
        assert row["bid"] == 1.08
        assert row["ask"] == 1.10
        assert abs(row["price"] - 1.09) < 1e-9

    def test_unknown_symbol_creates_row(self):
        q = QuoteTable()
        assert q.apply_tick("us", {"s": "MSFT", "p": 300.0}) == "MSFT"
        assert q.rows["MSFT"]["price"] == 300.0
        assert q.rows["MSFT"]["change"] is None  # no prev close known

    def test_tick_without_price_ignored(self):
        q = QuoteTable()
        assert q.apply_tick("us", {"s": "AAPL"}) is None
        assert q.apply_tick("us", {"p": 1.0}) is None
        assert q.apply_tick("forex", {"s": "EURUSD"}) is None

    def test_string_numerics_coerced(self):
        q = QuoteTable()
        q.apply_tick("crypto", {"s": "BTC-USD", "p": "50000.5", "q": "0.01"})
        assert q.rows["BTC-USD"]["price"] == 50000.5
        assert q.rows["BTC-USD"]["last_size"] == 0.01


def test_apply_tick_reports_the_tick_to_the_recorder():
    seen = []
    table = QuoteTable(on_tick=lambda *a: seen.append(a))
    table.apply_tick("us", {"s": "AAPL", "p": 100.5, "q": 3, "t": 1749565200000})
    assert len(seen) == 1
    sym, price, size, stamp = seen[0]
    assert (sym, price, size) == ("AAPL", 100.5, 3)


def test_forex_ticks_are_recorded_at_the_mid():
    seen = []
    table = QuoteTable(on_tick=lambda *a: seen.append(a))
    table.apply_tick("forex", {"s": "EURUSD", "b": 1.08, "a": 1.10, "t": 1749565200000})
    assert seen[0][1] == pytest.approx(1.09)


def test_a_rejected_tick_is_not_recorded():
    seen = []
    table = QuoteTable(on_tick=lambda *a: seen.append(a))
    table.apply_tick("us", {"s": "AAPL"})  # no price
    assert seen == []


def test_a_failing_recorder_never_breaks_the_grid():
    """Episode 8's feature must survive anything the cache does."""
    def boom(*_):
        raise RuntimeError("kdb exploded")

    table = QuoteTable(on_tick=boom)
    assert table.apply_tick("us", {"s": "AAPL", "p": 100.5, "q": 3}) == "AAPL"
    assert table.rows["AAPL"]["price"] == 100.5


def test_seed_reuses_a_fresh_cached_snapshot_instead_of_calling_the_vendor():
    """A debounced rebuild must not re-hit the vendor for a symbol we just fetched."""
    class Cache:
        def read_snapshot(self, symbol, max_age):
            return {"close": 100.0, "volume": 5.0}

        def write_snapshot(self, symbol, payload):
            raise AssertionError("should not write when the cache was fresh")

    calls = []

    class Client:
        def get_live_stock_prices(self, ticker):
            calls.append(ticker)
            return {"close": 1.0}

    table = QuoteTable(snapshots=Cache())
    table.seed(["AAPL"], Client())
    assert calls == []


def test_seed_falls_back_to_the_vendor_on_a_cache_miss():
    class Cache:
        def read_snapshot(self, symbol, max_age):
            return None

        def write_snapshot(self, symbol, payload):
            self.written = payload

    calls = []

    class Client:
        def get_live_stock_prices(self, ticker):
            calls.append(ticker)
            return {"close": 1.0}

    table = QuoteTable(snapshots=Cache())
    table.seed(["AAPL"], Client())
    assert len(calls) == 1


def test_seed_works_with_no_snapshot_cache_at_all():
    calls = []

    class Client:
        def get_live_stock_prices(self, ticker):
            calls.append(ticker)
            return {"close": 1.0}

    QuoteTable().seed(["AAPL"], Client())
    assert len(calls) == 1


def test_seed_falls_back_to_the_vendor_when_the_cache_raises_on_read():
    """A regression moving the read out of its try (or narrowing the except)
    must not take seeding down with it -- kdb+ being unreachable is exactly
    when the vendor fallback matters most."""
    class Cache:
        def read_snapshot(self, symbol, max_age):
            raise RuntimeError("kdb unreachable")

        def write_snapshot(self, symbol, payload):
            pass

    calls = []

    class Client:
        def get_live_stock_prices(self, ticker):
            calls.append(ticker)
            return {"close": 1.0}

    table = QuoteTable(snapshots=Cache())
    rows = table.seed(["AAPL"], Client())
    assert len(calls) == 1
    assert rows[0]["price"] == 1.0


def test_seed_falls_back_to_the_vendor_when_the_cache_raises_on_write():
    """A regression moving the write outside its try -- e.g. hoisting it past
    the vendor call -- must still leave seed() returning the vendor's row
    without raising; live-grid ships with no kdb+ at all in some deployments."""
    class Cache:
        def read_snapshot(self, symbol, max_age):
            return None

        def write_snapshot(self, symbol, payload):
            raise RuntimeError("kdb unreachable")

    calls = []

    class Client:
        def get_live_stock_prices(self, ticker):
            calls.append(ticker)
            return {"close": 1.0}

    table = QuoteTable(snapshots=Cache())
    rows = table.seed(["AAPL"], Client())
    assert len(calls) == 1
    assert rows[0]["price"] == 1.0

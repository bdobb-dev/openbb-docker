# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Tests for app.classify."""

from app.classify import FEEDS, classify, snapshot_ticker, split_by_feed


class TestClassify:
    def test_feeds_constant(self):
        assert FEEDS == ("us", "crypto", "forex")

    def test_plain_equity(self):
        assert classify("AAPL") == "us"

    def test_lowercase_equity(self):
        assert classify("aapl") == "us"

    def test_crypto_dash(self):
        assert classify("BTC-USD") == "crypto"

    def test_forex_pair(self):
        assert classify("EURUSD") == "forex"

    def test_six_letter_equity_not_forex(self):
        # 6 letters but halves are not both currency codes -> equity
        assert classify("GOOGLX") == "us"

    def test_five_letter_equity(self):
        assert classify("GOOGL") == "us"


class TestSplitByFeed:
    def test_groups_and_cleans(self):
        out = split_by_feed([" aapl ", "BTC-USD", "eurusd", "AAPL", ""])
        assert out == {"us": {"AAPL"}, "crypto": {"BTC-USD"}, "forex": {"EURUSD"}}

    def test_all_feeds_always_present(self):
        assert set(split_by_feed([]).keys()) == {"us", "crypto", "forex"}


class TestSnapshotTicker:
    def test_us(self):
        assert snapshot_ticker("AAPL") == "AAPL.US"

    def test_crypto(self):
        assert snapshot_ticker("btc-usd") == "BTC-USD.CC"

    def test_forex(self):
        assert snapshot_ticker("EURUSD") == "EURUSD.FOREX"

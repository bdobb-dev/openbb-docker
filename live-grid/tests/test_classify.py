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


# ---------- the fallback must not swallow unroutable symbols ----------

def test_hyphen_alone_is_not_crypto():
    """BRK-B and BF-B are US equities. Routing them to the crypto feed
    subscribes a symbol that never ticks, and never errors either."""
    from app.classify import classify

    assert classify("BRK-B") == "us"
    assert classify("BF-B") == "us"
    assert classify("BTC-USD") == "crypto"
    assert classify("ETH-USD") == "crypto"


def test_an_exchange_we_cannot_feed_is_unsupported_not_us():
    """TALA.TO is Toronto; EODHD's websockets carry only us/crypto/forex.
    Falling through to 'us' subscribed TALA.US, which never ticked."""
    from app.classify import UNSUPPORTED, classify

    assert classify("TALA.TO") == UNSUPPORTED
    assert classify("VOD.L") == UNSUPPORTED
    assert classify("AAPL.US") == "us"
    assert classify("BTC-USD.CC") == "crypto"


def test_split_by_feed_leaves_unroutable_symbols_out():
    from app.classify import split_by_feed, unsupported

    syms = ["AAPL", "TALA.TO", "BTC-USD", "EURUSD", "BRK-B"]
    out = split_by_feed(syms)
    assert out["us"] == {"AAPL", "BRK-B"}
    assert out["crypto"] == {"BTC-USD"}
    assert out["forex"] == {"EURUSD"}
    assert all("TALA.TO" not in v for v in out.values())
    assert unsupported(syms) == ["TALA.TO"]


def test_snapshot_ticker_never_double_qualifies():
    """TALA.TO + ".US" produced TALA.TO.US, which EODHD 404s."""
    from app.classify import snapshot_ticker

    assert snapshot_ticker("AAPL") == "AAPL.US"
    assert snapshot_ticker("TALA.TO") == "TALA.TO"
    assert snapshot_ticker("AAPL.US") == "AAPL.US"


def test_history_endpoint_names_the_symbol_it_cannot_route():
    import pytest
    from app.openbb_client import history_endpoint

    assert history_endpoint("AAPL") == "equity/price/historical"
    assert history_endpoint("BTC-USD") == "crypto/price/historical"
    with pytest.raises(ValueError, match="TALA.TO"):
        history_endpoint("TALA.TO")

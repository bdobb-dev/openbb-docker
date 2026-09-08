# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Tests for app.feeds.FeedManager (SDK client mocked via factory)."""

import asyncio
import json

import app.feeds as feeds_mod
from app.feeds import FeedManager
from app.quotes import QuoteTable


def make_manager(factory) -> FeedManager:
    return FeedManager("test_key", QuoteTable(), client_factory=factory, rebuild_delay=0.0)


class TestSyncFeeds:
    def test_builds_one_client_per_feed(self, fake_ws_client_factory):
        m = make_manager(fake_ws_client_factory)
        m.register("c1", ["AAPL", "BTC-USD", "EURUSD"])
        asyncio.run(m._sync_feeds())
        by_feed = {c.feed: c.symbols for c in fake_ws_client_factory.built}
        assert by_feed == {"us": ["AAPL"], "crypto": ["BTC-USD"], "forex": ["EURUSD"]}
        for c in fake_ws_client_factory.built:
            c.start.assert_called_once()

    def test_no_rebuild_when_union_unchanged(self, fake_ws_client_factory):
        m = make_manager(fake_ws_client_factory)
        m.register("c1", ["AAPL"])
        asyncio.run(m._sync_feeds())
        m.register("c2", ["AAPL"])  # same union
        asyncio.run(m._sync_feeds())
        assert len(fake_ws_client_factory.built) == 1

    def test_union_shrinks_on_unregister(self, fake_ws_client_factory):
        m = make_manager(fake_ws_client_factory)
        m.register("c1", ["AAPL", "MSFT"])
        m.register("c2", ["AAPL"])
        asyncio.run(m._sync_feeds())
        assert fake_ws_client_factory.built[0].symbols == ["AAPL", "MSFT"]
        m.unregister("c1")
        asyncio.run(m._sync_feeds())
        fake_ws_client_factory.built[0].stop.assert_called_once()
        assert fake_ws_client_factory.built[1].symbols == ["AAPL"]

    def test_empty_union_stops_client(self, fake_ws_client_factory):
        m = make_manager(fake_ws_client_factory)
        m.register("c1", ["AAPL"])
        asyncio.run(m._sync_feeds())
        m.unregister("c1")
        asyncio.run(m._sync_feeds())
        fake_ws_client_factory.built[0].stop.assert_called_once()
        assert m.status()["us"]["symbols"] == []

    def test_factory_failure_keeps_manager_alive(self, fake_ws_client_factory):
        def broken(api_key, feed, symbols):
            raise RuntimeError("bad key")

        m = FeedManager("k", QuoteTable(), client_factory=broken, rebuild_delay=0.0)
        m.register("c1", ["AAPL"])
        asyncio.run(m._sync_feeds())  # must not raise
        assert m.status()["us"]["running"] is False


class TestDrain:
    def test_ticks_update_quotes_and_route_dirty(self, fake_ws_client_factory):
        m = make_manager(fake_ws_client_factory)
        m.register("c1", ["AAPL"])
        m.register("c2", ["MSFT"])
        asyncio.run(m._sync_feeds())
        us = next(c for c in fake_ws_client_factory.built if c.feed == "us")
        us.data_list.append(json.dumps({"s": "AAPL", "p": 190.5, "q": 10, "t": 1700000000000}))
        m._drain_all()
        assert m.quotes.rows["AAPL"]["price"] == 190.5
        assert m.pop_dirty("c1") == {"AAPL"}
        assert m.pop_dirty("c2") == set()
        assert us.data_list == []  # consumed prefix trimmed

    def test_pop_dirty_clears(self, fake_ws_client_factory):
        m = make_manager(fake_ws_client_factory)
        m.register("c1", ["AAPL"])
        asyncio.run(m._sync_feeds())
        us = fake_ws_client_factory.built[0]
        us.data_list.append(json.dumps({"s": "AAPL", "p": 1.0}))
        m._drain_all()
        assert m.pop_dirty("c1") == {"AAPL"}
        assert m.pop_dirty("c1") == set()

    def test_garbage_messages_skipped(self, fake_ws_client_factory):
        m = make_manager(fake_ws_client_factory)
        m.register("c1", ["AAPL"])
        asyncio.run(m._sync_feeds())
        us = fake_ws_client_factory.built[0]
        us.data_list.extend(["not json", json.dumps({"status": "ok"})])
        m._drain_all()  # must not raise
        assert m.pop_dirty("c1") == set()


class TestLifecycle:
    def test_stop_all_stops_every_client(self, fake_ws_client_factory):
        m = make_manager(fake_ws_client_factory)
        m.register("c1", ["AAPL", "BTC-USD"])
        asyncio.run(m._sync_feeds())
        asyncio.run(m.stop_all())
        for c in fake_ws_client_factory.built:
            c.stop.assert_called_once()

    def test_status_shape(self, fake_ws_client_factory):
        m = make_manager(fake_ws_client_factory)
        status = m.status()
        assert set(status.keys()) == {"us", "crypto", "forex"}
        assert status["us"] == {"symbols": [], "running": False, "last_tick_age_s": None}


class TestLiveness:
    """_check_liveness: zombie feeds (connected, silent) get rebuilt."""

    MARKET_OPEN = __import__("datetime").datetime(2026, 9, 2, 15, 0, tzinfo=__import__("datetime").timezone.utc)
    MARKET_CLOSED = __import__("datetime").datetime(2026, 9, 2, 2, 0, tzinfo=__import__("datetime").timezone.utc)
    # 2026-09-05 is a Saturday -- same time-of-day as MARKET_OPEN, so this
    # isolates the weekday gate from the hour-of-day gate.
    MARKET_SATURDAY = __import__("datetime").datetime(2026, 9, 5, 15, 0, tzinfo=__import__("datetime").timezone.utc)

    def make(self, factory):
        t = {"now": 1000.0}
        m = FeedManager(
            "k", QuoteTable(), client_factory=factory, rebuild_delay=0.0,
            clock=lambda: t["now"],
        )
        return m, t

    def test_silent_us_feed_rebuilds_during_market_hours(self, fake_ws_client_factory):
        m, t = self.make(fake_ws_client_factory)
        m.register("c1", ["AAPL"])
        asyncio.run(m._sync_feeds())
        t["now"] += feeds_mod.STALE_AFTER + 1
        asyncio.run(m._check_liveness(when=self.MARKET_OPEN))
        assert m._rebuild_pending
        asyncio.run(m._sync_feeds())
        fake_ws_client_factory.built[0].stop.assert_called_once()
        assert len(fake_ws_client_factory.built) == 2  # fresh client constructed

    def test_buffer_activity_prevents_rebuild(self, fake_ws_client_factory):
        m, t = self.make(fake_ws_client_factory)
        m.register("c1", ["AAPL"])
        asyncio.run(m._sync_feeds())
        m._rebuild_pending = False  # register() set it; setup noise, not the signal
        t["now"] += feeds_mod.STALE_AFTER + 1
        fake_ws_client_factory.built[0].data_list.append(json.dumps({"s": "AAPL", "p": 1.0}))
        m._drain_all()  # stamps _last_tick
        asyncio.run(m._check_liveness(when=self.MARKET_OPEN))
        assert not m._rebuild_pending

    def test_no_rebuild_outside_market_hours(self, fake_ws_client_factory):
        m, t = self.make(fake_ws_client_factory)
        m.register("c1", ["AAPL"])
        asyncio.run(m._sync_feeds())
        m._rebuild_pending = False  # register() set it; setup noise, not the signal
        t["now"] += feeds_mod.STALE_AFTER + 1
        asyncio.run(m._check_liveness(when=self.MARKET_CLOSED))
        assert not m._rebuild_pending

    def test_crypto_expected_around_the_clock(self, fake_ws_client_factory):
        m, t = self.make(fake_ws_client_factory)
        m.register("c1", ["BTC-USD"])
        asyncio.run(m._sync_feeds())
        t["now"] += feeds_mod.STALE_AFTER + 1
        asyncio.run(m._check_liveness(when=self.MARKET_CLOSED))
        assert m._rebuild_pending

    def test_rebuild_rate_limited_to_one_per_stale_period(self, fake_ws_client_factory):
        m, t = self.make(fake_ws_client_factory)
        m.register("c1", ["BTC-USD"])
        asyncio.run(m._sync_feeds())
        t["now"] += feeds_mod.STALE_AFTER + 1
        asyncio.run(m._check_liveness(when=self.MARKET_OPEN))
        assert m._rebuild_pending
        m._rebuild_pending = False
        t["now"] += feeds_mod.LIVENESS_INTERVAL + 1  # next check window, still silent
        asyncio.run(m._check_liveness(when=self.MARKET_OPEN))
        assert not m._rebuild_pending  # _last_tick was reset; not stale again yet

    def test_status_exposes_last_tick_age(self, fake_ws_client_factory):
        m, t = self.make(fake_ws_client_factory)
        assert m.status()["us"]["last_tick_age_s"] is None
        m.register("c1", ["AAPL"])
        asyncio.run(m._sync_feeds())
        t["now"] += 12.5
        assert m.status()["us"]["last_tick_age_s"] == 12.5

    def test_zombie_dropped_when_last_subscriber_unregisters_before_sync(
        self, fake_ws_client_factory
    ):
        """A zombie whose only subscriber leaves between detection and the
        next _sync_feeds must not get stranded in _clients forever.

        _sync_feeds only acts when `want != _active`; if _check_liveness only
        cleared _active (not _clients) and `want` is now also empty, both
        sides read empty and _sync_feeds no-ops, leaving the stopped-in-name-
        only client in _clients to be re-detected as stale on every future
        pass. _check_liveness must stop and drop it itself.
        """
        m, t = self.make(fake_ws_client_factory)
        m.register("c1", ["AAPL"])
        asyncio.run(m._sync_feeds())
        fake_ws_client_factory.built[0].data_list.append(json.dumps({"s": "AAPL", "p": 1.0}))
        m._drain_all()  # stamps _last_tick
        t["now"] += feeds_mod.STALE_AFTER + 1
        m.unregister("c1")  # last subscriber gone before the next sync
        asyncio.run(m._check_liveness(when=self.MARKET_OPEN))
        asyncio.run(m._sync_feeds())
        fake_ws_client_factory.built[0].stop.assert_called_once()
        assert "us" not in m._clients

    def test_weekday_gate_excludes_saturday_within_market_hours(self, fake_ws_client_factory):
        assert not feeds_mod._expect_ticks("us", self.MARKET_SATURDAY)
        m, t = self.make(fake_ws_client_factory)
        m.register("c1", ["AAPL"])
        asyncio.run(m._sync_feeds())
        m._rebuild_pending = False  # register() set it; setup noise, not the signal
        t["now"] += feeds_mod.STALE_AFTER + 1
        asyncio.run(m._check_liveness(when=self.MARKET_SATURDAY))
        assert not m._rebuild_pending

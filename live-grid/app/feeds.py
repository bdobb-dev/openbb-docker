"""One official-SDK websocket client per EODHD feed, driven by grid subscriptions."""

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone

from app.classify import FEEDS, split_by_feed
from app.quotes import QuoteTable

log = logging.getLogger("live-grid")

PRUNE_INTERVAL = 60.0  # seconds between rolling-window prunes of the tick cache
LIVENESS_INTERVAL = 60.0  # seconds between zombie-feed checks
STALE_AFTER = float(os.getenv("LIVE_GRID_FEED_STALE_S", "300"))


def _expect_ticks(feed: str, when: datetime | None = None) -> bool:
    """Whether a healthy feed should be delivering ticks right now.

    crypto trades around the clock. For us, weekdays 14:30-20:00 UTC is
    inside NYSE regular hours whether the US is on EST or EDT, so silence
    there is never the market's fault.
    """
    # ponytail: ignores US market holidays (worst case: one spurious rebuild
    # per STALE_AFTER on a holiday) and never expects forex ticks (feed is
    # unused on this deployment). Add a holiday calendar / forex sessions if
    # either ever matters.
    if feed == "crypto":
        return True
    if feed != "us":
        return False
    now = when if when is not None else datetime.now(timezone.utc)
    return now.weekday() < 5 and (14, 30) <= (now.hour, now.minute) < (20, 0)


def _build_client(api_key: str, feed: str, symbols: list[str]):
    """Construct the official WebSocketClient without clobbering signal handlers.

    The SDK registers SIGINT/SIGTERM handlers in __init__ (fine for its CLI
    demo, fatal for a server: uvicorn/docker-stop would break). Snapshot and
    restore around construction. Must run on the main thread — signal.signal
    raises ValueError anywhere else.
    """
    # pylint: disable=import-outside-toplevel
    from eodhd import WebSocketClient

    prev_int = signal.getsignal(signal.SIGINT)
    prev_term = signal.getsignal(signal.SIGTERM)
    try:
        return WebSocketClient(
            api_key=api_key, endpoint=feed, symbols=list(symbols), store_data=True
        )
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)


def _stop_quietly(client) -> None:
    try:
        client.stop()
    except Exception as exc:  # noqa: BLE001 -- best-effort teardown
        log.warning("error stopping feed client: %s", exc)


class FeedManager:
    """Tracks grid connections; keeps EODHD subscriptions equal to their union."""

    def __init__(
        self,
        api_key: str,
        quotes: QuoteTable,
        *,
        client_factory=_build_client,
        drain_interval: float = 0.2,
        rebuild_delay: float = 1.0,
        recorder=None,
        clock=time.monotonic,
    ) -> None:
        self._api_key = api_key
        self.quotes = quotes
        self._client_factory = client_factory
        self._drain_interval = drain_interval
        self._rebuild_delay = rebuild_delay
        self.recorder = recorder
        self._conns: dict[str, dict[str, set[str]]] = {}  # conn_id -> feed -> symbols
        self._dirty: dict[str, set[str]] = {}  # conn_id -> symbols with fresh ticks
        self._clients: dict[str, object] = {}  # feed -> SDK WebSocketClient
        self._active: dict[str, frozenset[str]] = {}  # feed -> subscribed set
        self._rebuild_pending = False
        self._last_rebuild = float("-inf")
        # -inf, not 0.0, and for the same reason as _last_rebuild above:
        # loop.time() is CLOCK_MONOTONIC, so 0.0 is not a harmless "long
        # ago" -- it is a real instant this host may not have reached yet.
        # Comparing against it made the startup prune depend on machine
        # uptime, skipping it entirely on a host booted less than
        # PRUNE_INTERVAL ago (a CI runner is exactly that).
        self._last_prune = float("-inf")
        self._clock = clock
        self._last_tick: dict[str, float] = {}  # feed -> clock() at last buffer activity
        self._last_liveness = float("-inf")

    # -- subscription tracking ------------------------------------------------
    def register(self, conn_id: str, symbols: list[str]) -> None:
        # _dirty FIRST. These are two separate statements, so another thread
        # can observe the dict between them; establishing the dirty set before
        # the connection becomes visible means "conn_id is in _conns" always
        # implies "conn_id is in _dirty". The reverse order let a caller that
        # waited on _conns write a dirty mark into a dict that did not have the
        # key yet -- the mark went nowhere and the row was never flushed.
        self._dirty.setdefault(conn_id, set())
        self._conns[conn_id] = split_by_feed(symbols)
        self._rebuild_pending = True

    def unregister(self, conn_id: str) -> None:
        self._conns.pop(conn_id, None)
        self._dirty.pop(conn_id, None)
        self._rebuild_pending = True

    def pop_dirty(self, conn_id: str) -> set[str]:
        dirty = self._dirty.get(conn_id)
        if not dirty:
            return set()
        out = set(dirty)
        dirty.clear()
        return out

    def status(self) -> dict:
        now = self._clock()
        return {
            feed: {
                "symbols": sorted(self._active.get(feed, frozenset())),
                "running": bool(getattr(self._clients.get(feed), "running", False)),
                # Age of the last buffer activity -- the honest liveness
                # signal; `running` is only the SDK's connection flag.
                "last_tick_age_s": (
                    round(now - self._last_tick[feed], 1) if feed in self._last_tick else None
                ),
            }
            for feed in FEEDS
        }

    def _union(self, feed: str) -> frozenset[str]:
        out: set[str] = set()
        for by_feed in self._conns.values():
            out |= by_feed[feed]
        return frozenset(out)

    # -- EODHD client lifecycle -------------------------------------------------
    async def _sync_feeds(self) -> None:
        """Rebuild any feed whose wanted symbol set changed (the SDK fixes
        symbols at construction, so a change means stop + new client)."""
        for feed in FEEDS:
            want = self._union(feed)
            if want == self._active.get(feed, frozenset()):
                continue
            client = self._clients.pop(feed, None)
            if client is not None:
                # stop() joins reader threads -- do it off the event loop.
                await asyncio.to_thread(_stop_quietly, client)
            if want:
                try:
                    # Construct on the event-loop (main) thread: see _build_client.
                    new = self._client_factory(self._api_key, feed, sorted(want))
                    new.start()
                    self._clients[feed] = new
                    # Fresh client, fresh grace period for the liveness check.
                    self._last_tick[feed] = self._clock()
                except Exception as exc:  # noqa: BLE001 -- feed stays down, app stays up
                    log.warning(
                        "failed to start EODHD %s feed for %s: %s", feed, sorted(want), exc
                    )
                    want = frozenset()
            self._active[feed] = want

    def _drain_all(self) -> None:
        """Parse new SDK buffer entries, apply to quotes, mark subscribers dirty."""
        for feed, client in list(self._clients.items()):
            buf = getattr(client, "data_list", None)
            if not buf:
                continue
            # SDK thread appends at the tail; only we delete from the head.
            n = len(buf)
            raw = buf[:n]
            del buf[:n]
            # Buffer activity is connection-level liveness, parseable or not.
            self._last_tick[feed] = self._clock()
            for item in raw:
                try:
                    msg = json.loads(item) if isinstance(item, str) else item
                except (TypeError, ValueError):
                    continue
                if not isinstance(msg, dict):
                    continue
                sym = self.quotes.apply_tick(feed, msg)
                if not sym:
                    continue
                for conn_id, by_feed in self._conns.items():
                    if sym in by_feed[feed]:
                        self._dirty[conn_id].add(sym)

    async def _check_liveness(self, when: datetime | None = None) -> None:
        """Detect a zombie feed: connected, 'running', delivering nothing.

        Observed 2026-09-01: a second consumer on the same EODHD key left the
        us feed connected but silent for ~20 hours while status() reported
        running=True. The SDK's flag also survives its own "Max reconnect
        attempts reached" give-up. A feed with no buffer activity for
        STALE_AFTER during expected market activity is stopped and dropped
        here directly -- _sync_feeds is the only other place that mutates
        _active, and it no-ops when `want` already equals `_active` (e.g. the
        feed's last subscriber unregistered between detection and the next
        sync), which would otherwise strand the zombie client in _clients
        forever. Dropping it here means _sync_feeds sees the feed as absent
        and rebuilds only if there is still a non-empty `want`. Resetting
        _last_tick bounds the churn to one rebuild per STALE_AFTER even if
        the feed stays silent.
        """
        now = self._clock()
        if now - self._last_liveness < LIVENESS_INTERVAL:
            return
        self._last_liveness = now
        for feed in list(self._clients):
            if not _expect_ticks(feed, when):
                continue
            age = now - self._last_tick.get(feed, now)
            if age < STALE_AFTER:
                continue
            log.warning(
                "%s feed silent for %.0fs during expected activity -- rebuilding", feed, age
            )
            client = self._clients.pop(feed, None)
            if client is not None:
                # stop() joins the SDK's reader thread -- offload it like
                # _sync_feeds does. A wedged SDK is exactly the case this
                # feature exists for; blocking the event loop here would
                # stall tick draining and syncs for every other feed.
                await asyncio.to_thread(_stop_quietly, client)
            self._active.pop(feed, None)
            self._last_tick[feed] = now
            self._rebuild_pending = True

    async def stop_all(self) -> None:
        for feed in list(self._clients):
            client = self._clients.pop(feed)
            await asyncio.to_thread(_stop_quietly, client)
        self._active = {}

    async def run(self) -> None:
        """Drain ticks and apply pending subscription changes until cancelled."""
        try:
            while True:
                await asyncio.sleep(self._drain_interval)
                if self._rebuild_pending:
                    now = asyncio.get_running_loop().time()
                    if now - self._last_rebuild >= self._rebuild_delay:
                        self._rebuild_pending = False
                        self._last_rebuild = now
                        await self._sync_feeds()
                self._drain_all()
                await self._check_liveness()
                if self.recorder is not None:
                    try:
                        # to_thread keeps the event loop free; the recorder's store
                        # marshals the actual PyKX call onto its own owner thread.
                        await asyncio.to_thread(self.recorder.flush)
                        now = asyncio.get_running_loop().time()
                        if now - self._last_prune >= PRUNE_INTERVAL:
                            self._last_prune = now
                            await asyncio.to_thread(self.recorder.prune)
                    except Exception:  # noqa: BLE001 - recording must never break the feed
                        log.debug("tick recorder flush/prune failed", exc_info=True)
        finally:
            await self.stop_all()

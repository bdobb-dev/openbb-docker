"""Symbol-keyed TTL leases on the live feed.

A feed otherwise exists only while a `/live_grid_ws` client is connected, so a
caller that wants a symbol fed after its request returns has nothing to hold.
A lease is an ordinary registration under a synthetic connection id --
`_sync_feeds` unions symbols across every `_conns` entry and does not care
which of them came from a websocket -- plus an expiry the sweeper enforces.

Keyed by symbol rather than by a token because the caller is stateless per
request: it has no handle to present on the next call, so renewal has to be
addressable by the only thing it does know.
"""

import logging

log = logging.getLogger(__name__)

DEFAULT_TTL = 300.0


class LeaseRegistry:
    """symbol -> expiry, backed by FeedManager registrations."""

    def __init__(self, manager, ttl: float = DEFAULT_TTL):
        self._manager = manager
        self._ttl = ttl
        self._expiry: dict[str, float] = {}

    @staticmethod
    def _conn_id(symbol: str) -> str:
        return f"lease:{symbol}"

    def __len__(self) -> int:
        """Count of symbols currently leased, for /health."""
        return len(self._expiry)

    def symbols(self) -> list[str]:
        """The leased symbols, sorted.

        The subscription budget needs these by name, not merely counted: a symbol
        that is both pinned and leased is ONE subscription at the vendor, and only
        the names can tell you that.
        """
        return sorted(self._expiry)

    def renew(self, symbols, now: float, ttl: float | None = None) -> dict[str, float]:
        """Create or extend a lease per symbol. Returns symbol -> expiry."""
        span = self._ttl if ttl is None else ttl
        out = {}
        for raw in symbols:
            sym = str(raw).strip().upper()
            if not sym:
                continue
            if sym not in self._expiry:
                # Registering an already-leased symbol would be harmless but
                # sets _rebuild_pending, and a rebuild stops and reconstructs
                # the whole feed. Renewal must not pay that.
                self._manager.register(self._conn_id(sym), [sym])
            self._expiry[sym] = now + span
            out[sym] = self._expiry[sym]
        return out

    def sweep(self, now: float) -> list[str]:
        """Unregister every lapsed lease. Returns the symbols dropped."""
        dead = [s for s, exp in self._expiry.items() if exp <= now]
        for sym in dead:
            del self._expiry[sym]
            try:
                self._manager.unregister(self._conn_id(sym))
            except Exception:  # noqa: BLE001 - a sweep must never kill its loop
                log.warning("failed to unregister lease for %s", sym, exc_info=True)
        return dead

# kdb live quote provider — design

`/equity/price/quote?provider=kdb` serves the newest live tick from the kdb
tick store, and leases a live EODHD subscription for any symbol that is not
already being fed.

## Why

`/equity/price/quote` currently offers `fmp`, `intrinio` and `yfinance`. On
2026-08-26 `yfinance` returned zero rows for AAPL because Yahoo answers this
deployment's egress with HTTP 429; `fmp` worked. Meanwhile the stack already
carries a real-time EODHD feed — live-grid streams ticks over a websocket and
`TickRecorder` writes them into the same kdb instance the API talks to — and
none of that is reachable from the quote route.

`eodhd` is not an option on that route: OpenBB wires it into
`equity/price/historical` only. So the live data this stack already pays for
and already stores cannot answer a quote. This provider closes that gap.

## Decisions

**D1. A cold quote waits briefly for a first tick, then falls back.**
The fetcher leases the symbol, polls kdb for a first tick up to a deadline
(default 3s), and answers from EODHD's REST snapshot if nothing arrives.
Rejected: returning empty and letting the caller retry — that is precisely
what the broken `yfinance` path does today, and it reads as a fault.

**D2. Subscriptions are TTL leases over HTTP, not websocket-lifetime.**
live-grid gains `POST /subscribe`. Today a feed exists only while a
`/live_grid_ws` client is connected, so a provider acting as a websocket
client would drop the feed the moment the quote returned — every quote would
pay cold start again and the symbol would never actually stay live. A lease
outlives the request, so the second quote for a symbol is served from kdb.
Rejected: a wanted-symbols table in kdb polled by live-grid (makes the store a
coordination channel and adds poll latency), and the provider opening its own
EODHD websocket (duplicates the feed and recorder, and risks two subscriptions
against one connection limit).

**D3. The provider never asks whether a symbol is live; it always leases.**
The lease is idempotent — leasing a subscribed symbol renews its TTL. This
removes the liveness question rather than answering it. Tick recency cannot
answer it correctly anyway: a liquid name ticks constantly, but an illiquid or
halted one goes minutes without a tick while perfectly well subscribed, so a
recency threshold reads "quiet" as "dead" and re-subscribes it repeatedly. As
a side effect every quote renews the symbol, so hot symbols stay hot.

**D4. A lease is keyed by symbol, and is an ordinary registration.**
Keyed by symbol, not by an opaque token: the fetcher is stateless per request
and would never have a token to present on the next call, so token-based
renewal could not deliver the idempotence D3 depends on. `POST /subscribe`
with a symbol already leased simply extends that symbol's expiry.

`_sync_feeds()` rebuilds each feed from `self._union(feed)`, the union of
symbols across all `_conns` entries; a websocket connection is merely one such
entry. So each leased symbol registers under its own synthetic id,
`manager.register(f"lease:{symbol}", [symbol])`, and the sweeper calls the
existing `manager.unregister(f"lease:{symbol}")`. Per-symbol ids rather than one
shared lease id, so symbols expire independently instead of a single renewal
holding every symbol ever leased. No change to feed logic, and `register()`
already sets `_rebuild_pending` so the feed picks the symbol up on the next
drain cycle. The new code is a lease registry (`symbol -> expiry`) and a
sweeper.

**D5. Session fields come from kdb daily bars, not from EODHD REST.**
`last_price` and its timestamp come from the newest tick; `prev_close` and
`change` come from the last complete daily bar via the existing
`ReadThroughCache`. Everything stays inside kdb, and no EODHD REST call sits on
the hot path.

The accepted cost: daily bars are end-of-day, so **during a live session
today's `open`, `high`, `low` and `volume` are null** — they do not exist in a
daily bar until the close. Intraday the payload is a live price, a previous
close and a change; after the close it fills in completely. Rejected: overlaying
EODHD's delayed snapshot, whose ~16-minute-old high/low can contradict a live
last price that has just broken the range.

`kdb_store.aggregate.aggregate_ticks` could derive intraday OHLC from stored
ticks later, bounded to the leased window — a symbol first leased at 11:00 has
no true session open. That is an addition, not a redesign, and is out of scope
here.

## Data flow

    GET /equity/price/quote?symbol=AAPL&provider=kdb
      |
      +-- 1. POST http://127.0.0.1:6903/subscribe {symbols:[AAPL], ttl:300}
      |        idempotent: creates or renews the lease
      |
      +-- 2. newest tick for AAPL from kdb
      |        hit  -> use it
      |        miss -> poll to deadline (3s)
      |        none -> GET http://127.0.0.1:6903/snapshot?symbol=AAPL,
      |                 live-grid's delayed EODHD REST snapshot, flagged delayed
      |
      +-- 3. prev_close from the last complete daily bar (ReadThroughCache)

## Components

| unit | responsibility | depends on |
|---|---|---|
| `live-grid` `POST /subscribe` | lease registry `symbol -> expiry`; sweeper unregisters lapsed leases | `FeedManager.register` / `.unregister` |
| `openbb_kdb/models/quote.py` | `KdbEquityQuoteFetcher`: lease, read tick, assemble `EquityQuote` | lease client, tick read, `ReadThroughCache` |
| tick read | newest tick for a symbol | the `time, sym, price, size` table `TickRecorder` writes |

`KdbEquityQuoteFetcher` registers as `"EquityQuote"` in `openbb_kdb`'s
`fetcher_dict`; that registration is what makes `provider=kdb` appear on the
route.

## Interfaces

    POST /subscribe
      body    {"symbols": ["AAPL", "MSFT"], "ttl": 300}
      200     {"leases": {"AAPL": "<iso8601>", "MSFT": "<iso8601>"}}

    Idempotent and keyed by symbol: posting a symbol that is already leased
    extends its expiry. No token to hold, which is what lets a stateless
    fetcher renew on every quote.

## Lease lifecycle

Default TTL 300s, renewed by every quote, so a symbol queried more often than
every five minutes never falls off. The sweeper runs every 30s. All three
values are configurable.

`register()` also creates `_dirty[f"lease:{symbol}"]`, which nothing drains
because a lease has no websocket consumer. Each holds at most the one symbol it
was registered for, so it cannot grow; the sweeper's `unregister` reclaims it.

**The cost of a cold lease.** `_sync_feeds` rebuilds a feed by stopping the
client and constructing a new one, because the SDK fixes symbols at
construction. Leasing a cold symbol therefore restarts the whole US feed and
briefly interrupts ticks for every other symbol on it. The existing
`_rebuild_pending` + `rebuild_delay` coalescing means ten quotes in a second
cause one rebuild, but a steady trickle of one-off cold quotes means a steady
trickle of restarts. This is inherent to the SDK, and it is why the TTL is
generous rather than tight.

## Error handling

The fallback (`GET /snapshot`) is served *by live-grid*, not called directly
against EODHD by `openbb-kdb` — so "live-grid unreachable" means no lease
**and** no snapshot, not "skip the lease and fall back to EODHD REST" as an
earlier draft of this table said.

| failure | behaviour |
|---|---|
| live-grid unreachable | no lease taken, no snapshot available (it also goes through live-grid); the quote returns whatever tick kdb already holds, or nothing |
| kdb unreachable (tick read) | no tick; fall back to live-grid's `/snapshot`, flagged delayed |
| no tick within the deadline | fall back to live-grid's `/snapshot`, flagged delayed |
| live-grid's own snapshot lookup also fails/404s | empty results |
| daily bar missing | `last_price` and timestamp only; `prev_close`/`change` null |

The invariant: nothing in the subscribe leg can fail a quote. The only case
returning nothing is no tick **and** no snapshot — whether because live-grid
itself is unreachable or because live-grid reached EODHD and still came up
empty.

## Security

`POST /subscribe` is a state-changing endpoint on a service with no
authentication, so it deserves an explicit answer rather than an assumption. It
grants no capability that does not already exist: any tailnet client can
already cause EODHD subscriptions by opening `/live_grid_ws`. This is the same
power in a different shape.

It stays tailnet-only and must never be funnelled — the same posture
`docker-compose.yml` already records for `:6903`. See `SECURITY.md` for why the
funnelled API is the one that carries auth.

## Testing

**live-grid** — a lease creates a registration; re-posting a leased symbol
extends its expiry instead of adding a second registration; the sweeper
unregisters on expiry; expiry removes that symbol from `_union` while other
leases survive; an unreachable kdb does not break the endpoint. Against a fake
feed client, as the existing suite does.

**openbb-kdb** — the fetcher leases then reads; the deadline path falls back;
live-grid being down still returns a quote; a tick newer than the bar wins for
`last_price`; field mapping onto `EquityQuote`.

**Integration** — one network-gated test against real EODHD, following the
existing parity-test convention.

## Out of scope

Intraday OHLC aggregated from ticks (D5). Quote support for crypto, currency,
ETF or index — equities only. Authentication on `/subscribe`, which would be a
change to live-grid's whole posture, not to this feature.

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Route watchlist symbols to EODHD websocket feeds and REST tickers."""

FEEDS = ("us", "crypto", "forex")

# Currency codes recognized on EODHD's forex feed (both halves of a pair).
_CCY = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "SEK", "NOK",
    "DKK", "PLN", "CZK", "HUF", "TRY", "ZAR", "MXN", "SGD", "HKD", "CNH",
    "ILS", "THB", "KRW", "INR", "BRL",
}

_SNAPSHOT_SUFFIX = {"us": ".US", "crypto": ".CC", "forex": ".FOREX"}

# What a hyphenated symbol must END with to be crypto. "Contains a hyphen"
# was too loose: BRK-B and BF-B are ordinary US equities, and routing them to
# the crypto feed subscribes a symbol that never ticks -- silently, and
# forever, because a US symbol absent from the crypto feed is not an error.
_CRYPTO_QUOTES = {"USD", "USDT", "USDC", "EUR", "BTC", "ETH", "GBP", "JPY"}

# An explicitly exchange-qualified symbol says where it lives. EODHD's
# websockets carry only these three; anything else (TALA.TO on Toronto, say)
# has no feed here and must NOT fall through to "us".
_FEED_BY_SUFFIX = {"US": "us", "CC": "crypto", "FOREX": "forex"}

#: Routable nowhere on this deployment. Never a feed key -- callers handle it.
UNSUPPORTED = "unsupported"


def classify(symbol: str) -> str:
    """Feed for a watchlist symbol: 'crypto' | 'forex' | 'us' | 'unsupported'.

    A bare ticker that is neither crypto nor forex is assumed to be US. That
    is the only sane default and also the limit of what a string can tell you:
    TALA is a four-letter ticker on Toronto, indistinguishable from a US one.
    Qualify it (TALA.TO) and this answers 'unsupported' rather than silently
    subscribing TALA.US, which never ticks.
    """
    s = symbol.strip().upper()
    if "." in s:
        return _FEED_BY_SUFFIX.get(s.rsplit(".", 1)[1], UNSUPPORTED)
    if "-" in s and s.rsplit("-", 1)[1] in _CRYPTO_QUOTES:
        return "crypto"
    if len(s) == 6 and s[:3] in _CCY and s[3:] in _CCY:
        return "forex"
    return "us"


def split_by_feed(symbols: list[str]) -> dict[str, set[str]]:
    """Group cleaned (upper-cased, de-duplicated) symbols by feed; all feeds present."""
    out: dict[str, set[str]] = {feed: set() for feed in FEEDS}
    for raw in symbols:
        s = str(raw).strip().upper()
        if s and classify(s) in out:
            out[classify(s)].add(s)
    return out


def unsupported(symbols: list[str]) -> list[str]:
    """The symbols split_by_feed dropped, so a caller can say so out loud.

    Left out rather than routed to `us`: subscribing them looks healthy,
    because the US feed expects ticks and a symbol it does not carry reads as
    a quiet one, not a broken one.
    """
    return sorted({
        str(r).strip().upper() for r in symbols
        if str(r).strip() and classify(str(r)) == UNSUPPORTED
    })


def snapshot_ticker(symbol: str) -> str:
    """Exchange-qualified ticker for the REST /real-time snapshot (AAPL -> AAPL.US).

    An already-qualified symbol comes back unchanged: appending a second
    suffix produced TALA.TO.US, which EODHD 404s.
    """
    s = symbol.strip().upper()
    feed = classify(s)
    if feed == UNSUPPORTED or "." in s:
        return s
    return s + _SNAPSHOT_SUFFIX[feed]

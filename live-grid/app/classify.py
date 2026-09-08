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


def classify(symbol: str) -> str:
    """Feed for a bare watchlist symbol: 'crypto' | 'forex' | 'us'."""
    s = symbol.strip().upper()
    if "-" in s:
        return "crypto"
    if len(s) == 6 and s[:3] in _CCY and s[3:] in _CCY:
        return "forex"
    return "us"


def split_by_feed(symbols: list[str]) -> dict[str, set[str]]:
    """Group cleaned (upper-cased, de-duplicated) symbols by feed; all feeds present."""
    out: dict[str, set[str]] = {feed: set() for feed in FEEDS}
    for raw in symbols:
        s = str(raw).strip().upper()
        if s:
            out[classify(s)].add(s)
    return out


def snapshot_ticker(symbol: str) -> str:
    """Exchange-qualified ticker for the REST /real-time snapshot (AAPL -> AAPL.US)."""
    s = symbol.strip().upper()
    return s + _SNAPSHOT_SUFFIX[classify(s)]

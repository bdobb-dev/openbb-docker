# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Client for the OpenBB API on loopback.

Going through the real API (rather than importing the provider) means the demo
exercises exactly the path any other client takes -- including the cache
metadata the Platform attaches to every response.
"""

import os

OPENBB_URL = os.getenv("OPENBB_URL", "http://127.0.0.1:6900")
USERNAME = os.getenv("OPENBB_API_USERNAME", "")
PASSWORD = os.getenv("OPENBB_API_PASSWORD", "")

_UNKNOWN = {
    "cache": "unknown", "rows_from_cache": 0, "rows_from_upstream": 0,
    "gaps_fetched": 0, "upstream_ms": 0.0, "kdb_ms": 0.0,
}

# Same shape rules the live feeds use (classify.py). Routing everything
# through equity/price/historical sent BTC-USD upstream as BTC-USD.US,
# which EODHD 404s -- each asset class has its own Platform route, and the
# kdb provider registers a fetcher for all three.
_HISTORICAL_PATH = {
    "us": "equity/price/historical",
    "crypto": "crypto/price/historical",
    "forex": "currency/price/historical",
}


def history_endpoint(symbol: str) -> str:
    """The Platform route for this symbol's asset class.

    Raises for a symbol no feed here carries (TALA.TO), rather than
    KeyError-ing on the lookup -- the caller can then say which symbol.
    """
    from app.classify import UNSUPPORTED, classify

    feed = classify(symbol)
    if feed == UNSUPPORTED:
        raise ValueError(
            f"no historical route for {symbol!r}: its exchange has no feed here"
        )
    return _HISTORICAL_PATH[feed]


async def fetch_series(
    symbol: str, interval: str, start: str, end: str, provider: str = "kdb"
) -> tuple[list[dict], dict]:
    """Return (bars, cache_metadata) for one window."""
    import httpx

    params = {
        "symbol": symbol, "provider": provider, "interval": interval,
        "start_date": start, "end_date": end,
    }
    auth = (USERNAME, PASSWORD) if USERNAME else None
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            f"{OPENBB_URL}/api/v1/{history_endpoint(symbol)}", params=params, auth=auth
        )
        response.raise_for_status()
        payload = response.json()
    bars = payload.get("results") or []
    meta = (payload.get("extra") or {}).get("results_metadata") or dict(_UNKNOWN)
    return bars, meta

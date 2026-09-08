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
            f"{OPENBB_URL}/api/v1/equity/price/historical", params=params, auth=auth
        )
        response.raise_for_status()
        payload = response.json()
    bars = payload.get("results") or []
    meta = (payload.get("extra") or {}).get("results_metadata") or dict(_UNKNOWN)
    return bars, meta

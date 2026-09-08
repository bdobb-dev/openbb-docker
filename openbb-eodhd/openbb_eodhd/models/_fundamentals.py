# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Shared EODHD /fundamentals fetch with a single-flight TTL cache.

OpenBB invokes each widget's fetcher independently, so a dashboard of N
fundamentals widgets for one symbol would otherwise fire N identical
/fundamentals calls (EODHD bills each ~10 credits). Every fundamentals-derived
fetcher reads sections of ONE payload obtained here; concurrent and repeat
requests for the same symbol within the TTL window share a single HTTP call.
"""

from __future__ import annotations

import json
import time
from asyncio import Lock, to_thread
from datetime import datetime, timezone
from typing import Any

from openbb_core.app.model.abstract.error import OpenBBError
from openbb_core.provider.utils.errors import EmptyDataError, UnauthorizedError

from openbb_eodhd.models._client import get_client, raise_sdk_error

_TTL_SECONDS = 120.0            # in-process burst-coalescing TTL
_cache: dict[str, tuple[float, dict]] = {}
_locks: dict[str, Lock] = {}


def qualify(symbol: str, exchange: str = "US") -> str:
    s = symbol.strip().upper()
    return s if "." in s else f"{s}.{exchange.upper()}"


def _reset_cache_for_tests() -> None:
    _cache.clear()
    _locks.clear()


def _get_lock(sym: str) -> Lock:
    lock = _locks.get(sym)
    if lock is None:
        lock = _locks[sym] = Lock()
    return lock


def _cached(sym: str) -> dict | None:
    hit = _cache.get(sym)
    if hit is None:
        return None
    expiry, payload = hit
    if time.monotonic() >= expiry:
        _cache.pop(sym, None)
        return None
    return payload


def _fetch_sync(sym: str, credentials: dict[str, str] | None) -> dict:
    client = get_client(credentials)
    try:
        with client:
            response = client.get_fundamentals_data(sym)
    except (OpenBBError, UnauthorizedError):
        raise
    except Exception as exc:  # noqa: BLE001 - mapped below
        raise_sdk_error(exc, f"fundamentals for '{sym}'")
    if not isinstance(response, dict) or not response:
        raise EmptyDataError(f"EODHD returned no fundamentals for '{sym}'.")
    return response


async def get_bundle(
    symbol: str, exchange: str, credentials: dict[str, str] | None
) -> dict:
    sym = qualify(symbol, exchange)
    cached = _cached(sym)
    if cached is not None:
        return cached
    async with _get_lock(sym):
        cached = _cached(sym)  # another coroutine may have filled it while we waited
        if cached is not None:
            return cached
        payload = await to_thread(_fetch_sync, sym, credentials)
        now = time.monotonic()
        for k in [k for k, (exp, _) in list(_cache.items()) if exp <= now]:
            _cache.pop(k, None)
        _cache[sym] = (now + _TTL_SECONDS, payload)
        return payload


def _rows(section: Any) -> list[dict]:
    if isinstance(section, dict):
        return [v for v in section.values() if isinstance(v, dict)]
    if isinstance(section, list):
        return [v for v in section if isinstance(v, dict)]
    return []


def general(b: dict) -> dict:
    return b.get("General") or {}


def highlights(b: dict) -> dict:
    return b.get("Highlights") or {}


def valuation(b: dict) -> dict:
    return b.get("Valuation") or {}


def shares_stats(b: dict) -> dict:
    return b.get("SharesStats") or {}


def analyst_ratings(b: dict) -> dict:
    return b.get("AnalystRatings") or {}


def esg(b: dict) -> dict:
    return b.get("ESGScores") or {}


def etf_data(b: dict) -> dict:
    return b.get("ETF_Data") or {}


def holders_institutions(b: dict) -> list[dict]:
    return _rows((b.get("Holders") or {}).get("Institutions"))


def holders_funds(b: dict) -> list[dict]:
    return _rows((b.get("Holders") or {}).get("Funds"))


def earnings_history(b: dict) -> list[dict]:
    return _rows((b.get("Earnings") or {}).get("History"))


def earnings_trend(b: dict) -> list[dict]:
    return _rows((b.get("Earnings") or {}).get("Trend"))

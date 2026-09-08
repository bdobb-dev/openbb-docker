#!/usr/bin/env python3
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Entitlement smoke for a REAL (non-demo) EODHD key.

Verifies what the public demo key cannot: that YOUR key's plan serves
websockets and non-demo symbols. Resolves the key from, in order:

    1. $EODHD_API_KEY
    2. live-grid/.env.local
    3. ../credentials.env

Exits 0 with a skip notice when no key beyond the demo key is available, so
it is always safe to run. Exits 1 when a real key is present but not
entitled (e.g. free tier -> websocket 403).

    .venv/bin/python scripts/smoke_live.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from smoke import DEMO_WS_KEY  # noqa: E402
from app.feeds import FeedManager  # noqa: E402
from app.quotes import QuoteTable  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_KEY_FILES = [
    os.path.join(_HERE, "..", ".env.local"),
    os.path.join(_HERE, "..", "..", "credentials.env"),
]

# Deliberately OUTSIDE the demo symbol set (AAPL/MSFT/TSLA, BTC/ETH-USD,
# EURUSD) — proving the key serves more than the demo entitlement. SOL-USD is
# the hard requirement: crypto trades 24/7, so it must tick at any hour.
SYMBOLS = ["NVDA", "SPY", "SOL-USD", "GBPUSD"]
REQUIRED = "SOL-USD"


def _key_from_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("EODHD_API_KEY="):
                    value = line.split("=", 1)[1]
                    # Drop inline "# ..." comments (shell-style env files).
                    return value.split("#", 1)[0].strip().strip("'\"")
    except OSError:
        pass
    return ""


def resolve_key() -> tuple[str, str]:
    """Return (key, source-description)."""
    key = os.environ.get("EODHD_API_KEY", "").strip()
    if key:
        return key, "$EODHD_API_KEY"
    for path in _KEY_FILES:
        key = _key_from_file(path)
        if key:
            return key, os.path.relpath(path, os.path.join(_HERE, ".."))
    return "", ""


async def ws_check(key: str) -> list[str]:
    quotes = QuoteTable()
    manager = FeedManager(key, quotes, rebuild_delay=0.0)
    task = asyncio.create_task(manager.run())
    manager.register("smoke-live", SYMBOLS)
    await asyncio.sleep(12)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    ticked = []
    for sym in SYMBOLS:
        row = quotes.rows.get(sym)
        ok = row is not None and row["price"] is not None
        print(f"  WS   {sym:8} {'TICKED ' + str(row['price']) if ok else 'no ticks'}")
        if ok:
            ticked.append(sym)
    return ticked


def main() -> int:
    key, source = resolve_key()
    if not key or key in ("demo", DEMO_WS_KEY):
        print("SKIP: no key beyond the demo key available "
              "(set EODHD_API_KEY or fill live-grid/.env.local).")
        return 0

    # pylint: disable=import-outside-toplevel
    from eodhd import APIClient

    print(f"Using key from {source}")
    with APIClient(key) as client:
        info = client.get_user_info() or {}
        tier = str(info.get("subscriptionType", "unknown"))
        print(f"  account tier: {tier}")
        if tier.lower() == "free":
            print("FAIL: free-tier key — EODHD websockets require a plan with"
                  " real-time access (websocket connects will 403).")
            return 1
        # Non-demo REST snapshot (the demo key cannot serve NVDA).
        snap = client.get_live_stock_prices(ticker="NVDA.US")
        snap = snap[0] if isinstance(snap, list) else snap
        print(f"  REST NVDA.US  close={snap.get('close')}"
              f" prevClose={snap.get('previousClose')}")

    ticked = asyncio.run(ws_check(key))
    print(f"{len(ticked)}/{len(SYMBOLS)} symbols ticked: {ticked}")
    if REQUIRED not in ticked:
        print(f"FAIL: {REQUIRED} did not tick — crypto streams 24/7, so a live"
              " entitled key should always receive it.")
        return 1
    print("PASS: live key is websocket-entitled.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

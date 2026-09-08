#!/usr/bin/env python3
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Smoke-test every openbb-eodhd command against EODHD's PUBLIC demo token.

The `demo` token is public and serves — with NO limits, all data types — exactly
these symbols: AAPL.US, TSLA.US, VTI.US, AMZN.US, BTC-USD.CC, EURUSD.FOREX. That
covers equity, ETF, crypto and forex bars, a multi-symbol request, and the
fundamentals/corporate-action commands. NO private API key is used or required.

    python smoke.py            # uses the public 'demo' token
    EODHD_API_KEY=xxx python smoke.py   # or your own token (optional)

Exits non-zero if any command fails.
"""
import os
import sys
import warnings

warnings.filterwarnings("ignore")
# Public demo token — safe to commit. Override via the environment if desired.
os.environ.setdefault("EODHD_API_KEY", "demo")


def main() -> int:
    from openbb import obb

    token = os.environ["EODHD_API_KEY"]
    print(f"Using EODHD token: {token!r}\n")
    checks: list[tuple[str, int]] = []

    def run(label, fn):
        try:
            n = len(fn().results)
            print(f"  PASS  {label:34} {n} rows")
            checks.append((label, n))
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {label:34} {type(e).__name__}: {str(e).splitlines()[-1][:70]}")
            checks.append((label, -1))

    P = dict(provider="eodhd")
    run("equity.price.historical (1d)",
        lambda: obb.equity.price.historical("AAPL", interval="1d",
                start_date="2024-02-12", end_date="2024-02-16", **P))
    run("equity.price.historical (5m)",
        lambda: obb.equity.price.historical("TSLA", interval="5m", **P))
    run("equity.price.historical (multi)",
        lambda: obb.equity.price.historical("AAPL,TSLA,AMZN", interval="1d",
                start_date="2024-02-12", end_date="2024-02-16", **P))
    # EODHD serves ETFs through the same equity /eod endpoint (no separate ETF
    # fetcher), so VTI is queried via equity.price.historical.
    run("equity.price.historical (VTI etf)",
        lambda: obb.equity.price.historical("VTI", interval="1d",
                start_date="2024-02-12", end_date="2024-02-16", **P))
    run("crypto.price.historical",
        lambda: obb.crypto.price.historical("BTC-USD", interval="1d",
                start_date="2024-02-12", end_date="2024-02-16", **P))
    run("currency.price.historical",
        lambda: obb.currency.price.historical("EURUSD", interval="1d",
                start_date="2024-02-12", end_date="2024-02-16", **P))
    run("equity.fundamental.income",
        lambda: obb.equity.fundamental.income("AAPL", limit=3, **P))
    run("equity.fundamental.balance",
        lambda: obb.equity.fundamental.balance("AAPL", limit=3, **P))
    run("equity.fundamental.cash",
        lambda: obb.equity.fundamental.cash("AAPL", limit=3, **P))
    run("equity.fundamental.dividends",
        lambda: obb.equity.fundamental.dividends("AAPL",
                start_date="2023-01-01", end_date="2023-12-31", **P))
    run("equity.fundamental.historical_splits",
        lambda: obb.equity.fundamental.historical_splits("AAPL", **P))

    failed = [c for c, n in checks if n < 1]
    print(f"\n{'PASS' if not failed else 'FAIL'}: {len(checks) - len(failed)}/{len(checks)} commands ok")
    if failed:
        print("  failed:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

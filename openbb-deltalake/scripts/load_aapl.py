#!/usr/bin/env python3
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Load N days of daily OHLCV into a Delta Lake library and verify the round-trip.

Downloads daily bars from yfinance and writes them to a Delta library via the
openbb-deltalake integration, then reads them back through both the generic
store and the `provider="deltalake"` path.

Usage:
    python load_aapl.py [--symbol AAPL] [--days 30] [--library openbb] [--uri ...]

Env fallbacks: DELTA_URI, DELTA_LIBRARY (CLI args take precedence).
Defaults to a local Delta store under OPENBB_HOME (no server needed).
"""
# Requires: pip install openbb[yfinance] openbb-deltalake
import argparse
import os
import sys
import warnings
from datetime import date, timedelta

warnings.filterwarnings("ignore")


def main() -> int:
    p = argparse.ArgumentParser(description="Load daily OHLCV into a Delta library and verify.")
    p.add_argument("--symbol", default="AAPL")
    p.add_argument("--days", type=int, default=30, help="calendar days of history")
    p.add_argument("--library", default=os.getenv("DELTA_LIBRARY", "openbb"))
    p.add_argument("--uri", default=os.getenv("DELTA_URI"))  # None -> default local store
    args = p.parse_args()

    from openbb import obb
    from openbb_deltalake import store

    start = (date.today() - timedelta(days=args.days)).isoformat()
    print(f"[1/4] Downloading {args.symbol} daily OHLCV since {start} (yfinance)...")
    src = obb.equity.price.historical(args.symbol, provider="yfinance", start_date=start)
    n = len(src.results)
    if n == 0:
        print("  ERROR: yfinance returned no rows.", file=sys.stderr)
        return 1
    print(f"  got {n} rows: {src.results[0].date} -> {src.results[-1].date}")

    print(f"[2/4] Writing to Delta library '{args.library}'"
          + (f" @ {args.uri}" if args.uri else " (default local store)") + "...")
    info = src.deltalake.write(args.symbol, library=args.library, uri=args.uri,
                               metadata={"source": "yfinance", "interval": "1d"})
    print(f"  {info}")

    print("[3/4] Reading back via the generic store...")
    s = store(uri=args.uri, library=args.library)
    df = s.read(args.symbol, output="dataframe")
    print(f"  store.read: {len(df)} rows, columns={list(df.columns)}")

    print("[4/4] Reading back via provider='deltalake' (interval=1d)...")
    back = obb.equity.price.historical(
        args.symbol, provider="deltalake", interval="1d",
        uri=args.uri, library=args.library,
    )
    print(f"  provider rows: {len(back.results)}, last close: {back.results[-1].close}")

    ok = len(df) == n and len(back.results) == n
    print(f"\n{'PASS' if ok else 'FAIL'}: wrote {n}, store read {len(df)}, "
          f"provider read {len(back.results)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Aggregating recorded ticks into OHLCV bars.

The aggregation itself runs in q (`xbar`), next to the data. This module only
maps intervals to bucket widths and the returned frame to bar rows.
"""

BUCKET_NS = {
    "1s": 1_000_000_000,
    "1m": 60_000_000_000,
    "5m": 300_000_000_000,
    "15m": 900_000_000_000,
    "30m": 1_800_000_000_000,
    "1h": 3_600_000_000_000,
    "1d": 86_400_000_000_000,
}


def bucket_ns(interval: str) -> int:
    """Bucket width in nanoseconds for an interval q can aggregate."""
    width = BUCKET_NS.get(str(interval).strip())
    if width is None:
        raise ValueError(
            f"Interval {interval!r} cannot be aggregated from ticks. "
            f"Supported: {sorted(BUCKET_NS)}"
        )
    return width


def aggregate_ticks(store, symbol: str, interval: str, start, end) -> list[dict]:
    """OHLCV rows built from the ticks held for `symbol` within [start, end]."""
    import math

    frame = store.aggregate_frame(symbol, interval, start, end)
    if frame is None or getattr(frame, "empty", True):
        return []
    ordered = frame.sort_values("t")
    rows = []
    for row in ordered.itertuples():
        # vwap is the bar's true trade-weighted price (size wavg price in q).
        # A bucket whose ticks all carry size 0 gives q's null (NaN here) --
        # keep it None: "no trade data", never a fabricated value.
        vwap = float(getattr(row, "vwap", float("nan")))
        rows.append({
            "date": row.t,
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
            "vwap": None if math.isnan(vwap) else vwap,
        })
    return rows

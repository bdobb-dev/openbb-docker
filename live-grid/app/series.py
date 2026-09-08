# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Joining tick-derived bars onto cached history.

The seam sits at the first bar boundary FULLY covered by ticks, not at the
first tick. A bar that ticks only partly cover would be missing its own opening
trades -- a wrong candle that looks entirely plausible on a chart -- so the
straddling bar comes from history and ticks own only what they cover whole.
"""

from datetime import datetime, timedelta

EPOCH = datetime(1970, 1, 1)


def window_end(end: str) -> datetime:
    """The last instant the requested window covers.

    Ticks are held for a rolling window that always ends *now*, so without
    clipping, a request for a window that closed months ago
    (`?start=2024-01-01&end=2024-06-01`) gets today's tick-derived bars
    appended to it -- a chart whose right edge is years past its own axis.

    A date-only `end` is INCLUSIVE of that whole day: the historical provider
    returns that day's bar, so treating it as midnight would drop the final
    day's ticks. An unparseable value clips nothing, which is exactly the
    behaviour that existed before this function.
    """
    text = str(end).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.max
    if len(text) == 10:  # YYYY-MM-DD -- a whole day, not its first instant
        return parsed + timedelta(days=1) - timedelta(microseconds=1)
    return parsed


def seam_boundary(first_tick: datetime, interval: str) -> datetime:
    """The first bar boundary at or after `first_tick`."""
    from kdb_store.aggregate import bucket_ns

    width = timedelta(microseconds=bucket_ns(interval) / 1000)
    elapsed = first_tick - EPOCH
    buckets, remainder = divmod(elapsed, width)
    if remainder:
        buckets += 1
    return EPOCH + buckets * width


def tick_capable(interval: str, window: timedelta) -> bool:
    """True when a bar of this interval can fit inside the tick window."""
    from kdb_store.aggregate import bucket_ns

    try:
        width = timedelta(microseconds=bucket_ns(interval) / 1000)
    except ValueError:
        return False
    return width <= window


def _row_dt(value) -> datetime:
    """History rows carry the provider's ISO strings (passed through the
    response untouched, on purpose); tick rows carry datetimes. Compare as
    datetimes without rewriting either on the wire."""
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def stitch(history: list[dict], ticks: list[dict], boundary: datetime) -> list[dict]:
    """History strictly before `boundary`, then tick-derived bars, time-ordered."""
    if not ticks:
        return list(history)
    kept = [row for row in history if _row_dt(row["date"]) < boundary]
    if not kept:
        return sorted(ticks, key=lambda r: _row_dt(r["date"]))
    return sorted(kept + list(ticks), key=lambda r: _row_dt(r["date"]))

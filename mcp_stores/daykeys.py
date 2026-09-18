# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The per-day key grammar, in one place and with no I/O.

Two libraries key their Delta tables by symbol AND day: the EOD dump writes
`ticks_live/AAPL_2026_09_11` (one UTC day per table, re-runnable per day), and
the FirstRate sample is `ticks/goog_quote_2023_05_12`. Everything else is one
table per symbol. The doors present the base symbol (`AAPL`) and expand it to
the day tables a window needs, so a symbol picker shows a symbol once and a
read can cross midnight.

A base that is itself a table (`ticks_sip/AAPL`) expands to itself, whatever
the bounds: the read path filters inside the table, as it always has.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterable

_DAY_SUFFIX = re.compile(r"\A(.+)_(\d{4})_(\d{2})_(\d{2})\Z")


def split(key: str) -> tuple[str, date | None]:
    """(base, day) for a day-keyed table; (key, None) for anything else."""
    m = _DAY_SUFFIX.match(key)
    if not m:
        return key, None
    try:
        return m.group(1), date(int(m.group(2)), int(m.group(3)), int(m.group(4)))
    except ValueError:  # X_2026_13_40 is a name that happens to end in digits
        return key, None


def bases(keys: Iterable[str]) -> list[str]:
    """The symbols a library offers: day suffixes collapsed, sorted, unique."""
    return sorted({split(k)[0] for k in keys})


def _days_of(keys: Iterable[str], base: str) -> list[tuple[date, str]]:
    out = []
    for k in keys:
        b, d = split(k)
        if b == base and d is not None:
            out.append((d, k))
    return sorted(out)


def day_keys(keys: Iterable[str], base: str) -> list[str]:
    """Every table behind `base`, oldest day first. A plain key is itself."""
    keys = list(keys)
    if base in keys:
        return [base]
    return [k for _, k in _days_of(keys, base)]


def _day(bound) -> date | None:
    """The calendar day a bound falls on, or None for an open side.

    Bounds arrive as naive UTC text from the widget; day tables are UTC days
    (the EOD dump flushes one UTC day), so the date of the text IS the table.
    """
    if bound is None or bound == "":
        return None
    if isinstance(bound, datetime):
        return bound.date()
    if isinstance(bound, date):
        return bound
    # Lazy: the tests of this module import nothing from the store package.
    from openbb_deltalake.utils import parse_temporal  # noqa: PLC0415

    parsed = parse_temporal(str(bound))
    return parsed.date() if isinstance(parsed, datetime) else parsed


def in_window(keys: Iterable[str], base: str, start, end) -> list[str]:
    """The tables a read of `base` between `start` and `end` must open.

    No bounds means the newest day only: an unfiltered read of a symbol is
    bounded by construction, which is the guarantee delta_read makes.
    """
    keys = list(keys)
    if base in keys:
        return [base]
    days = _days_of(keys, base)
    if not days:
        return []
    lo, hi = _day(start), _day(end)
    if lo is None and hi is None:
        return [days[-1][1]]
    return [k for d, k in days if (lo is None or d >= lo) and (hi is None or d <= hi)]

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Indicators that are recurrences, not expressions.

Parabolic SAR's value at bar t depends on the extreme point and acceleration
factor carried forward from bar t-1, and on which side of the trend the series
is currently on. There is no vectorised form; this is a loop, and that is fine.

ZigZag, Chandelier Exit and ATR trailing stops join this module in phase 2.
They are the reason the seam exists in v1 rather than later.
"""

from __future__ import annotations

from collections.abc import Callable

import polars as pl


def parabolic_sar(df: pl.DataFrame, params: dict) -> dict[str, list[float | None]]:
    """Wilder's Parabolic SAR over raw OHLC.

    The first bar has no prior state, so it is None -- the same warmup
    convention the rolling indicators use.
    """
    step = float(params["acceleration"])
    cap = float(params["maximum"])
    highs = df["high"].to_list()
    lows = df["low"].to_list()
    if not highs:
        return {"sar": []}

    out: list[float | None] = [None]
    rising = True
    sar = lows[0]
    extreme = highs[0]
    accel = step

    for i in range(1, len(highs)):
        sar = sar + accel * (extreme - sar)
        if rising:
            # SAR may never enter the previous two bars' range.
            sar = min(sar, lows[i - 1], lows[max(i - 2, 0)])
            if lows[i] < sar:
                rising, sar, extreme, accel = False, extreme, lows[i], step
            elif highs[i] > extreme:
                extreme, accel = highs[i], min(accel + step, cap)
        else:
            sar = max(sar, highs[i - 1], highs[max(i - 2, 0)])
            if highs[i] > sar:
                rising, sar, extreme, accel = True, extreme, highs[i], step
            elif lows[i] < extreme:
                extreme, accel = lows[i], min(accel + step, cap)
        out.append(sar)

    return {"sar": out}


def supertrend(df: pl.DataFrame, params: dict) -> dict[str, list[float | None]]:
    """ATR-banded trailing stop that flips side when close crosses the band.

    Reads `tr:raw:0`, which the vectorised pass has already materialised, and
    smooths it with Wilder's alpha = 1/n here rather than declaring an ewm
    Base -- the Base vocabulary keys on a column name, and true range is
    keyed `tr:raw:0` regardless of period.
    """
    period = int(params["period"])
    multiplier = float(params["multiplier"])
    highs = df["high"].to_list()
    lows = df["low"].to_list()
    closes = df["close"].to_list()
    count = len(closes)
    if not count:
        return {"supertrend": [], "st_direction": []}

    true_range = df["tr:raw:0"].to_list()
    atr: list[float | None] = [None] * count
    running = None
    for i, value in enumerate(true_range):
        if value is None:
            continue
        running = value if running is None else running + (value - running) / period
        atr[i] = running

    line: list[float | None] = [None] * count
    direction: list[float | None] = [None] * count
    upper = lower = None
    way = 1.0
    # Bar 0 has no prior close to test the band against (the same "no prior
    # state" warmup SAR uses) -- tr:raw:0 is non-null at bar 0 (it degrades to
    # high - low), so atr[0] is set and this loop must skip it explicitly
    # rather than rely on `atr[i] is None` to do it.
    for i in range(1, count):
        if atr[i] is None:
            continue
        middle = (highs[i] + lows[i]) / 2
        basic_upper = middle + multiplier * atr[i]
        basic_lower = middle - multiplier * atr[i]
        # A band only ever tightens, until price closes through it.
        upper = (basic_upper if upper is None or basic_upper < upper
                 or closes[i - 1] > upper else upper)
        lower = (basic_lower if lower is None or basic_lower > lower
                 or closes[i - 1] < lower else lower)
        if way == 1.0 and closes[i] < lower:
            way = -1.0
        elif way == -1.0 and closes[i] > upper:
            way = 1.0
        line[i] = lower if way == 1.0 else upper
        direction[i] = way
    return {"supertrend": line, "st_direction": direction}


ITERATIVE: dict[str, Callable[[pl.DataFrame, dict], dict[str, list]]] = {
    "sar": parabolic_sar,
    "supertrend": supertrend,
}

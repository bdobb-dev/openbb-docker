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


ITERATIVE: dict[str, Callable[[pl.DataFrame, dict], dict[str, list]]] = {
    "sar": parabolic_sar,
}

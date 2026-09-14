"""Roll ticks into OHLCV bars, and roll those bars up further.

The session filter is the single largest lever on any comparison against a
consolidated reference feed: FirstRate ticks include extended hours, and most
reference bars do not. It is a parameter, never a hidden default.
"""

from __future__ import annotations

from datetime import time

import pandas as pd

EASTERN = "America/New_York"
BAR_COLUMNS = ["open", "high", "low", "close", "volume"]

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)

SESSIONS = ("regular", "all")

# pandas resample rules for the intervals a reference source may hand back.
_RULES = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "1d": "1D",
}

_AGG = {
    "open": ("open", "first"),
    "high": ("high", "max"),
    "low": ("low", "min"),
    "close": ("close", "last"),
    "volume": ("volume", "sum"),
}


class SessionError(ValueError):
    """An unknown --session value."""


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(
        {c: pd.Series(dtype="float64") for c in BAR_COLUMNS},
        index=pd.DatetimeIndex([], tz="UTC", name=None),
    )


def to_minute_bars(trades: pd.DataFrame, session: str = "regular") -> pd.DataFrame:
    """Aggregate trade ticks into 1-minute OHLCV bars.

    `trades` must carry a tz-aware UTC index and `price`/`volume` columns.
    Bars are left-closed and left-labeled: the 09:30 bar covers [09:30, 09:31).
    Minutes with no trades are dropped rather than forward-filled -- an absent
    bar and a flat bar are different facts, and the comparison counts them
    differently.
    """
    if session not in SESSIONS:
        raise SessionError(
            f"unknown session {session!r}; expected one of {', '.join(SESSIONS)}"
        )
    if trades.empty:
        return _empty_bars()

    df = trades
    if session == "regular":
        local = df.index.tz_convert(EASTERN)
        # 09:30 inclusive, 16:00 exclusive -- 390 one-minute bars.
        mask = (local.time >= REGULAR_OPEN) & (local.time < REGULAR_CLOSE)
        df = df[mask]
        if df.empty:
            return _empty_bars()

    bars = df.resample("1min", label="left", closed="left").agg(
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        volume=("volume", "sum"),
    )
    return bars.dropna(subset=["open"])[BAR_COLUMNS]


def _resample_ohlc(bars: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Apply the shared OHLCV resample/aggregate/dropna rule for one interval."""
    out = bars.resample(rule, label="left", closed="left").agg(**_AGG)
    return out.dropna(subset=["open"])[BAR_COLUMNS]


def aggregate(bars: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Roll 1-minute bars up to a coarser interval.

    `1d` buckets on Eastern calendar days rather than UTC days: bars are
    converted to America/New_York before resampling. A US session can
    straddle UTC midnight (e.g. an after-hours print past 20:00 ET) while
    still belonging to one Eastern trading day, so bucketing on the raw UTC
    index could split or mislabel a session that a daily reference feed
    treats as a single date. That bucketing (which trades land in which
    daily bar) is untouched by the labeling choice below.

    The resulting *label*, however, is normalized to UTC midnight of that
    same Eastern calendar date -- e.g. the 2023-05-12 bar is labeled
    2023-05-12 00:00:00+00:00, not the Eastern midnight's UTC clock time
    (04:00Z in EDT). This matches both EODHD adapters
    (`reference/eodhd_api.py`, `reference/eodhd_local.py`), which localize a
    naive daily date straight to UTC midnight: a daily bar is conventionally
    keyed by its calendar date, not a specific instant. `tz_convert("UTC")`
    here would instead shift the Eastern midnight by the UTC offset (04:00Z
    in EDT, 05:00Z in EST) and never line up with either adapter, which is
    exactly the bug this fixed -- `report.compare()` intersects on the
    index, so the two sides shared no timestamps at all and every 1d
    comparison silently reported zero bars compared.
    """
    if interval not in _RULES:
        raise ValueError(
            f"unsupported interval {interval!r}; expected one of {', '.join(_RULES)}"
        )
    if interval == "1m" or bars.empty:
        return bars

    if interval == "1d":
        local = bars.tz_convert(EASTERN)
        out = _resample_ohlc(local, "1D")
        # Re-label at UTC midnight for the same calendar date, rather than
        # converting the Eastern midnight's clock time to UTC -- see the
        # docstring above.
        out.index = out.index.tz_localize(None).tz_localize("UTC")
        return out

    return _resample_ohlc(bars, _RULES[interval])

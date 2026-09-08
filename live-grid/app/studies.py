# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Per-symbol RSI(14) and anchored VWAP for the live grid's studies column.

One `compute()` call over one frame -- one frame built, one pass, one set of
columns out -- so the grid's RSI cannot drift from the chart's: both read the
same registry entry (`app.ta.registry`'s `rsi`, Wilder's smoothing with
alpha=1/n), not the prototype's Cutler SMA. Note this is NOT a `Base` dedup:
both `rsi` and `avwap` declare `deps=lambda p: []`, so neither contributes a
shared `Base` and there is nothing to deduplicate between them -- the win
here is one frame and one round trip, not shared sub-series.

**The AVWAP takes an explicit anchor**, because an anchored VWAP without an
anchor is not one: with `anchor=None` the value is a cumulative mean from
whatever bar the caller's window happens to start at, so it moves when the
window rolls and means something different every day. `studies_for` takes
the anchor from its caller. A watchlist mixes US equities, crypto and forex,
which share no session open -- so the caller's default is the current UTC
day's start, the one boundary all three do share and the same boundary
kdb's own `/day` endpoint slices on.

**RSI needs enough bars, and Wilder will not tell you when it doesn't.**
Wilder's smoothing is an EWM (`ewm_mean(alpha=1/n)`), which has no
`min_periods`: it yields a confident number from the SECOND bar on, where a
`rolling_mean(14)` would return null until the fourteenth. Measured: RSI(14)
over two synthetic bars returns 100.0. So a caller whose window is too short
gets a plausible, authoritative, meaningless value -- which is exactly the
failure the null discipline below exists to prevent. `studies_for` therefore
refuses to report an RSI until it has more bars than the period, whatever
interval those bars turned out to be.

Null discipline: a symbol with no trade data has no AVWAP, and a symbol with
too little history has no RSI. Both are None, never 0 and never 50 -- a
scanner sorted on this column must not rank a cold symbol as neutral.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from app.ta.compute import compute
from app.ta.payload import bars_to_frame
from app.ta.registry import col_suffix, resolve

RSI_PERIOD = 14
# ~40 calendar days is ~28 trading days, comfortably past RSI(14)'s warmup on
# daily bars -- and a fourteenth of the 365-day default, which fired fifty
# year-long fetches at once on every mount.
WINDOW_DAYS = 40
_RSI_REQ = resolve("rsi", period=RSI_PERIOD)
_RSI_COL = "rsi" + col_suffix(_RSI_REQ)



def _valid_iso(raw: str) -> bool:
    """Does `_avwap_anchor` accept this? Asked here, not there, on purpose.

    A bad anchor raises inside the polars `build()` -- which runs inside
    `compute()`, which `studies_for` calls OUTSIDE the per-symbol try that
    swallows a failed fetch. So one mistyped cell would 500 the whole
    request and blank fifty rows. Validating at the edge keeps a typo local
    to the row that made it.
    """
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def parse_anchors(raw: str, default: str) -> tuple[str, dict[str, str]]:
    """The Anchor column's wire format: `SYM=ISO,SYM=ISO`.

    Returns the grid-wide anchor and the per-symbol overrides. An unkeyed
    chunk sets the grid-wide one, so "anchor everything at the open" is one
    edit rather than fifty, and a keyed chunk overrides a single row.

    Keyed on `=` and split on `,` because an ISO 8601 timestamp contains
    `:`, `-` and `T` but neither of those two -- so the format needs no
    escaping and no positional coupling to the symbol list. Only the edited
    rows travel, which is what makes a fifty-row watchlist send one pair
    instead of fifty empty slots.

    Symbols are upper-cased to match `_parse_symbols`; a lower-case cell
    would otherwise miss its lookup and silently fall back to the default.
    Unparseable chunks are dropped, not raised (see `_valid_iso`), and a
    repeated symbol takes its last value.
    """
    fallback = default
    over: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        sym, sep, stamp = chunk.partition("=")
        if sep:
            sym, stamp = sym.strip().upper(), stamp.strip()
            if sym and _valid_iso(stamp):
                over[sym] = stamp
        elif _valid_iso(chunk):
            fallback = chunk
    return fallback, over



def window_start(anchor_iso: str, today: date) -> str:
    """The earliest date the fetch window must reach, given one anchor.

    Two floors, and the window takes whichever is earlier. RSI(14) needs
    `WINDOW_DAYS` of warmup or `studies_for` refuses to report it. And the
    anchor itself needs to fall INSIDE the window: an anchor older than every
    bar fetched filters nothing, so `avwap` would report a cumulative mean
    from the window's first bar while the Anchor cell still claimed the
    time the user typed. That is the same authoritative-and-wrong failure as
    an RSI off four bars, so the window stretches to cover the anchor rather
    than quietly redefining it.

    An unparseable anchor falls back to the floor; `parse_anchors` has
    already dropped those, so this is belt-and-braces for a default that
    changes shape later.
    """
    floor = today - timedelta(days=WINDOW_DAYS)
    try:
        anchored = datetime.fromisoformat(anchor_iso.replace("Z", "+00:00")).date()
    except ValueError:
        return str(floor)
    return str(min(floor, anchored))


def _blank(symbol: str, anchor: str | None) -> dict[str, Any]:
    return {"symbol": symbol, "rsi": None, "avwap": None, "avwap_dev": None,
            "vwap_start": anchor}


def studies_for(symbol: str, bars: list[dict], anchor: str | None = None) -> dict[str, Any]:
    """RSI(14) and anchored-VWAP deviation from one symbol's bars.

    `anchor` is an ISO 8601 timestamp the AVWAP runs forward from. Passing
    None keeps the old cumulative-from-the-window behaviour, which is only
    meaningful when the caller intends exactly that.

    `avwap_dev` is `(price - avwap) / avwap`, the signed fraction the grid's
    deviation bar renders. It is kept separate from `avwap`: the bar wants
    the fraction, a trader reading the column wants the price.
    """
    if not bars:
        return _blank(symbol, anchor)
    frame = bars_to_frame(bars)
    if frame.is_empty():
        return _blank(symbol, anchor)
    avwap_req = resolve("avwap", anchor=anchor)
    avwap_col = "avwap" + col_suffix(avwap_req)
    out = compute(frame, [_RSI_REQ, avwap_req])
    # Wilder is an EWM with no min_periods, so it reports a number from the
    # second bar. Refuse anything short of a full period rather than pass a
    # meaningless value off as a reading.
    rsi = out[_RSI_COL][-1] if frame.height > RSI_PERIOD else None
    avwap = out[avwap_col][-1]
    price = out["close"][-1]
    dev = None
    if avwap is not None and avwap != 0 and price is not None:
        dev = (price - avwap) / avwap
    return {
        "symbol": symbol,
        "rsi": None if rsi is None else float(rsi),
        "avwap": None if avwap is None else float(avwap),
        "avwap_dev": None if dev is None else float(dev),
        # The anchor ACTUALLY used, echoed back so the Anchor cell shows
        # what the number beside it was computed from. A client that echoed
        # its own request instead would keep displaying a time the server had
        # dropped as unparseable or never received.
        "vwap_start": anchor,
    }

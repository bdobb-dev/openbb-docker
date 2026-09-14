"""The reference-source contract.

Two rules make the comparison honest:

1. Adapters CLASSIFY failures. "No data" is not one thing: a retention limit,
   an unentitled symbol, a bad key and a genuinely empty window are four
   different facts, and only some of them mean "try a coarser bar".
2. Stepping down the interval ladder is explicit and recorded, so the report
   can show what was asked for, what came back, and why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import pandas as pd

INTERVAL_LADDER = ("1m", "5m", "15m", "30m", "1h", "1d")

# Only these mean "this source cannot serve this window at this resolution".
# Anything else is a real problem and must surface, not be stepped past.
_STEPPABLE = ("retention", "empty")

# The documented closed set a ReferenceError.kind may take:
# - retention: data retention limit; try a coarser interval
# - empty: no rows in the window; try a coarser interval
# - auth: authentication failure (bad key); permanent
# - entitlement: unauthorized for this plan; permanent
# - transport: network or server failure; permanent
# - not_covered: symbol/route not available from this source; permanent
_KINDS = _STEPPABLE + ("auth", "entitlement", "transport", "not_covered")


class ReferenceError(Exception):
    """A classified failure from a reference source."""

    def __init__(self, kind: str, detail: str):
        if kind not in _KINDS:
            raise ValueError(f"unknown ReferenceError kind {kind!r}; expected one of {_KINDS}")
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


@dataclass
class Attempt:
    interval: str
    error: ReferenceError | None = None


@dataclass
class ReferenceResult:
    frame: pd.DataFrame
    interval: str
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def stepped_down(self) -> bool:
        return len(self.attempts) > 1


class ReferenceAdapter(Protocol):
    name: str
    supported_intervals: tuple[str, ...]

    def fetch(
        self, symbol: str, start: Any, end: Any, interval: str
    ) -> pd.DataFrame: ...


def normalize_daily_index(frame):
    """Label daily bars at UTC midnight, matching `rollup.aggregate(..., "1d")`.

    Providers label a daily bar at the exchange's local midnight -- yfinance
    returns 2023-05-12 04:00Z for the 2023-05-12 US session -- while our own
    roll-up labels it 2023-05-12 00:00Z. Both mean the same session, but
    `report.compare` intersects on the index, so left alone the two sides
    overlap in ZERO bars and the report claims a full set of coverage gaps and
    nothing compared. That is the failure this normalization exists to prevent,
    and it is on the DOCUMENTED fallback path: yfinance cannot serve 1m for a
    2023 window, so every such comparison lands here.

    Doing it here covers all adapters at one funnel. Note the assumption: it
    floors in UTC, which is right for exchanges west of Greenwich (local
    midnight is the same UTC day). An exchange at a positive UTC offset would
    need flooring in its own timezone instead.
    """
    if frame is None or frame.empty:
        return frame
    out = frame.copy()
    idx = pd.DatetimeIndex(out.index)
    idx = idx.tz_convert("UTC") if idx.tz is not None else idx.tz_localize("UTC")
    out.index = idx.normalize()
    return out


def fetch_finest(
    adapter: ReferenceAdapter,
    symbol: str,
    start: Any,
    end: Any,
    wanted: str = "1m",
) -> ReferenceResult:
    """Fetch at the finest interval this source can actually serve.

    Walks the ladder from `wanted` downward in resolution, recording each
    attempt. Retention and empty-window failures step down; everything else
    (auth, entitlement, transport) is re-raised immediately.
    """
    if wanted not in INTERVAL_LADDER:
        raise ValueError(
            f"unsupported interval {wanted!r}; expected one of {', '.join(INTERVAL_LADDER)}"
        )

    attempts: list[Attempt] = []
    for interval in INTERVAL_LADDER[INTERVAL_LADDER.index(wanted) :]:
        if interval not in adapter.supported_intervals:
            continue
        try:
            frame = adapter.fetch(symbol, start, end, interval)
        except ReferenceError as err:
            attempts.append(Attempt(interval, err))
            if err.kind in _STEPPABLE:
                continue
            raise
        attempts.append(Attempt(interval))
        if interval == "1d":
            frame = normalize_daily_index(frame)
        return ReferenceResult(frame=frame, interval=interval, attempts=attempts)

    raise ReferenceError(
        "empty",
        f"no interval from {adapter.name} could serve {symbol} over {start}..{end}",
    )

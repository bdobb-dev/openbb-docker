# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The regular trading session, per feed.

`session=regular` on the study sockets means "compute on the regular session
only": the client still draws every bar (it veils the extended ones rather
than deleting them), but every indicator behind the veil -- AVWAP included --
must be the one the regular session produces, and that can only be decided
where the bars are.

Keyed by feed exactly as `app.classify.classify` names it, so the table reads
the same from both ends: `classify(symbol)` in, a session out. A feed absent
from the table trades round the clock (crypto, forex) and is never filtered.
"""

import re
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import polars as pl

from app.classify import classify

#: Regular session per feed: (IANA zone, open, close). Half-open [open, close)
#: -- the 16:00 bar belongs to the close auction, not to the session's bars.
SESSIONS: dict[str, tuple[str, str, str]] = {
    "us": ("America/New_York", "09:30", "16:00"),
}

# A daily bar IS a session, so `1d` and anything coarser is never filtered:
# only intraday buckets can straddle the open. The tick plane's intervals are
# 1s/1m/5m/15m/30m/1h (kdb_store.aggregate.BUCKET_NS), and this matches the
# shape rather than the list so a new bucket width needs no edit here. Case
# matters: `1m` is a minute, `1M` would be a month.
_INTRADAY = re.compile(r"^\d+[smh]$")


def is_intraday(interval: str) -> bool:
    """True when one bar is shorter than a session.

    Both session questions turn on this -- which bars are inside the regular
    window, and whether a `bd` window means sessions or bars -- so they ask
    it in one place rather than each matching the shape themselves.
    """
    return bool(_INTRADAY.match(str(interval).strip()))


def _naive_utc(raw) -> datetime | None:
    """A bar's `date` as a naive UTC datetime, or None when it will not read."""
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def regular_only(bars: list[dict], symbol: str, interval: str = "1m") -> list[dict]:
    """The bars that fall inside the feed's regular session.

    `date` is naive UTC (the tick plane's clock), so the comparison converts
    to the feed's own zone -- a fixed offset would be wrong for half the year.
    A symbol whose feed has no session, and any daily-or-coarser interval,
    comes back unchanged.

    ponytail: early closes are not read here. The client's veil is calendar-
    aware (it has the bundled NYSE file, half-days included); the server's
    window is the ordinary 09:30-16:00 session, so on a half-day the studies
    include the hours after the early close. Upgrade path: the server reads
    the same calendar file.
    """
    session = SESSIONS.get(classify(symbol))
    if session is None or not is_intraday(interval):
        return bars
    zone, open_at, close_at = session
    tz = ZoneInfo(zone)
    opens, closes = time.fromisoformat(open_at), time.fromisoformat(close_at)
    kept = []
    for bar in bars:
        dt = _naive_utc(bar.get("date"))
        # A timestamp this cannot read is kept, not dropped: the filter's job
        # is to narrow the window, never to lose a bar to a parse failure.
        if dt is None:
            kept.append(bar)
            continue
        # ponytail: weekends are not excluded -- no us bars exist on them
        # today, so a time-of-day filter is the whole test; one weekday() < 5
        # test closes it if a feed ever ticks on a Saturday.
        if opens <= dt.replace(tzinfo=timezone.utc).astimezone(tz).time() < closes:
            kept.append(bar)
    return kept


#: The column `session_closes` groups by and `LocalSource` joins back on.
SESSION_DATE = "session_date"


def session_date(feed: str) -> pl.Expr:
    """Each bar's session, as the exchange-local calendar date.

    ponytail: the session's date IS the local calendar date. That holds for
    every feed in SESSIONS -- a US session, extended hours included, runs
    04:00-20:00 ET and never crosses local midnight -- and for the UTC
    fallback. A feed whose session straddles midnight (futures at 18:00-17:00
    ET) would need the session's own open to roll the date. Upgrade path:
    offset by the open before taking the date.

    ponytail: a feed with no session (crypto, forex) has no exchange clock, so
    a business day there is a UTC calendar day -- the same boundary the tick
    plane already stamps its bars against. Upgrade path: a per-feed "day
    starts at" when one of them grows a settlement hour that matters.

    ponytail: `date` is assumed naive, so `replace_time_zone("UTC")` stamps
    UTC on it rather than converting -- on an already tz-aware column that
    would overwrite the real zone instead of reading it. `bars_to_frame`
    only ever produces naive dates in this engine, so nothing hits this yet.
    Upgrade path: skip the replace when the dtype already carries a zone.
    """
    zone = SESSIONS.get(feed, ("UTC", "", ""))[0]
    return (
        pl.col("date").cast(pl.Datetime).dt.replace_time_zone("UTC")
        .dt.convert_time_zone(zone).dt.date().alias(SESSION_DATE)
    )


def running_session_start(frame: pl.DataFrame, symbol: str = "") -> int:
    """The row index of the first bar of the frame's LAST session.

    How far back a `bd` window's delta has to reach, and no further. A tick
    revises the running session's close, and `session_closes` hands that close
    to every bar of the session -- so those bars move together, while every
    session before them is closed and cannot move. Calling the whole chart
    "repainting" instead resends all of it: 21,450 bars a second on a
    55-session 1m chart (3.47 MB/s) against ~390 here.

    A daily-or-coarser frame needs no special case: one bar is one session, so
    this is the last row and the delta is the tail it always was.
    """
    if frame.height == 0 or "date" not in frame.columns:
        return 0
    stamps = frame.select(session_date(classify(symbol))).to_series().to_list()
    # A date that would not parse is null, and `.index(None)` would match the
    # first such bar rather than this session's first -- resend the tail, the
    # same thing an unusable timestamp gets everywhere else in this module.
    if stamps[-1] is None:
        return frame.height - 1
    return stamps.index(stamps[-1])


def session_closes(frame: pl.DataFrame, feed: str = "us") -> pl.DataFrame:
    """One row per session: that session's bars rolled up into its close.

    This is the series a `bd` window is computed on -- `sma:period=50bd` is
    fifty BUSINESS DAYS, not fifty bars, so on an intraday chart it has to see
    one value per session. `date` is the session's LAST bar time and `close`
    its last close, so the current (partial) session is present and carries
    the running bar: 49 prior closes plus today, exactly as the spec asks.

    Columns the frame does not carry are simply not aggregated -- a
    tick-derived frame may have no `adj_close`, and a missing column is not a
    reason to refuse the whole roll-up.
    """
    if frame.height == 0 or "date" not in frame.columns:
        return frame
    # open first, high max, low min, close last, volume sum, vwap last: the
    # session's own bar, built from its minutes.
    aggregates = {
        "date": pl.col("date").last(),
        "open": pl.col("open").first(),
        "high": pl.col("high").max(),
        "low": pl.col("low").min(),
        "close": pl.col("close").last(),
        "adj_close": pl.col("adj_close").last(),
        "volume": pl.col("volume").sum(),
        "vwap": pl.col("vwap").last(),
    }
    return (
        frame.with_columns(session_date(feed))
        .group_by(SESSION_DATE)
        .agg(*(expr for name, expr in aggregates.items() if name in frame.columns))
        .sort(SESSION_DATE)
    )

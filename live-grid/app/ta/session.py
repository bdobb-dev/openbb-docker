# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
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
    if session is None or not _INTRADAY.match(str(interval).strip()):
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
        if opens <= dt.replace(tzinfo=timezone.utc).astimezone(tz).time() < closes:
            kept.append(bar)
    return kept

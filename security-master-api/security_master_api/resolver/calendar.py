# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The pinned pandas_market_calendars adapter: deterministic baseline sessions with provenance."""

from __future__ import annotations

from datetime import UTC, date, datetime
from importlib.metadata import version

import pandas as pd
import pandas_market_calendars as mcal


def adapter_version() -> str:
    return f"pandas_market_calendars/{version('pandas_market_calendars')}"


def _opt(value):
    """A schedule cell as an aware UTC instant; a missing break column reads as None."""
    if value is None or pd.isna(value):
        return None
    return value.to_pydatetime().astimezone(UTC)


def generate_sessions(calendar_alias: str, start: date, end: date, calendar_id: str,
                      rule_id: str, generated_at: datetime | None = None) -> list[dict]:
    """Baseline `silver.market_sessions` rows for one calendar, one row per open session.

    The package's rules are the source: a day it does not schedule is not a session, which is
    how holidays leave the range. Every row carries the adapter version that produced it, so a
    later package upgrade that moves a session is a visible correction rather than a silent one.
    """
    cal = mcal.get_calendar(calendar_alias)
    schedule = cal.schedule(start_date=str(start), end_date=str(end))
    now = generated_at or datetime.now(UTC)
    rows = []
    for session, row in schedule.iterrows():
        session_date = session.date()
        rows.append({
            "calendar_id": calendar_id,
            "session_date": session_date,
            "trade_date": session_date,
            "market_open": _opt(row["market_open"]),
            "market_close": _opt(row["market_close"]),
            "break_start": _opt(row.get("break_start")),
            "break_end": _opt(row.get("break_end")),
            "rule_id": rule_id,
            "source_version": adapter_version(),
            "effective_from": datetime.combine(session_date, datetime.min.time(), tzinfo=UTC),
            "effective_to": None,
            "observed_at": now,
            "available_at": now,
            "system_from": now,
            "system_to": None,
            "capture_id": None,
            "assertion_id": f"as_sess_{calendar_id}_{session_date.isoformat()}",
            "assertion_status": "current",
            "supersedes_assertion_id": None,
        })
    return rows

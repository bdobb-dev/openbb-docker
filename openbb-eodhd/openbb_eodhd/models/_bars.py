# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Shared EODHD price-bar helpers (used by equity, crypto, and forex historical).

EODHD serves every asset class through the same two endpoints — `/api/eod`
(daily/weekly/monthly) and `/api/intraday` (1m/5m/1h) — differing only by the
symbol suffix (`.US`, `.CC`, `.FOREX`). This module holds the request/parse
logic once (via the official `eodhd` SDK); each asset-class fetcher supplies
its own symbol qualification.
"""

from datetime import datetime, time, timezone
from typing import Any

from openbb_core.app.model.abstract.error import OpenBBError
from openbb_core.provider.utils.errors import EmptyDataError, UnauthorizedError

from openbb_eodhd.models._client import get_client, raise_sdk_error

# OpenBB interval -> EODHD. Intraday -> /api/intraday; daily+ -> /api/eod.
INTRADAY_MAP = {"1m": "1m", "5m": "5m", "1h": "1h"}
EOD_PERIOD_MAP = {"1d": "d", "1W": "w", "1M": "m"}
INTERVAL_CHOICES = list(INTRADAY_MAP) + list(EOD_PERIOD_MAP)


def to_unix(day: Any, *, end_of_day: bool) -> int:
    """Convert a date to a UTC unix timestamp (start or end of that day)."""
    clock = time.max if end_of_day else time.min
    return int(datetime.combine(day, clock, tzinfo=timezone.utc).timestamp())


async def fetch_bars(
    interval: str,
    symbols: list[str],
    start_date: Any,
    end_date: Any,
    credentials: dict[str, str] | None,
) -> list[dict]:
    """Fetch raw bars for already-qualified symbols (e.g. AAPL.US, BTC-USD.CC)."""
    # The official SDK is synchronous (requests); run it off the event loop.
    # pylint: disable=import-outside-toplevel
    from asyncio import to_thread

    return await to_thread(
        _fetch_bars_sync, interval, symbols, start_date, end_date, credentials
    )


def _fetch_bars_sync(
    interval: str,
    symbols: list[str],
    start_date: Any,
    end_date: Any,
    credentials: dict[str, str] | None,
) -> list[dict]:
    client = get_client(credentials)
    intraday = interval in INTRADAY_MAP
    multiple = len(symbols) > 1
    results: list[dict] = []
    with client:
        for sym in symbols:
            try:
                if intraday:
                    response = client.get_intraday_historical_data(
                        symbol=sym,
                        interval=INTRADAY_MAP[interval],
                        from_unix_time=to_unix(start_date, end_of_day=False),
                        to_unix_time=to_unix(end_date, end_of_day=True),
                    )
                else:
                    response = client.get_eod_historical_stock_market_data(
                        symbol=sym,
                        period=EOD_PERIOD_MAP[interval],
                        from_date=str(start_date),
                        to_date=str(end_date),
                        order="a",
                    )
            except (OpenBBError, UnauthorizedError):
                raise
            except Exception as exc:
                raise_sdk_error(exc, f"bars for '{sym}'")
            if isinstance(response, dict):
                # A 200 response that isn't a bar list is an error payload.
                msg = response.get("errors") or response.get("message") or response
                raise UnauthorizedError(f"EODHD ({sym}): {msg}")
            for bar in response or []:
                if multiple:
                    bar["_symbol"] = sym
                results.append(bar)

    if not results:
        raise EmptyDataError("The request was returned empty.")
    return results


def rows_from_bars(interval: str, multiple: bool, data: list[dict]) -> list[dict]:
    """Turn raw EODHD bars into standard-model row dicts."""
    # pylint: disable=import-outside-toplevel
    from pandas import isna, to_datetime

    intraday = interval in INTRADAY_MAP
    rows: list[dict] = []
    for bar in data:
        # EODHD emits null-OHLC rows for no-trade buckets; drop them.
        if bar.get("close") is None:
            continue
        if intraday:
            # `timestamp` is a UTC unix epoch; keep bars tz-aware in UTC.
            bar_date: Any = to_datetime(bar.get("timestamp"), unit="s", utc=True, errors="coerce")
        else:
            bar_date = to_datetime(bar.get("date"), errors="coerce")
        # Skip bars whose date is missing/unparseable — never emit a NaT row.
        if isna(bar_date):
            continue
        if not intraday:
            bar_date = bar_date.date()
        row = {
            "date": bar_date,
            "open": bar.get("open"),
            "high": bar.get("high"),
            "low": bar.get("low"),
            "close": bar.get("close"),
            "volume": bar.get("volume"),
            "adjusted_close": bar.get("adjusted_close"),
        }
        if multiple:
            row["symbol"] = bar.get("_symbol")
        rows.append(row)
    # Chronological, then by symbol when multiple. Every row has a real date
    # (NaT rows were skipped above), so the sort key is safe.
    rows.sort(key=lambda r: (str(r.get("symbol", "")), r["date"]))
    return rows

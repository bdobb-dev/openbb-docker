"""Equity quotes served from the live tick store.

`last_price` comes from the newest tick live-grid recorded; `prev_close` from
the last complete daily bar. Intraday open/high/low/volume are deliberately
absent during a session: the quote takes only the previous session's close
from the daily bar and never reads intraday OHLV from it, and deriving them
from the single latest tick would report open == high == low == last_price,
which looks like data rather than like the absence of it.
"""

import asyncio
import logging
import os
from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.equity_quote import (
    EquityQuoteData,
    EquityQuoteQueryParams,
)

from openbb_kdb.leasing import lease, snapshot

log = logging.getLogger(__name__)

DEADLINE_S = 3.0
POLL_S = 0.1
PREV_CLOSE_TIMEOUT_S = 3.0  # bounds the daily-bar fetch; D1's 3s only covers the tick read


def _add_change(row: dict[str, Any], price: float, prev_close: float | None) -> None:
    """Set prev_close/change/change_percent on `row`, or leave them absent.

    Shared by both quote builders below: change math (and the zero-prev_close
    divide-by-zero guard) must not drift between the tick path and the
    snapshot path.
    """
    if prev_close is None:
        return
    row["prev_close"] = prev_close
    row["change"] = price - prev_close
    if prev_close:
        row["change_percent"] = row["change"] / prev_close


def build_quote(symbol: str, tick: dict | None, prev_close: float | None) -> dict | None:
    """Assemble one EquityQuote row. Returns None when there is no tick."""
    if not tick:
        return None
    row: dict[str, Any] = {
        "symbol": symbol,
        "last_price": tick["price"],
        "last_timestamp": tick["time"],
        "delayed": False,
    }
    # EquityQuoteData.last_size is int | None; kdb reports size as a float.
    # A whole-valued float (40.0) coerces cleanly, but a fractional one would
    # raise ValidationError and 500 the route -- omit it instead of guessing.
    size = tick.get("size")
    if size is not None and float(size).is_integer():
        row["last_size"] = int(size)
    _add_change(row, tick["price"], prev_close)
    return row


def build_quote_from_snapshot(symbol: str, snap: dict | None) -> dict | None:
    """Assemble a row from the delayed REST snapshot. None when unavailable
    or malformed -- like every other leg of the fallback, a bad snapshot
    must degrade to no rows, never raise."""
    if not isinstance(snap, dict) or snap.get("price") is None:
        return None
    try:
        price = float(snap["price"])
    except (TypeError, ValueError):
        return None
    row: dict[str, Any] = {"symbol": symbol, "last_price": price,
                            "delayed": bool(snap.get("delayed", True))}
    prev = snap.get("prev_close")
    if prev is not None:
        try:
            prev = float(prev)
        except (TypeError, ValueError):
            prev = None
    _add_change(row, price, prev)
    return row


async def _await_tick(store, symbol: str, deadline: float) -> dict | None:
    """Poll for a first tick until the deadline. Returns None if none arrives.

    Each `store.latest_tick` call runs in a worker thread and is bounded by
    `asyncio.wait_for` against the *remaining* deadline: a cold session's own
    connect budget (kdb_store.session._CONNECT_BUDGET_S = 5.0) or a wedged
    call could otherwise block past -- or forever past -- our 3.0s default,
    defeating the reason this deadline exists.

    `wait_for` around `to_thread` abandons the worker thread on timeout, it
    cannot cancel it -- a wedged q leaves that thread blocked indefinitely.
    That degrades to "this call always falls back", never a hang, because we
    stop waiting on it; it does not free the thread.
    """
    loop = asyncio.get_running_loop()
    stop = loop.time() + deadline
    while True:
        remaining = stop - loop.time()
        try:
            tick = await asyncio.wait_for(
                asyncio.to_thread(store.latest_tick, symbol), timeout=max(remaining, 0)
            )
        except asyncio.TimeoutError:
            return None
        if tick is not None:
            return tick
        remaining = stop - loop.time()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(POLL_S, remaining))


def _row_date(row: dict):
    """Coerce a bar row's `date` (str, date or datetime) to a plain date."""
    from datetime import date, datetime

    value = row.get("date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


async def _prev_close(cache, symbol: str, credentials) -> float | None:
    """Close of the most recent complete daily bar, or None.

    Best-effort exactly like the lease: a quote that knows the last price but
    not yesterday's close is still a useful quote, and is what the spec asks
    for when no bar is available. Everything below -- the fetch, the date
    coercion and the float coercion -- is inside the same try/except: any of
    them failing must degrade to "no prev_close", never fail the quote.
    """
    from datetime import date, timedelta

    from openbb_kdb.models.historical import _as_datetime

    today = date.today()
    end = today - timedelta(days=1)
    try:
        # get() answers (rows, metadata) -- unpack, do not index the tuple.
        # ReadThroughCache compares start/end against last_complete_boundary(),
        # which is a datetime (see cache.py) -- a bare `date` fails that
        # comparison with a TypeError that _get() swallows into a live
        # upstream bypass, defeating D5. _as_datetime is the same converter
        # historical.py's _extract() uses for this exact purpose.
        rows, _meta = await asyncio.wait_for(
            cache.get(
                symbol=symbol, interval="1d",
                start=_as_datetime(end - timedelta(days=10)), end=_as_datetime(end),
                model="EquityHistorical", params={}, credentials=credentials,
            ),
            timeout=PREV_CLOSE_TIMEOUT_S,
        )
        # Belt and suspenders: ReadThroughCache serves whatever window it is
        # asked for with no complete-boundary filter, so a same-day forming
        # bar is real and gets served on any window reaching today (see
        # test_tail_types.py's module docstring). `end` above already keeps
        # the request out of today, but a stored forward-dated row should not
        # be able to leak through either.
        rows = [row for row in rows if _row_date(row) < today]
        if not rows:
            return None
        close = rows[-1].get("close")
        return float(close) if close is not None else None
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.debug("prev_close lookup for %s failed: %s", symbol, exc)
        return None


class KdbEquityQuoteFetcher(Fetcher[EquityQuoteQueryParams, list[EquityQuoteData]]):
    """The newest live tick for a symbol, leasing a feed if one is not running."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EquityQuoteQueryParams:
        return EquityQuoteQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:
        from openbb_kdb.models.historical import _cache

        symbol = query.symbol.upper()
        # Unconditional and idempotent: renewing keeps a hot symbol hot, and
        # tick recency cannot distinguish "quiet" from "not subscribed".
        await lease(symbol)

        cache = _cache(credentials)
        store = cache.store
        deadline = float(os.getenv("KDB_QUOTE_DEADLINE_S", DEADLINE_S))
        try:
            tick = await _await_tick(store, symbol, deadline)
        except Exception as exc:  # noqa: BLE001 - kdb down falls back, per the spec
            log.debug("tick read for %s failed: %s", symbol, exc)
            tick = None
        prev = await _prev_close(cache, symbol, credentials)
        snap = None if tick else await snapshot(symbol)
        return {"symbol": symbol, "tick": tick, "prev_close": prev, "snapshot": snap}

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EquityQuoteData]:
        row = build_quote(data["symbol"], data.get("tick"), data.get("prev_close"))
        if row is None:
            row = build_quote_from_snapshot(data["symbol"], data.get("snapshot"))
        return [EquityQuoteData.model_validate(row)] if row else []

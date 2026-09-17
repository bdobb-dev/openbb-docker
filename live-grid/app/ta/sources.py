# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Two interchangeable engines behind one shape.

Both emit the same column names, so panes and figure never learn which ran.
That is the whole point of offering a choice: toggling source must not move
the line.

EODHD bills five API calls per indicator per request, so this source is cached
and gated on bar close. A six-indicator macro refreshed every second would be
~108,000 calls an hour against a 100,000-a-day limit (spec D7).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import polars as pl

from app.classify import classify
from app.ta.compute import compute
from app.ta.registry import Req, col_suffix, get
from app.ta.session import (
    SESSION_DATE,
    is_intraday,
    session_closes,
    session_date,
)

log = logging.getLogger("live-grid.ta")

CALLS_PER_REQUEST = 5
EODHD_URL = "https://eodhd.com/api/technical/{symbol}"


@dataclass(frozen=True)
class Annotation:
    """Why a series is not what the chosen source would have given."""

    column: str
    source: str
    note: str


@dataclass
class Result:
    frame: pl.DataFrame
    annotations: list[Annotation] = field(default_factory=list)
    calls: int = 0


def columns_of(req: Req) -> list[str]:
    """The frame columns this request produces, suffixed per its parameters."""
    suffix = col_suffix(req)
    return [c + suffix for c in get(req.name).render]


def eodhd_query(req: Req) -> dict:
    """The query parameters for one indicator. Raises if it has no mapping."""
    ind = get(req.name)
    if ind.eodhd is None:
        raise ValueError(f"{req.name!r} has no EODHD equivalent")
    query = {"function": ind.eodhd.function}
    for their_name, our_name in ind.eodhd.params.items():
        query[their_name] = req.params[our_name] + ind.eodhd.offsets.get(their_name, 0)
    return query


def _is_bd(req: Req) -> bool:
    """True when any of this request's windows counts business days."""
    return "bd" in req.units.values()


class LocalSource:
    """Polars compute over the bars already in hand."""

    name = "local"

    def series(self, df: pl.DataFrame, reqs: list[Req],
               interval: str = "1d", symbol: str = "") -> Result:
        """`interval` and `symbol` are read only by `bd` windows.

        A `bd` window counts sessions: it is computed on one row per session
        (`session_closes`) and then broadcast back onto every bar of its
        session, so `SMA(50bd)` on a 1m chart is fifty DAYS of closes rather
        than fifty minutes. The join is by session date and not by time, or
        every bar before the session's last would read yesterday's value.

        On a daily-or-coarser frame a bar IS a session, so `50bd` is 50 bars
        and the ordinary path is already the right answer -- the defaults say
        so, which keeps every existing caller (and the parity tests) intact.

        ponytail: that is only literally true on `1d`. `is_intraday` treats
        `1wk`/`1mo` as "not intraday" too, so `50bd` there silently falls
        through to the ordinary path and means 50 bars -- 50 weeks or months,
        not 50 sessions. Upgrade path: reject a `bd` unit when the interval is
        coarser than `1d`, once a caller actually asks for one.
        """
        bd = [r for r in reqs if _is_bd(r)]
        if not bd or df.height == 0 or not is_intraday(interval):
            return Result(compute(df, reqs))
        feed = classify(symbol)
        wanted = [c for r in bd for c in columns_of(r)]
        # Two frames, one per clock: the bar frame for ordinary requests, the
        # session frame for the bd ones. Their Base columns never meet, which
        # is what keeps `period=50` and `period=50bd` honest.
        sessions = compute(session_closes(df, feed), bd)
        frame = compute(df, [r for r in reqs if not _is_bd(r)])
        joined = (
            frame.with_columns(session_date(feed))
            .join_asof(sessions.select([SESSION_DATE, *wanted]),
                       on=SESSION_DATE, strategy="backward")
            .drop(SESSION_DATE)
        )
        return Result(joined)


class EodhdSource:
    """EODHD's pre-calculated indicators, cached and bar-close-gated."""

    name = "eodhd"

    def __init__(self, api_key: str, fetch=None, min_refetch_s: float = 60.0):
        self._key = api_key
        self._fetch = fetch or self._http_fetch
        self._min_refetch_s = min_refetch_s
        self._cache: dict[tuple, list[dict]] = {}
        self._fetched_at: dict[tuple, float] = {}
        # Cumulative spend for /health. EODHD's quota is the real limit; this
        # is the number that makes an accidental unthrottled loop visible
        # before the daily budget is gone (spec D7).
        self.total_calls = 0

    async def _http_fetch(self, query: dict) -> list[dict]:
        import httpx

        symbol = query.pop("_symbol")
        params = {**query, "api_token": self._key, "fmt": "json"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(EODHD_URL.format(symbol=symbol), params=params)
            response.raise_for_status()
            return response.json()

    async def series(
        self, df: pl.DataFrame, reqs: list[Req], symbol: str,
        interval: str, last_closed: str,
    ) -> Result:
        # EODHD's technical endpoint has no intraday, and eodhd_query sends no
        # interval at all -- so on a 5m chart their daily value would be fanned
        # across every bar of the day and passed off as intraday. Compute
        # locally and say so, rather than draw a plausible lie.
        if interval != "1d":
            return Result(LocalSource().series(df, reqs, interval, symbol).frame, [
                Annotation(col, "local", "EODHD has no intraday data")
                for r in reqs for col in columns_of(r)
            ])

        mapped = [r for r in reqs if get(r.name).eodhd is not None]
        unmapped = [r for r in reqs if get(r.name).eodhd is None]

        annotations = [
            Annotation(col, "local", f"{r.name} has no EODHD equivalent")
            for r in unmapped for col in columns_of(r)
        ]
        frame = compute(df, unmapped) if unmapped else df
        calls = 0
        fetched: list[tuple[Req, list[dict] | None]] = []

        for req in mapped:
            key = (symbol, interval, req.name, tuple(sorted(
                (k, v) for k, v in req.params.items() if k != "style")), last_closed)
            now = time.monotonic()
            cached = self._cache.get(key)
            if cached is not None:
                fetched.append((req, cached))
                continue
            if now - self._fetched_at.get(key, -1e9) < self._min_refetch_s:
                # A recent ATTEMPT failed and the floor has not elapsed.
                annotations.extend(
                    Annotation(col, "local", "EODHD refetch throttled")
                    for col in columns_of(req)
                )
                fetched.append((req, None))
                continue
            # Stamp BEFORE the attempt, not on success. Stamping on success
            # makes this throttle unreachable: whenever _fetched_at holds a real
            # value the cache check above has already returned, so the only way
            # to arrive here is a MISS -- i.e. a previous failure, which never
            # stamped. A persistently-failing indicator would then retry on every
            # push with no backoff, at five billed calls each time.
            self._fetched_at[key] = now
            try:
                rows = await self._fetch({**eodhd_query(req), "_symbol": symbol})
                self._cache[key] = rows
                calls += CALLS_PER_REQUEST
                self.total_calls += CALLS_PER_REQUEST
                fetched.append((req, rows))
            except Exception as exc:  # noqa: BLE001 - a chart beats an error page
                log.warning("eodhd %s failed for %s: %s", req.name, symbol, exc)
                annotations.extend(
                    Annotation(col, "local", f"EODHD fetch failed: {exc}")
                    for col in columns_of(req)
                )
                fetched.append((req, None))

        local_fallback = [r for r, rows in fetched if rows is None]
        if local_fallback:
            frame = compute(frame, local_fallback)

        for req, rows in fetched:
            if rows is None:
                continue
            try:
                frame, absent = self._join(frame, req, rows)
            except Exception as exc:  # noqa: BLE001 - one bad series, not a dead chart
                log.warning("eodhd %s returned an unusable payload: %s", req.name, exc)
                frame = compute(frame, [req])
                annotations.extend(
                    Annotation(col, "local", f"EODHD response unusable: {exc}")
                    for col in columns_of(req)
                )
                continue
            annotations.extend(
                Annotation(col, "eodhd", "EODHD did not supply this field")
                for col in absent
            )
        return Result(frame, annotations, calls)

    @staticmethod
    def _join(
        frame: pl.DataFrame, req: Req, rows: list[dict]
    ) -> tuple[pl.DataFrame, list[str]]:
        """Rename EODHD's response fields onto our columns and join on date.

        Returns the joined frame plus the columns EODHD did not supply.

        A field missing from EVERY row means their response shape is not what
        the registry expects. That is not hypothetical: EODHD's stochastic is
        documented as returning `slow_k`/`slow_d` and actually returns
        `k_values`/`d_values`. Building the frame from only the keys that
        happen to be present and then casting the full expected list raises
        ColumnNotFoundError, so absent columns are materialised as nulls and
        reported instead.
        """
        fields = get(req.name).eodhd.fields
        suffix = col_suffix(req)
        wanted = [c + suffix for c in fields.values()]
        if not rows:
            nulls = [pl.lit(None, dtype=pl.Float64).alias(c) for c in wanted]
            return frame.with_columns(nulls), wanted

        present = {fields[k] + suffix for r in rows for k in fields if k in r}
        absent = [c for c in wanted if c not in present]
        incoming = pl.DataFrame([
            {"date": r["date"],
             **{fields[k] + suffix: r.get(k) for k in fields if k in r}}
            for r in rows
        ])
        if absent:
            incoming = incoming.with_columns(
                [pl.lit(None, dtype=pl.Float64).alias(c) for c in absent]
            )
        incoming = incoming.with_columns([
            pl.col("date").str.to_date(),
            pl.col(wanted).cast(pl.Float64, strict=False),
        ])
        # Bar dates are timestamps now (intraday keeps its time of day), while
        # EODHD dates them by day. Join on the date PART so a daily bar stamped
        # at anything other than midnight still matches.
        joined = frame.join(
            incoming, left_on=pl.col("date").dt.date(), right_on="date", how="left"
        )
        return joined.drop("date_right", strict=False), absent

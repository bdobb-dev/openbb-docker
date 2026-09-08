# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The read-through algorithm: what gets fetched, what gets served, what it reports."""

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from openbb_kdb.cache import ReadThroughCache, _to_frame, last_complete_boundary
from kdb_store.config import KdbConfig
from kdb_store.session import KdbUnavailable

D = lambda s: datetime.fromisoformat(s)  # noqa: E731


def cfg(**kw) -> KdbConfig:
    base = dict(host="127.0.0.1", port=5000, may_spawn=True, memory_mb=1024,
                watermark=0.75, upstream="eodhd", qhome="/opt/kx", qlic="/opt/kx",
                local_qhome="/opt/kx")
    base.update(kw)
    return KdbConfig(**base)


class FakeStore:
    """In-memory stand-in for KdbStore.

    `write_bars` upserts on the timestamp, exactly like the q statement in
    store.py -- a fake that appended duplicates would hide a stale bar being
    served after a refetch.
    """

    def __init__(self, coverage=None, bars=None, heap=0):
        self.coverage = coverage or {}
        self.bars = bars or {}
        self.heap = heap
        self.written = []
        self.dropped = []
        self.evicted_to = None

    def read_coverage(self, symbol, interval):
        return self.coverage.get((symbol, interval), [])

    def record_coverage(self, symbol, interval, r):
        self.coverage.setdefault((symbol, interval), []).append(r)

    def read_bars(self, symbol, interval, start, end):
        df = self.bars.get((symbol, interval), pd.DataFrame())
        if df.empty:
            return df
        return df[(df["t"] >= start) & (df["t"] <= end)]

    def write_bars(self, symbol, interval, df):
        self.written.append((symbol, interval, df))
        prior = self.bars.get((symbol, interval), pd.DataFrame())
        merged = df.copy() if prior.empty else pd.concat([prior, df])
        merged = merged.sort_values("t").drop_duplicates(subset="t", keep="last")
        self.bars[(symbol, interval)] = merged.reset_index(drop=True)

    def touch(self, symbol, interval):
        pass

    def drop(self, symbol, interval):
        self.dropped.append((symbol, interval))
        self.coverage.pop((symbol, interval), None)
        self.bars.pop((symbol, interval), None)

    def memory(self):
        return {"used": self.heap, "heap": self.heap, "wmax": 4_000_000_000}

    def evict_until_below(self, budget):
        self.evicted_to = budget
        return []

    def table_name(self, symbol, interval):
        return f"bars_{symbol}_{interval}"


def bars_frame(dates, close=1.0, column="close"):
    return pd.DataFrame({"t": [D(d) for d in dates], column: [close] * len(dates)})


def make_cache(store, fetches, **cfg_kw):
    """Wire a cache whose upstream records calls and returns canned rows.

    Responses are returned in order; the LAST one is sticky, so a test that
    retries (the bypass path) keeps getting data instead of an empty list.
    """
    calls = []

    async def fake_fetch(provider, model, params, credentials):
        calls.append(params)
        if not fetches:
            return []
        return fetches.pop(0) if len(fetches) > 1 else fetches[0]

    cache = ReadThroughCache(store, cfg(**cfg_kw))
    cache._fetch_gap = fake_fetch
    return cache, calls


def served(rows, stamp):
    """The single served row at `stamp`, or None."""
    wanted = pd.Timestamp(stamp)
    hits = [r for r in rows if pd.Timestamp(r["date"]) == wanted]
    return hits[0] if hits else None


async def test_cold_cache_is_a_miss_and_fetches_everything():
    store = FakeStore()
    rows_up = [{"date": "2024-01-02", "close": 1.0}]
    cache, calls = make_cache(store, [rows_up])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-01"), D("2024-01-31"),
        "EquityHistorical", {"symbol": "AAPL"}, None,
    )
    assert meta["cache"] == "miss"
    assert meta["gaps_fetched"] == 1
    assert len(calls) == 1


async def test_full_hit_makes_no_upstream_call():
    """A fully covered request must not touch the network -- not even for the
    corporate-action backstop, which is why a cached bar sits ON the coverage
    tail here: that is where the old ungated check went looking."""
    store = FakeStore(
        coverage={("AAPL", "1d"): [(D("2024-01-01"), D("2024-12-31"))]},
        bars={("AAPL", "1d"): bars_frame(["2024-06-03", "2024-12-30", "2024-12-31"])},
    )
    cache, calls = make_cache(store, [])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-06-01"), D("2024-06-30"),
        "EquityHistorical", {"symbol": "AAPL"}, None, now=D("2025-03-01"),
    )
    assert meta["cache"] == "hit"
    assert meta["gaps_fetched"] == 0
    assert meta["rows_from_upstream"] == 0
    assert meta["rows_from_cache"] == len(rows) == 1
    assert calls == []


async def test_the_zoom_fetches_only_the_missing_years():
    """1y cached, 3y requested -> exactly one gap, and it is the missing prefix."""
    store = FakeStore(
        coverage={("AAPL", "1d"): [(D("2024-01-01"), D("2024-12-31"))]},
        bars={("AAPL", "1d"): bars_frame(["2024-06-01"])},
    )
    cache, calls = make_cache(store, [[{"date": "2022-01-03", "close": 1.0}]])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2022-01-01"), D("2024-12-31"),
        "EquityHistorical", {"symbol": "AAPL"}, None,
    )
    assert meta["cache"] == "partial"
    assert len(calls) == 1
    assert calls[0]["start_date"] == D("2022-01-01").date()
    assert calls[0]["end_date"] == D("2023-12-31").date()


async def test_the_zoom_to_the_present_fetches_the_prefix_and_the_tail():
    """The headline path: zoom out with `end` at now, not at a past date."""
    now = D("2025-06-10T15:00:00")
    store = FakeStore(
        coverage={("AAPL", "1d"): [(D("2024-01-01"), D("2025-06-08"))]},
        bars={("AAPL", "1d"): bars_frame(["2024-06-01", "2025-06-06", "2025-06-08"])},
    )
    cache, calls = make_cache(store, [[{"date": "2022-01-03", "close": 1.0}]])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2022-01-01"), now,
        "EquityHistorical", {"symbol": "AAPL"}, None, now=now,
    )
    assert meta["cache"] == "partial"
    # The two missing years, and the tail -- never the cached middle.
    assert len(calls) == 2
    assert (calls[0]["start_date"], calls[0]["end_date"]) == (
        D("2022-01-01").date(), D("2023-12-31").date()
    )
    # The tail call reaches the present and starts no earlier than the few
    # overlap bars the corporate-action backstop rides on.
    assert calls[1]["end_date"] == now.date()
    assert calls[1]["start_date"] >= D("2025-06-06").date()


async def test_the_incomplete_tail_is_refetched_on_the_next_request():
    """Today's bar is still forming: a later request the same day must refetch
    it and serve the UPDATED value, not the one first written."""
    store = FakeStore()
    cache, calls = make_cache(store, [
        [{"date": "2025-06-09", "close": 1.0}, {"date": "2025-06-10", "close": 1.0}],
        [{"date": "2025-06-10", "close": 2.0}],
    ])
    first_now = D("2025-06-10T15:00:00")
    rows, meta = await cache.get(
        "AAPL", "1d", D("2025-06-01"), first_now,
        "EquityHistorical", {"symbol": "AAPL"}, None, now=first_now,
    )
    assert served(rows, "2025-06-10")["close"] == 1.0
    recorded = store.coverage[("AAPL", "1d")]
    assert all(end <= D("2025-06-09") for _, end in recorded)

    later = D("2025-06-10T15:59:00")
    rows, meta = await cache.get(
        "AAPL", "1d", D("2025-06-01"), later,
        "EquityHistorical", {"symbol": "AAPL"}, None, now=later,
    )
    assert meta["cache"] == "partial"
    assert meta["gaps_fetched"] == 1
    assert len(calls) == 2
    assert calls[1]["end_date"] == later.date()
    # The whole point: the still-forming bar was refetched and replaced.
    assert served(rows, "2025-06-10")["close"] == 2.0
    assert served(rows, "2025-06-09")["close"] == 1.0


async def test_a_split_invalidates_the_symbol():
    """Overlapping closes disagree -> adjusted history was rewritten."""
    store = FakeStore(
        coverage={("AAPL", "1d"): [(D("2024-01-01"), D("2024-12-31"))]},
        bars={("AAPL", "1d"): bars_frame(["2024-12-30", "2024-12-31"], close=200.0)},
    )
    cache, calls = make_cache(
        store,
        [[{"date": "2024-12-30", "close": 100.0}, {"date": "2024-12-31", "close": 100.0}],
         [{"date": "2024-01-02", "close": 100.0}]],
    )
    now = D("2025-01-05")
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-01"), now,
        "EquityHistorical", {"symbol": "AAPL"}, None, now=now,
    )
    # Dropping the symbol clears its coverage, so the refill is a full miss --
    # which is exactly right: none of the old adjusted history is trustworthy.
    assert ("AAPL", "1d") in store.dropped
    assert meta["cache"] == "miss"
    # The check itself cost no extra call: it rode on the tail gap fetch.
    assert calls[0]["end_date"] == now.date()


async def test_the_backstop_survives_a_provider_without_a_close_column():
    """`adj_close` and no `close` must not raise -- that bypassed forever."""
    store = FakeStore(
        coverage={("AAPL", "1d"): [(D("2024-01-01"), D("2024-12-31"))]},
        bars={("AAPL", "1d"): bars_frame(
            ["2024-12-30", "2024-12-31"], close=200.0, column="adj_close")},
    )
    cache, calls = make_cache(
        store, [[{"date": "2024-12-30", "adj_close": 100.0},
                 {"date": "2025-01-02", "adj_close": 100.0}]],
    )
    now = D("2025-01-05")
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-01"), now,
        "EquityHistorical", {"symbol": "AAPL"}, None, now=now,
    )
    assert meta["cache"] != "bypass"
    # adj_close is the shared column, and it drifted -> the split was caught.
    assert ("AAPL", "1d") in store.dropped


async def test_the_backstop_skips_when_no_price_column_is_shared():
    """Nothing comparable -> skip the check, never raise, never drop."""
    store = FakeStore(
        coverage={("AAPL", "1d"): [(D("2024-01-01"), D("2024-12-31"))]},
        bars={("AAPL", "1d"): bars_frame(["2024-12-30", "2024-12-31"], close=200.0)},
    )
    cache, calls = make_cache(
        store, [[{"date": "2024-12-30", "adj_close": 1.0},
                 {"date": "2025-01-02", "adj_close": 1.0}]],
    )
    now = D("2025-01-05")
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-01"), now,
        "EquityHistorical", {"symbol": "AAPL"}, None, now=now,
    )
    assert meta["cache"] == "partial"
    assert store.dropped == []


async def test_the_backstop_compares_across_a_timezone_mismatch():
    """tz-aware upstream vs naive cache must still compare, not silently pass."""
    store = FakeStore(
        coverage={("AAPL", "1d"): [(D("2024-01-01"), D("2024-12-31"))]},
        bars={("AAPL", "1d"): bars_frame(["2024-12-30", "2024-12-31"], close=200.0)},
    )
    cache, calls = make_cache(
        store, [[{"date": "2024-12-30T00:00:00+00:00", "close": 100.0}],
                [{"date": "2024-01-02", "close": 100.0}]],
    )
    now = D("2025-01-05")
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-01"), now,
        "EquityHistorical", {"symbol": "AAPL"}, None, now=now,
    )
    assert ("AAPL", "1d") in store.dropped


async def test_unreachable_kdb_passes_through():
    """The cache must never be the reason a request fails."""

    class DeadStore(FakeStore):
        def read_coverage(self, symbol, interval):
            raise KdbUnavailable("no q")

    cache, calls = make_cache(DeadStore(), [[{"date": "2024-01-02", "close": 1.0}]])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-01"), D("2024-01-31"),
        "EquityHistorical", {"symbol": "AAPL"}, None,
    )
    assert meta["cache"] == "bypass"
    # Same shape as the cached path: `date` is a Timestamp either way.
    assert rows == [{"date": pd.Timestamp("2024-01-02"), "close": 1.0}]
    assert meta["rows_from_upstream"] + meta["rows_from_cache"] == len(rows)


async def test_dead_q_midway_still_returns_data():
    """A write failing after a successful fetch must not lose the fetched rows
    -- nor refetch what the network already paid for."""

    class FlakyStore(FakeStore):
        def write_bars(self, symbol, interval, df):
            raise RuntimeError("Attempted to use a closed IPC connection")

    cache, calls = make_cache(FlakyStore(), [[{"date": "2024-01-02", "close": 1.0}]])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-01"), D("2024-01-31"),
        "EquityHistorical", {"symbol": "AAPL"}, None,
    )
    assert rows == [{"date": pd.Timestamp("2024-01-02"), "close": 1.0}]
    assert meta["cache"] == "bypass"
    assert len(calls) == 1          # the window was already fetched: do not pay twice
    assert meta["gaps_fetched"] == 1
    assert meta["upstream_ms"] > 0


async def test_bypass_metadata_counts_every_fetch_that_happened():
    """A store that dies on the SECOND gap must still report all three fetches
    and all the time they took."""

    class FlakyStore(FakeStore):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.writes = 0

        def write_bars(self, symbol, interval, df):
            self.writes += 1
            if self.writes == 2:
                raise RuntimeError("Attempted to use a closed IPC connection")
            super().write_bars(symbol, interval, df)

    store = FlakyStore(
        coverage={("AAPL", "1d"): [(D("2024-02-01"), D("2024-02-29"))]},
        bars={("AAPL", "1d"): bars_frame(["2024-02-15"])},
    )
    cache, calls = make_cache(store, [
        [{"date": "2024-01-15", "close": 1.0}],
        [{"date": "2024-03-15", "close": 1.0}],
        [{"date": "2024-02-15", "close": 1.0}],
    ])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-01"), D("2024-03-31"),
        "EquityHistorical", {"symbol": "AAPL"}, None, now=D("2024-06-01"),
    )
    assert meta["cache"] == "bypass"
    # Two gap fetches happened before the failure; only the still-missing
    # middle is fetched on the way out.
    assert len(calls) == 3
    assert meta["gaps_fetched"] == 3
    assert meta["upstream_ms"] > 0
    assert {pd.Timestamp(r["date"]) for r in rows} == {
        pd.Timestamp("2024-01-15"), pd.Timestamp("2024-02-15"), pd.Timestamp("2024-03-15")
    }
    assert meta["rows_from_upstream"] + meta["rows_from_cache"] == len(rows)


async def test_eviction_runs_against_the_budget_not_the_workspace():
    store = FakeStore(heap=10_000_000)
    cache, _ = make_cache(store, [[{"date": "2024-01-02", "close": 1.0}]], memory_mb=1024)
    await cache.get(
        "AAPL", "1d", D("2024-01-01"), D("2024-01-31"),
        "EquityHistorical", {"symbol": "AAPL"}, None,
    )
    assert store.evicted_to == int(1024 * 1024 * 1024 * 0.75)


async def test_concurrent_requests_for_one_symbol_fetch_once():
    """Two widgets opening the same live chart must not both fetch the history.

    Requesting up to `now` is the real case: the second request is serialised
    behind the first and finds everything cached except the still-forming bar,
    so it pays for that bar and nothing else.
    """
    import asyncio

    store = FakeStore()
    now = D("2025-06-10T15:00:00")
    cache, calls = make_cache(store, [[{"date": "2025-06-09", "close": 1.0}]])
    args = ("AAPL", "1d", D("2024-01-01"), now,
            "EquityHistorical", {"symbol": "AAPL"}, None)
    await asyncio.gather(cache.get(*args, now=now), cache.get(*args, now=now))
    assert len(calls) == 2
    # Exactly one call fetched the 18 months of history...
    assert calls[0]["start_date"] == D("2024-01-01").date()
    # ...and the other only the tail, not the whole window again.
    assert calls[1]["start_date"] >= D("2025-06-06").date()


async def test_concurrent_requests_for_a_closed_window_fetch_once():
    """With no live tail involved, the second request costs nothing at all."""
    import asyncio

    store = FakeStore()
    cache, calls = make_cache(store, [[{"date": "2024-01-02", "close": 1.0}]])
    args = ("AAPL", "1d", D("2024-01-01"), D("2024-01-31"),
            "EquityHistorical", {"symbol": "AAPL"}, None)
    await asyncio.gather(*(cache.get(*args, now=D("2024-06-01")) for _ in range(2)))
    assert len(calls) == 1


async def test_intraday_gaps_go_out_as_whole_day_dates_and_are_covered_as_such():
    """The provider's query params only take dates, so a 14:00->15:07 hole goes
    on the wire as one DATE -- and the whole day it brings back is recorded as
    coverage, so the morning is never fetched twice. What is SERVED is still
    narrowed to the caller's window."""
    store = FakeStore()
    first_now = D("2025-06-10T15:07:00")
    cache, calls = make_cache(store, [
        [{"date": "2025-06-10T09:30:00", "close": 1.0},   # before the window
         {"date": "2025-06-10T14:00:00", "close": 1.0},
         {"date": "2025-06-10T15:00:00", "close": 1.0}],
        [{"date": "2025-06-10T15:05:00", "close": 2.0}],
    ])
    rows, meta = await cache.get(
        "AAPL", "5m", D("2025-06-10T14:00:00"), first_now,
        "EquityHistorical", {"symbol": "AAPL"}, None, now=first_now,
    )
    # Dates, never datetimes: pydantic rejects a datetime with a time of day.
    assert calls[0]["start_date"] == calls[0]["end_date"] == date(2025, 6, 10)
    assert not isinstance(calls[0]["start_date"], datetime)
    assert not isinstance(calls[0]["end_date"], datetime)
    # The 09:30 bar was stored but is outside the requested window.
    assert [pd.Timestamp(r["date"]) for r in rows] == [
        pd.Timestamp("2025-06-10T14:00:00"), pd.Timestamp("2025-06-10T15:00:00")
    ]
    assert meta["rows_from_cache"] + meta["rows_from_upstream"] == len(rows)
    # Coverage spans the whole day up to the last COMPLETE bar, no further.
    assert store.coverage[("AAPL", "5m")] == [
        (D("2025-06-10T00:00:00"), D("2025-06-10T15:00:00"))
    ]

    later = D("2025-06-10T15:12:00")
    rows, meta = await cache.get(
        "AAPL", "5m", D("2025-06-10T14:00:00"), later,
        "EquityHistorical", {"symbol": "AAPL"}, None, now=later,
    )
    # One gap -- the newly formed bars -- not the morning all over again.
    assert meta["cache"] == "partial"
    assert meta["gaps_fetched"] == 1
    assert len(calls) == 2
    assert served(rows, "2025-06-10T15:05:00")["close"] == 2.0


@pytest.mark.parametrize(
    "interval,start,end,now",
    [
        ("1d", D("2024-01-01"), D("2024-01-31"), D("2024-06-01")),
        ("5m", D("2025-06-10T09:30:00"), D("2025-06-10T15:12:00"),
         D("2025-06-10T15:12:00")),
    ],
)
async def test_emitted_params_validate_against_the_real_query_model(
    interval, start, end, now
):
    """The fake upstream validates nothing, which is how datetimes on the wire
    got through review. This one runs OpenBB's OWN query-params model over the
    parameters the cache emits -- the model that types start_date as `date` and
    rejects a datetime carrying a time of day."""
    from openbb_core.provider.standard_models.equity_historical import (
        EquityHistoricalQueryParams,
    )

    calls = []

    async def validating_fetch(provider, model, params, credentials):
        calls.append(dict(params))
        EquityHistoricalQueryParams(**params)  # raises if the cache emits junk
        return [{"date": "2024-01-02", "close": 1.0}]

    cache = ReadThroughCache(FakeStore(), cfg())
    cache._fetch_gap = validating_fetch
    rows, meta = await cache.get(
        "AAPL", interval, start, end,
        "EquityHistorical", {"symbol": "AAPL"}, None, now=now,
    )
    # Nothing raised, and the failure did not merely hide in a bypass.
    assert meta["cache"] != "bypass"
    assert calls and len(calls) == 1
    for call in calls:
        model = EquityHistoricalQueryParams(**call)
        assert isinstance(model.start_date, date)
        assert not isinstance(model.start_date, datetime)


async def test_an_empty_range_is_fetched_once_and_remembered():
    """The pre-IPO prefix of a zoomed-out chart: the provider legitimately has
    nothing. "We asked and there is no data" is what the coverage table is for,
    so the second and third requests must cost nothing."""
    store = FakeStore()
    cache, calls = make_cache(store, [[]])
    args = ("AAPL", "1d", D("2019-01-01"), D("2019-12-31"),
            "EquityHistorical", {"symbol": "AAPL"}, None)
    for _ in range(3):
        rows, meta = await cache.get(*args, now=D("2024-06-01"))
    assert rows == []
    assert len(calls) == 1
    assert meta["cache"] == "hit"
    assert meta["gaps_fetched"] == 0
    assert store.written == []          # nothing to write, but coverage was kept
    assert store.coverage[("AAPL", "1d")] == [(D("2019-01-01"), D("2019-12-31"))]


async def test_the_backstop_widens_only_the_last_gap():
    """Two gaps, one call each. The corporate-action overlap rides on the gap
    that reaches the tail; widening an interior gap would re-request cached
    bars for nothing."""
    store = FakeStore(
        coverage={("AAPL", "1d"): [(D("2024-01-01"), D("2024-01-10")),
                                   (D("2024-02-01"), D("2024-02-10"))]},
        bars={("AAPL", "1d"): bars_frame(["2024-01-05", "2024-02-05"])},
    )
    cache, calls = make_cache(store, [[{"date": "2024-01-15", "close": 1.0}]])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-01"), D("2024-03-31"),
        "EquityHistorical", {"symbol": "AAPL"}, None, now=D("2024-06-01"),
    )
    assert len(calls) == 2
    # The interior gap starts exactly where the hole starts: not widened.
    assert calls[0]["start_date"] == D("2024-01-11").date()
    # The last gap reaches back _OVERLAP_BARS into cached data -- same call.
    assert calls[1]["start_date"] == D("2024-02-08").date()
    assert calls[1]["end_date"] == D("2024-03-31").date()


async def test_row_counts_survive_padding_on_a_partial_hit():
    """Cached rows AND padding outside the window in one request: counting the
    cache side as `len(served) - len(everything fetched)` gets this wrong."""
    store = FakeStore(
        coverage={("AAPL", "1d"): [(D("2024-01-01"), D("2024-01-10"))]},
        bars={("AAPL", "1d"): bars_frame(["2024-01-02", "2024-01-03"])},
    )
    cache, calls = make_cache(store, [[
        {"date": "2024-01-11", "close": 1.0},
        {"date": "2024-01-25", "close": 1.0},   # padding, past the window
        {"date": "2024-01-26", "close": 1.0},   # padding, past the window
    ]])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-01"), D("2024-01-20"),
        "EquityHistorical", {"symbol": "AAPL"}, None, now=D("2024-06-01"),
    )
    assert len(rows) == 3
    assert meta["rows_from_upstream"] == 1
    assert meta["rows_from_cache"] == 2
    assert meta["rows_from_cache"] + meta["rows_from_upstream"] == len(rows)


async def test_daily_gaps_are_sent_as_dates():
    store = FakeStore()
    cache, calls = make_cache(store, [[{"date": "2024-01-02", "close": 1.0}]])
    await cache.get(
        "AAPL", "1d", D("2024-01-01"), D("2024-01-31"),
        "EquityHistorical", {"symbol": "AAPL"}, None, now=D("2024-06-01"),
    )
    assert calls[0]["start_date"] == D("2024-01-01").date()
    assert not isinstance(calls[0]["start_date"], datetime)


async def test_row_counts_ignore_padding_outside_the_window():
    """Providers pad. Padding is fetched but never served, so it cannot be
    counted -- the two row counts must add up to what came back."""
    store = FakeStore()
    cache, calls = make_cache(store, [[
        {"date": "2024-01-01", "close": 1.0},   # before the window
        {"date": "2024-01-02", "close": 1.0},
        {"date": "2024-01-03", "close": 1.0},
        {"date": "2024-01-04", "close": 1.0},
        {"date": "2024-01-08", "close": 1.0},   # after the window
    ]])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-02"), D("2024-01-04"),
        "EquityHistorical", {"symbol": "AAPL"}, None, now=D("2024-06-01"),
    )
    assert len(rows) == 3
    assert meta["rows_from_upstream"] == 3
    assert meta["rows_from_cache"] == 0
    assert meta["rows_from_cache"] + meta["rows_from_upstream"] == len(rows)


async def test_row_counts_split_a_partial_hit():
    store = FakeStore(
        coverage={("AAPL", "1d"): [(D("2024-01-01"), D("2024-01-10"))]},
        bars={("AAPL", "1d"): bars_frame(["2024-01-02", "2024-01-03"])},
    )
    cache, calls = make_cache(store, [[{"date": "2024-01-11", "close": 1.0}]])
    rows, meta = await cache.get(
        "AAPL", "1d", D("2024-01-01"), D("2024-01-20"),
        "EquityHistorical", {"symbol": "AAPL"}, None, now=D("2024-06-01"),
    )
    assert meta["cache"] == "partial"
    assert meta["rows_from_cache"] == 2
    assert meta["rows_from_upstream"] == 1
    assert meta["rows_from_cache"] + meta["rows_from_upstream"] == len(rows) == 3


@pytest.mark.parametrize(
    "interval,now,expected",
    [
        # Bar TIMESTAMPS, not period-end instants: the last bar that is done.
        ("1d", D("2025-06-10T15:00:00"), D("2025-06-09T00:00:00")),
        ("1d", D("2025-06-11T00:00:00"), D("2025-06-10T00:00:00")),   # rollover
        ("1d", D("2025-06-10T23:59:59"), D("2025-06-09T00:00:00")),
        ("1h", D("2025-06-10T15:30:00"), D("2025-06-10T14:00:00")),
        ("5m", D("2025-06-10T15:07:00"), D("2025-06-10T15:00:00")),
        ("5m", D("2025-06-10T15:05:00"), D("2025-06-10T15:00:00")),   # exact edge
        # A weekly bar is complete only once its week has ended.
        ("1w", D("2025-06-10T15:00:00"), D("2025-06-02T00:00:00")),   # Tue -> prev Mon
        ("1w", D("2025-06-09T00:00:00"), D("2025-06-02T00:00:00")),   # Mon 00:00
        ("1w", D("2025-06-08T23:00:00"), D("2025-05-26T00:00:00")),   # Sun, week not done
        # A monthly bar is complete only once its month has ended.
        ("1mo", D("2025-06-10T15:00:00"), D("2025-05-01T00:00:00")),
        ("1mo", D("2025-01-01T00:00:00"), D("2024-12-01T00:00:00")),
        ("3mo", D("2025-06-10T15:00:00"), D("2025-03-01T00:00:00")),
    ],
)
def test_last_complete_boundary(interval, now, expected):
    assert last_complete_boundary(interval, now) == expected


@pytest.mark.parametrize("interval", ["1d", "1w", "1mo", "1h", "5m"])
def test_last_complete_boundary_is_always_in_the_past(interval):
    now = D("2025-06-10T15:07:00")
    assert last_complete_boundary(interval, now) < now


async def test_a_weekly_request_does_not_cache_the_half_formed_week():
    now = D("2025-06-10T15:00:00")  # Tuesday
    store = FakeStore()
    cache, calls = make_cache(store, [[{"date": "2025-06-02", "close": 1.0}]])
    await cache.get(
        "AAPL", "1w", D("2025-01-01"), now,
        "EquityHistorical", {"symbol": "AAPL"}, None, now=now,
    )
    recorded = store.coverage[("AAPL", "1w")]
    assert all(end <= D("2025-06-02") for _, end in recorded)


def test_to_frame_handles_no_rows():
    frame = _to_frame([])
    assert frame.empty
    assert "t" in frame.columns


def test_to_frame_rejects_rows_without_a_timestamp():
    with pytest.raises(ValueError, match="no recognisable timestamp column"):
        _to_frame([{"close": 1.0, "volume": 10}])


def test_to_frame_accepts_alternative_stamp_names():
    frame = _to_frame([{"timestamp": "2024-01-02", "close": 1.0}])
    assert list(frame["t"]) == [pd.Timestamp("2024-01-02")]


def test_to_frame_resolves_a_date_and_t_collision():
    """A row carrying both `date` and a raw `t` used to rename into two columns
    called `t`, raising -- a silent, permanent bypass. The preference order
    decides and the loser is dropped."""
    frame = _to_frame([
        {"date": "2024-01-02", "t": 1704153600000, "close": 1.0},
        {"date": "2024-01-03", "t": 1704240000000, "close": 2.0},
    ])
    assert list(frame["t"]) == [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")]
    assert list(frame["close"]) == [1.0, 2.0]


def test_to_frame_normalises_timezones_and_sorts():
    frame = _to_frame([
        {"date": "2024-01-03T00:00:00+00:00", "close": 2.0},
        {"date": "2024-01-02T00:00:00+00:00", "close": 1.0},
    ])
    assert list(frame["close"]) == [1.0, 2.0]
    assert frame["t"].dt.tz is None
    assert frame["t"].iloc[0] == pd.Timestamp("2024-01-02")


def test_locks_are_bounded_and_loop_scoped():
    import asyncio

    from openbb_kdb.cache import _MAX_LOCKS

    cache = ReadThroughCache(FakeStore(), cfg())
    for i in range(_MAX_LOCKS + 50):
        cache._lock(f"SYM{i}", "1d")
    assert len(cache._locks) <= _MAX_LOCKS

    async def one():
        return cache._lock("AAPL", "1d")

    first = asyncio.run(one())
    second = asyncio.run(one())
    assert first is not second  # a lock is never reused across event loops


def test_interval_step_still_drives_adjacency():
    """Guard the assumption the boundary maths leans on."""
    from kdb_store.ranges import interval_step

    assert interval_step("1d") == timedelta(days=1)
    assert interval_step("5m") == timedelta(minutes=5)

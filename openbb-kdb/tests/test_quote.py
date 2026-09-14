"""Quote assembly: live tick over daily-bar session fields."""

import os
from datetime import date, datetime, timedelta

import pytest

from openbb_kdb.models.quote import build_quote


def test_a_tick_supplies_last_price_size_and_timestamp():
    got = build_quote("AAPL", {"time": datetime(2026, 8, 26, 15, 14), "price": 312.95,
                               "size": 40.0}, prev_close=309.9)
    assert got["symbol"] == "AAPL"
    assert got["last_price"] == 312.95
    assert got["last_size"] == 40.0
    assert got["last_timestamp"] == datetime(2026, 8, 26, 15, 14)


def test_a_tick_sourced_quote_is_not_marked_delayed():
    got = build_quote("AAPL", {"time": datetime(2026, 8, 26), "price": 312.95, "size": 1.0},
                      prev_close=309.9)
    assert got["delayed"] is False


def test_change_is_computed_against_the_previous_close():
    got = build_quote("AAPL", {"time": datetime(2026, 8, 26), "price": 312.95, "size": 1.0},
                      prev_close=309.9)
    assert got["prev_close"] == 309.9
    assert round(got["change"], 2) == 3.05
    assert round(got["change_percent"], 4) == round(3.05 / 309.9, 4)


def test_intraday_session_fields_are_absent_not_guessed():
    """Daily bars are end-of-day, so today's OHLC does not exist yet. Leaving
    them out is the documented behaviour (spec D5); inventing them from the
    tick would make open == high == low == last_price, which reads as real."""
    got = build_quote("AAPL", {"time": datetime(2026, 8, 26), "price": 312.95, "size": 1.0},
                      prev_close=309.9)
    for field in ("open", "high", "low", "volume"):
        assert got.get(field) is None


def test_a_missing_previous_close_leaves_change_undefined_rather_than_zero():
    """change=0.0 would render as "unchanged", which is a claim we cannot make."""
    got = build_quote("AAPL", {"time": datetime(2026, 8, 26), "price": 312.95, "size": 1.0},
                      prev_close=None)
    assert got["last_price"] == 312.95
    assert got.get("change") is None
    assert got.get("change_percent") is None


def test_a_zero_previous_close_does_not_divide_by_zero():
    got = build_quote("AAPL", {"time": datetime(2026, 8, 26), "price": 5.0, "size": 1.0},
                      prev_close=0.0)
    assert got.get("change_percent") is None


def test_no_tick_yields_no_row():
    assert build_quote("AAPL", None, prev_close=309.9) is None


@pytest.mark.asyncio
async def test_waiting_returns_the_first_tick_that_appears():
    from openbb_kdb.models.quote import _await_tick

    calls = {"n": 0}

    class Store:
        def latest_tick(self, symbol):
            calls["n"] += 1
            return {"time": datetime(2026, 8, 26), "price": 1.0, "size": 1.0} \
                if calls["n"] >= 2 else None

    assert await _await_tick(Store(), "AAPL", deadline=2.0) is not None
    assert calls["n"] >= 2


@pytest.mark.asyncio
async def test_waiting_gives_up_at_the_deadline_rather_than_hanging():
    from openbb_kdb.models.quote import _await_tick

    class Never:
        def latest_tick(self, symbol):
            return None

    assert await _await_tick(Never(), "AAPL", deadline=0.3) is None


def test_the_fetcher_is_registered_for_the_quote_model():
    """This registration is what puts `kdb` on /equity/price/quote."""
    import openbb_kdb

    assert openbb_kdb.kdb_provider.fetcher_dict["EquityQuote"] is not None


def test_transform_data_omits_a_fractional_last_size_rather_than_raising():
    """last_size is int | None on EquityQuoteData; kdb reports size as a
    float. A fractional size must not raise ValidationError -- it must
    validate through the real model with last_size simply absent."""
    from openbb_kdb.models.quote import KdbEquityQuoteFetcher

    data = {"symbol": "AAPL",
            "tick": {"time": datetime(2026, 8, 26), "price": 312.95, "size": 40.5},
            "prev_close": None}
    rows = KdbEquityQuoteFetcher.transform_data(None, data)
    assert len(rows) == 1
    assert rows[0].last_size is None


def test_transform_data_keeps_a_whole_last_size():
    from openbb_kdb.models.quote import KdbEquityQuoteFetcher

    data = {"symbol": "AAPL",
            "tick": {"time": datetime(2026, 8, 26), "price": 312.95, "size": 40.0},
            "prev_close": None}
    rows = KdbEquityQuoteFetcher.transform_data(None, data)
    assert rows[0].last_size == 40


@pytest.mark.asyncio
async def test_waiting_is_capped_by_the_deadline_even_when_a_single_call_blocks_past_it():
    """A cold/dead kdb session's own connect budget (or a wedged call) must
    not be able to push the total wait past `deadline` -- each `to_thread`
    call is bounded by `wait_for`, not just checked for between calls."""
    import time

    from openbb_kdb.models.quote import _await_tick

    class SlowThenTick:
        def latest_tick(self, symbol):
            time.sleep(0.6)
            return {"time": datetime(2026, 8, 26), "price": 1.0, "size": 1.0}

    start = time.monotonic()
    got = await _await_tick(SlowThenTick(), "AAPL", deadline=0.2)
    elapsed = time.monotonic() - start

    assert got is None
    assert elapsed < 0.6


@pytest.mark.asyncio
async def test_prev_close_comes_from_the_last_complete_daily_bar():
    from openbb_kdb.models.quote import _prev_close

    class Cache:
        async def get(self, **kwargs):
            # The real ReadThroughCache compares start/end against
            # last_complete_boundary()'s datetime (ranges.trim_tail /
            # .subtract) -- a bare `date` raises TypeError there, which
            # _get() swallows and routes to a live upstream bypass. A fake
            # that only checked values, not types, would never catch that.
            assert isinstance(kwargs["start"], datetime), "start must be a datetime"
            assert isinstance(kwargs["end"], datetime), "end must be a datetime"
            # ReadThroughCache.get returns (rows, metadata), not rows.
            return ([{"date": "2026-08-24", "close": 300.0},
                     {"date": "2026-08-25", "close": 309.9}], {})

    assert await _prev_close(Cache(), "AAPL", credentials=None) == 309.9


@pytest.mark.asyncio
async def test_a_failing_bar_lookup_does_not_fail_the_quote():
    """Spec: a missing daily bar yields last_price only, never an error."""
    from openbb_kdb.models.quote import _prev_close

    class Broken:
        async def get(self, **kwargs):
            raise RuntimeError("kdb down")

    assert await _prev_close(Broken(), "AAPL", credentials=None) is None


@pytest.mark.skipif(
    not os.getenv("KDB_QUOTE_LIVE_TEST"),
    reason="needs a running live-grid, kdb and EODHD key; set KDB_QUOTE_LIVE_TEST=1",
)
@pytest.mark.asyncio
async def test_live_quote_end_to_end():
    """Leases a real symbol, waits for a real tick, asserts a sane quote.

    Deliberately not asserting an exact price: the point is that the lease
    reached live-grid, a tick landed in kdb and the fields assembled.
    """
    from openbb_kdb.models.quote import KdbEquityQuoteFetcher

    query = KdbEquityQuoteFetcher.transform_query({"symbol": "AAPL"})
    raw = await KdbEquityQuoteFetcher.aextract_data(query, credentials=None)
    rows = KdbEquityQuoteFetcher.transform_data(query, raw)
    assert rows, "no quote returned -- is live-grid reachable and the market open?"
    assert rows[0].last_price and rows[0].last_price > 0


@pytest.mark.asyncio
async def test_prev_close_ignores_todays_still_forming_bar():
    """A window ending today includes today's forming bar (kdb never caches
    it as complete -- see test_tail_types.py docstring). _prev_close must
    skip it and return the previous session's close, not today's."""
    from openbb_kdb.models.quote import _prev_close

    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    class Cache:
        async def get(self, **kwargs):
            return ([{"date": yesterday, "close": 309.9},
                     {"date": today, "close": 500.0}], {})

    assert await _prev_close(Cache(), "AAPL", credentials=None) == 309.9


@pytest.mark.asyncio
async def test_prev_close_treats_a_non_numeric_close_as_missing_not_an_error():
    from openbb_kdb.models.quote import _prev_close

    class Cache:
        async def get(self, **kwargs):
            return ([{"date": (date.today() - timedelta(days=1)).isoformat(),
                       "close": "not-a-number"}], {})

    assert await _prev_close(Cache(), "AAPL", credentials=None) is None


@pytest.mark.asyncio
async def test_prev_close_is_bounded_by_a_timeout(monkeypatch):
    """D1 gives the tick read a 3s budget; a wedged bars fetch must not be
    allowed to push total quote latency past that unbounded."""
    import asyncio
    import time

    from openbb_kdb.models import quote as quote_mod

    monkeypatch.setattr(quote_mod, "PREV_CLOSE_TIMEOUT_S", 0.1)

    class Wedged:
        async def get(self, **kwargs):
            await asyncio.sleep(5)
            return ([], {})  # pragma: no cover - never reached

    start = time.monotonic()
    got = await quote_mod._prev_close(Wedged(), "AAPL", credentials=None)
    elapsed = time.monotonic() - start

    assert got is None
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_snapshot_client_returns_none_when_live_grid_is_down():
    from openbb_kdb.leasing import snapshot

    async def boom(url, params, timeout):
        raise OSError("connection refused")

    assert await snapshot("AAPL", get=boom) is None


@pytest.mark.asyncio
async def test_snapshot_client_returns_none_on_404():
    from openbb_kdb.leasing import snapshot

    async def missing(url, params, timeout):
        raise LookupError("404")

    assert await snapshot("AAPL", get=missing) is None


def test_a_snapshot_builds_a_quote_when_no_tick_exists():
    from openbb_kdb.models.quote import build_quote_from_snapshot

    got = build_quote_from_snapshot("AAPL", {"price": 312.95, "prev_close": 309.9})
    assert got["last_price"] == 312.95
    assert got["prev_close"] == 309.9
    assert round(got["change"], 2) == 3.05


def test_a_snapshot_sourced_quote_is_marked_delayed():
    """/snapshot always returns delayed: true; the marker must survive onto
    the assembled row so a consumer can tell a live tick from a ~20-minute
    -old REST price (spec asks for this in three places)."""
    from openbb_kdb.models.quote import build_quote_from_snapshot

    got = build_quote_from_snapshot("AAPL", {"price": 312.95, "delayed": True})
    assert got["delayed"] is True


def test_a_non_dict_snapshot_does_not_raise():
    """A malformed snapshot must degrade to no row, never raise -- nothing
    in the fallback leg may fail a quote."""
    from openbb_kdb.models.quote import build_quote_from_snapshot

    assert build_quote_from_snapshot("AAPL", "not-a-dict") is None
    assert build_quote_from_snapshot("AAPL", 312.95) is None


def test_a_non_numeric_snapshot_price_does_not_raise():
    from openbb_kdb.models.quote import build_quote_from_snapshot

    assert build_quote_from_snapshot("AAPL", {"price": "NA"}) is None


def test_no_tick_and_no_snapshot_yields_no_rows():
    """The only case the spec allows to return nothing."""
    from openbb_kdb.models.quote import KdbEquityQuoteFetcher

    query = KdbEquityQuoteFetcher.transform_query({"symbol": "AAPL"})
    rows = KdbEquityQuoteFetcher.transform_data(
        query, {"symbol": "AAPL", "tick": None, "prev_close": None, "snapshot": None}
    )
    assert rows == []

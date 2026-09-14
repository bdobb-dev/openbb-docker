"""Source adapters. Both engines must emit identical column names."""

import pytest

from app.ta.registry import resolve
from app.ta.sources import (
    CALLS_PER_REQUEST,
    EodhdSource,
    LocalSource,
    eodhd_query,
)
from tests.ta_helpers import col, cols, fixture_frame


def test_local_source_produces_the_registry_column_names():
    result = LocalSource().series(fixture_frame(), [resolve("rsi", period=14)])
    assert col("rsi", period=14) in result.frame.columns
    assert result.annotations == []
    assert result.calls == 0


def test_eodhd_query_maps_our_params_onto_their_names():
    q = eodhd_query(resolve("stoch", k=14, smooth_k=3, d=3))
    assert q["function"] == "stochastic"
    assert q["period"] == 14 and q["slow_kperiod"] == 3 and q["slow_dperiod"] == 3


def test_eodhd_query_uses_the_indicator_period_not_a_default():
    assert eodhd_query(resolve("sma", period=200))["period"] == 200


def test_eodhd_query_refuses_an_unmapped_indicator():
    with pytest.raises(ValueError, match="no EODHD equivalent"):
        eodhd_query(resolve("vwap"))


@pytest.mark.asyncio
async def test_eodhd_source_renames_response_fields_to_our_columns():
    async def fake_fetch(query):
        return [{"date": "2024-01-21", "uband": 3.0, "mband": 2.0, "lband": 1.0}]

    src = EodhdSource("k", fetch=fake_fetch)
    df = fixture_frame()
    result = await src.series(df, [resolve("bbands", period=20, k=2.0)],
                              "AAPL.US", "1d", "2024-01-21")
    assert set(cols(resolve("bbands", period=20, k=2.0))) <= set(result.frame.columns)
    assert result.calls == CALLS_PER_REQUEST


@pytest.mark.asyncio
async def test_an_unmapped_indicator_falls_back_to_local_and_is_annotated():
    async def fake_fetch(query):  # pragma: no cover - must not be called
        raise AssertionError("vwap has no EODHD mapping and must not be fetched")

    src = EodhdSource("k", fetch=fake_fetch)
    result = await src.series(fixture_frame(), [resolve("vwap")],
                              "AAPL.US", "1d", "2024-01-21")
    assert col("vwap") in result.frame.columns
    assert [a.source for a in result.annotations] == ["local"]
    assert result.calls == 0


@pytest.mark.asyncio
async def test_a_fetch_failure_degrades_to_local_rather_than_erroring():
    async def failing_fetch(query):
        raise RuntimeError("403 Forbidden")

    src = EodhdSource("k", fetch=failing_fetch)
    result = await src.series(fixture_frame(), [resolve("rsi", period=14)],
                              "AAPL.US", "1d", "2024-01-21")
    assert col("rsi", period=14) in result.frame.columns
    assert "403" in result.annotations[0].note


@pytest.mark.asyncio
async def test_a_response_missing_a_field_nulls_it_and_says_so():
    """EODHD field names have drifted before; a partial payload must not raise."""
    async def partial_fetch(query):
        # bbands wants uband/mband/lband; lband is absent from every row.
        return [{"date": "2024-01-21", "uband": 3.0, "mband": 2.0}]

    src = EodhdSource("k", fetch=partial_fetch)
    result = await src.series(fixture_frame(), [resolve("bbands", period=20, k=2.0)],
                              "AAPL.US", "1d", "2024-01-21")
    bb_lo = col("bbands", "bb_lo", period=20, k=2.0)
    assert bb_lo in result.frame.columns
    assert result.frame[bb_lo].null_count() == result.frame.height
    assert [a.column for a in result.annotations] == [bb_lo]


@pytest.mark.asyncio
async def test_an_unusable_payload_degrades_one_series_not_the_whole_chart():
    """A malformed response must fall back to local compute, per spec D8."""
    async def broken_fetch(query):
        return [{"no_date_key_at_all": 1.0}]

    src = EodhdSource("k", fetch=broken_fetch)
    result = await src.series(fixture_frame(), [resolve("rsi", period=14)],
                              "AAPL.US", "1d", "2024-01-21")
    rsi = col("rsi", period=14)
    assert rsi in result.frame.columns
    assert result.frame[rsi].null_count() < result.frame.height
    assert any("unusable" in a.note for a in result.annotations)


@pytest.mark.asyncio
async def test_a_failing_fetch_is_throttled_rather_than_retried_every_call():
    """A failure must start the backoff clock, or min_refetch_s never engages."""
    attempts = []

    async def always_fails(query):
        attempts.append(query)
        raise RuntimeError("503 Service Unavailable")

    src = EodhdSource("k", fetch=always_fails, min_refetch_s=3600)
    df, req = fixture_frame(), [resolve("rsi", period=14)]
    await src.series(df, req, "AAPL.US", "1d", "2024-01-21")
    await src.series(df, req, "AAPL.US", "1d", "2024-01-21")
    await src.series(df, req, "AAPL.US", "1d", "2024-01-21")
    assert len(attempts) == 1, "a failing indicator must not retry every call"


@pytest.mark.asyncio
async def test_the_cache_is_keyed_on_the_last_closed_bar():
    calls = []

    async def counting_fetch(query):
        calls.append(query)
        return [{"date": "2024-01-21", "rsi": 55.0}]

    src = EodhdSource("k", fetch=counting_fetch, min_refetch_s=0)
    df, req = fixture_frame(), [resolve("rsi", period=14)]
    await src.series(df, req, "AAPL.US", "1d", "2024-01-21")
    await src.series(df, req, "AAPL.US", "1d", "2024-01-21")
    assert len(calls) == 1, "same closed bar must be served from cache"
    await src.series(df, req, "AAPL.US", "1d", "2024-01-22")
    assert len(calls) == 2, "a new closed bar must refetch"


@pytest.mark.asyncio
async def test_cumulative_call_spend_is_tracked_for_health():
    async def fake_fetch(query):
        return [{"date": "2024-01-21", "rsi": 55.0}]

    src = EodhdSource("k", fetch=fake_fetch, min_refetch_s=0)
    assert src.total_calls == 0
    await src.series(fixture_frame(), [resolve("rsi", period=14)],
                     "AAPL.US", "1d", "2024-01-21")
    assert src.total_calls == CALLS_PER_REQUEST
    # A cached hit must not be billed again.
    await src.series(fixture_frame(), [resolve("rsi", period=14)],
                     "AAPL.US", "1d", "2024-01-21")
    assert src.total_calls == CALLS_PER_REQUEST


@pytest.mark.asyncio
async def test_an_intraday_chart_never_asks_eodhd_and_says_why():
    """EODHD's technical endpoint is daily only, and eodhd_query sends no
    interval -- so a 5m chart would fan one daily value across every bar."""
    async def fake_fetch(query):  # pragma: no cover - must not be called
        raise AssertionError("EODHD has no intraday technical data")

    src = EodhdSource("k", fetch=fake_fetch)
    result = await src.series(fixture_frame(), [resolve("sma", period=50)],
                              "AAPL.US", "5m", "2024-01-21")
    sma = col("sma", period=50)
    assert result.frame[sma].null_count() < result.frame.height
    assert result.calls == 0
    assert [(a.column, a.note) for a in result.annotations] == [
        (sma, "EODHD has no intraday data")
    ]

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Aggregating ticks into OHLCV buckets."""

from datetime import datetime

import pandas as pd
import pytest

from kdb_store.aggregate import aggregate_ticks, bucket_ns

D = lambda s: datetime.fromisoformat(s)  # noqa: E731


class FakeStore:
    """Returns a canned aggregation frame and records the call."""

    def __init__(self, frame=None):
        self.frame = frame if frame is not None else pd.DataFrame()
        self.calls = []

    def aggregate_frame(self, symbol, interval, start, end):
        self.calls.append((symbol, interval, start, end))
        return self.frame


def test_bucket_ns_for_supported_intervals():
    assert bucket_ns("1m") == 60_000_000_000
    assert bucket_ns("5m") == 300_000_000_000
    assert bucket_ns("1h") == 3_600_000_000_000
    assert bucket_ns("1d") == 86_400_000_000_000


def test_bucket_ns_rejects_unknown():
    with pytest.raises(ValueError):
        bucket_ns("1fortnight")


def test_aggregate_maps_q_columns_to_bar_rows():
    frame = pd.DataFrame({
        "t": [pd.Timestamp("2025-06-10T14:00:00")],
        "open": [100.0], "high": [103.0], "low": [100.0],
        "close": [101.0], "volume": [16.0], "vwap": [101.5],
    })
    rows = aggregate_ticks(
        FakeStore(frame), "AAPL", "1m", D("2025-06-10T14:00"), D("2025-06-10T15:00")
    )
    assert rows == [{
        "date": pd.Timestamp("2025-06-10T14:00:00"),
        "open": 100.0, "high": 103.0, "low": 100.0, "close": 101.0, "volume": 16.0,
        "vwap": 101.5,
    }]


def test_aggregate_nulls_vwap_when_the_bucket_had_no_sized_ticks():
    """size wavg price over all-zero sizes is q null (NaN) -- 'no trade
    data', never a fabricated number."""
    frame = pd.DataFrame({
        "t": [pd.Timestamp("2025-06-10T14:00:00")],
        "open": [100.0], "high": [103.0], "low": [100.0],
        "close": [101.0], "volume": [0.0], "vwap": [float("nan")],
    })
    rows = aggregate_ticks(
        FakeStore(frame), "AAPL", "1m", D("2025-06-10T14:00"), D("2025-06-10T15:00")
    )
    assert rows[0]["vwap"] is None


def test_aggregate_returns_empty_list_for_no_ticks():
    assert aggregate_ticks(FakeStore(), "AAPL", "1m", D("2025-01-01"), D("2025-01-02")) == []


def test_aggregate_passes_the_window_through_unchanged():
    store = FakeStore()
    start, end = D("2025-06-10T14:00"), D("2025-06-10T15:00")
    aggregate_ticks(store, "AAPL", "5m", start, end)
    assert store.calls == [("AAPL", "5m", start, end)]


def test_rows_are_time_ordered():
    frame = pd.DataFrame({
        "t": [pd.Timestamp("2025-06-10T14:01:00"), pd.Timestamp("2025-06-10T14:00:00")],
        "open": [99.0, 100.0], "high": [99.0, 103.0], "low": [99.0, 100.0],
        "close": [99.0, 101.0], "volume": [7.0, 16.0],
    })
    rows = aggregate_ticks(
        FakeStore(frame), "AAPL", "1m", D("2025-06-10T14:00"), D("2025-06-10T15:00")
    )
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The tick buffer: bounded, batched, and honest about what it dropped."""

from datetime import datetime, timedelta

from app.recorder import TickRecorder

D = lambda s: datetime.fromisoformat(s)  # noqa: E731


class FakeStore:
    def __init__(self, fail=False):
        self.batches = []
        self.pruned = []
        self.fail = fail

    def write_ticks(self, frame):
        if self.fail:
            raise RuntimeError("closed IPC connection")
        self.batches.append(frame)
        return len(frame)

    def prune_ticks(self, cutoff):
        self.pruned.append(cutoff)
        return 0


def make(**kw):
    store = FakeStore(**{k: v for k, v in kw.items() if k == "fail"})
    rec = TickRecorder(store, max_buffer=kw.get("max_buffer", 1000),
                       window=kw.get("window", timedelta(days=1)))
    return rec, store


def test_record_buffers_without_writing():
    rec, store = make()
    rec.record("AAPL", 100.0, 1.0, D("2025-06-10T14:00:00"))
    assert rec.buffered == 1
    assert store.batches == []


def test_flush_writes_one_batch_and_empties_the_buffer():
    rec, store = make()
    for i in range(5):
        rec.record("AAPL", 100.0 + i, 1.0, D("2025-06-10T14:00:00") + timedelta(seconds=i))
    assert rec.flush() == 5
    assert len(store.batches) == 1
    assert list(store.batches[0].columns) == ["time", "sym", "price", "size"]
    assert rec.buffered == 0
    assert rec.written == 5


def test_flush_with_an_empty_buffer_does_not_call_the_store():
    rec, store = make()
    assert rec.flush() == 0
    assert store.batches == []


def test_buffer_is_bounded_and_drops_oldest():
    """An unbounded buffer growing while q is down is the failure to prevent."""
    rec, _ = make(max_buffer=3)
    for i in range(5):
        rec.record("AAPL", float(i), 1.0, D("2025-06-10T14:00:00") + timedelta(seconds=i))
    assert rec.buffered == 3
    assert rec.dropped == 2
    frame = rec._frame()
    assert list(frame["price"]) == [2.0, 3.0, 4.0]


def test_a_failed_flush_clears_the_buffer_immediately():
    """Pins the discard itself: checked before any record() call can mask it
    through ordinary maxlen eviction."""
    rec, _ = make(fail=True, max_buffer=3)
    for i in range(3):
        rec.record("AAPL", float(i), 1.0, D("2025-06-10T14:00:00"))
    assert rec.flush() == 0
    assert rec.buffered == 0


def test_a_failed_flush_does_not_grow_the_buffer_without_bound():
    rec, _ = make(fail=True, max_buffer=3)
    for i in range(3):
        rec.record("AAPL", float(i), 1.0, D("2025-06-10T14:00:00"))
    assert rec.flush() == 0
    for i in range(3):
        rec.record("AAPL", float(i), 1.0, D("2025-06-10T14:00:00"))
    assert rec.buffered <= 3


def test_a_failed_flush_counts_the_whole_batch_as_dropped():
    rec, _ = make(fail=True, max_buffer=10)
    for i in range(3):
        rec.record("AAPL", float(i), 1.0, D("2025-06-10T14:00:00"))
    assert rec.dropped == 0
    rec.flush()
    assert rec.dropped == 3


def test_missing_size_records_zero():
    """Forex carries no trade size; sum over it must still be a number."""
    rec, store = make()
    rec.record("EURUSD", 1.08, None, D("2025-06-10T14:00:00"))
    rec.flush()
    assert list(store.batches[0]["size"]) == [0.0]


def test_prune_uses_the_configured_window():
    rec, store = make(window=timedelta(hours=2))
    rec.prune(now=D("2025-06-10T15:00:00"))
    assert store.pruned == [D("2025-06-10T13:00:00")]


def test_prune_survives_a_store_failure():
    rec, store = make(fail=True)
    store.prune_ticks = lambda cutoff: (_ for _ in ()).throw(RuntimeError("no q"))
    rec.prune(now=D("2025-06-10T15:00:00"))  # must not raise

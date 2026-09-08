# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Buffering ticks for batched writes into kdb.

Per-tick IPC cannot keep up with a live feed, so ticks accumulate here and are
written as one batch per flush. The buffer is bounded: when q is unreachable an
unbounded buffer would grow until the process died, which is a worse failure
than losing ticks a cache was never promised to keep.
"""

import logging
from collections import deque
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


class TickRecorder:
    """A bounded tick buffer with batched writes and a rolling-window prune."""

    def __init__(self, store, max_buffer: int = 100_000, window: timedelta = timedelta(days=1)):
        self.store = store
        self.window = window
        self._buf: deque = deque(maxlen=max_buffer)
        self.written = 0
        self.dropped = 0

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def record(self, sym: str, price: float, size, stamp: datetime) -> None:
        """Append one tick. Oldest is dropped when the buffer is full."""
        if len(self._buf) == self._buf.maxlen:
            self.dropped += 1
        self._buf.append((stamp, sym, float(price), float(size) if size is not None else 0.0))

    def _frame(self):
        import pandas as pd

        rows = list(self._buf)
        return pd.DataFrame(rows, columns=["time", "sym", "price", "size"])

    def flush(self) -> int:
        """Write the buffer as one batch. Returns rows written."""
        if not self._buf:
            return 0
        frame = self._frame()
        try:
            written = self.store.write_ticks(frame)
        except Exception as exc:  # noqa: BLE001 - a cache write must not kill the feed
            log.warning("tick flush failed, dropping %d buffered ticks: %s", len(frame), exc)
            self.dropped += len(frame)
            self._buf.clear()
            return 0
        self._buf.clear()
        self.written += written
        return written

    def prune(self, now: datetime | None = None) -> int:
        """Drop ticks older than the rolling window. Never raises."""
        cutoff = (now or datetime.now()) - self.window
        try:
            return self.store.prune_ticks(cutoff)
        except Exception as exc:  # noqa: BLE001
            log.warning("tick prune failed: %s", exc)
            return 0

    def stats(self) -> dict:
        """Counters for /health."""
        return {"buffered": self.buffered, "written": self.written, "dropped": self.dropped}

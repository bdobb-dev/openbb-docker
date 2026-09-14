"""Runnable (pyarrow/deltalake/pytest-free) tests for `tick_vault.settle`
(task-8 brief): `settle_pass`/`daily_pass` against fakes (`tick_vault.
capture.FakeTransport`, a duck-typed fake `CaptureStore`, fake `writer`/
`events_writer`, and a fake `existing_latest_reader` seam).

Reuses the same fixture/incoming-payload construction as `tests/
test_diffing.py` (`tests/fixtures/eodhd_ticks_sample.json`, modified so
seq 1002's price changes, seq 1003 vanishes, and seq 1004 is newly
present) so settle's REVISED/CANCELLED/LATE_ADD behavior is exercised
against the exact same known-good diff shape `tick_vault.diffing`'s own
tests already assert on.
"""
import copy
import datetime as dt
import json
from pathlib import Path

import pandas as pd

from tick_vault.capture import FakeTransport
from tick_vault.engine import Budget, SpanMemory
from tick_vault.settle import (
    STATUS_FAILED,
    STATUS_NO_CHANGES,
    STATUS_SETTLED,
    daily_pass,
    settle_pass,
)
from tick_vault.tick_parser import COLUMNS, parse_tick_payload

UTC = dt.timezone.utc
NOW = dt.datetime(2024, 1, 10, 12, 0, tzinfo=UTC)

# A single-day window (fits in one default 7-day-span window -> exactly
# one HTTP call/bronze capture per settle attempt), covering the fixture's
# tick timestamps (2021-11-05, well inside this range).
FROM_SEC = 1636118400
TO_SEC = FROM_SEC + 86399

PAYLOAD = json.loads((Path(__file__).parent / "fixtures/eodhd_ticks_sample.json").read_text())

INCOMING_PAYLOAD = copy.deepcopy(PAYLOAD)
# seq 1002 price revised (150.26 -> 150.27)
for record in INCOMING_PAYLOAD:
    if record["seq"] == 1002:
        record["price"] = 150.27
# seq 1003 dropped (revealed as cancelled)
INCOMING_PAYLOAD = [r for r in INCOMING_PAYLOAD if r["seq"] != 1003]
# seq 1004 appended (late add)
INCOMING_PAYLOAD.append(
    {"ts": 1636119010000, "price": 151.00, "shares": 10, "seq": 1004,
     "sl": "@   ", "mkt": "Q", "sub_mkt": "", "ex": "US"}
)


def _tick_json(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


class _FakeRecord:
    def __init__(self, capture_id):
        self.capture_id = capture_id


class FakeStore:
    """Duck-typed stand-in for `tick_vault.capture.CaptureStore`: only
    implements `.record(...)`, same convention as `tests/
    test_engine_core.py`'s own `FakeStore`."""

    def __init__(self):
        self.calls = []
        self._n = 0

    def record(self, **kwargs):
        self._n += 1
        self.calls.append(kwargs)
        return _FakeRecord(capture_id=f"cap_fake_{self._n}")


class FakeWriter:
    """Fake `write_tick_versions` replacement: records every frame it's
    asked to write."""

    def __init__(self):
        self.writes = []

    def __call__(self, root, df, *, vendor_request_symbol, committed_at, source_system, price_venue_type):
        self.writes.append(df.copy())
        return len(df)


class FakeEventsWriter:
    """Fake `write_correction_events` replacement."""

    def __init__(self):
        self.writes = []

    def __call__(self, root, events_df):
        self.writes.append(events_df.copy())
        return len(events_df)


def _item():
    return {
        "listing_id": "lst_a", "instrument_id": "ins_a",
        "vendor_symbol_at_date": "AAA.US",
        "request_from_sec": FROM_SEC, "request_to_sec": TO_SEC,
    }


def _existing():
    return parse_tick_payload(
        PAYLOAD, capture_id="cap_seed", listing_id="lst_a",
        instrument_id="ins_a", observed_at=NOW - dt.timedelta(days=1),
    )


# ---------------------------------------------------------------------------
# 1. settle_pass revises/cancels/late-adds, unchanged rows emit nothing
# ---------------------------------------------------------------------------

def test_settle_pass_revises_cancels_and_late_adds():
    existing = _existing()
    transport = FakeTransport([(200, _tick_json(INCOMING_PAYLOAD))])
    store = FakeStore()
    writer = FakeWriter()
    events_writer = FakeEventsWriter()
    span_memory = SpanMemory()
    budget = Budget(sleeper=lambda seconds: None)

    def existing_latest_reader(root, item):
        return existing

    result = settle_pass(
        transport, store, "/tmp/fake-root", _item(),
        existing_latest_reader=existing_latest_reader,
        span_memory=span_memory, budget=budget, now=NOW,
        writer=writer, events_writer=events_writer,
    )

    assert result.status == STATUS_SETTLED
    assert result.captures == 1
    assert result.new_versions == 3
    assert result.revisions == 1
    assert result.cancellations == 1
    assert result.late_adds == 1
    assert result.error is None
    assert len(writer.writes) == 1
    assert len(events_writer.writes) == 1

    written = writer.writes[0].to_dict("records")

    revised = [r for r in written if r["is_correction"]]
    assert len(revised) == 1
    revised = revised[0]
    assert revised["logical_tick_id"].endswith("|1002")
    assert revised["revision_number"] == 2
    assert revised["available_at_ts"] == NOW  # settle knowledge time, not the original trade time
    assert revised["supersedes_tick_version_id"] is not None

    tomb = [r for r in written if r["is_cancelled"]]
    assert len(tomb) == 1
    assert tomb[0]["logical_tick_id"].endswith("|1003")

    late = [r for r in written if r["is_late_add"]]
    assert len(late) == 1
    assert late[0]["logical_tick_id"].endswith("|1004")
    assert late[0]["revision_number"] == 1
    assert late[0]["available_at_ts"] == NOW

    ids = [r["logical_tick_id"] for r in written]
    assert not any(i.endswith("|1001") for i in ids), "unchanged row must produce no new version"

    events = events_writer.writes[0].to_dict("records")
    assert len(events) == 3
    assert all(e["revealing_capture_id"] == "cap_fake_1" for e in events)
    assert sorted(e["correction_kind"] for e in events) == ["CANCELLED", "LATE_ADD", "REVISED"]


# ---------------------------------------------------------------------------
# 2. second identical settle (existing updated to post-settle latest) is a
#    no-op: NO_CHANGES, zero further writer/events_writer calls.
# ---------------------------------------------------------------------------

def test_settle_pass_second_identical_settle_is_no_op():
    existing = _existing()
    transport = FakeTransport([(200, _tick_json(INCOMING_PAYLOAD))])
    store = FakeStore()
    writer = FakeWriter()
    events_writer = FakeEventsWriter()
    span_memory = SpanMemory()
    budget = Budget(sleeper=lambda seconds: None)

    state = {"existing": existing}

    def existing_latest_reader(root, item):
        return state["existing"]

    first = settle_pass(
        transport, store, "/tmp/fake-root", _item(),
        existing_latest_reader=existing_latest_reader,
        span_memory=span_memory, budget=budget, now=NOW,
        writer=writer, events_writer=events_writer,
    )
    assert first.status == STATUS_SETTLED

    # Build the post-settle "latest, non-cancelled" frame a real
    # latest_ticks() query would return: 1001 untouched, 1002 replaced by
    # its REVISED row, 1003's tombstone excluded entirely (cancelled ->
    # not latest-non-cancelled), 1004's LATE_ADD row included.
    written = writer.writes[0]
    unchanged = existing[existing["logical_tick_id"].str.endswith("|1001")]
    still_latest = written[~written["is_cancelled"]]
    state["existing"] = pd.concat([unchanged, still_latest], ignore_index=True)[COLUMNS]

    transport2 = FakeTransport([(200, _tick_json(INCOMING_PAYLOAD))])
    second = settle_pass(
        transport2, store, "/tmp/fake-root", _item(),
        existing_latest_reader=existing_latest_reader,
        span_memory=span_memory, budget=budget, now=NOW,
        writer=writer, events_writer=events_writer,
    )

    assert second.status == STATUS_NO_CHANGES
    assert second.new_versions == 0
    assert second.cancellations == 0
    assert second.late_adds == 0
    assert second.revisions == 0
    # writer/events_writer were NOT called again for the no-op settle.
    assert len(writer.writes) == 1
    assert len(events_writer.writes) == 1


# ---------------------------------------------------------------------------
# 3. settle fetch failure (all windows fail) -> FAILED, no writes
# ---------------------------------------------------------------------------

def test_settle_fetch_failure_is_failed_status_with_no_writes():
    existing = _existing()
    # A one-day window with 3 consecutive 500s exhausts max_window_retries
    # without ever halving (window_days == 1.0, not > 1.0) - same shape as
    # tests/test_engine_core.py's test_failed_week_leaves_manifest_retryable.
    transport = FakeTransport([(500, b"e1"), (500, b"e2"), (500, b"e3")])
    store = FakeStore()
    writer = FakeWriter()
    events_writer = FakeEventsWriter()
    span_memory = SpanMemory()
    budget = Budget(sleeper=lambda seconds: None)

    def existing_latest_reader(root, item):
        return existing

    result = settle_pass(
        transport, store, "/tmp/fake-root", _item(),
        existing_latest_reader=existing_latest_reader,
        span_memory=span_memory, budget=budget, now=NOW,
        writer=writer, events_writer=events_writer,
    )

    assert result.status == STATUS_FAILED
    assert result.new_versions == 0
    assert result.cancellations == 0
    assert result.late_adds == 0
    assert result.revisions == 0
    assert result.error is not None
    assert writer.writes == []
    assert events_writer.writes == []


# ---------------------------------------------------------------------------
# 4. daily_pass: same mechanics over a list of items
# ---------------------------------------------------------------------------

def test_daily_pass_runs_settle_pass_per_item_in_order():
    existing = _existing()
    transport = FakeTransport([
        (200, _tick_json(INCOMING_PAYLOAD)),
        (500, b"e1"), (500, b"e2"), (500, b"e3"),
    ])
    store = FakeStore()
    writer = FakeWriter()
    events_writer = FakeEventsWriter()
    span_memory = SpanMemory()
    budget = Budget(sleeper=lambda seconds: None)

    def existing_latest_reader(root, item):
        return existing

    item_a = _item()
    item_b = dict(_item())
    item_b["listing_id"] = "lst_b"
    item_b["vendor_symbol_at_date"] = "BBB.US"

    results = daily_pass(
        transport, store, "/tmp/fake-root", [item_a, item_b],
        existing_latest_reader=existing_latest_reader,
        span_memory=span_memory, budget=budget, now=NOW,
        writer=writer, events_writer=events_writer,
    )

    assert len(results) == 2
    assert results[0].status == STATUS_SETTLED
    assert results[1].status == STATUS_FAILED
    # Only the first (successful) item's diff was ever written.
    assert len(writer.writes) == 1
    assert len(events_writer.writes) == 1

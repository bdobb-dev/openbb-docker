"""Runnable (pyarrow/deltalake/pytest-free) tests for the pure/seam-injected
parts of `tick_vault.engine` (task-7 brief): `generate_manifest`, `symbol_at`,
`Budget`, `SpanMemory`, `build_tick_url`/`request_windows`/`dedup_ticks`, and
`fetch_week` (against fakes: `tick_vault.capture.FakeTransport`, a
duck-typed fake `CaptureStore`, and a fake `writer`).

`tick_vault.engine` imports bare (no pyarrow/deltalake at module scope -
only `tick_vault.ids`/`tick_vault.tick_parser`/`tick_vault.tick_writer`,
all themselves pyarrow-free at module scope), so it's importable and
runnable here. `ManifestBuilder`'s and `fetch_week`'s Delta-backed
*defaults* (real deltalake reads/writes) are exercised only in
`tests/deferred/test_engine.py` (pytest, real tmp_path lake).

This file covers the task brief's 8 named scenarios verbatim, plus a
handful of smaller pure-unit tests for `Budget`/`SpanMemory`/
`request_windows`/`symbol_at` in isolation.
"""
import datetime as dt
import json

import pandas as pd

from tick_vault.capture import FakeTransport
from tick_vault.engine import (
    Budget,
    BudgetExhausted,
    FetchResult,
    ManifestBuilder,
    SpanMemory,
    STATUS_BLOCKED_IDENTITY,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    build_tick_url,
    dedup_ticks,
    fetch_week,
    generate_manifest,
    request_windows,
    symbol_at,
    update_manifest_row,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2024, 1, 10, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _FakeRecord:
    def __init__(self, capture_id):
        self.capture_id = capture_id


class FakeStore:
    """Duck-typed stand-in for `tick_vault.capture.CaptureStore`: only
    implements `.record(...)` (see `fetch_week`'s docstring for why it
    calls `.record` directly rather than `.fetch_and_capture`)."""

    def __init__(self):
        self.calls = []
        self._n = 0

    def record(self, **kwargs):
        self._n += 1
        self.calls.append(kwargs)
        return _FakeRecord(capture_id=f"cap_fake_{self._n}")


class FakeWriter:
    """Fake `tick_vault.tick_writer.write_tick_versions` replacement:
    records every frame it's asked to write (never touches deltalake)."""

    def __init__(self):
        self.writes = []

    def __call__(self, root, df, *, vendor_request_symbol, committed_at, source_system, price_venue_type):
        self.writes.append(df.copy())
        return len(df)


def _tick_json(ts_ms, seq, price=100.0, shares=10):
    return json.dumps([
        {"ts": ts_ms, "price": price, "shares": shares, "seq": seq,
         "sl": "@   ", "mkt": "Q", "sub_mkt": "", "ex": "US"}
    ]).encode("utf-8")


# ---------------------------------------------------------------------------
# 1. manifest week overlap includes mid-week add
# ---------------------------------------------------------------------------

def test_manifest_week_overlap_includes_midweek_add():
    week_monday = dt.date(2024, 1, 1)  # a Monday
    mid_week_wednesday = dt.date(2024, 1, 3)

    membership_df = pd.DataFrame([
        {
            "listing_id": "lst_a",
            "instrument_id": "ins_a",
            "resolution_status": "RESOLVED",
            "membership_effective_from": mid_week_wednesday,
            "membership_effective_to": None,
        },
    ])
    assignments_df = pd.DataFrame([
        {
            "listing_id": "lst_a",
            "id_namespace": "EODHD_SYMBOL",
            "id_value": "AAA.US",
            "effective_from_ts": dt.datetime(2024, 1, 3, tzinfo=UTC),
            "effective_to_ts": None,
            "system_from_ts": dt.datetime(2024, 1, 3, tzinfo=UTC),
            "system_to_ts": None,
            "observed_at_ts": dt.datetime(2024, 1, 3, tzinfo=UTC),
        },
    ])

    out = generate_manifest(
        membership_df, assignments_df, week_monday, week_monday,
        existing_manifest_df=None, created_at=NOW,
    )

    assert len(out) == 1, "mid-week add must still get the whole week's manifest row"
    row = out.iloc[0]
    assert row["listing_id"] == "lst_a"
    assert row["week_monday"] == week_monday
    assert row["status"] == STATUS_PENDING
    # Monday itself predates the assignment (starts Wed) - the fallback
    # over the rest of the week must still resolve it.
    assert row["vendor_symbol_at_date"] == "AAA.US"


def test_manifest_membership_end_is_exclusive():
    # MEMBERSHIP_BOUNDARY_V2: EndDate is the first NON-member session, so a
    # removal effective Monday 2024-01-08 gets no row for that week.
    membership_df = pd.DataFrame([{
        "listing_id": "lst_a", "instrument_id": "ins_a", "resolution_status": "RESOLVED",
        "membership_effective_from": dt.date(2020, 1, 1),
        "membership_effective_to": dt.date(2024, 1, 8),
    }])
    assignments_df = pd.DataFrame([{
        "listing_id": "lst_a", "id_namespace": "EODHD_SYMBOL", "id_value": "AAA.US",
        "effective_from_ts": dt.datetime(2020, 1, 1, tzinfo=UTC), "effective_to_ts": None,
        "system_from_ts": dt.datetime(2020, 1, 1, tzinfo=UTC), "system_to_ts": None,
        "observed_at_ts": dt.datetime(2020, 1, 1, tzinfo=UTC),
    }])

    out = generate_manifest(
        membership_df, assignments_df, dt.date(2024, 1, 1), dt.date(2024, 1, 8),
        existing_manifest_df=None, created_at=NOW,
    )

    assert list(out["week_monday"]) == [dt.date(2024, 1, 1)]

    # `vault manifest --generate` passes ISO strings straight from argparse
    from_cli = generate_manifest(
        membership_df, assignments_df, "2024-01-01", "2024-01-08",
        existing_manifest_df=None, created_at=NOW,
    )
    assert list(from_cli["week_monday"]) == [dt.date(2024, 1, 1)]


# ---------------------------------------------------------------------------
# 2. blocked identity when unresolvable
# ---------------------------------------------------------------------------

def test_manifest_blocked_identity_when_unresolvable():
    week_monday = dt.date(2024, 1, 1)
    membership_df = pd.DataFrame([
        {
            "listing_id": "lst_b",
            "instrument_id": "ins_b",
            "resolution_status": "RESOLVED",
            "membership_effective_from": dt.date(2023, 1, 1),
            "membership_effective_to": None,
        },
    ])
    # No assignment rows at all for lst_b -> unresolvable on every day.
    assignments_df = pd.DataFrame(columns=[
        "listing_id", "id_namespace", "id_value", "effective_from_ts",
        "effective_to_ts", "system_from_ts", "system_to_ts", "observed_at_ts",
    ])

    out = generate_manifest(
        membership_df, assignments_df, week_monday, week_monday, created_at=NOW,
    )

    assert len(out) == 1
    row = out.iloc[0]
    assert row["status"] == STATUS_BLOCKED_IDENTITY
    assert row["vendor_symbol_at_date"] is None
    assert row["priority"] == 999999


def test_manifest_idempotent_skips_existing_pair():
    week_monday = dt.date(2024, 1, 1)
    membership_df = pd.DataFrame([
        {
            "listing_id": "lst_c", "instrument_id": "ins_c",
            "resolution_status": "RESOLVED",
            "membership_effective_from": dt.date(2023, 1, 1),
            "membership_effective_to": None,
        },
    ])
    assignments_df = pd.DataFrame(columns=[
        "listing_id", "id_namespace", "id_value", "effective_from_ts",
        "effective_to_ts", "system_from_ts", "system_to_ts", "observed_at_ts",
    ])
    existing = pd.DataFrame([{"listing_id": "lst_c", "week_monday": week_monday}])

    out = generate_manifest(
        membership_df, assignments_df, week_monday, week_monday,
        existing_manifest_df=existing, created_at=NOW,
    )
    assert out.empty


# ---------------------------------------------------------------------------
# 3. fetch_week happy path: writes bronze+silver, completes
# ---------------------------------------------------------------------------

def test_fetch_week_happy_path_writes_bronze_silver_and_completes():
    from_sec = 1704067200  # 2024-01-01T00:00:00Z (a Monday)
    to_sec = from_sec + 86399  # one day span, fits in a single default window

    transport = FakeTransport([(200, _tick_json(from_sec * 1000 + 500, 1))])
    store = FakeStore()
    writer = FakeWriter()
    span_memory = SpanMemory()
    budget = Budget(sleeper=lambda seconds: None)

    item = {
        "listing_id": "lst_a", "instrument_id": "ins_a",
        "vendor_symbol_at_date": "AAA.US",
        "request_from_sec": from_sec, "request_to_sec": to_sec,
    }

    result = fetch_week(
        transport, store, "/tmp/fake-root", item,
        span_memory=span_memory, budget=budget, now=NOW,
        writer=writer, monotonic=lambda: 0.0,
    )

    assert isinstance(result, FetchResult)
    assert result.status == STATUS_COMPLETE
    assert result.captures == 1
    assert result.rows_written == 1
    assert len(store.calls) == 1
    assert store.calls[0]["table"] == "bronze.eodhd_tick_capture"
    assert store.calls[0]["extra_cols"]["request_symbol"] == "AAA.US"
    assert len(writer.writes) == 1
    assert len(writer.writes[0]) == 1


# ---------------------------------------------------------------------------
# 4. span halving on slow 5xx, remembered
# ---------------------------------------------------------------------------

def test_fetch_week_halves_span_on_slow_5xx_and_remembers():
    from_sec = 1704067200  # Monday
    to_sec = from_sec + 604799  # exactly 7 days minus 1 second

    half_week_seconds = 302400
    first_half_end = from_sec + half_week_seconds - 1
    second_half_start = first_half_end + 1

    transport = FakeTransport([
        (500, b"server error"),                                  # full-week window -> slow 500
        (200, _tick_json(from_sec * 1000 + 1000, 1)),             # first half-week -> 200
        (200, _tick_json((second_half_start + 10) * 1000, 2)),    # second half-week -> 200
    ])
    store = FakeStore()
    writer = FakeWriter()
    span_memory = SpanMemory()
    budget = Budget(sleeper=lambda seconds: None)

    item = {
        "listing_id": "lst_mega", "instrument_id": "ins_mega",
        "vendor_symbol_at_date": "MEGA",
        "request_from_sec": from_sec, "request_to_sec": to_sec,
    }

    result = fetch_week(
        transport, store, "/tmp/fake-root", item,
        span_memory=span_memory, budget=budget, now=NOW,
        writer=writer, monotonic=lambda: 0.0,
    )

    assert span_memory.span("MEGA") == 3.5
    assert result.status == STATUS_COMPLETE
    assert result.captures == 3
    assert result.rows_written == 2


# ---------------------------------------------------------------------------
# 5. 429 budget wait then success
# ---------------------------------------------------------------------------

def test_fetch_week_budget_429_waits_then_succeeds():
    from_sec = 1704067200
    to_sec = from_sec + 86399

    transport = FakeTransport([
        (429, b""),
        (200, _tick_json(from_sec * 1000 + 500, 1)),
    ])
    store = FakeStore()
    writer = FakeWriter()
    span_memory = SpanMemory()
    sleeps = []
    budget = Budget(sleeper=lambda seconds: sleeps.append(seconds))

    item = {
        "listing_id": "lst_a", "instrument_id": "ins_a",
        "vendor_symbol_at_date": "AAA.US",
        "request_from_sec": from_sec, "request_to_sec": to_sec,
    }

    result = fetch_week(
        transport, store, "/tmp/fake-root", item,
        span_memory=span_memory, budget=budget, now=NOW,
        writer=writer, monotonic=lambda: 0.0,
    )

    assert sleeps == [1800]
    assert result.status == STATUS_COMPLETE
    assert result.rows_written == 1
    assert budget.consecutive_waits == 0  # reset on the subsequent success


def test_budget_raises_after_max_consecutive_waits():
    sleeps = []
    budget = Budget(sleeper=lambda seconds: sleeps.append(seconds), max_consecutive_waits=12)
    for _ in range(12):
        budget.wait()
    assert len(sleeps) == 12
    try:
        budget.wait()
        raise AssertionError("expected BudgetExhausted")
    except BudgetExhausted:
        pass


# ---------------------------------------------------------------------------
# 6. boundary-second dedup across windows
# ---------------------------------------------------------------------------

def test_boundary_second_dedup():
    from_sec = 1704067200
    to_sec = from_sec + 6 * 86400 - 1  # 6 days

    boundary_ts_ms = (from_sec + 3 * 86400) * 1000
    boundary_seq = 999

    span_memory = SpanMemory(spans={"DUP": 3.0})  # forces exactly 2 windows over 6 days
    transport = FakeTransport([
        (200, _tick_json(boundary_ts_ms, boundary_seq)),
        (200, _tick_json(boundary_ts_ms, boundary_seq)),  # same (ts, seq) again
    ])
    store = FakeStore()
    writer = FakeWriter()
    budget = Budget(sleeper=lambda seconds: None)

    item = {
        "listing_id": "lst_dup", "instrument_id": "ins_dup",
        "vendor_symbol_at_date": "DUP",
        "request_from_sec": from_sec, "request_to_sec": to_sec,
    }

    result = fetch_week(
        transport, store, "/tmp/fake-root", item,
        span_memory=span_memory, budget=budget, now=NOW,
        writer=writer, monotonic=lambda: 0.0,
    )

    assert result.captures == 2
    assert result.rows_written == 1, "duplicate (ts, seq) across windows must collapse to one row"
    assert len(writer.writes[0]) == 1


# ---------------------------------------------------------------------------
# 7. failed week leaves manifest retryable
# ---------------------------------------------------------------------------

def test_failed_week_leaves_manifest_retryable():
    from_sec = 1704067200
    to_sec = from_sec + 86399  # exactly one day -> window_days == 1.0, NOT > 1.0

    transport = FakeTransport([(500, b"e1"), (500, b"e2"), (500, b"e3")])
    store = FakeStore()
    writer = FakeWriter()
    span_memory = SpanMemory()
    budget = Budget(sleeper=lambda seconds: None)

    item = {
        "listing_id": "lst_fail", "instrument_id": "ins_fail",
        "vendor_symbol_at_date": "FAIL",
        "request_from_sec": from_sec, "request_to_sec": to_sec,
    }

    result = fetch_week(
        transport, store, "/tmp/fake-root", item,
        span_memory=span_memory, budget=budget, now=NOW,
        writer=writer, monotonic=lambda: 0.0,
    )

    assert result.status == STATUS_FAILED
    assert result.rows_written == 0
    assert result.error is not None
    assert writer.writes == []  # nothing written

    manifest_row = {
        "work_id": "wrk_x", "listing_id": "lst_fail", "week_monday": dt.date(2024, 1, 1),
        "status": STATUS_PENDING, "attempts": 0, "last_error": None,
        "updated_at_ts": None, "completed_at_ts": None,
    }
    updated = update_manifest_row(manifest_row, result, completed_at=NOW)
    assert updated["status"] == STATUS_FAILED
    assert updated["attempts"] == 1
    assert updated["last_error"] == result.error
    assert updated["completed_at_ts"] is None  # only COMPLETE stamps completed_at_ts


# ---------------------------------------------------------------------------
# 8. re-run of a completed tranche is idempotent
# ---------------------------------------------------------------------------

def test_rerun_completed_tranche_is_idempotent():
    from_sec = 1704067200
    to_sec = from_sec + 86399
    ts_ms = from_sec * 1000 + 500
    seq = 42

    # Simulates the SAME logical tick already sitting in silver from a
    # prior COMPLETE run.
    existing_df = pd.DataFrame([{"trade_ts_ms": ts_ms, "session_seq": seq}])

    def existing_ticks_reader(root, listing_id):
        return existing_df

    transport = FakeTransport([(200, _tick_json(ts_ms, seq))])
    store = FakeStore()
    writer = FakeWriter()
    span_memory = SpanMemory()
    budget = Budget(sleeper=lambda seconds: None)

    item = {
        "listing_id": "lst_a", "instrument_id": "ins_a",
        "vendor_symbol_at_date": "AAA.US",
        "request_from_sec": from_sec, "request_to_sec": to_sec,
    }

    result = fetch_week(
        transport, store, "/tmp/fake-root", item,
        span_memory=span_memory, budget=budget, now=NOW,
        writer=writer, existing_ticks_reader=existing_ticks_reader,
        monotonic=lambda: 0.0,
    )

    assert result.status == STATUS_COMPLETE
    assert result.rows_written == 0, "already-present (ts, seq) must not be re-appended"
    assert writer.writes == []
    assert result.captures == 1, "the HTTP call/bronze capture still happens on a re-run"


# ---------------------------------------------------------------------------
# small pure-unit tests: SpanMemory, request_windows, symbol_at, dedup_ticks,
# build_tick_url, ManifestBuilder wiring
# ---------------------------------------------------------------------------

def test_span_memory_defaults_halves_and_floors():
    mem = SpanMemory()
    assert mem.span("X") == 7.0
    assert mem.halve("X") == 3.5
    assert mem.span("X") == 3.5
    mem.set_span("X", 0.6)
    assert mem.halve("X") == 0.5  # floored at min_span_days
    assert mem.halve("X") == 0.5  # stays floored


def test_span_memory_json_round_trip():
    mem = SpanMemory()
    mem.halve("AAPL.US")
    payload = mem.to_json()
    restored = SpanMemory.from_json(payload)
    assert restored.span("AAPL.US") == 3.5
    assert restored.span("UNSEEN") == 7.0


def test_request_windows_covers_range_exactly():
    windows = request_windows(0, 604799, 7.0)
    assert windows == [(0, 604799)]
    windows2 = request_windows(0, 604799, 3.5)
    assert windows2 == [(0, 302399), (302400, 604799)]


def test_build_tick_url_shape():
    url = build_tick_url("AAPL.US", 100, 200, "TOKEN123")
    assert url == "https://eodhd.com/api/ticks?s=AAPL.US&from=100&to=200&api_token=TOKEN123"


def test_dedup_ticks_empty_frames():
    out = dedup_ticks([])
    assert out.empty
    assert list(out.columns)[:3] == ["tick_version_id", "logical_tick_id", "source_capture_id"]


def test_symbol_at_inverse_of_listing_resolution():
    assignments_df = pd.DataFrame([
        {
            "listing_id": "lst_z", "id_namespace": "EODHD_SYMBOL", "id_value": "ZZZ.US",
            "effective_from_ts": dt.datetime(2020, 1, 1, tzinfo=UTC),
            "effective_to_ts": dt.datetime(2022, 1, 1, tzinfo=UTC),
            "system_from_ts": dt.datetime(2020, 1, 1, tzinfo=UTC),
            "system_to_ts": None, "observed_at_ts": dt.datetime(2020, 1, 1, tzinfo=UTC),
        },
        {
            "listing_id": "lst_z", "id_namespace": "EODHD_SYMBOL", "id_value": "ZNEW.US",
            "effective_from_ts": dt.datetime(2022, 1, 1, tzinfo=UTC),
            "effective_to_ts": None,
            "system_from_ts": dt.datetime(2022, 1, 1, tzinfo=UTC),
            "system_to_ts": None, "observed_at_ts": dt.datetime(2022, 1, 1, tzinfo=UTC),
        },
    ])
    assert symbol_at(assignments_df, "lst_z", dt.date(2021, 6, 1)) == "ZZZ.US"
    assert symbol_at(assignments_df, "lst_z", dt.date(2023, 6, 1)) == "ZNEW.US"
    assert symbol_at(assignments_df, "lst_z", dt.date(2010, 1, 1)) is None
    assert symbol_at(assignments_df, "lst_missing", dt.date(2021, 6, 1)) is None


def test_manifest_builder_wires_seams_and_coerces_blocked_symbol_for_write():
    week_monday = dt.date(2024, 1, 1)
    membership_df = pd.DataFrame([
        {
            "listing_id": "lst_b", "instrument_id": "ins_b",
            "resolution_status": "RESOLVED",
            "membership_effective_from": dt.date(2023, 1, 1),
            "membership_effective_to": None,
        },
    ])
    assignments_df = pd.DataFrame(columns=[
        "listing_id", "id_namespace", "id_value", "effective_from_ts",
        "effective_to_ts", "system_from_ts", "system_to_ts", "observed_at_ts",
    ])
    existing_manifest_df = pd.DataFrame(columns=["listing_id", "week_monday"])
    written = {}

    def fake_manifest_writer(root, table, df):
        written["table"] = table
        written["df"] = df

    builder = ManifestBuilder(
        "/tmp/fake-root",
        membership_reader=lambda root: membership_df,
        assignments_reader=lambda root: assignments_df,
        manifest_reader=lambda root: existing_manifest_df,
        manifest_writer=fake_manifest_writer,
    )

    new_rows = builder.generate(week_monday, week_monday, created_at=NOW)

    assert len(new_rows) == 1
    assert new_rows.iloc[0]["vendor_symbol_at_date"] is None  # pure return value stays None
    assert written["table"] == "ops.backfill_manifest"
    # the persisted (written) frame coerces None -> "" for the NOT NULL column
    assert written["df"].iloc[0]["vendor_symbol_at_date"] == ""

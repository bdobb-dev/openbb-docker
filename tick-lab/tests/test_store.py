# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Date-range bounds, and the Delta Lake round-trip.

Unit tests build `TickStore` against a local filesystem path (the `base`
override) so they run with no MinIO. The round-trip-over-real-S3 tests below
are skipped unless TICK_LAB_TEST_S3=1 and the DELTA_S3_* variables point at a
disposable MinIO. See tick-lab/README.md.
"""

import os
import uuid
from datetime import date

import pandas as pd
import pytest

from tick_lab.config import S3Config, from_env
from tick_lab.store import LibraryNotFoundError, StoreWriteError, TickStore, to_bounds

pytestmark_integration = pytest.mark.skipif(
    os.getenv("TICK_LAB_TEST_S3") != "1",
    reason="set TICK_LAB_TEST_S3=1 with a disposable MinIO to run",
)

DUMMY_CFG = S3Config(endpoint="x", bucket="x", access="x", secret="x")


def test_date_end_covers_the_whole_day():
    _, end = to_bounds(None, "2023-05-12")
    assert end == pd.Timestamp("2023-05-12 23:59:59.999999999")


def test_datetime_end_is_exact():
    _, end = to_bounds(None, "2023-05-12 15:00:00")
    assert end == pd.Timestamp("2023-05-12 15:00:00")


def test_iso_datetime_midnight_with_t_separator_is_widened():
    # The plausible "I typed a datetime where a date was wanted" mistake:
    # midnight, spelled the codebase's own ISO way, must mean the whole day.
    _, end = to_bounds(None, "2023-05-12T00:00:00")
    assert end == pd.Timestamp("2023-05-12 23:59:59.999999999")


def test_datetime_midnight_with_space_separator_is_widened():
    _, end = to_bounds(None, "2023-05-12 00:00:00")
    assert end == pd.Timestamp("2023-05-12 23:59:59.999999999")


def test_date_object_end_is_widened():
    _, end = to_bounds(None, date(2023, 5, 12))
    assert end == pd.Timestamp("2023-05-12 23:59:59.999999999")


def test_none_bounds_stay_none():
    assert to_bounds(None, None) == (None, None)


def test_start_is_parsed():
    start, _ = to_bounds("2023-05-12", None)
    assert start == pd.Timestamp("2023-05-12 00:00:00")


# --- local-base unit tests (no MinIO required) -----------------------------


def test_read_on_missing_library_raises_clear_error(tmp_path):
    store = TickStore(DUMMY_CFG, base=str(tmp_path))

    with pytest.raises(ValueError, match="no-such-library"):
        store.read("no-such-library", "MSFT")


def test_read_on_missing_symbol_raises_plain_value_error(tmp_path):
    # The library itself exists (has a written symbol), but the requested
    # symbol does not -- a different, narrower error than the library not
    # existing at all, so it must NOT be a LibraryNotFoundError.
    store = TickStore(DUMMY_CFG, base=str(tmp_path))
    library = f"ticklabtest{uuid.uuid4().hex[:8]}"
    idx = pd.DatetimeIndex(["2023-05-12T13:30:00Z"]).tz_convert("UTC")
    store.write(library, "A", pd.DataFrame({"price": [1.0]}, index=idx))

    with pytest.raises(ValueError, match="B") as exc:
        store.read(library, "B")
    assert not isinstance(exc.value, LibraryNotFoundError)


def test_write_and_read_round_trips(tmp_path):
    store = TickStore(DUMMY_CFG, base=str(tmp_path))
    library = f"ticklabtest{uuid.uuid4().hex[:8]}"
    frame = pd.DataFrame(
        {"price": [1.0, 2.0], "volume": [10, 20]},
        index=pd.DatetimeIndex(
            ["2023-05-12T13:30:00Z", "2023-05-12T13:31:00Z"]
        ).tz_convert("UTC"),
    )
    store.write(library, "MSFT", frame)

    assert store.has(library, "MSFT")
    assert "MSFT" in store.list_symbols(library)

    got = store.read(library, "MSFT")
    assert str(got.index.tz) == "UTC"
    # Delta round-trips the index through a "date" column (so the index name
    # comes back as "date" even though `frame`'s was unnamed) and Parquet
    # stores timestamps at microsecond resolution (frame's is pandas' default
    # nanosecond) -- everything else (values, order) is unchanged.
    expected = frame.set_axis(frame.index.as_unit("us").rename("date"))
    pd.testing.assert_frame_equal(got, expected)


def test_write_overwrites_so_reload_is_idempotent(tmp_path):
    store = TickStore(DUMMY_CFG, base=str(tmp_path))
    library = f"ticklabtest{uuid.uuid4().hex[:8]}"
    idx = pd.DatetimeIndex(["2023-05-12T13:30:00Z"]).tz_convert("UTC")
    store.write(library, "MSFT", pd.DataFrame({"price": [1.0], "volume": [10]}, index=idx))
    store.write(library, "MSFT", pd.DataFrame({"price": [2.0], "volume": [20]}, index=idx))

    got = store.read(library, "MSFT")
    assert len(got) == 1
    assert got["price"].tolist() == [2.0]


def test_read_filters_by_date_range(tmp_path):
    store = TickStore(DUMMY_CFG, base=str(tmp_path))
    library = f"ticklabtest{uuid.uuid4().hex[:8]}"
    idx = pd.date_range("2023-05-12 13:30:00Z", periods=5, freq="1min")
    store.write(library, "MSFT", pd.DataFrame({"price": range(5), "volume": range(5)}, index=idx))

    got = store.read(library, "MSFT", start="2023-05-12 13:31:00", end="2023-05-12 13:32:00")
    assert len(got) == 2


def test_store_write_error_wraps_delta_error(tmp_path):
    # Simplest deterministic trigger for delta-rs raising DeltaError: the
    # target path already exists as a plain FILE, not a directory.
    store = TickStore(DUMMY_CFG, base=str(tmp_path))
    library = "lib"
    symbol_path = tmp_path / library / "SYM"
    symbol_path.parent.mkdir(parents=True)
    symbol_path.write_text("not a delta table")

    frame = pd.DataFrame(
        {"price": [1.0]},
        index=pd.DatetimeIndex(["2023-05-12T13:30:00Z"]).tz_convert("UTC"),
    )
    with pytest.raises(StoreWriteError):
        store.write(library, "SYM", frame)


# --- round-trip against real S3/MinIO ---------------------------------------


@pytestmark_integration
def test_round_trip_preserves_utc_index():
    cfg = from_env()
    store = TickStore(cfg)
    library = f"ticklabtest{uuid.uuid4().hex[:8]}"
    frame = pd.DataFrame(
        {"price": [1.0, 2.0], "volume": [10, 20]},
        index=pd.DatetimeIndex(
            ["2023-05-12T13:30:00Z", "2023-05-12T13:31:00Z"]
        ).tz_convert("UTC"),
    )
    store.write(library, "MSFT", frame)

    assert store.has(library, "MSFT")
    assert "MSFT" in store.list_symbols(library)

    got = store.read(library, "MSFT")
    assert str(got.index.tz) == "UTC"
    expected = frame.set_axis(frame.index.as_unit("us").rename("date"))
    pd.testing.assert_frame_equal(got, expected)


@pytestmark_integration
def test_read_filters_by_date_range_over_s3():
    cfg = from_env()
    store = TickStore(cfg)
    library = f"ticklabtest{uuid.uuid4().hex[:8]}"
    idx = pd.date_range("2023-05-12 13:30:00Z", periods=5, freq="1min")
    store.write(library, "MSFT", pd.DataFrame({"price": range(5), "volume": range(5)}, index=idx))

    got = store.read(library, "MSFT", start="2023-05-12 13:31:00", end="2023-05-12 13:32:00")
    assert len(got) == 2

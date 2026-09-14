"""Date-range bounds, and the ArcticDB round-trip against a real MinIO.

The round-trip test is skipped unless TICK_LAB_TEST_S3=1 and the ARCTICDB_S3_*
variables point at a disposable MinIO. See tick-lab/README.md.
"""

import os
import uuid
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from tick_lab.config import from_env
from tick_lab.store import TickStore, to_bounds

pytestmark_integration = pytest.mark.skipif(
    os.getenv("TICK_LAB_TEST_S3") != "1",
    reason="set TICK_LAB_TEST_S3=1 with a disposable MinIO to run",
)


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


def test_read_on_missing_library_raises_clear_error():
    store = TickStore.__new__(TickStore)
    store._cfg = None
    store._arctic = SimpleNamespace(has_library=lambda library: False)

    with pytest.raises(ValueError, match="no-such-library"):
        store.read("no-such-library", "MSFT")


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
    pd.testing.assert_frame_equal(got, frame)


@pytestmark_integration
def test_read_filters_by_date_range():
    cfg = from_env()
    store = TickStore(cfg)
    library = f"ticklabtest{uuid.uuid4().hex[:8]}"
    idx = pd.date_range("2023-05-12 13:30:00Z", periods=5, freq="1min")
    store.write(library, "MSFT", pd.DataFrame({"price": range(5), "volume": range(5)}, index=idx))

    got = store.read(library, "MSFT", start="2023-05-12 13:31:00", end="2023-05-12 13:32:00")
    assert len(got) == 2

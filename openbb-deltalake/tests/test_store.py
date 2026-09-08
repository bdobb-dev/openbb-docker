# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Tests for DeltaStore round-trips (requires a temporary local Delta store)."""

import pandas as pd
import pytest

from openbb_deltalake.store import DeltaStore


def _df():
    return pd.DataFrame(
        {"open": [1.0, 2.0], "high": [3.0, 4.0], "low": [0.5, 1.5], "close": [2.0, 3.0], "volume": [100, 200]},
        index=pd.to_datetime(["2026-01-01", "2026-01-02"]),
    )


class TestWriteRead:
    def test_write_and_read_dataframe(self, store: DeltaStore):
        store.write("AAPL", _df())
        df = store.read("AAPL", output="dataframe")
        assert len(df) == 2
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]

    def test_read_as_obbject(self, store: DeltaStore):
        store.write("AAPL", _df())
        obj = store.read("AAPL")
        assert len(obj.results) == 2
        assert obj.provider == "deltalake"
        assert obj.extra["symbol"] == "AAPL"

    def test_read_with_date_range(self, store: DeltaStore):
        store.write("AAPL", _df())
        df = store.read("AAPL", start_date="2026-01-02", output="dataframe")
        assert len(df) == 1

    def test_read_with_columns(self, store: DeltaStore):
        store.write("AAPL", _df())
        df = store.read("AAPL", columns=["close"], output="dataframe")
        assert list(df.columns) == ["close"]

    def test_read_nonexistent(self, store: DeltaStore):
        with pytest.raises(Exception):
            store.read("NONEXIST", output="dataframe")


class TestAppend:
    def test_append(self, store: DeltaStore):
        store.write("AAPL", _df())
        more = pd.DataFrame(
            {"open": [3.0, 4.0], "high": [5.0, 6.0], "low": [2.0, 3.0], "close": [4.0, 5.0], "volume": [300, 400]},
            index=pd.to_datetime(["2026-01-03", "2026-01-04"]),
        )
        store.append("AAPL", more)
        df = store.read("AAPL", output="dataframe")
        assert len(df) == 4


class TestCatalog:
    def test_list_symbols(self, store: DeltaStore):
        store.write("AAPL", _df())
        store.write("MSFT", _df())
        symbols = store.list_symbols()
        assert set(symbols) == {"AAPL", "MSFT"}

    def test_has_true(self, store: DeltaStore):
        store.write("AAPL", _df())
        assert store.has("AAPL") is True

    def test_has_false(self, store: DeltaStore):
        assert store.has("NONEXIST") is False

    def test_has_no_library(self, tmp_path):
        """A fresh store with no library yet should return False, not raise."""
        s = DeltaStore(uri=str(tmp_path), library="nonexistent")
        assert s.has("X") is False

    def test_delete(self, store: DeltaStore):
        store.write("AAPL", _df())
        store.delete("AAPL")
        assert store.has("AAPL") is False

    def test_read_metadata(self, store: DeltaStore):
        store.write("AAPL", _df(), metadata={"source": "test"})
        meta = store.read_metadata("AAPL")
        assert meta["source"] == "test"


class TestWriteRecords:
    def test_list_of_dicts(self, store: DeltaStore):
        data = [
            {"date": "2026-01-01", "value": 10},
            {"date": "2026-01-02", "value": 20},
        ]
        store.write("GDP", data)
        df = store.read("GDP", output="dataframe")
        assert len(df) == 2
        assert list(df.columns) == ["value"]
        assert isinstance(df.index, pd.DatetimeIndex)

    def test_empty_raises(self, store: DeltaStore):
        with pytest.raises(ValueError, match="No data"):
            store.write("EMPTY", pd.DataFrame())


class TestTimeTravel:
    def test_as_of_version_time_travel(self, store: DeltaStore):
        df1 = pd.DataFrame({"date": pd.to_datetime(["2026-01-02"]), "close": [1.0]})
        df2 = pd.DataFrame({"date": pd.to_datetime(["2026-01-03"]), "close": [2.0]})
        store.write("AAPL", df1)
        store.write("AAPL", df2)
        old = store.read("AAPL", as_of=0, output="dataframe")
        new = store.read("AAPL", output="dataframe")
        assert old["close"].tolist() == [1.0]
        assert new["close"].tolist() == [2.0]

    def test_write_returns_version_and_rows(self, store: DeltaStore):
        df = pd.DataFrame({"date": pd.to_datetime(["2026-01-02"]), "close": [1.0]})
        out = store.write("AAPL", df)
        assert out["version"] == 0 and out["rows"] == 1
        out = store.write("AAPL", df)
        assert out["version"] == 1

    def test_as_of_timestamp_time_travel(self, store: DeltaStore):
        """Non-int as_of must accept a naive/aware datetime AND an ISO string
        (delta-rs wants int | RFC3339-str | datetime; str(naive_timestamp) is
        not RFC3339 and used to raise ValueError -- see fix report)."""
        import time

        df1 = pd.DataFrame({"date": pd.to_datetime(["2026-01-02"]), "close": [1.0]})
        df2 = pd.DataFrame({"date": pd.to_datetime(["2026-01-03"]), "close": [2.0]})
        store.write("AAPL", df1)
        time.sleep(1.1)  # commit timestamps land at ~1s resolution
        between = pd.Timestamp.now("UTC")
        time.sleep(1.1)
        store.write("AAPL", df2)

        by_datetime = store.read("AAPL", as_of=between.to_pydatetime(), output="dataframe")
        by_iso_string = store.read("AAPL", as_of=between.isoformat(), output="dataframe")
        assert by_datetime["close"].tolist() == [1.0]
        assert by_iso_string["close"].tolist() == [1.0]

    def test_as_of_date_object_does_not_raise(self, store: DeltaStore):
        """A plain `date` (not datetime/str/int) must not raise inside load_as_version."""
        from datetime import date, timedelta

        df = pd.DataFrame({"date": pd.to_datetime(["2026-01-02"]), "close": [1.0]})
        store.write("AAPL", df)
        future = date.today() + timedelta(days=1)
        out = store.read("AAPL", as_of=future, output="dataframe")
        assert out["close"].tolist() == [1.0]


class TestDatetimeIndex:
    def test_datetime_index_round_trips_as_date_index(self, store: DeltaStore):
        df = pd.DataFrame(
            {"close": [1.0, 2.0]},
            index=pd.DatetimeIndex(pd.to_datetime(["2026-01-03", "2026-01-02"]), name="date"),
        )
        store.write("AAPL", df)
        back = store.read("AAPL", output="dataframe")
        assert back.index.name == "date"
        assert list(back.index) == sorted(back.index)  # sorted on read
        assert back["close"].tolist() == [2.0, 1.0]

    def test_unnamed_datetime_index_round_trips_as_date(self, store: DeltaStore):
        """An unnamed DatetimeIndex must still come back as a 'date' column/index."""
        df = pd.DataFrame(
            {"close": [1.0, 2.0]},
            index=pd.DatetimeIndex(pd.to_datetime(["2026-01-02", "2026-01-03"])),
        )
        assert df.index.name is None
        store.write("AAPL", df)
        back = store.read("AAPL", output="dataframe")
        assert back.index.name == "date"

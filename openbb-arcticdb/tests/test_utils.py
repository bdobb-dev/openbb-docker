"""Tests for connection helpers and date utilities (no ArcticDB connection needed)."""

from datetime import date, datetime

import pandas as pd
import pytest

from openbb_arcticdb.utils import (
    get_library,
    normalize_index,
    parse_temporal,
    redact_uri,
    resolve_config,
    to_bounds,
)

S3_URI_WITH_SECRETS = (
    "s3s://minio.example.ts.net:openbb"
    "?port=9000&access=rootuser&secret=hunter2&use_virtual_addressing=false"
)


# ---------------------------------------------------------------------------
# resolve_config
# ---------------------------------------------------------------------------

class TestResolveConfig:
    def test_defaults(self):
        uri, lib = resolve_config(None, None, None)
        assert lib == "openbb"
        assert uri.endswith("/arcticdb")
        assert uri.startswith("lmdb://")

    def test_explicit_overrides_everything(self):
        uri, lib = resolve_config("lmdb:///custom", "mine", {"arcticdb_uri": "x", "arcticdb_library": "y"})
        assert uri == "lmdb:///custom"
        assert lib == "mine"

    def test_credentials_fallback(self):
        uri, lib = resolve_config(None, None, {"arcticdb_uri": "lmdb:///from_creds", "arcticdb_library": "creds_lib"})
        assert uri == "lmdb:///from_creds"
        assert lib == "creds_lib"


# ---------------------------------------------------------------------------
# normalize_index
# ---------------------------------------------------------------------------

class TestNormalizeIndex:
    def test_datetimeindex_unchanged(self):
        idx = pd.date_range("2026-01-01", periods=3, freq="D")
        df = pd.DataFrame({"v": [1, 2, 3]}, index=idx)
        out = normalize_index(df)
        assert isinstance(out.index, pd.DatetimeIndex)
        assert out.index[0] == pd.Timestamp("2026-01-01")

    def test_date_column_becomes_index(self):
        df = pd.DataFrame({"date": ["2026-01-01", "2026-01-03"], "v": [1, 2]})
        out = normalize_index(df)
        assert isinstance(out.index, pd.DatetimeIndex)
        assert "date" not in out.columns

    def test_rangeindex_unchanged(self):
        df = pd.DataFrame({"v": [1, 2, 3]})
        out = normalize_index(df)
        assert isinstance(out.index, pd.RangeIndex)

    def test_numeric_index_unchanged(self):
        df = pd.DataFrame({"v": [1, 2, 3]}, index=pd.Index([10, 20, 30]))
        out = normalize_index(df)
        assert list(out.index) == [10, 20, 30]

    def test_sorts_by_index(self):
        idx = pd.to_datetime(["2026-01-03", "2026-01-01", "2026-01-02"])
        df = pd.DataFrame({"v": [3, 1, 2]}, index=idx)
        out = normalize_index(df)
        assert list(out["v"]) == [1, 2, 3]


# ---------------------------------------------------------------------------
# parse_temporal
# ---------------------------------------------------------------------------

class TestParseTemporal:
    def test_none(self):
        assert parse_temporal(None) is None

    def test_datetime_passthrough(self):
        d = datetime(2026, 6, 1, 9, 30)
        assert parse_temporal(d) is d

    def test_date_passthrough(self):
        d = date(2026, 6, 1)
        assert parse_temporal(d) is d

    def test_iso_with_time(self):
        result = parse_temporal("2026-06-01T09:30:00")
        assert isinstance(result, datetime)
        assert result.hour == 9

    def test_date_only_string(self):
        result = parse_temporal("2026-06-01")
        assert isinstance(result, date)
        assert not isinstance(result, datetime)

    def test_string_with_space_time(self):
        result = parse_temporal("2026-06-01 14:30")
        assert isinstance(result, datetime)
        assert result.hour == 14


# ---------------------------------------------------------------------------
# to_bounds
# ---------------------------------------------------------------------------

class TestToBounds:
    def test_both_none(self):
        s, e = to_bounds(None, None)
        assert s is None
        assert e is None

    def test_date_end_widened(self):
        _, e = to_bounds(None, date(2026, 6, 1))
        assert e.hour == 23
        assert e.minute == 59

    def test_datetime_end_exact(self):
        _, e = to_bounds(None, datetime(2026, 6, 1, 9, 30))
        assert e.hour == 9
        assert e.minute == 30

    def test_start_date(self):
        s, _ = to_bounds(date(2026, 1, 1), None)
        assert s is not None


# ---------------------------------------------------------------------------
# redact_uri
# ---------------------------------------------------------------------------

class TestRedactUri:
    def test_access_and_secret_are_replaced(self):
        redacted = redact_uri(S3_URI_WITH_SECRETS)
        assert "hunter2" not in redacted
        assert "rootuser" not in redacted
        assert "access=%2A%2A%2A" in redacted or "access=***" in redacted
        assert "secret=%2A%2A%2A" in redacted or "secret=***" in redacted

    def test_other_query_params_survive(self):
        redacted = redact_uri(S3_URI_WITH_SECRETS)
        assert "port=9000" in redacted
        assert "use_virtual_addressing=false" in redacted

    def test_lmdb_uri_has_no_query_string_and_passes_through(self):
        assert redact_uri("lmdb:///tmp/arcticdb") == "lmdb:///tmp/arcticdb"

    def test_uri_without_query_string_passes_through(self):
        assert redact_uri("s3s://host:bucket") == "s3s://host:bucket"


# ---------------------------------------------------------------------------
# get_library: the credential must never reach the error message
# ---------------------------------------------------------------------------

class TestGetLibraryRedactsSecrets:
    def test_missing_library_error_never_contains_the_secret(self, monkeypatch):
        class _FakeArctic:
            def __init__(self, uri):
                pass

            def has_library(self, name):
                return False

        monkeypatch.setattr("arcticdb.Arctic", _FakeArctic)
        try:
            with pytest.raises(FileNotFoundError) as exc:
                get_library(S3_URI_WITH_SECRETS, "missing-lib", create_if_missing=False)
        finally:
            from openbb_arcticdb.utils import _arctic_cache
            _arctic_cache.pop(S3_URI_WITH_SECRETS, None)

        message = str(exc.value)
        assert "hunter2" not in message
        assert "rootuser" not in message
        assert "missing-lib" in message

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

from app.probes import TestResult
from app.rows import build_rows

VALUES = {"EODHD_API_KEY": "demo", "FMP_API_KEY": "realkey99", "FRED_API_KEY": ""}


def row(rows, env_var):
    return next(r for r in rows if r.get("env_var") == env_var)


class TestStatus:
    def test_set_empty_missing(self):
        rows = build_rows(VALUES, tier=1, tests=None)
        assert row(rows, "FMP_API_KEY")["status"] == "set"
        assert row(rows, "FRED_API_KEY")["status"] == "empty"
        assert row(rows, "BLS_API_KEY")["status"] == "missing"

    def test_demo_flagged(self):
        rows = build_rows(VALUES, tier=1, tests=None)
        assert row(rows, "EODHD_API_KEY")["demo"] is True
        assert row(rows, "FMP_API_KEY")["demo"] is False

    def test_ignored_vars_absent(self):
        rows = build_rows({"KDB_HOST": "x"}, tier=1, tests=None)
        assert not any(r.get("env_var") == "KDB_HOST" for r in rows)

    def test_unregistered_credential_var_gets_generic_row(self):
        rows = build_rows({"NEWTHING_API_KEY": "v"}, tier=1, tests=None)
        r = row(rows, "NEWTHING_API_KEY")
        assert r["provider"] == "NEWTHING_API_KEY"
        assert r["status"] == "set"


class TestTierRedaction:
    def test_value_absent_below_tier_3(self):
        for tier in (1, 2):
            for r in build_rows(VALUES, tier=tier, tests=None):
                assert "value" not in r

    def test_value_present_at_tier_3(self):
        rows = build_rows(VALUES, tier=3, tests=None)
        assert row(rows, "FMP_API_KEY")["value"] == "realkey99"


class TestTests:
    def test_test_field_attached(self):
        tests = {"FMP_API_KEY": TestResult("ok", "HTTP 200")}
        rows = build_rows(VALUES, tier=2, tests=tests)
        assert row(rows, "FMP_API_KEY")["test"] == {"result": "ok", "detail": "HTTP 200"}

    def test_no_tests_no_field(self):
        rows = build_rows(VALUES, tier=2, tests=None)
        assert "test" not in row(rows, "FMP_API_KEY")


class TestDegradation:
    def test_missing_file_yields_banner_and_unknown_rows(self):
        rows = build_rows(None, tier=2, tests=None)
        assert rows[0]["provider"] == "⚠ credentials.env"
        assert rows[0]["status"] == "unknown"
        assert all(r["status"] == "unknown" for r in rows[1:])


class TestMalformed:
    def test_malformed_entries_produce_trailing_warning_rows(self):
        rows = build_rows(VALUES, tier=3, tests=None, malformed=["line 7"])
        assert rows[-1] == {
            "provider": "⚠ malformed line",
            "env_var": "line 7",
            "status": "unknown",
            "demo": False,
        }
        assert "value" not in rows[-1]

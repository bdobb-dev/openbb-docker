# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

from app.probes import TestResult
from app.rows import build_rows, build_summary

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


class TestSummary:
    def test_never_renders_a_credential_value(self):
        """The reason this widget declares no raw view.

        At tier 3 build_rows() puts the credential itself in row["value"].
        The summary is built from those rows, so a careless f-string here
        would publish every key to any dashboard that adds the widget.
        """
        # Distinctive values on purpose. The shared VALUES fixture sets EODHD
        # to the literal string "demo", which collides with the summary's own
        # "Public demo key" line and would fail this assertion without a leak.
        secrets = {
            "EODHD_API_KEY": "SECRET-eodhd-a1b2c3",
            "FMP_API_KEY": "SECRET-fmp-d4e5f6",
        }
        rows = build_rows(secrets, tier=3, tests=None)
        assert any("value" in r for r in rows), "tier 3 should carry values"
        summary = build_summary(rows)
        for secret in secrets.values():
            assert secret not in summary

    def test_counts_and_names_the_missing(self):
        summary = build_summary(build_rows(VALUES, tier=1, tests=None))
        assert "Missing" in summary
        # FRED is present but blank -- that is "empty", not "missing".
        assert "Present but empty" in summary

    def test_flags_a_public_demo_key(self):
        summary = build_summary(build_rows(VALUES, tier=1, tests=None))
        assert "Public demo key" in summary

    def test_unreadable_credentials_file_is_reported(self):
        summary = build_summary(build_rows(None, tier=1, tests=None))
        assert "Unreadable" in summary

    def test_all_private_keys_says_so(self):
        from app.registry import PROVIDERS

        every = {var: "private-value" for var in PROVIDERS}
        summary = build_summary(build_rows(every, tier=1, tests=None))
        assert "Every provider is configured" in summary
        assert "private-value" not in summary

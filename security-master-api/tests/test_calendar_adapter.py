# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
from datetime import date

from security_master_api.resolver.calendar import adapter_version, generate_sessions


def test_nyse_sessions_carry_provenance_and_skip_holidays():
    rows = generate_sessions("NYSE", date(2026, 12, 20), date(2027, 1, 5), "cal_xnys",
                             "rule_xnys_1")
    dates = {str(r["session_date"]) for r in rows}
    assert "2026-12-25" not in dates and "2027-01-01" not in dates
    assert "2026-12-24" in dates
    row = next(r for r in rows if str(r["session_date"]) == "2026-12-24")
    assert row["rule_id"] == "rule_xnys_1"
    assert row["source_version"] == adapter_version()
    assert row["assertion_status"] == "current"
    assert row["market_close"].hour == 18  # 13:00 New York in UTC
    assert row["assertion_id"] == "as_sess_cal_xnys_2026-12-24"


def test_adapter_version_names_the_package():
    assert adapter_version().startswith("pandas_market_calendars/")

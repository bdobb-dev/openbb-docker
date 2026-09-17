# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime

from security_master_api.acquire.normalize import (
    normalize_exchange_calendar,
    normalize_price_daily,
    normalize_shares_outstanding,
    validate,
)
from security_master_api.store.schemas import RELATIONS

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


def test_price_rows_carry_provenance_and_intervals():
    payload = {"results": [{"date": "2026-09-15", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
                            "volume": 10}]}
    rows = normalize_price_daily(payload, "lst_apple", "cap_x", NOW, "raw")
    r = rows[0]
    assert (r["listing_id"], str(r["market_date"]), r["close"]) == ("lst_apple", "2026-09-15", 1.5)
    assert r["capture_id"] == "cap_x" and r["available_at"] == NOW and r["system_to"] is None
    assert r["assertion_id"] == "as_px_lst_apple_2026-09-15_cap_x"
    assert validate("silver.prices_normalized", rows) == []


def test_price_validation_catches_bad_ranges():
    rows = normalize_price_daily({"results": [{"date": "2026-09-15", "open": 1, "high": 0.4,
                                               "low": 0.5, "close": 1.5, "volume": -1}]},
                                 "lst_apple", "cap_x", NOW, "raw")
    problems = validate("silver.prices_normalized", rows)
    assert any("high" in p for p in problems) and any("volume" in p for p in problems)


def test_exchange_calendar_rows_never_claim_authority():
    payload = {"results": {"Code": "US", "Timezone": "America/New_York", "ExchangeHolidays": {
        "0": {"Holiday": "Christmas", "Date": "2026-12-25", "Type": "official"},
        "1": {"Holiday": "Early close", "Date": "2026-12-24", "Type": "early_close"}}}}
    rows = normalize_exchange_calendar(payload, "cal_xnys", "cap_c", NOW)
    by = {str(r["session_date"]): r for r in rows}
    assert by["2026-12-25"]["market_effect"] == "closed"
    assert by["2026-12-24"]["market_effect"] == "early_close"
    assert all(r["assertion_domain"] == "market_session" and r["source_kind"] == "eodhd"
               and r["evidence_status"] == "estimated" and r["authority_type"] == "calendar_package"
               for r in rows)


def test_shares_outstanding_row():
    payload = {"results": [{"shares_outstanding": 1480000000, "period_end": "2026-06-30"}]}
    rows = normalize_shares_outstanding(payload, "iss_example", "cap_s", NOW)
    assert rows[0]["fact"] == "shares_outstanding" and rows[0]["value"] == 1.48e9
    assert str(rows[0]["period_end"]) == "2026-06-30"


def test_malformed_payload_is_a_problem_not_a_crash():
    assert normalize_price_daily({"nope": 1}, "l", "c", NOW, "raw") == []
    assert validate("silver.prices_normalized", []) == ["no rows"]


def test_every_normalizer_states_exactly_its_relation_columns():
    """A row with a stray or missing column is rejected by `store.tables.append`, so the
    column set is part of the normalizer's contract and not something a later writer checks."""
    cases = [
        ("silver.prices_normalized",
         normalize_price_daily({"results": [{"date": "2026-09-15", "close": 1}]},
                               "lst_apple", "cap_x", NOW, "raw")),
        ("silver.calendar_exceptions",
         normalize_exchange_calendar({"results": {"ExchangeHolidays": {
             "0": {"Holiday": "X", "Date": "2026-12-25", "Type": "official"}}}},
             "cal_xnys", "cap_c", NOW)),
        ("silver.fundamental_facts",
         normalize_shares_outstanding({"results": [{"shares_outstanding": 1, "period_end":
                                                    "2026-06-30"}]}, "iss_x", "cap_s", NOW)),
    ]
    for relation, rows in cases:
        assert rows, relation
        assert set(rows[0]) == set(RELATIONS[relation].names), relation

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.compare import compare
from security_master_api.resolver.context import parse_context
from security_master_api.resolver.lineage import lineage
from security_master_api.store.seed import seed
from security_master_api.store.tables import append


def _reorg_listing(security_id: str, effective_from: str, effective_to: str | None) -> dict:
    return {
        "listing_id": "lst_042", "instrument_id": "ins_042", "issuer_id": "iss_042",
        "security_id": security_id, "exchange_id": "exch_xnas", "mic": "XNAS",
        "symbol": "RORG", "provider_symbol": "RORG.US", "currency": "USD", "status": "active",
        "name": "Reorg Holdings Inc.", "instrument_type": "Common Stock",
        "effective_from": effective_from, "effective_to": effective_to,
        "observed_at": "2026-06-20T00:00:00Z", "available_at": "2026-06-20T00:00:00Z",
        "system_from": "2026-06-20T00:00:00Z", "system_to": None,
        "capture_id": "cap_cusip_1", "assertion_id": f"as_lst_042_{security_id}",
        "assertion_status": "current", "supersedes_assertion_id": None,
    }


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    # The cusip-change fixture carries no listing, so the reorg never reaches
    # gold.security_master. One listing whose security_id moves sec_100 -> sec_101 across
    # 2026-07-01 is what makes the identity-successor comparison observable.
    append(settings=s, relation="silver.listings", rows=[
        _reorg_listing("sec_100", "2015-01-01T00:00:00Z", "2026-07-01T00:00:00Z"),
        _reorg_listing("sec_101", "2026-07-01T00:00:00Z", None),
    ])
    return s


def test_lineage_reaches_the_corrected_capture(settings):
    chain = lineage(settings, parse_context(None), "silver.prices_normalized",
                    "as_px_apple_20260914_2")["chain"]
    kinds = [c["kind"] for c in chain]
    assert kinds[0] == "assertion" and "capture" in kinds
    capture = next(c for c in chain if c["kind"] == "capture")
    assert capture["capture_id"] == "cap_00489"
    assert chain[0]["supersedes_assertion_id"] == "as_px_apple_20260914_1"
    assert chain[0]["system_from"] == "2026-09-15T09:20:00Z"
    assert capture["captured_at"] == "2026-09-15T09:20:00Z"
    assert "payload" not in capture


def test_lineage_rejects_a_gold_relation_and_an_unknown_assertion(settings):
    with pytest.raises(DomainError) as exc:
        lineage(settings, parse_context(None), "gold.price_daily", "as_px_apple_20260914_2")
    assert exc.value.code == "QUERY_REJECTED"
    with pytest.raises(DomainError) as exc:
        lineage(settings, parse_context(None), "silver.prices_normalized", "as_nope")
    assert exc.value.code == "NO_MATCHING_FACTS"


def test_compare_classifies_a_correction(settings):
    left = parse_context({"mode": "known_at", "known_at": "2026-09-14T20:05:00Z"})
    right = parse_context({"mode": "known_at", "known_at": "2026-09-15T09:20:00Z"})
    out = compare(settings, "gold.price_daily",
                  {"listing_id": "lst_apple", "market_date": "2026-09-14"}, left, right)
    close = next(d for d in out["differences"] if d["field"] == "close")
    assert (close["left"], close["right"], close["classification"]) == (231.40, 231.74, "corrected")
    assert out["left_manifest"] != out["right_manifest"] or out["left_manifest"]


def test_compare_classifies_newly_known(settings):
    left = parse_context({"mode": "known_at", "known_at": "2026-07-15T00:00:00Z"})
    right = parse_context({"mode": "known_at", "known_at": "2026-08-05T12:00:00Z"})
    out = compare(settings, "gold.odp_equity_info", {"symbol": "EXMP"}, left, right)
    so = next(d for d in out["differences"] if d["field"] == "shares_outstanding")
    assert so["classification"] == "newly_known"


def test_compare_classifies_identity_successor(settings):
    left = parse_context({"mode": "effective_on", "effective_at": "2026-06-30T00:00:00Z"})
    right = parse_context({"mode": "effective_on", "effective_at": "2026-07-02T00:00:00Z"})
    out = compare(settings, "gold.security_master", {"issuer_id": "iss_042"}, left, right)
    sec = next(d for d in out["differences"] if d["field"] == "security_id")
    assert sec["classification"] == "identity_successor"
    assert (sec["left"], sec["right"]) == ("sec_100", "sec_101")


def test_compare_needs_a_selection(settings):
    ctx = parse_context(None)
    with pytest.raises(DomainError) as exc:
        compare(settings, "gold.security_master", {}, ctx, ctx)
    assert exc.value.code == "QUERY_REJECTED"

# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import parse_context
from security_master_api.resolver.identity import resolve
from security_master_api.store.seed import seed
from security_master_api.store.tables import append


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return s


def one(settings, identifier, raw_ctx, **kw):
    out = resolve(settings, parse_context(raw_ctx), identifier, **kw)
    assert len(out["candidates"]) == 1, out
    return out["candidates"][0]


def test_cusip_resolves_by_effective_date(settings):
    old = one(settings, "000111AA1", {"mode": "effective_on", "effective_at": "2026-06-30T00:00:00Z"})
    assert (old["security_id"], old["reason"]) == ("sec_100", "active")
    new = one(settings, "000222BB2", {"mode": "effective_on", "effective_at": "2026-07-02T00:00:00Z"})
    assert (new["security_id"], new["reason"]) == ("sec_101", "active")


def test_old_cusip_without_a_date_is_historical_not_current(settings):
    c = one(settings, "000111AA1", None)
    assert c["reason"] == "historical_alias"
    assert c["security_id"] == "sec_100"
    assert c["successor_security_id"] == "sec_101"


def test_ticker_change_keeps_the_listing(settings):
    fb = one(settings, "FB", {"mode": "effective_on", "effective_at": "2022-06-07T00:00:00Z"})
    meta = one(settings, "META", {"mode": "effective_on", "effective_at": "2022-06-10T00:00:00Z"})
    assert fb["listing_id"] == meta["listing_id"] == "lst_000042"
    assert fb["instrument_id"] == meta["instrument_id"] == "ins_000042"
    assert (fb["symbol"], meta["symbol"]) == ("FB", "META")
    current = one(settings, "FB", None)
    assert current["reason"] == "historical_alias" and current["symbol"] == "META"
    # DuckDB hands TIMESTAMPTZ back in the host's local zone; the wire format stays UTC.
    assert current["effective_to"] == "2022-06-09T00:00:00Z"
    assert current["effective_from"] == "2012-05-18T00:00:00Z"


def test_unresolved_and_ambiguous(settings):
    with pytest.raises(DomainError) as exc:
        resolve(settings, parse_context(None), "ZZZZ")
    assert exc.value.code == "IDENTITY_UNRESOLVED"
    out = resolve(settings, parse_context(None), "FB", include_historical=False)
    assert out["candidates"] == [] or all(c["reason"] != "historical_alias" for c in out["candidates"])


def _dual_row(suffix: str) -> dict:
    return {
        "identifier_type": "ticker", "identifier": "DUAL",
        "listing_id": f"lst_dual_{suffix}", "instrument_id": f"ins_dual_{suffix}",
        "security_id": f"sec_dual_{suffix}", "issuer_id": f"iss_dual_{suffix}",
        "effective_from": "2020-01-01T00:00:00Z", "effective_to": None,
        "observed_at": "2020-01-01T00:00:00Z", "available_at": "2020-01-01T00:00:00Z",
        "system_from": "2020-01-01T00:00:00Z", "system_to": None,
        "capture_id": "cap_cusip_1", "assertion_id": f"as_id_dual_{suffix}",
        "assertion_status": "current", "supersedes_assertion_id": None,
    }


def test_two_current_listings_for_one_ticker_are_ambiguous(settings):
    # Two live `ticker` rows for DUAL on different listings: without an effective_at there is
    # nothing to choose between them, and resolve never guesses.
    append(settings, "silver.identifiers", [_dual_row("a"), _dual_row("b")])
    with pytest.raises(DomainError) as exc:
        resolve(settings, parse_context(None), "DUAL")
    assert exc.value.code == "IDENTITY_AMBIGUOUS"
    assert len(exc.value.details["candidates"]) == 2
    assert {c["listing_id"] for c in exc.value.details["candidates"]} == {"lst_dual_a", "lst_dual_b"}


def test_unknown_identifier_type_is_rejected(settings):
    with pytest.raises(DomainError) as exc:
        resolve(settings, parse_context(None), "FB", identifier_type="lei")
    assert exc.value.code == "QUERY_REJECTED"


def test_a_stable_id_resolves_directly(settings):
    c = one(settings, "lst_000042", None)
    assert (c["symbol"], c["reason"], c["identifier_type"]) == ("META", "active", "listing_id")


def test_an_identifier_that_is_not_yet_in_force_is_unresolved(settings):
    # The store holds both, but neither interval has started under these contexts. An empty
    # candidate list would read as "never heard of it", which is a different answer.
    for ident, ctx, starts in (
        ("META", {"mode": "effective_on", "effective_at": "2012-06-01T00:00:00Z"},
         "2022-06-09T00:00:00Z"),
        ("000222BB2", {"mode": "effective_on", "effective_at": "2020-01-01T00:00:00Z"},
         "2026-07-01T00:00:00Z"),
    ):
        with pytest.raises(DomainError) as exc:
            resolve(settings, parse_context(ctx), ident)
        assert exc.value.code == "IDENTITY_UNRESOLVED"
        assert exc.value.details["status"] == "not_effective"
        assert exc.value.details["effective_from"] == starts


def test_a_stable_id_without_a_listing_still_resolves(settings):
    # No listing in the cusip fixture carries sec_100, so gold.security_master cannot see it;
    # silver.securities can, and the store holding the id means it resolves.
    c = one(settings, "sec_100", None)
    assert c["security_id"] == "sec_100"
    assert c["successor_security_id"] == "sec_101"
    assert c["reason"] == "active"

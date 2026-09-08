# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared /fundamentals helper and its single-flight cache."""

import asyncio
import pytest

from openbb_eodhd.models import _fundamentals as F


BUNDLE = {
    "General": {"Code": "AAPL", "Name": "Apple Inc.", "Sector": "Technology"},
    "Highlights": {"MarketCapitalization": 100, "PERatio": 30},
    "Holders": {"Institutions": {"0": {"name": "BlackRock Inc", "date": "2026-03-31",
                                       "currentShares": 1144695425}}},
    "Earnings": {"History": {"0": {"reportDate": "2026-10-29", "epsActual": None,
                                   "epsEstimate": 1.98}}},
    "AnalystRatings": {"TargetPrice": 324.45, "Rating": 4.04},
}


@pytest.fixture(autouse=True)
def _clear():
    F._reset_cache_for_tests()
    yield
    F._reset_cache_for_tests()


def test_qualify_defaults_and_preserves():
    assert F.qualify("aapl") == "AAPL.US"
    assert F.qualify("AAPL", "US") == "AAPL.US"
    assert F.qualify("VOD.LSE") == "VOD.LSE"


def test_rows_normalizes_index_keyed_dict_and_list():
    assert F._rows({"0": {"a": 1}, "1": {"a": 2}}) == [{"a": 1}, {"a": 2}]
    assert F._rows([{"a": 1}]) == [{"a": 1}]
    assert F._rows(None) == []
    assert F._rows("nope") == []


def test_accessors_read_sections():
    assert F.general(BUNDLE)["Name"] == "Apple Inc."
    assert F.holders_institutions(BUNDLE)[0]["name"] == "BlackRock Inc"
    assert F.earnings_history(BUNDLE)[0]["epsEstimate"] == 1.98
    assert F.analyst_ratings(BUNDLE)["TargetPrice"] == 324.45


def test_single_flight_coalesces_concurrent_calls(monkeypatch):
    """N concurrent get_bundle() for one symbol -> ONE underlying fetch."""
    calls = {"n": 0}

    def fake_fetch_sync(sym, creds):
        calls["n"] += 1
        return BUNDLE

    monkeypatch.setattr(F, "_fetch_sync", fake_fetch_sync)

    async def go():
        return await asyncio.gather(
            *[F.get_bundle("AAPL", "US", {"eodhd_api_key": "k"}) for _ in range(10)]
        )

    results = asyncio.run(go())
    assert calls["n"] == 1
    assert all(r is results[0] for r in results)


def test_ttl_expiry_refetches(monkeypatch):
    calls = {"n": 0}
    clock = {"t": 1000.0}
    monkeypatch.setattr(F, "_fetch_sync", lambda s, c: (calls.__setitem__("n", calls["n"] + 1) or BUNDLE))
    monkeypatch.setattr(F.time, "monotonic", lambda: clock["t"])

    async def go():
        await F.get_bundle("AAPL", "US", {"eodhd_api_key": "k"})
        clock["t"] += F._TTL_SECONDS + 1  # advance past TTL
        await F.get_bundle("AAPL", "US", {"eodhd_api_key": "k"})

    asyncio.run(go())
    assert calls["n"] == 2


def test_expired_entries_swept_on_next_populate(monkeypatch):
    """An expired symbol's L1 entry is dropped when a different symbol is fetched."""
    monkeypatch.setattr(F, "_fetch_sync", lambda s, c: BUNDLE)
    monkeypatch.setattr(F.time, "monotonic", lambda: 1000.0)

    asyncio.run(F.get_bundle("AAAA", "US", {"eodhd_api_key": "k"}))
    assert "AAAA.US" in F._cache

    monkeypatch.setattr(F.time, "monotonic", lambda: 1000.0 + F._TTL_SECONDS + 1)
    asyncio.run(F.get_bundle("BBBB", "US", {"eodhd_api_key": "k"}))

    assert "AAAA.US" not in F._cache
    assert "BBBB.US" in F._cache
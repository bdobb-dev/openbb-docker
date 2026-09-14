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
    monkeypatch.setattr(F, "_l2_get", lambda sym: None)   # isolate L1 + fetch
    monkeypatch.setattr(F, "_l2_put", lambda sym, b: None)

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
    monkeypatch.setattr(F, "_l2_get", lambda sym: None)
    monkeypatch.setattr(F, "_l2_put", lambda sym, b: None)
    monkeypatch.setattr(F.time, "monotonic", lambda: clock["t"])

    async def go():
        await F.get_bundle("AAPL", "US", {"eodhd_api_key": "k"})
        clock["t"] += F._TTL_SECONDS + 1  # advance past TTL
        await F.get_bundle("AAPL", "US", {"eodhd_api_key": "k"})

    asyncio.run(go())
    assert calls["n"] == 2


def test_l2_hit_within_ttl_skips_eodhd(monkeypatch):
    """A fresh ArcticDB entry is served without any EODHD call."""
    calls = {"n": 0}
    monkeypatch.setattr(F, "_fetch_sync", lambda s, c: (calls.__setitem__("n", calls["n"] + 1) or BUNDLE))
    monkeypatch.setattr(F, "_l2_get", lambda sym: BUNDLE)     # L2 fresh hit
    monkeypatch.setattr(F, "_l2_put", lambda sym, b: None)

    out = asyncio.run(F.get_bundle("AAPL", "US", {"eodhd_api_key": "k"}))
    assert out is BUNDLE
    assert calls["n"] == 0


def test_l2_miss_fetches_and_writes_back(monkeypatch):
    """L2 miss -> one EODHD fetch -> written back to L2."""
    calls = {"fetch": 0, "put": 0}
    monkeypatch.setattr(F, "_fetch_sync", lambda s, c: (calls.__setitem__("fetch", calls["fetch"] + 1) or BUNDLE))
    monkeypatch.setattr(F, "_l2_get", lambda sym: None)       # L2 miss
    monkeypatch.setattr(F, "_l2_put", lambda sym, b: calls.__setitem__("put", calls["put"] + 1))

    out = asyncio.run(F.get_bundle("AAPL", "US", {"eodhd_api_key": "k"}))
    assert out is BUNDLE
    assert calls == {"fetch": 1, "put": 1}


def test_arctic_library_passes_resolved_uri_not_none(monkeypatch):
    U = pytest.importorskip(
        "openbb_arcticdb.utils",
        reason="L2 is a soft dependency; without openbb-arcticdb it degrades to L1",
    )
    captured = {}
    monkeypatch.setattr(U, "resolve_config",
        lambda uri=None, library=None, credentials=None: ("lmdb:///tmp/eodhd-test", library))
    monkeypatch.setattr(U, "get_library",
        lambda uri, library, create_if_missing=True: captured.update(uri=uri, library=library) or "LIB")
    assert F._arctic_library() == "LIB"
    assert captured["uri"] == "lmdb:///tmp/eodhd-test"   # regression guard: never None
    assert captured["library"] == "eodhd_fundamentals_cache"


def test_expired_entries_swept_on_next_populate(monkeypatch):
    """An expired symbol's L1 entry is dropped when a different symbol is fetched."""
    monkeypatch.setattr(F, "_fetch_sync", lambda s, c: BUNDLE)
    monkeypatch.setattr(F, "_l2_get", lambda sym: None)
    monkeypatch.setattr(F, "_l2_put", lambda sym, b: None)
    monkeypatch.setattr(F.time, "monotonic", lambda: 1000.0)

    asyncio.run(F.get_bundle("AAAA", "US", {"eodhd_api_key": "k"}))
    assert "AAAA.US" in F._cache

    monkeypatch.setattr(F.time, "monotonic", lambda: 1000.0 + F._TTL_SECONDS + 1)
    asyncio.run(F.get_bundle("BBBB", "US", {"eodhd_api_key": "k"}))

    assert "AAAA.US" not in F._cache
    assert "BBBB.US" in F._cache


def test_l2_unavailable_falls_back_to_live_fetch(monkeypatch):
    """If ArcticDB can't be reached, get/put no-op and the request still succeeds."""
    calls = {"n": 0}
    monkeypatch.setattr(F, "_arctic_library", lambda: None)   # ArcticDB absent
    monkeypatch.setattr(F, "_fetch_sync", lambda s, c: (calls.__setitem__("n", calls["n"] + 1) or BUNDLE))

    out = asyncio.run(F.get_bundle("AAPL", "US", {"eodhd_api_key": "k"}))
    assert out is BUNDLE
    assert calls["n"] == 1
    # _l2_get / _l2_put must swallow the missing library rather than raise
    assert F._l2_get("AAPL.US") is None
    F._l2_put("AAPL.US", BUNDLE)  # no exception

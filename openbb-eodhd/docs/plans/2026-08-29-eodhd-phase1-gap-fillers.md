# EODHD Phase 1 — Foundation + Gap-Fillers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the shared `/fundamentals` cache and the EODHD fetchers that fill the FMP-402 gaps (ownership, insider, historical EPS, estimates, price target), so those `..._eodhd_obb` widgets render on the NAS.

**Architecture:** A shared `_fundamentals.py` helper fetches the whole EODHD `/fundamentals` payload once per symbol behind a single-flight TTL cache; fundamentals-derived fetchers are thin section-extractors over it. InsiderTrading uses EODHD's dedicated insider endpoint. Each fetcher maps an OpenBB standard model FMP already registers, so OpenBB auto-generates the parallel `_eodhd_obb` widget ids.

**Tech Stack:** Python 3.10+, `openbb-core` provider framework, the official `eodhd` SDK (via `openbb_eodhd/models/_client.py`), pydantic v2, pytest.

**Spec:** `openbb-eodhd/docs/specs/2026-08-29-eodhd-fmp-parity-design.md`

## Global Constraints

- Register each fetcher under the **exact** OpenBB standard-model name FMP uses (`InstitutionalOwnership`, `EquityOwnership`, `InsiderTrading`, `HistoricalEps`, `AnalystEstimates`, `ForwardEpsEstimates`, `PriceTargetConsensus`) — that is what yields matching `_eodhd_obb` widget ids.
- All EODHD HTTP goes through `openbb_eodhd.models._client.get_client()`; never build a session directly. Map SDK exceptions with `raise_sdk_error(exc, context)`.
- Symbols are qualified `TICKER.EXCHANGE`, default exchange `US`; each QueryParams subclass adds `exchange: str = Field(default="US", ...)`.
- Credential is `eodhd_api_key` (already declared on the provider).
- The fundamentals cache is **two-tier**: L1 in-process single-flight (stdlib: `time.monotonic()` + dict + `asyncio.Lock`, `_TTL_SECONDS = 120.0`) for burst coalescing, and L2 an **ArcticDB read-through** for persistence. L2 soft-imports `openbb_arcticdb.utils.get_library` and is **best-effort**: any ArcticDB error or its absence falls back to a live EODHD fetch and never breaks a request (no hard dependency on `openbb-arcticdb`). L2 TTL is `EODHD_FUNDAMENTALS_TTL_HOURS` (default 24), library `eodhd_fundamentals_cache`.
- Data models **subclass** the standard `*Data` and add EODHD extras as explicit optional fields (follow `models/corporate_actions.py`).
- Tests are offline unit tests over sample dicts, using `tests/conftest.py::run_async`; no network in the test suite.

---

### Task 1: Shared `/fundamentals` helper with single-flight TTL cache

**Files:**
- Create: `openbb_eodhd/models/_fundamentals.py`
- Test: `tests/test_fundamentals_cache.py`

**Interfaces:**
- Produces:
  - `qualify(symbol: str, exchange: str = "US") -> str`
  - `async get_bundle(symbol: str, exchange: str, credentials: dict[str,str]|None) -> dict` — full `/fundamentals` payload, coalesced (L1) and read-through cached (L2).
  - `_fetch_sync(sym, credentials) -> dict` — the raw EODHD SDK call (monkeypatched in tests).
  - `_read_through_sync(sym, credentials) -> dict` — L2 get → EODHD fetch → L2 put.
  - `_l2_get(sym) -> dict | None`, `_l2_put(sym, bundle) -> None` — best-effort ArcticDB, never raise.
  - `_rows(section) -> list[dict]` — normalize EODHD index-keyed dict / list → `list[dict]`.
  - section accessors: `general(b)`, `highlights(b)`, `valuation(b)`, `shares_stats(b)`, `analyst_ratings(b)`, `esg(b)`, `etf_data(b)`, `holders_institutions(b)`, `holders_funds(b)`, `earnings_history(b)`, `earnings_trend(b)` — each returns dict or `list[dict]`.
  - `_reset_cache_for_tests() -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fundamentals_cache.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd openbb-eodhd && python -m pytest tests/test_fundamentals_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: openbb_eodhd.models._fundamentals`.

- [ ] **Step 3: Write minimal implementation**

```python
# openbb_eodhd/models/_fundamentals.py
"""Shared EODHD /fundamentals fetch with a single-flight TTL cache.

OpenBB invokes each widget's fetcher independently, so a dashboard of N
fundamentals widgets for one symbol would otherwise fire N identical
/fundamentals calls (EODHD bills each ~10 credits). Every fundamentals-derived
fetcher reads sections of ONE payload obtained here; concurrent and repeat
requests for the same symbol within the TTL window share a single HTTP call.
"""

from __future__ import annotations

import json
import os
import time
from asyncio import Lock, to_thread
from datetime import datetime, timezone
from typing import Any

from openbb_core.app.model.abstract.error import OpenBBError
from openbb_core.provider.utils.errors import EmptyDataError, UnauthorizedError

from openbb_eodhd.models._client import get_client, raise_sdk_error

_TTL_SECONDS = 120.0            # L1 in-process burst-coalescing TTL
_L2_LIBRARY = "eodhd_fundamentals_cache"
_cache: dict[str, tuple[float, dict]] = {}
_locks: dict[str, Lock] = {}


def _l2_ttl_seconds() -> float:
    try:
        return float(os.getenv("EODHD_FUNDAMENTALS_TTL_HOURS", "24")) * 3600.0
    except ValueError:
        return 24 * 3600.0


def qualify(symbol: str, exchange: str = "US") -> str:
    s = symbol.strip().upper()
    return s if "." in s else f"{s}.{exchange.upper()}"


def _reset_cache_for_tests() -> None:
    _cache.clear()
    _locks.clear()


def _get_lock(sym: str) -> Lock:
    lock = _locks.get(sym)
    if lock is None:
        lock = _locks[sym] = Lock()
    return lock


def _cached(sym: str) -> dict | None:
    hit = _cache.get(sym)
    if hit is None:
        return None
    expiry, payload = hit
    if time.monotonic() >= expiry:
        _cache.pop(sym, None)
        return None
    return payload


def _fetch_sync(sym: str, credentials: dict[str, str] | None) -> dict:
    client = get_client(credentials)
    try:
        with client:
            response = client.get_fundamentals_data(sym)
    except (OpenBBError, UnauthorizedError):
        raise
    except Exception as exc:  # noqa: BLE001 - mapped below
        raise_sdk_error(exc, f"fundamentals for '{sym}'")
    if not isinstance(response, dict) or not response:
        raise EmptyDataError(f"EODHD returned no fundamentals for '{sym}'.")
    return response


# --- L2: ArcticDB read-through (best-effort; never raises) ---------------------
def _arctic_library():
    """Return the ArcticDB cache library, or None if unavailable/unconfigured.

    Soft dependency: openbb-arcticdb is present in the container but not required
    for this extension to work (standalone dev, MinIO down). Any failure -> None.
    """
    try:
        # pylint: disable=import-outside-toplevel
        from openbb_arcticdb.utils import get_library

        return get_library(uri=None, library=_L2_LIBRARY, create_if_missing=True)
    except Exception:  # noqa: BLE001 - L2 is optional; degrade to L1 + live fetch
        return None


def _l2_get(sym: str) -> dict | None:
    """Fresh cached bundle for sym, or None. Never raises."""
    try:
        lib = _arctic_library()
        if lib is None or not lib.has_symbol(sym):
            return None
        meta = (lib.read_metadata(sym).metadata or {})
        fetched = meta.get("fetched_at")
        if not fetched:
            return None
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(fetched)).total_seconds()
        if age > _l2_ttl_seconds():
            return None
        return json.loads(lib.read(sym).data["payload"].iloc[0])
    except Exception:  # noqa: BLE001
        return None


def _l2_put(sym: str, bundle: dict) -> None:
    """Persist a bundle for sym. Never raises."""
    try:
        lib = _arctic_library()
        if lib is None:
            return
        # pylint: disable=import-outside-toplevel
        from pandas import DataFrame, Timestamp

        now = datetime.now(timezone.utc)
        df = DataFrame({"payload": [json.dumps(bundle)]}, index=[Timestamp(now)])
        lib.write(sym, df, metadata={"fetched_at": now.isoformat()})
    except Exception:  # noqa: BLE001
        return


def _read_through_sync(sym: str, credentials: dict[str, str] | None) -> dict:
    """L2 get -> EODHD fetch -> L2 put. Runs in a worker thread."""
    hit = _l2_get(sym)
    if hit is not None:
        return hit
    bundle = _fetch_sync(sym, credentials)
    _l2_put(sym, bundle)
    return bundle


async def get_bundle(
    symbol: str, exchange: str, credentials: dict[str, str] | None
) -> dict:
    sym = qualify(symbol, exchange)
    cached = _cached(sym)
    if cached is not None:
        return cached
    async with _get_lock(sym):
        cached = _cached(sym)  # another coroutine may have filled it while we waited
        if cached is not None:
            return cached
        payload = await to_thread(_read_through_sync, sym, credentials)
        _cache[sym] = (time.monotonic() + _TTL_SECONDS, payload)
        return payload


def _rows(section: Any) -> list[dict]:
    if isinstance(section, dict):
        return [v for v in section.values() if isinstance(v, dict)]
    if isinstance(section, list):
        return [v for v in section if isinstance(v, dict)]
    return []


def general(b: dict) -> dict:
    return b.get("General") or {}


def highlights(b: dict) -> dict:
    return b.get("Highlights") or {}


def valuation(b: dict) -> dict:
    return b.get("Valuation") or {}


def shares_stats(b: dict) -> dict:
    return b.get("SharesStats") or {}


def analyst_ratings(b: dict) -> dict:
    return b.get("AnalystRatings") or {}


def esg(b: dict) -> dict:
    return b.get("ESGScores") or {}


def etf_data(b: dict) -> dict:
    return b.get("ETF_Data") or {}


def holders_institutions(b: dict) -> list[dict]:
    return _rows((b.get("Holders") or {}).get("Institutions"))


def holders_funds(b: dict) -> list[dict]:
    return _rows((b.get("Holders") or {}).get("Funds"))


def earnings_history(b: dict) -> list[dict]:
    return _rows((b.get("Earnings") or {}).get("History"))


def earnings_trend(b: dict) -> list[dict]:
    return _rows((b.get("Earnings") or {}).get("Trend"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fundamentals_cache.py -v`
Expected: PASS (8 tests: qualify, rows, accessors, single-flight, TTL expiry, L2 hit, L2 miss+write, L2 unavailable).

- [ ] **Step 5: Commit**

```bash
git add openbb-eodhd/openbb_eodhd/models/_fundamentals.py openbb-eodhd/tests/test_fundamentals_cache.py
git commit -m "feat(eodhd): shared /fundamentals single-flight TTL cache"
```

---

### Task 2: Refactor the 3 statement fetchers onto the shared bundle

**Files:**
- Modify: `openbb_eodhd/models/fundamental.py` (its `_fetch_section`/`_fetch_section_sync` → use `_fundamentals.get_bundle`)
- Test: `tests/test_fundamental.py` (add one coalescing test; keep existing green)

**Interfaces:**
- Consumes: `_fundamentals.get_bundle`, `_fundamentals._rows`.
- Produces: no signature change to the three fetchers; `transform_data`/`_transform` untouched.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_fundamental.py
import asyncio
from openbb_eodhd.models import _fundamentals as F
from openbb_eodhd.models.fundamental import (
    EODHDIncomeStatementFetcher, EODHDBalanceSheetFetcher, EODHDCashFlowStatementFetcher,
    EODHDIncomeStatementQueryParams, EODHDBalanceSheetQueryParams, EODHDCashFlowStatementQueryParams,
)

def test_three_statements_one_symbol_share_one_fetch(monkeypatch):
    """Income+Balance+CashFlow for one symbol -> ONE /fundamentals fetch."""
    F._reset_cache_for_tests()
    calls = {"n": 0}
    bundle = {"Financials": {
        "Income_Statement": {"yearly": {"2025-09-30": {"date": "2025-09-30", "totalRevenue": 1}}, "quarterly": {}},
        "Balance_Sheet": {"yearly": {"2025-09-30": {"date": "2025-09-30", "totalAssets": 2}}, "quarterly": {}},
        "Cash_Flow": {"yearly": {"2025-09-30": {"date": "2025-09-30", "netIncome": 3}}, "quarterly": {}},
    }}
    monkeypatch.setattr(F, "_fetch_sync", lambda s, c: (calls.__setitem__("n", calls["n"] + 1) or bundle))
    creds = {"eodhd_api_key": "k"}

    async def go():
        for Fetcher, QP in [
            (EODHDIncomeStatementFetcher, EODHDIncomeStatementQueryParams),
            (EODHDBalanceSheetFetcher, EODHDBalanceSheetQueryParams),
            (EODHDCashFlowStatementFetcher, EODHDCashFlowStatementQueryParams),
        ]:
            q = QP(symbol="AAPL")
            data = await Fetcher.aextract_data(q, creds)
            Fetcher.transform_data(q, data)

    asyncio.run(go())
    assert calls["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fundamental.py::test_three_statements_one_symbol_share_one_fetch -v`
Expected: FAIL — the current fetchers call `client.get_fundamentals_data(sym, filter=...)` per section (3 fetches, or a mismatch because `_fetch_sync` isn't the code path).

- [ ] **Step 3: Write minimal implementation**

In `fundamental.py`, replace the section fetch so `aextract_data` reads the shared bundle and passes the section dict to `transform_data`. Change each fetcher's `aextract_data` to:

```python
    @staticmethod
    async def aextract_data(query, credentials, **kwargs):  # pylint: disable=unused-argument
        from openbb_eodhd.models._fundamentals import get_bundle
        bundle = await get_bundle(query.symbol, query.exchange, credentials)
        financials = bundle.get("Financials") or {}
        return financials.get("Income_Statement") or {}   # Balance_Sheet / Cash_Flow per fetcher
```

Keep `transform_data` and `_transform` exactly as they are (they already accept the section dict with `yearly`/`quarterly`). Delete the now-unused `_fetch_section`/`_fetch_section_sync`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_fundamental.py -v`
Expected: PASS — existing transform tests still pass, plus the new coalescing test.

- [ ] **Step 5: Commit**

```bash
git add openbb-eodhd/openbb_eodhd/models/fundamental.py openbb-eodhd/tests/test_fundamental.py
git commit -m "refactor(eodhd): statements read the shared /fundamentals bundle (3 calls -> 1)"
```

---

### Task 3: InstitutionalOwnership fetcher

**Files:**
- Create: `openbb_eodhd/models/ownership.py`
- Test: `tests/test_ownership.py`

**Interfaces:**
- Consumes: `_fundamentals.get_bundle`, `holders_institutions`, `qualify`.
- Produces: `EODHDInstitutionalOwnershipFetcher`, `EODHDInstitutionalOwnershipQueryParams`, `EODHDInstitutionalOwnershipData`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ownership.py
from openbb_eodhd.models.ownership import EODHDInstitutionalOwnershipFetcher as Fetcher
from openbb_eodhd.models.ownership import EODHDInstitutionalOwnershipQueryParams as QP

BUNDLE = {"Holders": {"Institutions": {
    "0": {"name": "BlackRock Inc", "date": "2026-03-31", "totalShares": 7.83,
          "totalAssets": 5.07, "currentShares": 1144695425, "change": -9970306, "change_p": -0.86},
    "1": {"name": "Vanguard Group Inc", "date": "2026-03-31", "totalShares": 8.9,
          "totalAssets": 6.1, "currentShares": 1300000000, "change": 100, "change_p": 0.01},
}}}

def test_maps_institutional_holders():
    q = QP(symbol="AAPL")
    rows = Fetcher.transform_data(q, BUNDLE)
    assert len(rows) == 2
    top = rows[0]
    assert top.symbol == "AAPL"
    assert str(top.date) == "2026-03-31"
    assert top.name == "BlackRock Inc"
    assert top.current_shares == 1144695425

def test_empty_holders_returns_empty_list():
    q = QP(symbol="AAPL")
    assert Fetcher.transform_data(q, {"Holders": {"Institutions": {}}}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ownership.py -v`
Expected: FAIL — `ModuleNotFoundError: openbb_eodhd.models.ownership`.

- [ ] **Step 3: Write minimal implementation**

```python
# openbb_eodhd/models/ownership.py
"""EODHD ownership models: institutional & fund holders from /fundamentals."""

from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.institutional_ownership import (
    InstitutionalOwnershipData, InstitutionalOwnershipQueryParams,
)
from openbb_core.provider.standard_models.equity_ownership import (
    EquityOwnershipData, EquityOwnershipQueryParams,
)
from pydantic import Field

from openbb_eodhd.models import _fundamentals as F


def _date(v):
    from pandas import isna, to_datetime
    if not v:
        return None
    ts = to_datetime(v, errors="coerce")
    return None if isna(ts) else ts.date()


class EODHDInstitutionalOwnershipQueryParams(InstitutionalOwnershipQueryParams):
    """EODHD Institutional Ownership Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDInstitutionalOwnershipData(InstitutionalOwnershipData):
    """EODHD Institutional Ownership Data."""
    name: str | None = Field(default=None, description="Holder name.")
    total_shares_percent: float | None = Field(default=None, description="Holder % of shares outstanding.")
    total_assets_percent: float | None = Field(default=None, description="Holder % of its portfolio.")
    current_shares: int | None = Field(default=None, description="Shares currently held.")
    change: float | None = Field(default=None, description="Share change since prior period.")
    change_percent: float | None = Field(default=None, description="Percent share change.")


class EODHDInstitutionalOwnershipFetcher(
    Fetcher[EODHDInstitutionalOwnershipQueryParams, list[EODHDInstitutionalOwnershipData]]
):
    """EODHD institutional holders (Holders.Institutions)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDInstitutionalOwnershipQueryParams:
        return EODHDInstitutionalOwnershipQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDInstitutionalOwnershipData]:  # pylint: disable=unused-argument
        rows = []
        for h in F.holders_institutions(data):
            d = _date(h.get("date"))
            if d is None:
                continue
            rows.append(EODHDInstitutionalOwnershipData.model_validate({
                "symbol": query.symbol.upper(),
                "date": d,
                "name": h.get("name"),
                "total_shares_percent": h.get("totalShares"),
                "total_assets_percent": h.get("totalAssets"),
                "current_shares": h.get("currentShares"),
                "change": h.get("change"),
                "change_percent": h.get("change_p"),
            }))
        rows.sort(key=lambda r: (r.current_shares or 0), reverse=True)
        return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ownership.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add openbb-eodhd/openbb_eodhd/models/ownership.py openbb-eodhd/tests/test_ownership.py
git commit -m "feat(eodhd): InstitutionalOwnership fetcher (Holders.Institutions)"
```

---

### Task 4: EquityOwnership fetcher

**Files:**
- Modify: `openbb_eodhd/models/ownership.py` (add the EquityOwnership classes)
- Test: `tests/test_ownership.py` (add cases)

**Interfaces:**
- Consumes: `_fundamentals.holders_institutions`, `holders_funds`.
- Produces: `EODHDEquityOwnershipFetcher`, `EODHDEquityOwnershipQueryParams`, `EODHDEquityOwnershipData`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_ownership.py
from openbb_eodhd.models.ownership import EODHDEquityOwnershipFetcher as OFetcher
from openbb_eodhd.models.ownership import EODHDEquityOwnershipQueryParams as OQP

def test_equity_ownership_uses_investor_name_and_filing_date():
    q = OQP(symbol="AAPL")
    rows = OFetcher.transform_data(q, BUNDLE)
    assert len(rows) == 2
    r = rows[0]
    assert r.investor_name == "BlackRock Inc"
    assert r.symbol == "AAPL"
    assert str(r.date) == "2026-03-31"
    assert str(r.filing_date) == "2026-03-31"  # EODHD has no separate filing date; mirror `date`
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ownership.py -k equity_ownership -v`
Expected: FAIL — `ImportError: cannot import name 'EODHDEquityOwnershipFetcher'`.

- [ ] **Step 3: Write minimal implementation**

Append to `ownership.py`:

```python
class EODHDEquityOwnershipQueryParams(EquityOwnershipQueryParams):
    """EODHD Equity Ownership Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDEquityOwnershipData(EquityOwnershipData):
    """EODHD Equity Ownership Data."""
    current_shares: int | None = Field(default=None, description="Shares currently held.")
    total_shares_percent: float | None = Field(default=None, description="Holder % of shares outstanding.")


class EODHDEquityOwnershipFetcher(
    Fetcher[EODHDEquityOwnershipQueryParams, list[EODHDEquityOwnershipData]]
):
    """EODHD ownership (institutional + fund holders)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEquityOwnershipQueryParams:
        return EODHDEquityOwnershipQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDEquityOwnershipData]:  # pylint: disable=unused-argument
        rows = []
        for h in F.holders_institutions(data) + F.holders_funds(data):
            d = _date(h.get("date"))
            if d is None or not h.get("name"):
                continue
            rows.append(EODHDEquityOwnershipData.model_validate({
                "investor_name": h.get("name"),
                "date": d,
                "filing_date": d,  # EODHD exposes only the position date
                "symbol": query.symbol.upper(),
                "current_shares": h.get("currentShares"),
                "total_shares_percent": h.get("totalShares"),
            }))
        rows.sort(key=lambda r: (r.current_shares or 0), reverse=True)
        return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ownership.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add openbb-eodhd/openbb_eodhd/models/ownership.py openbb-eodhd/tests/test_ownership.py
git commit -m "feat(eodhd): EquityOwnership fetcher (holders)"
```

---

### Task 5: InsiderTrading fetcher (dedicated endpoint)

**Files:**
- Create: `openbb_eodhd/models/insider.py`
- Test: `tests/test_insider.py`

**Interfaces:**
- Consumes: `_client.get_client`, `raise_sdk_error`; SDK method `get_insider_transactions_data(code=..., limit=...)`.
- Produces: `EODHDInsiderTradingFetcher`, `EODHDInsiderTradingQueryParams`, `EODHDInsiderTradingData`.

> Note: the `/fundamentals` `InsiderTransactions` section is often empty; EODHD's dedicated `/insider-transactions` endpoint is the richer source and supports `limit`. This fetcher does NOT use the shared bundle. Confirm the exact EODHD field names against a live response during Step 2/3 — the fixture below uses EODHD's documented insider schema.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_insider.py
from openbb_eodhd.models.insider import EODHDInsiderTradingFetcher as Fetcher
from openbb_eodhd.models.insider import EODHDInsiderTradingQueryParams as QP

RAW = [
    {"date": "2026-08-01", "code": "AAPL", "ownerName": "COOK TIMOTHY D",
     "ownerRelationship": "CEO", "transactionDate": "2026-07-30", "transactionCode": "S",
     "transactionAmount": 50000, "transactionPrice": 315.0,
     "transactionAcquiredDisposed": "D", "postTransactionAmount": 3200000,
     "secLink": "https://www.sec.gov/x"},
]

def test_maps_insider_rows():
    q = QP(symbol="AAPL")
    rows = Fetcher.transform_data(q, RAW)
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "AAPL"
    assert r.owner_name == "COOK TIMOTHY D"
    assert r.owner_title == "CEO"
    assert str(r.transaction_date) == "2026-07-30"
    assert r.transaction_type == "S"
    assert r.securities_transacted == 50000
    assert r.transaction_price == 315.0
    assert r.acquisition_or_disposition == "D"
    assert r.filing_url == "https://www.sec.gov/x"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_insider.py -v`
Expected: FAIL — `ModuleNotFoundError: openbb_eodhd.models.insider`.

- [ ] **Step 3: Write minimal implementation**

```python
# openbb_eodhd/models/insider.py
"""EODHD insider transactions (/api/insider-transactions)."""

from typing import Any

from openbb_core.app.model.abstract.error import OpenBBError
from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.insider_trading import (
    InsiderTradingData, InsiderTradingQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError, UnauthorizedError
from pydantic import Field

from openbb_eodhd.models._client import get_client, raise_sdk_error
from openbb_eodhd.models._fundamentals import qualify


def _date(v):
    from pandas import isna, to_datetime
    if not v:
        return None
    ts = to_datetime(v, errors="coerce")
    return None if isna(ts) else ts.date()


class EODHDInsiderTradingQueryParams(InsiderTradingQueryParams):
    """EODHD Insider Trading Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDInsiderTradingData(InsiderTradingData):
    """EODHD Insider Trading Data."""
    post_transaction_amount: int | None = Field(default=None, description="Shares held after the transaction.")


class EODHDInsiderTradingFetcher(
    Fetcher[EODHDInsiderTradingQueryParams, list[EODHDInsiderTradingData]]
):
    """EODHD insider transactions."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDInsiderTradingQueryParams:
        return EODHDInsiderTradingQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        def _sync():
            client = get_client(credentials)
            sym = qualify(query.symbol, query.exchange)
            try:
                with client:
                    resp = client.get_insider_transactions_data(
                        code=sym, limit=query.limit or 100
                    )
            except (OpenBBError, UnauthorizedError):
                raise
            except Exception as exc:  # noqa: BLE001
                raise_sdk_error(exc, f"insider for '{sym}'")
            if isinstance(resp, dict):
                raise UnauthorizedError(f"EODHD ({sym}): {resp.get('message') or resp}")
            if not resp:
                raise EmptyDataError(f"EODHD returned no insider data for '{sym}'.")
            return resp

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDInsiderTradingData]:  # pylint: disable=unused-argument
        rows = []
        for it in data:
            rows.append(EODHDInsiderTradingData.model_validate({
                "symbol": (it.get("code") or query.symbol).upper(),
                "owner_name": it.get("ownerName"),
                "owner_title": it.get("ownerRelationship"),
                "transaction_date": _date(it.get("transactionDate")),
                "filing_date": _date(it.get("date")),
                "transaction_type": it.get("transactionCode"),
                "securities_transacted": it.get("transactionAmount"),
                "transaction_price": it.get("transactionPrice"),
                "acquisition_or_disposition": it.get("transactionAcquiredDisposed"),
                "post_transaction_amount": it.get("postTransactionAmount"),
                "filing_url": it.get("secLink"),
            }))
        return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_insider.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add openbb-eodhd/openbb_eodhd/models/insider.py openbb-eodhd/tests/test_insider.py
git commit -m "feat(eodhd): InsiderTrading fetcher (dedicated endpoint)"
```

---

### Task 6: HistoricalEps fetcher

**Files:**
- Create: `openbb_eodhd/models/estimates.py`
- Test: `tests/test_estimates.py`

**Interfaces:**
- Consumes: `_fundamentals.get_bundle`, `earnings_history`.
- Produces: `EODHDHistoricalEpsFetcher`, `EODHDHistoricalEpsQueryParams`, `EODHDHistoricalEpsData`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_estimates.py
from openbb_eodhd.models.estimates import EODHDHistoricalEpsFetcher as EpsFetcher
from openbb_eodhd.models.estimates import EODHDHistoricalEpsQueryParams as EpsQP

BUNDLE = {"Earnings": {"History": {
    "0": {"reportDate": "2026-10-29", "date": "2026-09-30", "epsActual": None, "epsEstimate": 1.98},
    "1": {"reportDate": "2026-07-31", "date": "2026-06-30", "epsActual": 1.57, "epsEstimate": 1.43},
}, "Trend": {
    "0": {"date": "2027-09-30", "period": "+1y", "earningsEstimateAvg": "9.53",
          "earningsEstimateLow": "8.24", "earningsEstimateHigh": "10.67",
          "earningsEstimateNumberOfAnalysts": "39.0",
          "revenueEstimateAvg": "525003468150.00", "revenueEstimateLow": "483496000000.00",
          "revenueEstimateHigh": "594863000000.00", "revenueEstimateNumberOfAnalysts": "39.0"},
}}}

def test_historical_eps_maps_actual_and_estimate():
    q = EpsQP(symbol="AAPL")
    rows = EpsFetcher.transform_data(q, BUNDLE)
    assert len(rows) == 2
    r = [x for x in rows if str(x.date) == "2026-06-30"][0]
    assert r.symbol == "AAPL"
    assert r.eps_actual == 1.57
    assert r.eps_estimated == 1.43
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_estimates.py -k historical_eps -v`
Expected: FAIL — `ModuleNotFoundError: openbb_eodhd.models.estimates`.

- [ ] **Step 3: Write minimal implementation**

```python
# openbb_eodhd/models/estimates.py
"""EODHD earnings/estimates models from /fundamentals (History + Trend)."""

from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.historical_eps import (
    HistoricalEpsData, HistoricalEpsQueryParams,
)
from pydantic import Field

from openbb_eodhd.models import _fundamentals as F


def _date(v):
    from pandas import isna, to_datetime
    if not v:
        return None
    ts = to_datetime(v, errors="coerce")
    return None if isna(ts) else ts.date()


def _f(v):
    try:
        return float(v) if v not in (None, "", "NA") else None
    except (TypeError, ValueError):
        return None


class EODHDHistoricalEpsQueryParams(HistoricalEpsQueryParams):
    """EODHD Historical EPS Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDHistoricalEpsData(HistoricalEpsData):
    """EODHD Historical EPS Data."""
    report_date: Any = Field(default=None, description="Announced report date.")


class EODHDHistoricalEpsFetcher(
    Fetcher[EODHDHistoricalEpsQueryParams, list[EODHDHistoricalEpsData]]
):
    """EODHD historical EPS (Earnings.History)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDHistoricalEpsQueryParams:
        return EODHDHistoricalEpsQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDHistoricalEpsData]:  # pylint: disable=unused-argument
        rows = []
        for e in F.earnings_history(data):
            d = _date(e.get("date") or e.get("reportDate"))
            if d is None:
                continue
            rows.append(EODHDHistoricalEpsData.model_validate({
                "symbol": query.symbol.upper(),
                "date": d,
                "eps_actual": _f(e.get("epsActual")),
                "eps_estimated": _f(e.get("epsEstimate")),
                "report_date": _date(e.get("reportDate")),
            }))
        rows.sort(key=lambda r: r.date)
        return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_estimates.py -k historical_eps -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add openbb-eodhd/openbb_eodhd/models/estimates.py openbb-eodhd/tests/test_estimates.py
git commit -m "feat(eodhd): HistoricalEps fetcher (Earnings.History)"
```

---

### Task 7: AnalystEstimates fetcher

**Files:**
- Modify: `openbb_eodhd/models/estimates.py` (add AnalystEstimates classes)
- Test: `tests/test_estimates.py` (add cases; reuse `BUNDLE`)

**Interfaces:**
- Consumes: `_fundamentals.earnings_trend`, module-level `_f`, `_date`.
- Produces: `EODHDAnalystEstimatesFetcher`, `EODHDAnalystEstimatesQueryParams`, `EODHDAnalystEstimatesData`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_estimates.py
from openbb_eodhd.models.estimates import EODHDAnalystEstimatesFetcher as AeFetcher
from openbb_eodhd.models.estimates import EODHDAnalystEstimatesQueryParams as AeQP

def test_analyst_estimates_maps_trend():
    q = AeQP(symbol="AAPL")
    rows = AeFetcher.transform_data(q, BUNDLE)
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "AAPL"
    assert str(r.date) == "2027-09-30"
    assert r.estimated_eps_avg == 9.53
    assert r.estimated_eps_low == 8.24
    assert r.estimated_eps_high == 10.67
    assert r.estimated_revenue_avg == 525003468150.0
    assert r.number_analysts_estimated_eps == 39
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_estimates.py -k analyst_estimates -v`
Expected: FAIL — `ImportError: cannot import name 'EODHDAnalystEstimatesFetcher'`.

- [ ] **Step 3: Write minimal implementation**

Append to `estimates.py`:

```python
from openbb_core.provider.standard_models.analyst_estimates import (
    AnalystEstimatesData, AnalystEstimatesQueryParams,
)


def _i(v):
    f = _f(v)
    return int(f) if f is not None else None


class EODHDAnalystEstimatesQueryParams(AnalystEstimatesQueryParams):
    """EODHD Analyst Estimates Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDAnalystEstimatesData(AnalystEstimatesData):
    """EODHD Analyst Estimates Data."""


class EODHDAnalystEstimatesFetcher(
    Fetcher[EODHDAnalystEstimatesQueryParams, list[EODHDAnalystEstimatesData]]
):
    """EODHD analyst estimates (Earnings.Trend)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDAnalystEstimatesQueryParams:
        return EODHDAnalystEstimatesQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        return await F.get_bundle(query.symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDAnalystEstimatesData]:  # pylint: disable=unused-argument
        rows = []
        for t in F.earnings_trend(data):
            d = _date(t.get("date"))
            if d is None:
                continue
            rows.append(EODHDAnalystEstimatesData.model_validate({
                "symbol": query.symbol.upper(),
                "date": d,
                "estimated_eps_avg": _f(t.get("earningsEstimateAvg")),
                "estimated_eps_low": _f(t.get("earningsEstimateLow")),
                "estimated_eps_high": _f(t.get("earningsEstimateHigh")),
                "estimated_revenue_avg": _f(t.get("revenueEstimateAvg")),
                "estimated_revenue_low": _f(t.get("revenueEstimateLow")),
                "estimated_revenue_high": _f(t.get("revenueEstimateHigh")),
                "number_analysts_estimated_eps": _i(t.get("earningsEstimateNumberOfAnalysts")),
                "number_analyst_estimated_revenue": _i(t.get("revenueEstimateNumberOfAnalysts")),
            }))
        rows.sort(key=lambda r: r.date)
        return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_estimates.py -k analyst_estimates -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add openbb-eodhd/openbb_eodhd/models/estimates.py openbb-eodhd/tests/test_estimates.py
git commit -m "feat(eodhd): AnalystEstimates fetcher (Earnings.Trend)"
```

---

### Task 8: ForwardEpsEstimates fetcher

**Files:**
- Modify: `openbb_eodhd/models/estimates.py` (add ForwardEpsEstimates classes)
- Test: `tests/test_estimates.py` (add cases)

**Interfaces:**
- Consumes: `_fundamentals.earnings_trend`, `_f`, `_i`, `_date`.
- Produces: `EODHDForwardEpsEstimatesFetcher`, `EODHDForwardEpsEstimatesQueryParams`, `EODHDForwardEpsEstimatesData`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_estimates.py
from openbb_eodhd.models.estimates import EODHDForwardEpsEstimatesFetcher as FeFetcher
from openbb_eodhd.models.estimates import EODHDForwardEpsEstimatesQueryParams as FeQP

def test_forward_eps_maps_trend():
    q = FeQP(symbol="AAPL")
    rows = FeFetcher.transform_data(q, BUNDLE)
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "AAPL"
    assert str(r.date) == "2027-09-30"
    assert r.fiscal_period == "+1y"
    assert r.mean == 9.53
    assert r.low_estimate == 8.24
    assert r.high_estimate == 10.67
    assert r.number_of_analysts == 39
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_estimates.py -k forward_eps -v`
Expected: FAIL — `ImportError: cannot import name 'EODHDForwardEpsEstimatesFetcher'`.

- [ ] **Step 3: Write minimal implementation**

Append to `estimates.py`:

```python
from openbb_core.provider.standard_models.forward_eps_estimates import (
    ForwardEpsEstimatesData, ForwardEpsEstimatesQueryParams,
)


class EODHDForwardEpsEstimatesQueryParams(ForwardEpsEstimatesQueryParams):
    """EODHD Forward EPS Estimates Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDForwardEpsEstimatesData(ForwardEpsEstimatesData):
    """EODHD Forward EPS Estimates Data."""


class EODHDForwardEpsEstimatesFetcher(
    Fetcher[EODHDForwardEpsEstimatesQueryParams, list[EODHDForwardEpsEstimatesData]]
):
    """EODHD forward EPS estimates (Earnings.Trend)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDForwardEpsEstimatesQueryParams:
        return EODHDForwardEpsEstimatesQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        symbol = query.symbol or "AAPL"
        return await F.get_bundle(symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDForwardEpsEstimatesData]:  # pylint: disable=unused-argument
        sym = (query.symbol or "").upper()
        rows = []
        for t in F.earnings_trend(data):
            d = _date(t.get("date"))
            if d is None:
                continue
            rows.append(EODHDForwardEpsEstimatesData.model_validate({
                "symbol": sym,
                "date": d,
                "fiscal_period": t.get("period"),
                "mean": _f(t.get("earningsEstimateAvg")),
                "low_estimate": _f(t.get("earningsEstimateLow")),
                "high_estimate": _f(t.get("earningsEstimateHigh")),
                "number_of_analysts": _i(t.get("earningsEstimateNumberOfAnalysts")),
            }))
        rows.sort(key=lambda r: r.date)
        return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_estimates.py -k forward_eps -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add openbb-eodhd/openbb_eodhd/models/estimates.py openbb-eodhd/tests/test_estimates.py
git commit -m "feat(eodhd): ForwardEpsEstimates fetcher (Earnings.Trend)"
```

---

### Task 9: PriceTargetConsensus fetcher

**Files:**
- Modify: `openbb_eodhd/models/estimates.py` (add PriceTargetConsensus classes)
- Test: `tests/test_estimates.py` (add cases)

**Interfaces:**
- Consumes: `_fundamentals.analyst_ratings`, `general`, `_f`.
- Produces: `EODHDPriceTargetConsensusFetcher`, `EODHDPriceTargetConsensusQueryParams`, `EODHDPriceTargetConsensusData`.

> EODHD gives a single consensus target (`AnalystRatings.TargetPrice`), not per-analyst rows. `PriceTargetConsensus` is the clean fit; `PriceTarget` (per-analyst, dated) is deferred to a later phase with a documented single-row limitation.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_estimates.py
from openbb_eodhd.models.estimates import EODHDPriceTargetConsensusFetcher as PtcFetcher
from openbb_eodhd.models.estimates import EODHDPriceTargetConsensusQueryParams as PtcQP

BUNDLE_RATINGS = {
    "General": {"Name": "Apple Inc."},
    "AnalystRatings": {"Rating": 4.04, "TargetPrice": 324.45,
                       "StrongBuy": 23, "Buy": 7, "Hold": 16, "Sell": 1, "StrongSell": 1},
}

def test_price_target_consensus():
    q = PtcQP(symbol="AAPL")
    rows = PtcFetcher.transform_data(q, BUNDLE_RATINGS)
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "AAPL"
    assert r.name == "Apple Inc."
    assert r.target_consensus == 324.45

def test_price_target_consensus_empty_when_no_target():
    q = PtcQP(symbol="AAPL")
    assert PtcFetcher.transform_data(q, {"AnalystRatings": {"TargetPrice": 0}}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_estimates.py -k price_target_consensus -v`
Expected: FAIL — `ImportError: cannot import name 'EODHDPriceTargetConsensusFetcher'`.

- [ ] **Step 3: Write minimal implementation**

Append to `estimates.py`:

```python
from openbb_core.provider.standard_models.price_target_consensus import (
    PriceTargetConsensusData, PriceTargetConsensusQueryParams,
)


class EODHDPriceTargetConsensusQueryParams(PriceTargetConsensusQueryParams):
    """EODHD Price Target Consensus Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDPriceTargetConsensusData(PriceTargetConsensusData):
    """EODHD Price Target Consensus Data."""
    rating: float | None = Field(default=None, description="EODHD analyst rating (1=strong buy .. 5=strong sell).")


class EODHDPriceTargetConsensusFetcher(
    Fetcher[EODHDPriceTargetConsensusQueryParams, list[EODHDPriceTargetConsensusData]]
):
    """EODHD price target consensus (AnalystRatings)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDPriceTargetConsensusQueryParams:
        return EODHDPriceTargetConsensusQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        symbol = query.symbol or "AAPL"
        return await F.get_bundle(symbol, query.exchange, credentials)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDPriceTargetConsensusData]:  # pylint: disable=unused-argument
        ar = F.analyst_ratings(data)
        target = _f(ar.get("TargetPrice"))
        if not target:
            return []
        return [EODHDPriceTargetConsensusData.model_validate({
            "symbol": (query.symbol or "").upper(),
            "name": F.general(data).get("Name"),
            "target_consensus": target,
            "rating": _f(ar.get("Rating")),
        })]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_estimates.py -k price_target_consensus -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add openbb-eodhd/openbb_eodhd/models/estimates.py openbb-eodhd/tests/test_estimates.py
git commit -m "feat(eodhd): PriceTargetConsensus fetcher (AnalystRatings)"
```

---

### Task 10: Register the new fetchers on the provider

**Files:**
- Modify: `openbb_eodhd/__init__.py`
- Modify: `pyproject.toml` (version bump)
- Test: `tests/test_provider_registration.py`

**Interfaces:**
- Consumes: all fetchers from Tasks 3–9.
- Produces: `eodhd_provider.fetcher_dict` gains keys `InstitutionalOwnership`, `EquityOwnership`, `InsiderTrading`, `HistoricalEps`, `AnalystEstimates`, `ForwardEpsEstimates`, `PriceTargetConsensus`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_provider_registration.py
from openbb_eodhd import eodhd_provider

def test_phase1_models_registered():
    for key in [
        "InstitutionalOwnership", "EquityOwnership", "InsiderTrading",
        "HistoricalEps", "AnalystEstimates", "ForwardEpsEstimates",
        "PriceTargetConsensus",
    ]:
        assert key in eodhd_provider.fetcher_dict, f"missing {key}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_provider_registration.py -v`
Expected: FAIL — keys not yet in `fetcher_dict`.

- [ ] **Step 3: Write minimal implementation**

In `__init__.py`, add imports and `fetcher_dict` entries:

```python
from openbb_eodhd.models.ownership import (
    EODHDInstitutionalOwnershipFetcher, EODHDEquityOwnershipFetcher,
)
from openbb_eodhd.models.insider import EODHDInsiderTradingFetcher
from openbb_eodhd.models.estimates import (
    EODHDHistoricalEpsFetcher, EODHDAnalystEstimatesFetcher,
    EODHDForwardEpsEstimatesFetcher, EODHDPriceTargetConsensusFetcher,
)
```

Add to `fetcher_dict`:

```python
        "InstitutionalOwnership": EODHDInstitutionalOwnershipFetcher,
        "EquityOwnership": EODHDEquityOwnershipFetcher,
        "InsiderTrading": EODHDInsiderTradingFetcher,
        "HistoricalEps": EODHDHistoricalEpsFetcher,
        "AnalystEstimates": EODHDAnalystEstimatesFetcher,
        "ForwardEpsEstimates": EODHDForwardEpsEstimatesFetcher,
        "PriceTargetConsensus": EODHDPriceTargetConsensusFetcher,
```

Add each fetcher name to `__all__`. In `pyproject.toml`, bump `version = "9.1.0"` and update the provider `description` to mention ownership/insider/estimates.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ -v`
Expected: PASS — the registration test plus the full suite.

- [ ] **Step 5: Commit**

```bash
git add openbb-eodhd/openbb_eodhd/__init__.py openbb-eodhd/pyproject.toml openbb-eodhd/tests/test_provider_registration.py
git commit -m "feat(eodhd): register Phase-1 ownership/insider/estimates fetchers; bump 9.1.0"
```

---

### Task 11: Rebuild the container and verify live on the NAS

**Files:**
- None (build + deploy + smoke). No code changes.

**Interfaces:**
- Consumes: the registered provider; `update-openbb-docker` skill's `--container`.

> This task confirms deployment. Resolve the spec's open item first: check whether the NAS `openbb-local:1.0.0` image is built on the NAS from `<nas-checkout>` or pulled from GHCR (`docker --context nas inspect openbb-api` image + `<nas-checkout>/docker-compose.yml`). Rebuild wherever that image originates. Steps below assume the standard Mac build → GHCR → NAS pull; adjust to the NAS-local build if that is the actual origin.

- [ ] **Step 1: Run the extension unit suite (offline gate)**

Run: `cd openbb-eodhd && python -m pytest tests/ -q`
Expected: PASS (all tasks' tests).

- [ ] **Step 2: Rebuild the container at the extension ceilings**

Run: `bash ~/.claude/skills/update-openbb-docker/scripts/update-openbb.sh --container`
Expected: image builds; the build-time provider assertion lists `eodhd` and registers without error.

- [ ] **Step 3: Verify the new providers register in the built image**

Run:
```bash
docker run --rm --entrypoint python openbb-local:$(cd ~/Developer/openbb-docker/openbb-eodhd && grep -m1 '^version' pyproject.toml | cut -d'"' -f2) \
  -c "from openbb_eodhd import eodhd_provider as p; print(sorted(k for k in p.fetcher_dict if k in {'InstitutionalOwnership','EquityOwnership','InsiderTrading','HistoricalEps','AnalystEstimates','ForwardEpsEstimates','PriceTargetConsensus'}))"
```
Expected: all 7 keys printed.

- [ ] **Step 4: Deploy to the NAS and regenerate widgets.json, then smoke-test**

Push/pull per the resolved deployment path, recreate `openbb-api`, then:
```bash
AUTH="Authorization: Basic <token from backends.json>"
BASE="https://openbb.<your-tailnet>.ts.net"
for ep in equity/ownership/institutional equity/fundamental/historical_eps equity/estimates/consensus; do
  curl -s -m 30 -H "$AUTH" "$BASE/api/v1/$ep?symbol=AAPL&provider=eodhd" \
    -o /tmp/r.json -w "[%{http_code}] $ep -> "; \
  python3 -c "import json;d=json.load(open('/tmp/r.json'));r=d.get('results');print('rows='+str(len(r)) if isinstance(r,list) else r)"
done
curl -s -H "$AUTH" "$BASE/widgets.json" | python3 -c "import json,sys;d=json.load(sys.stdin);print(sorted(k for k in d if k.endswith('_eodhd_obb') and any(s in k for s in ['ownership','insider','historical_eps','estimates'])))"
```
Expected: each endpoint returns `200` with `rows>0`; `widgets.json` lists the new `_eodhd_obb` widget ids.

- [ ] **Step 5: Commit the openbb-docker repo changes**

```bash
cd ~/Developer/openbb-docker
git add openbb-eodhd extension-constraints.txt
git commit -m "chore(eodhd): Phase-1 provider expansion (ownership/insider/estimates)"
```

---

## Self-Review

**Spec coverage (Phase 1 slice):** InstitutionalOwnership (T3), EquityOwnership (T4), InsiderTrading (T5), HistoricalEps (T6), AnalystEstimates (T7), ForwardEpsEstimates (T8), PriceTargetConsensus (T9) — all present. PriceTarget is explicitly deferred with a documented reason (T9 note). The spec's two-tier cache (L1 in-process single-flight + L2 ArcticDB best-effort read-through, configurable `EODHD_FUNDAMENTALS_TTL_HOURS`) is T1 (8 tests incl. L2 hit/miss/unavailable), exercised by T2. Phases 2–4 (company core, calendars/discovery/market data, beyond-FMP options/macro) are separate plans.

**Placeholder scan:** No TBD/TODO. The one flagged verification (EODHD insider field names, T5) is called out as a live-response check during the RED/GREEN cycle against a controlled fixture — not a placeholder in the code.

**Type consistency:** `get_bundle(symbol, exchange, credentials)` and the section accessors are defined in T1 and consumed with those exact names in T3–T9. `_f`/`_i`/`_date` helpers are defined in `estimates.py` (T6) before T7–T9 reuse them; `ownership.py` and `insider.py` each define their own `_date`. Fetcher/QueryParams/Data class names in T10's imports match those produced in T3–T9.

---

## Follow-on plans (not in this document)

- **Phase 2 — Company core:** EquityInfo, EquityQuote, KeyMetrics, FinancialRatios, ShareStatistics, KeyExecutives, CompanyNews, EsgScore(deprecated), plus the cross-provider TrailingDividendYield.
- **Phase 3 — Calendars / discovery / market data:** CalendarEarnings/Dividend/Ipo/Splits, EconomicCalendar, HistoricalMarketCap, EquityScreener, EquitySearch, EtfSearch/CryptoSearch, CurrencyPairs/Snapshots, AvailableIndices, IndexHistorical, TreasuryRates, YieldCurve, GovernmentTrades, WorldNews, MarketSnapshots, EtfInfo/Holdings/Sectors/Countries.
- **Phase 4 — Beyond-FMP extras (subscription-gated):** OptionsChains, IndexConstituents (both currently 403 on the account — build with tests marked xfail until the marketplace feeds are enabled), and the macro cluster (EconomicIndicators, CountryProfile, GdpReal/GdpNominal, ConsumerPriceIndex, Unemployment).

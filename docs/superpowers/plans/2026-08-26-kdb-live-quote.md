# kdb live-quote provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve `/equity/price/quote?provider=kdb` from the newest live EODHD tick in kdb, leasing a live subscription for any symbol not already being fed.

**Architecture:** live-grid gains `POST /subscribe`, a symbol-keyed TTL lease that registers under a synthetic connection id so the existing feed machinery picks it up unchanged. `openbb-kdb` gains `KdbEquityQuoteFetcher`, which leases the symbol, reads the newest tick from the shared kdb `trades` table, and takes `prev_close` from the last complete daily bar.

**Tech Stack:** Python 3.12, FastAPI, PyKX (unlicensed IPC client), pytest, OpenBB Platform provider interface.

**Spec:** `docs/superpowers/specs/2026-08-26-kdb-live-quote-design.md`

## Global Constraints

- Nothing in the subscribe leg may fail a quote. Every lease call is best-effort; on any failure the fetcher proceeds to read kdb.
- Leases are keyed by **symbol**, never by an opaque token — the fetcher is stateless per request (spec D4).
- Default TTL 300s, first-tick deadline 3.0s, sweeper interval 30s. All configurable via env.
- `POST /subscribe` stays tailnet-only and must never be funnelled.
- Emptiness of a q aggregate MUST be tested in q, before the aggregate runs. PyKX's `.pd()` reinterprets a q null timestamp's raw int64 as an offset from the q epoch and returns a real-looking 1700s `Timestamp`; `pd.isna()` never fires. See `KdbStore.tick_span` for the established idiom.
- Tick table is `trades`, columns `time, sym, price, size`.
- `ReadThroughCache.get(...)` returns `tuple[list[dict], dict]` — `(rows, metadata)`. Unpack it; never index the tuple as if it were the rows.
- Equities only. No crypto/currency/etf/index quote fetchers.
- Follow existing test style: fake connections and fake feed clients, no live q and no network except the one network-gated test in Task 5.

---

### Task 1: `KdbStore.latest_tick`

**Files:**
- Modify: `kdb-store/kdb_store/store.py` (add method after `tick_span`, ~line 316)
- Test: `kdb-store/tests/test_store.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `KdbStore.latest_tick(symbol: str) -> dict | None` returning `{"time": datetime, "price": float, "size": float}` for the newest tick, or `None` when the symbol has no ticks.

- [ ] **Step 1: Write the failing tests**

Add to `kdb-store/tests/test_store.py`:

```python
def test_latest_tick_checks_emptiness_in_q_not_in_pandas():
    """A q null timestamp survives .pd() as a real-looking 1700s Timestamp, so
    `pd.isna` cannot detect "no ticks" -- the guard has to run inside q, before
    the aggregate. Same trap tick_span documents."""
    conn = FakeConn()
    store = KdbStore(FakeSession(conn))
    store.latest_tick("AAPL")
    q = conn.queries[-1]
    assert "0 = count select from trades where sym = " in q, q
    assert q.index("0 = count") < q.index("max time"), "guard must precede the aggregate"


def test_latest_tick_returns_none_when_there_are_no_ticks():
    import pandas as pd

    conn = FakeConn()
    conn.responses["max time"] = pd.DataFrame({"time": [], "price": [], "size": []})
    store = KdbStore(FakeSession(conn))
    assert store.latest_tick("AAPL") is None


def test_latest_tick_returns_the_newest_row():
    import pandas as pd

    conn = FakeConn()
    conn.responses["max time"] = pd.DataFrame({
        "time": [pd.Timestamp("2026-08-26T15:14:00")],
        "price": [312.95],
        "size": [40.0],
    })
    store = KdbStore(FakeSession(conn))
    got = store.latest_tick("AAPL")
    assert got["price"] == 312.95
    assert got["size"] == 40.0
    assert got["time"] == D("2026-08-26T15:14:00")


def test_latest_tick_binds_the_symbol_as_a_parameter_not_by_interpolation():
    """Interpolating the symbol into the q string would let a symbol containing
    q syntax change the statement."""
    conn = FakeConn()
    store = KdbStore(FakeSession(conn))
    store.latest_tick("AAPL")
    query, args = conn.calls[-1]
    assert "AAPL" not in query
    assert args, "symbol must be passed as a bound argument"
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd kdb-store && pytest tests/test_store.py -q -k latest_tick`
Expected: FAIL — `AttributeError: 'KdbStore' object has no attribute 'latest_tick'`

- [ ] **Step 3: Implement `latest_tick`**

Add to `kdb-store/kdb_store/store.py`, immediately after `tick_span`:

```python
    def latest_tick(self, symbol: str):
        """The newest tick held for a symbol, or None if there are none.

        The emptiness check runs in q, before the aggregate, for the reason
        `tick_span` spells out: an ungrouped q aggregate over zero rows yields
        a row of q nulls, and PyKX's `.pd()` turns a null timestamp into a
        plausible-looking 1700s `Timestamp` rather than `NaT`, so no pandas-side
        check can catch it.
        """
        import pandas as pd

        def newest(conn):
            got = conn(
                "{[qwsym] $[0 = count select from trades where sym = qwsym;"
                " ([] time:`timestamp$(); price:`float$(); size:`float$());"
                " select time, price, size from trades"
                " where sym = qwsym, time = max time]}",
                _q_symbol(symbol),
            ).pd()
            if got is None or got.empty:
                return None
            row = got.iloc[-1]
            if pd.isna(row["time"]):
                return None
            return {
                "time": row["time"].to_pydatetime(),
                "price": float(row["price"]),
                "size": float(row["size"]),
            }

        return self._call(newest)
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `cd kdb-store && pytest tests/test_store.py -q -k latest_tick`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the whole kdb-store suite for regressions**

Run: `cd kdb-store && pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add kdb-store/kdb_store/store.py kdb-store/tests/test_store.py
git commit -m "feat(kdb-store): read the newest tick for a symbol"
```

---

### Task 2: Lease registry and `POST /subscribe` on live-grid

**Files:**
- Create: `live-grid/app/leases.py`
- Modify: `live-grid/app/main.py` (route beside `/health` ~line 359; sweeper in `lifespan` ~line 130)
- Test: `live-grid/tests/test_leases.py`

**Interfaces:**
- Consumes: `FeedManager.register(conn_id: str, symbols: list[str])` and `FeedManager.unregister(conn_id: str)`, both existing and unchanged.
- Produces:
  - `LeaseRegistry(manager, ttl: float = 300.0)` with `renew(symbols: list[str], now: float) -> dict[str, float]` returning symbol → expiry epoch seconds, and `sweep(now: float) -> list[str]` returning the symbols it expired.
  - `POST /subscribe` accepting `{"symbols": [...], "ttl": <optional float>}` and returning `{"leases": {"<SYMBOL>": "<iso8601>"}}`.

- [ ] **Step 1: Write the failing tests**

Create `live-grid/tests/test_leases.py`:

```python
"""Symbol-keyed TTL leases: a subscription that outlives the request."""

from datetime import datetime

from app.leases import LeaseRegistry


class FakeManager:
    def __init__(self):
        self.registered = {}
        self.unregistered = []

    def register(self, conn_id, symbols):
        self.registered[conn_id] = list(symbols)

    def unregister(self, conn_id):
        self.unregistered.append(conn_id)
        self.registered.pop(conn_id, None)


def test_a_lease_registers_the_symbol_under_its_own_id():
    """Per-symbol ids, not one shared id: symbols must expire independently."""
    m = FakeManager()
    LeaseRegistry(m, ttl=300.0).renew(["AAPL"], now=1000.0)
    assert m.registered == {"lease:AAPL": ["AAPL"]}


def test_renewing_extends_the_expiry_without_a_second_registration():
    """The fetcher leases on EVERY quote; that must renew, not accumulate."""
    m = FakeManager()
    reg = LeaseRegistry(m, ttl=300.0)
    first = reg.renew(["AAPL"], now=1000.0)
    second = reg.renew(["AAPL"], now=1200.0)
    assert first["AAPL"] == 1300.0
    assert second["AAPL"] == 1500.0
    assert list(m.registered) == ["lease:AAPL"]


def test_sweep_unregisters_only_the_lapsed_symbols():
    m = FakeManager()
    reg = LeaseRegistry(m, ttl=300.0)
    reg.renew(["AAPL"], now=1000.0)
    reg.renew(["MSFT"], now=1200.0)
    expired = reg.sweep(now=1400.0)
    assert expired == ["AAPL"]
    assert m.unregistered == ["lease:AAPL"]
    assert "lease:MSFT" in m.registered


def test_sweep_is_idempotent():
    """The sweeper runs on a timer; a second pass must not re-unregister."""
    m = FakeManager()
    reg = LeaseRegistry(m, ttl=300.0)
    reg.renew(["AAPL"], now=1000.0)
    reg.sweep(now=1400.0)
    assert reg.sweep(now=1500.0) == []
    assert m.unregistered == ["lease:AAPL"]


def test_a_symbol_relased_after_expiry_registers_again():
    m = FakeManager()
    reg = LeaseRegistry(m, ttl=300.0)
    reg.renew(["AAPL"], now=1000.0)
    reg.sweep(now=1400.0)
    reg.renew(["AAPL"], now=1500.0)
    assert m.registered == {"lease:AAPL": ["AAPL"]}


def test_symbols_are_upper_cased_so_case_cannot_split_a_lease():
    m = FakeManager()
    reg = LeaseRegistry(m, ttl=300.0)
    reg.renew(["aapl"], now=1000.0)
    reg.renew(["AAPL"], now=1100.0)
    assert list(m.registered) == ["lease:AAPL"]
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd live-grid && pytest tests/test_leases.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.leases'`

- [ ] **Step 3: Implement the registry**

Create `live-grid/app/leases.py`:

```python
"""Symbol-keyed TTL leases on the live feed.

A feed otherwise exists only while a `/live_grid_ws` client is connected, so a
caller that wants a symbol fed after its request returns has nothing to hold.
A lease is an ordinary registration under a synthetic connection id --
`_sync_feeds` unions symbols across every `_conns` entry and does not care
which of them came from a websocket -- plus an expiry the sweeper enforces.

Keyed by symbol rather than by a token because the caller is stateless per
request: it has no handle to present on the next call, so renewal has to be
addressable by the only thing it does know.
"""

import logging

log = logging.getLogger(__name__)

DEFAULT_TTL = 300.0


class LeaseRegistry:
    """symbol -> expiry, backed by FeedManager registrations."""

    def __init__(self, manager, ttl: float = DEFAULT_TTL):
        self._manager = manager
        self._ttl = ttl
        self._expiry: dict[str, float] = {}

    @staticmethod
    def _conn_id(symbol: str) -> str:
        return f"lease:{symbol}"

    def renew(self, symbols, now: float, ttl: float | None = None) -> dict[str, float]:
        """Create or extend a lease per symbol. Returns symbol -> expiry."""
        span = self._ttl if ttl is None else ttl
        out = {}
        for raw in symbols:
            sym = str(raw).strip().upper()
            if not sym:
                continue
            if sym not in self._expiry:
                # Registering an already-leased symbol would be harmless but
                # sets _rebuild_pending, and a rebuild stops and reconstructs
                # the whole feed. Renewal must not pay that.
                self._manager.register(self._conn_id(sym), [sym])
            self._expiry[sym] = now + span
            out[sym] = self._expiry[sym]
        return out

    def sweep(self, now: float) -> list[str]:
        """Unregister every lapsed lease. Returns the symbols dropped."""
        dead = [s for s, exp in self._expiry.items() if exp <= now]
        for sym in dead:
            del self._expiry[sym]
            try:
                self._manager.unregister(self._conn_id(sym))
            except Exception:  # noqa: BLE001 - a sweep must never kill its loop
                log.warning("failed to unregister lease for %s", sym, exc_info=True)
        return dead
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `cd live-grid && pytest tests/test_leases.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Write the failing route tests**

Append to `live-grid/tests/test_leases.py`:

```python
def test_subscribe_route_returns_iso_expiries():
    from tests.test_main import make_client

    client = make_client()
    body = client.post("/subscribe", json={"symbols": ["AAPL"]}).json()
    assert set(body) == {"leases"}
    datetime.fromisoformat(body["leases"]["AAPL"])  # parses, or raises


def test_subscribe_route_rejects_an_empty_symbol_list():
    from tests.test_main import make_client

    client = make_client()
    assert client.post("/subscribe", json={"symbols": []}).status_code == 422


def test_subscribe_route_puts_the_symbol_into_the_feed_union():
    """The point of the lease: the feed must actually want the symbol."""
    from tests.test_main import make_client

    client = make_client()
    client.post("/subscribe", json={"symbols": ["AAPL"]})
    manager = client.app.state.manager
    assert "AAPL" in manager._union("us")
```

- [ ] **Step 6: Run them and verify they fail**

Run: `cd live-grid && pytest tests/test_leases.py -q -k route`
Expected: FAIL — 404, the route does not exist

- [ ] **Step 7: Add the route and the sweeper**

In `live-grid/app/main.py`, import at the top of the module:

```python
from app.leases import DEFAULT_TTL, LeaseRegistry
```

Inside `create_app`, after `manager` is constructed and before the routes:

```python
    leases = LeaseRegistry(manager, ttl=float(os.getenv("LIVE_GRID_LEASE_TTL_S", DEFAULT_TTL)))
```

Add the route immediately after `/health`:

```python
    @app.post("/subscribe")
    async def subscribe(body: dict):
        """Lease a live feed for symbols, keyed by symbol and renewable.

        Tailnet-only, like every live-grid route: it confers no power a caller
        does not already have by opening /live_grid_ws. Never funnel it.
        """
        symbols = [s for s in (body.get("symbols") or []) if str(s).strip()]
        if not symbols:
            raise HTTPException(status_code=422, detail="symbols must be a non-empty list")
        ttl = body.get("ttl")
        granted = leases.renew(
            symbols,
            now=asyncio.get_running_loop().time(),
            ttl=float(ttl) if ttl is not None else None,
        )
        base = datetime.now(timezone.utc)
        loop_now = asyncio.get_running_loop().time()
        return {
            "leases": {
                sym: (base + timedelta(seconds=exp - loop_now)).isoformat()
                for sym, exp in granted.items()
            }
        }
```

Add `HTTPException` to the existing `fastapi` import, and `timezone`/`timedelta` to the existing `datetime` import.

In `lifespan`, alongside the existing `manager.run()` task:

```python
    async def _sweep_leases() -> None:
        interval = float(os.getenv("LIVE_GRID_LEASE_SWEEP_S", "30"))
        while True:
            await asyncio.sleep(interval)
            try:
                leases.sweep(asyncio.get_running_loop().time())
            except Exception:  # noqa: BLE001 - the sweeper must outlive its errors
                log.warning("lease sweep failed", exc_info=True)
```

and start/cancel it exactly as `manager.run()` is started and cancelled.

Expose the registry for the tests and `/health`:

```python
    app.state.leases = leases
```

- [ ] **Step 8: Run the route tests and verify they pass**

Run: `cd live-grid && pytest tests/test_leases.py -q`
Expected: PASS (9 tests)

- [ ] **Step 9: Run the whole live-grid suite for regressions**

Run: `cd live-grid && PYTHONFAULTHANDLER=1 pytest -q --capture=sys`
Expected: all pass, no hang

- [ ] **Step 10: Commit**

```bash
git add live-grid/app/leases.py live-grid/app/main.py live-grid/tests/test_leases.py
git commit -m "feat(live-grid): symbol-keyed TTL leases via POST /subscribe"
```

---

### Task 3: Lease client in openbb-kdb

**Files:**
- Create: `openbb-kdb/openbb_kdb/leasing.py`
- Test: `openbb-kdb/tests/test_leasing.py`

**Interfaces:**
- Consumes: `POST /subscribe` from Task 2.
- Produces: `async lease(symbol: str, url: str | None = None, ttl: float | None = None) -> bool` — True when the lease was granted, False on any failure. Never raises.

- [ ] **Step 1: Write the failing tests**

Create `openbb-kdb/tests/test_leasing.py`:

```python
"""The lease leg is best-effort: a quote must survive live-grid being down."""

import pytest

from openbb_kdb.leasing import lease


@pytest.mark.asyncio
async def test_lease_returns_true_when_granted():
    async def fake_post(url, json, timeout):
        return {"leases": {"AAPL": "2026-08-26T15:20:00+00:00"}}

    assert await lease("AAPL", post=fake_post) is True


@pytest.mark.asyncio
async def test_lease_returns_false_when_live_grid_is_unreachable():
    """The whole point: never raise into the fetcher."""
    async def boom(url, json, timeout):
        raise OSError("connection refused")

    assert await lease("AAPL", post=boom) is False


@pytest.mark.asyncio
async def test_lease_returns_false_on_a_malformed_response():
    async def weird(url, json, timeout):
        return {"unexpected": True}

    assert await lease("AAPL", post=weird) is False


@pytest.mark.asyncio
async def test_lease_sends_the_symbol_upper_cased():
    seen = {}

    async def capture(url, json, timeout):
        seen.update(json)
        return {"leases": {"AAPL": "2026-08-26T15:20:00+00:00"}}

    await lease("aapl", post=capture)
    assert seen["symbols"] == ["AAPL"]
```

- [ ] **Step 2: Run them and verify they fail**

Run: `cd openbb-kdb && pytest tests/test_leasing.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'openbb_kdb.leasing'`

- [ ] **Step 3: Implement the client**

Create `openbb-kdb/openbb_kdb/leasing.py`:

```python
"""Best-effort lease of a live feed from live-grid.

Every failure here is swallowed by design. The quote path degrades to whatever
kdb already holds and then to EODHD's REST snapshot, so a lease that could not
be taken costs freshness, never an error. That invariant is the reason this
module exists separately from the fetcher.
"""

import logging
import os

log = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:6903/subscribe"
TIMEOUT_S = 1.0


async def _post(url: str, json: dict, timeout: float):
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=json)
        response.raise_for_status()
        return response.json()


async def lease(symbol: str, url: str | None = None, ttl: float | None = None, post=None) -> bool:
    """Lease `symbol` on the live feed. True if granted; never raises."""
    sym = str(symbol).strip().upper()
    if not sym:
        return False
    target = url or os.getenv("LIVE_GRID_SUBSCRIBE_URL", DEFAULT_URL)
    body: dict = {"symbols": [sym]}
    if ttl is not None:
        body["ttl"] = ttl
    try:
        payload = await (post or _post)(target, json=body, timeout=TIMEOUT_S)
        return sym in (payload or {}).get("leases", {})
    except Exception as exc:  # noqa: BLE001 - a lease failure must not fail a quote
        log.debug("lease for %s failed: %s", sym, exc)
        return False
```

- [ ] **Step 4: Run them and verify they pass**

Run: `cd openbb-kdb && pytest tests/test_leasing.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add openbb-kdb/openbb_kdb/leasing.py openbb-kdb/tests/test_leasing.py
git commit -m "feat(openbb-kdb): best-effort lease client for live-grid"
```

---

### Task 4: `KdbEquityQuoteFetcher`

**Files:**
- Create: `openbb-kdb/openbb_kdb/models/quote.py`
- Modify: `openbb-kdb/openbb_kdb/__init__.py` (import and `fetcher_dict`, lines 6-28)
- Test: `openbb-kdb/tests/test_quote.py`

**Interfaces:**
- Consumes: `KdbStore.latest_tick` (Task 1), `lease` (Task 3), the existing `_cache(credentials)` helper in `openbb_kdb/models/historical.py`.
- Produces: `KdbEquityQuoteFetcher`, registered as `"EquityQuote"` in `fetcher_dict`, which is what makes `provider=kdb` appear on `/equity/price/quote`.
- Produces: `build_quote(symbol: str, tick: dict | None, prev_close: float | None) -> dict` — the pure field-assembly step, unit-testable without kdb.

- [ ] **Step 1: Write the failing assembly tests**

Create `openbb-kdb/tests/test_quote.py`:

```python
"""Quote assembly: live tick over daily-bar session fields."""

from datetime import datetime

import pytest

from openbb_kdb.models.quote import build_quote


def test_a_tick_supplies_last_price_size_and_timestamp():
    got = build_quote("AAPL", {"time": datetime(2026, 8, 26, 15, 14), "price": 312.95,
                               "size": 40.0}, prev_close=309.9)
    assert got["symbol"] == "AAPL"
    assert got["last_price"] == 312.95
    assert got["last_size"] == 40.0
    assert got["last_timestamp"] == datetime(2026, 8, 26, 15, 14)


def test_change_is_computed_against_the_previous_close():
    got = build_quote("AAPL", {"time": datetime(2026, 8, 26), "price": 312.95, "size": 1.0},
                      prev_close=309.9)
    assert got["prev_close"] == 309.9
    assert round(got["change"], 2) == 3.05
    assert round(got["change_percent"], 4) == round(3.05 / 309.9, 4)


def test_intraday_session_fields_are_absent_not_guessed():
    """Daily bars are end-of-day, so today's OHLC does not exist yet. Leaving
    them out is the documented behaviour (spec D5); inventing them from the
    tick would make open == high == low == last_price, which reads as real."""
    got = build_quote("AAPL", {"time": datetime(2026, 8, 26), "price": 312.95, "size": 1.0},
                      prev_close=309.9)
    for field in ("open", "high", "low", "volume"):
        assert got.get(field) is None


def test_a_missing_previous_close_leaves_change_undefined_rather_than_zero():
    """change=0.0 would render as "unchanged", which is a claim we cannot make."""
    got = build_quote("AAPL", {"time": datetime(2026, 8, 26), "price": 312.95, "size": 1.0},
                      prev_close=None)
    assert got["last_price"] == 312.95
    assert got.get("change") is None
    assert got.get("change_percent") is None


def test_a_zero_previous_close_does_not_divide_by_zero():
    got = build_quote("AAPL", {"time": datetime(2026, 8, 26), "price": 5.0, "size": 1.0},
                      prev_close=0.0)
    assert got.get("change_percent") is None


def test_no_tick_yields_no_row():
    assert build_quote("AAPL", None, prev_close=309.9) is None
```

- [ ] **Step 2: Run them and verify they fail**

Run: `cd openbb-kdb && pytest tests/test_quote.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'openbb_kdb.models.quote'`

- [ ] **Step 3: Implement assembly and the fetcher**

Create `openbb-kdb/openbb_kdb/models/quote.py`:

```python
"""Equity quotes served from the live tick store.

`last_price` comes from the newest tick live-grid recorded; `prev_close` from
the last complete daily bar. Intraday open/high/low/volume are deliberately
absent during a session: a daily bar has no row for today until the close, and
deriving them from the single latest tick would report open == high == low ==
last_price, which looks like data rather than like the absence of it.
"""

import asyncio
import logging
import os
from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.equity_quote import (
    EquityQuoteData,
    EquityQuoteQueryParams,
)

from openbb_kdb.leasing import lease

log = logging.getLogger(__name__)

DEADLINE_S = 3.0
POLL_S = 0.1


def build_quote(symbol: str, tick: dict | None, prev_close: float | None) -> dict | None:
    """Assemble one EquityQuote row. Returns None when there is no tick."""
    if not tick:
        return None
    row: dict[str, Any] = {
        "symbol": symbol,
        "last_price": tick["price"],
        "last_size": tick.get("size"),
        "last_timestamp": tick["time"],
    }
    if prev_close is not None:
        row["prev_close"] = prev_close
        row["change"] = tick["price"] - prev_close
        if prev_close:
            row["change_percent"] = (tick["price"] - prev_close) / prev_close
    return row


async def _await_tick(store, symbol: str, deadline: float) -> dict | None:
    """Poll for a first tick until the deadline. Returns None if none arrives."""
    loop = asyncio.get_running_loop()
    stop = loop.time() + deadline
    while True:
        tick = await asyncio.to_thread(store.latest_tick, symbol)
        if tick is not None:
            return tick
        if loop.time() >= stop:
            return None
        await asyncio.sleep(POLL_S)


class KdbEquityQuoteFetcher(Fetcher[EquityQuoteQueryParams, list[EquityQuoteData]]):
    """The newest live tick for a symbol, leasing a feed if one is not running."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EquityQuoteQueryParams:
        return EquityQuoteQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:
        from openbb_kdb.models.historical import _cache

        symbol = query.symbol.upper()
        # Unconditional and idempotent: renewing keeps a hot symbol hot, and
        # tick recency cannot distinguish "quiet" from "not subscribed".
        await lease(symbol)

        cache = _cache(credentials)
        store = cache.store
        deadline = float(os.getenv("KDB_QUOTE_DEADLINE_S", DEADLINE_S))
        tick = await _await_tick(store, symbol, deadline)
        return {"symbol": symbol, "tick": tick, "prev_close": None}

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EquityQuoteData]:
        row = build_quote(data["symbol"], data.get("tick"), data.get("prev_close"))
        return [EquityQuoteData.model_validate(row)] if row else []
```

- [ ] **Step 4: Run them and verify they pass**

Run: `cd openbb-kdb && pytest tests/test_quote.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Write the failing fetcher tests**

Append to `openbb-kdb/tests/test_quote.py`:

```python
@pytest.mark.asyncio
async def test_waiting_returns_the_first_tick_that_appears():
    from openbb_kdb.models.quote import _await_tick

    calls = {"n": 0}

    class Store:
        def latest_tick(self, symbol):
            calls["n"] += 1
            return {"time": datetime(2026, 8, 26), "price": 1.0, "size": 1.0} \
                if calls["n"] >= 2 else None

    assert await _await_tick(Store(), "AAPL", deadline=2.0) is not None
    assert calls["n"] >= 2


@pytest.mark.asyncio
async def test_waiting_gives_up_at_the_deadline_rather_than_hanging():
    from openbb_kdb.models.quote import _await_tick

    class Never:
        def latest_tick(self, symbol):
            return None

    assert await _await_tick(Never(), "AAPL", deadline=0.3) is None


def test_the_fetcher_is_registered_for_the_quote_model():
    """This registration is what puts `kdb` on /equity/price/quote."""
    import openbb_kdb

    assert openbb_kdb.kdb_provider.fetcher_dict["EquityQuote"] is not None
```

- [ ] **Step 6: Run them and verify they fail**

Run: `cd openbb-kdb && pytest tests/test_quote.py -q -k registered`
Expected: FAIL — `KeyError: 'EquityQuote'`

- [ ] **Step 7: Register the fetcher**

In `openbb-kdb/openbb_kdb/__init__.py`, add to the existing import block from `.models.historical` a new import line, and add the entry to `fetcher_dict`:

```python
from openbb_kdb.models.quote import KdbEquityQuoteFetcher
```

```python
        "EquityQuote": KdbEquityQuoteFetcher,
```

- [ ] **Step 8: Run the whole openbb-kdb suite**

Run: `cd openbb-kdb && pytest -q`
Expected: all pass

- [ ] **Step 9: Commit**

```bash
git add openbb-kdb/openbb_kdb/models/quote.py openbb-kdb/openbb_kdb/__init__.py openbb-kdb/tests/test_quote.py
git commit -m "feat(openbb-kdb): serve equity quotes from the live tick store"
```

---

### Task 5: prev_close from the daily bar, and end-to-end wiring

**Files:**
- Modify: `openbb-kdb/openbb_kdb/models/quote.py` (`aextract_data`)
- Test: `openbb-kdb/tests/test_quote.py`
- Modify: `live-grid/README.md`, `openbb-kdb/README.md`

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces: `prev_close` populated from the last complete daily bar; no new public names.

- [ ] **Step 1: Write the failing test**

Append to `openbb-kdb/tests/test_quote.py`:

```python
@pytest.mark.asyncio
async def test_prev_close_comes_from_the_last_complete_daily_bar():
    from openbb_kdb.models.quote import _prev_close

    class Cache:
        async def get(self, **kwargs):
            # ReadThroughCache.get returns (rows, metadata), not rows.
            return ([{"date": "2026-08-24", "close": 300.0},
                     {"date": "2026-08-25", "close": 309.9}], {})

    assert await _prev_close(Cache(), "AAPL", credentials=None) == 309.9


@pytest.mark.asyncio
async def test_a_failing_bar_lookup_does_not_fail_the_quote():
    """Spec: a missing daily bar yields last_price only, never an error."""
    from openbb_kdb.models.quote import _prev_close

    class Broken:
        async def get(self, **kwargs):
            raise RuntimeError("kdb down")

    assert await _prev_close(Broken(), "AAPL", credentials=None) is None
```

- [ ] **Step 2: Run them and verify they fail**

Run: `cd openbb-kdb && pytest tests/test_quote.py -q -k prev_close`
Expected: FAIL — `ImportError: cannot import name '_prev_close'`

- [ ] **Step 3: Implement `_prev_close` and call it**

Add to `openbb-kdb/openbb_kdb/models/quote.py`:

```python
async def _prev_close(cache, symbol: str, credentials) -> float | None:
    """Close of the most recent complete daily bar, or None.

    Best-effort exactly like the lease: a quote that knows the last price but
    not yesterday's close is still a useful quote, and is what the spec asks
    for when no bar is available.
    """
    from datetime import date, timedelta

    end = date.today()
    try:
        # get() answers (rows, metadata) -- unpack, do not index the tuple.
        rows, _meta = await cache.get(
            symbol=symbol, interval="1d", start=end - timedelta(days=10), end=end,
            model="EquityHistorical", params={}, credentials=credentials,
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.debug("prev_close lookup for %s failed: %s", symbol, exc)
        return None
    if not rows:
        return None
    close = rows[-1].get("close")
    return float(close) if close is not None else None
```

and in `aextract_data`, replace the `"prev_close": None` placeholder:

```python
        prev = await _prev_close(cache, symbol, credentials)
        return {"symbol": symbol, "tick": tick, "prev_close": prev}
```

- [ ] **Step 4: Run them and verify they pass**

Run: `cd openbb-kdb && pytest tests/test_quote.py -q`
Expected: PASS

- [ ] **Step 5: Write the network-gated integration test**

Append to `openbb-kdb/tests/test_quote.py`:

```python
@pytest.mark.skipif(
    not os.getenv("KDB_QUOTE_LIVE_TEST"),
    reason="needs a running live-grid, kdb and EODHD key; set KDB_QUOTE_LIVE_TEST=1",
)
@pytest.mark.asyncio
async def test_live_quote_end_to_end():
    """Leases a real symbol, waits for a real tick, asserts a sane quote.

    Deliberately not asserting an exact price: the point is that the lease
    reached live-grid, a tick landed in kdb and the fields assembled.
    """
    from openbb_kdb.models.quote import KdbEquityQuoteFetcher

    query = KdbEquityQuoteFetcher.transform_query({"symbol": "AAPL"})
    raw = await KdbEquityQuoteFetcher.aextract_data(query, credentials=None)
    rows = KdbEquityQuoteFetcher.transform_data(query, raw)
    assert rows, "no quote returned -- is live-grid reachable and the market open?"
    assert rows[0].last_price and rows[0].last_price > 0
```

Add `import os` to the test module's imports.

- [ ] **Step 6: Run the full suites for both packages**

Run: `cd openbb-kdb && pytest -q && cd ../live-grid && PYTHONFAULTHANDLER=1 pytest -q --capture=sys && cd ../kdb-store && pytest -q`
Expected: all pass; the live test is skipped

- [ ] **Step 7: Document it**

In `live-grid/README.md`, add under the routes section:

```markdown
### `POST /subscribe`

Leases a live feed for symbols, keyed by symbol and renewable:

    curl -X POST http://127.0.0.1:6903/subscribe \
      -H 'content-type: application/json' \
      -d '{"symbols":["AAPL"],"ttl":300}'
    {"leases":{"AAPL":"2026-08-26T15:20:00+00:00"}}

Posting a symbol that is already leased extends its expiry rather than adding
a second registration. Leases lapse after their TTL unless renewed; a sweeper
drops them. Tailnet-only, like every route here — never funnel it.

Leasing a symbol that is not already fed rebuilds its EODHD feed, because the
SDK fixes symbols at construction. That briefly interrupts ticks for other
symbols on the same feed, which is why the TTL is generous rather than tight.
```

In `openbb-kdb/README.md`, add:

```markdown
## Quotes

`/equity/price/quote?provider=kdb` returns the newest live tick, leasing a
live-grid subscription for symbols that are not already being fed. During a
session the payload carries `last_price`, `last_size`, `last_timestamp`,
`prev_close` and `change`; today's `open`/`high`/`low`/`volume` stay empty
because daily bars only exist after the close.

Environment: `LIVE_GRID_SUBSCRIBE_URL` (default
`http://127.0.0.1:6903/subscribe`), `KDB_QUOTE_DEADLINE_S` (default 3).
```

- [ ] **Step 8: Commit**

```bash
git add openbb-kdb/openbb_kdb/models/quote.py openbb-kdb/tests/test_quote.py live-grid/README.md openbb-kdb/README.md
git commit -m "feat(openbb-kdb): previous close from the daily bar, and docs"
```

---

### Task 6: EODHD snapshot fallback when no tick arrives

**Files:**
- Modify: `live-grid/app/main.py` (route beside `/subscribe`)
- Modify: `openbb-kdb/openbb_kdb/leasing.py`
- Modify: `openbb-kdb/openbb_kdb/models/quote.py`
- Test: `live-grid/tests/test_leases.py`, `openbb-kdb/tests/test_quote.py`

**Why this lives in live-grid.** The spec's error table falls back to an EODHD
REST snapshot in three cases (live-grid unreachable, kdb unreachable, no tick
before the deadline). The `eodhd` client is a live-grid dependency — a pinned
GitHub tarball — and live-grid already owns the symbol-to-ticker mapping
(`snapshot_ticker`, `AAPL` -> `AAPL.US`) and a TTL'd snapshot cache in kdb.
Pulling that dependency and that mapping into the provider would duplicate all
three, so live-grid exposes the snapshot it can already produce and the
provider stays a kdb client.

Kept separate from `/subscribe` so each route does one job: this is a cold-path
call made only when no tick arrived, whereas `/subscribe` is called on every
quote.

**Interfaces:**
- Consumes: `lease` (Task 3), `build_quote` (Task 4).
- Produces: `GET /snapshot?symbol=AAPL` -> `{"symbol": "AAPL", "price": 312.95, "prev_close": 309.9, "delayed": true}`, or 404 when no snapshot is available.
- Produces: `async snapshot(symbol: str, url: str | None = None, get=None) -> dict | None` in `openbb_kdb/leasing.py` — None on any failure, never raises.
- Produces: `build_quote_from_snapshot(symbol: str, snap: dict | None) -> dict | None` in `openbb_kdb/models/quote.py`.

- [ ] **Step 1: Write the failing route tests**

Append to `live-grid/tests/test_leases.py`:

```python
def test_snapshot_route_returns_a_delayed_flagged_price():
    from tests.test_main import make_client

    client = make_client()
    body = client.get("/snapshot", params={"symbol": "AAPL"}).json()
    assert body["symbol"] == "AAPL"
    assert body["delayed"] is True, "a REST snapshot is delayed and must say so"
    assert isinstance(body["price"], float)


def test_snapshot_route_404s_when_the_vendor_gives_nothing():
    """A missing snapshot is not something the caller should crash on; the
    fetcher reads 404 as 'no fallback available' and returns no rows."""
    from tests.test_main import make_client

    class Empty:
        def get_live_stock_prices(self, ticker):
            return None

    client = make_client(seed_client=Empty())
    assert client.get("/snapshot", params={"symbol": "ZZZZ"}).status_code == 404
```

- [ ] **Step 2: Run them and verify they fail**

Run: `cd live-grid && pytest tests/test_leases.py -q -k snapshot`
Expected: FAIL — 404 for both; the route does not exist

- [ ] **Step 3: Add the route**

In `live-grid/app/main.py`, immediately after `/subscribe`:

```python
    @app.get("/snapshot")
    async def snapshot(symbol: str):
        """The delayed REST snapshot for one symbol.

        Exists so the kdb quote provider has a fallback without taking on the
        eodhd client, the AAPL -> AAPL.US mapping and the snapshot cache that
        already live here. `delayed` is always true: this is EODHD's REST
        endpoint, roughly 15-20 minutes behind, never the websocket.
        """
        sym = symbol.strip().upper()
        rows = await asyncio.to_thread(quotes.seed, [sym], seed_client)
        row = rows[0] if rows else None
        price = (row or {}).get("price")
        if price is None:
            raise HTTPException(status_code=404, detail=f"no snapshot for {sym}")
        return {
            "symbol": sym,
            "price": float(price),
            "prev_close": row.get("prev_close"),
            "delayed": True,
        }
```

- [ ] **Step 4: Run them and verify they pass**

Run: `cd live-grid && pytest tests/test_leases.py -q -k snapshot`
Expected: PASS (2 tests)

- [ ] **Step 5: Write the failing client and fetcher tests**

Append to `openbb-kdb/tests/test_quote.py`:

```python
@pytest.mark.asyncio
async def test_snapshot_client_returns_none_when_live_grid_is_down():
    from openbb_kdb.leasing import snapshot

    async def boom(url, params, timeout):
        raise OSError("connection refused")

    assert await snapshot("AAPL", get=boom) is None


@pytest.mark.asyncio
async def test_snapshot_client_returns_none_on_404():
    from openbb_kdb.leasing import snapshot

    async def missing(url, params, timeout):
        raise LookupError("404")

    assert await snapshot("AAPL", get=missing) is None


def test_a_snapshot_builds_a_quote_when_no_tick_exists():
    from openbb_kdb.models.quote import build_quote_from_snapshot

    got = build_quote_from_snapshot("AAPL", {"price": 312.95, "prev_close": 309.9})
    assert got["last_price"] == 312.95
    assert got["prev_close"] == 309.9
    assert round(got["change"], 2) == 3.05


def test_no_tick_and_no_snapshot_yields_no_rows():
    """The only case the spec allows to return nothing."""
    from openbb_kdb.models.quote import KdbEquityQuoteFetcher

    query = KdbEquityQuoteFetcher.transform_query({"symbol": "AAPL"})
    rows = KdbEquityQuoteFetcher.transform_data(
        query, {"symbol": "AAPL", "tick": None, "prev_close": None, "snapshot": None}
    )
    assert rows == []
```

- [ ] **Step 6: Run them and verify they fail**

Run: `cd openbb-kdb && pytest tests/test_quote.py -q -k snapshot`
Expected: FAIL — `ImportError: cannot import name 'snapshot'`

- [ ] **Step 7: Implement the client and wire the fallback**

Add to `openbb-kdb/openbb_kdb/leasing.py`:

```python
DEFAULT_SNAPSHOT_URL = "http://127.0.0.1:6903/snapshot"


async def _get(url: str, params: dict, timeout: float):
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


async def snapshot(symbol: str, url: str | None = None, get=None) -> dict | None:
    """Delayed REST snapshot for a symbol, or None. Never raises."""
    sym = str(symbol).strip().upper()
    if not sym:
        return None
    target = url or os.getenv("LIVE_GRID_SNAPSHOT_URL", DEFAULT_SNAPSHOT_URL)
    try:
        return await (get or _get)(target, params={"symbol": sym}, timeout=TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - a missing fallback is not an error
        log.debug("snapshot for %s failed: %s", sym, exc)
        return None
```

Add to `openbb-kdb/openbb_kdb/models/quote.py`:

```python
def build_quote_from_snapshot(symbol: str, snap: dict | None) -> dict | None:
    """Assemble a row from the delayed REST snapshot. None when unavailable."""
    if not snap or snap.get("price") is None:
        return None
    price = float(snap["price"])
    prev = snap.get("prev_close")
    row: dict[str, Any] = {"symbol": symbol, "last_price": price}
    if prev is not None:
        row["prev_close"] = float(prev)
        row["change"] = price - float(prev)
        if float(prev):
            row["change_percent"] = row["change"] / float(prev)
    return row
```

Change the import at the top of `quote.py` to
`from openbb_kdb.leasing import lease, snapshot`, then in `aextract_data`
make the kdb read survivable and take the snapshot only when no tick arrived:

```python
        try:
            tick = await _await_tick(store, symbol, deadline)
        except Exception as exc:  # noqa: BLE001 - kdb down falls back, per the spec
            log.debug("tick read for %s failed: %s", symbol, exc)
            tick = None
        prev = await _prev_close(cache, symbol, credentials)
        snap = None if tick else await snapshot(symbol)
        return {"symbol": symbol, "tick": tick, "prev_close": prev, "snapshot": snap}
```

and in `transform_data`, fall through to it:

```python
    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EquityQuoteData]:
        row = build_quote(data["symbol"], data.get("tick"), data.get("prev_close"))
        if row is None:
            row = build_quote_from_snapshot(data["symbol"], data.get("snapshot"))
        return [EquityQuoteData.model_validate(row)] if row else []
```

- [ ] **Step 8: Run them and verify they pass**

Run: `cd openbb-kdb && pytest tests/test_quote.py -q`
Expected: PASS

- [ ] **Step 9: Run every affected suite**

Run: `cd kdb-store && pytest -q && cd ../openbb-kdb && pytest -q && cd ../live-grid && PYTHONFAULTHANDLER=1 pytest -q --capture=sys`
Expected: all pass

- [ ] **Step 10: Document the fallback**

Append to the `openbb-kdb/README.md` section added in Task 5:

```markdown
When no tick arrives before `KDB_QUOTE_DEADLINE_S`, the quote falls back to
live-grid's `GET /snapshot` — EODHD's REST price, roughly 15-20 minutes
delayed. Only when that is unavailable too does the route return no rows.
Environment: `LIVE_GRID_SNAPSHOT_URL` (default
`http://127.0.0.1:6903/snapshot`).
```

- [ ] **Step 11: Commit**

```bash
git add live-grid/app/main.py live-grid/tests/test_leases.py \
        openbb-kdb/openbb_kdb/leasing.py openbb-kdb/openbb_kdb/models/quote.py \
        openbb-kdb/tests/test_quote.py openbb-kdb/README.md
git commit -m "feat: fall back to the delayed EODHD snapshot when no tick arrives"
```

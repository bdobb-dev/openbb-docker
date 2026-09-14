# live-grid subscription manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A widget served by live-grid that manages a durable EODHD subscription list — add and remove symbols, grouped by feed, against a shared 50-symbol budget.

**Architecture:** The widget is an `iframe` entry in live-grid's `widgets.json` pointing at an HTML page live-grid serves itself, so the front end needs no changes. A JSON file on a new writable mount holds the pinned symbols; they register with `FeedManager` under synthetic `watchlist:<SYMBOL>` ids, the same mechanism the TTL leases use. The cap counts pinned and leased symbols together.

**Tech Stack:** Python 3.12, FastAPI, vanilla HTML/JS (no build step), pytest.

**Spec:** No spec file — this is the bounded path. The approved design is in the conversation and is restated in full under Global Constraints and each task's preamble.

## Global Constraints

- **The budget is a UNION, not a sum.** EODHD sees one set of symbols per connection, so a symbol that is both pinned and leased occupies ONE vendor slot. Compute `len(pinned | leased)`, never `len(pinned) + len(leased)`.
- Cap default 50, from `LIVE_GRID_MAX_SYMBOLS`. It is account-wide across all three feeds, not per feed.
- Watchlist path from `LIVE_GRID_WATCHLIST`, default `/data/watchlist.json`.
- Symbols normalise as `str(s).strip().upper()` — identical to `LeaseRegistry.renew` and `classify()`, so the same symbol cannot appear twice under different casing.
- Writes are atomic: write a temp file in the same directory, then `os.replace`. A half-written watchlist must never be readable.
- A missing, empty or corrupt watchlist file loads as an empty list and logs a warning. It must never raise on startup — a bad file cannot be allowed to stop live-grid from serving.
- The three feeds are `us`, `crypto`, `forex` (`app/classify.py:3`). The widget labels them **Equity**, **Crypto**, **Forex**. There is no separate "Currency"/"FX" split — they are one feed.
- Leased symbols are shown but NOT removable through this widget; they lapse on their own TTL.
- `/subscriptions` and `/api/subscriptions*` are tailnet-only like every live-grid route, and must never be funnelled.
- Follow existing test style: fake feed clients via `make_client`, no live q, no network.

## File structure

| file | responsibility |
|---|---|
| `live-grid/app/watchlist.py` (new) | durable symbol set: load, add, remove, list. Knows nothing about feeds, caps or HTTP. |
| `live-grid/app/leases.py` (modify) | gains `symbols()` so the budget can see leased symbols by name, not just count. |
| `live-grid/app/main.py` (modify) | the four routes, cap enforcement, and startup registration. |
| `live-grid/app/static/subscriptions.html` (new) | the page: grouped list, add/remove, budget bar. |
| `live-grid/widgets.json` (modify) | the iframe widget entry. |
| `docker-compose.yml` (modify) | the writable mount live-grid needs. |

---

### Task 1: `Watchlist` — a durable symbol set

A plain JSON-backed set of symbols. Deliberately knows nothing about feeds, caps
or HTTP: those belong to the route layer, and keeping them out is what makes this
testable without a running app.

**Files:**
- Create: `live-grid/app/watchlist.py`
- Test: `live-grid/tests/test_watchlist.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `Watchlist(path: str | Path)` with `symbols() -> list[str]` (sorted), `add(symbol: str) -> bool` (False if already present), `remove(symbol: str) -> bool` (False if absent), and `reload() -> None`.
  - `DEFAULT_PATH = "/data/watchlist.json"`

- [ ] **Step 1: Write the failing tests**

Create `live-grid/tests/test_watchlist.py`:

```python
"""The durable half of the subscription manager."""

import json

import pytest

from app.watchlist import Watchlist


def test_a_missing_file_loads_as_empty_rather_than_raising(tmp_path):
    """live-grid must start even with no watchlist yet -- this is the first-run case."""
    assert Watchlist(tmp_path / "nope.json").symbols() == []


def test_add_then_reload_from_disk_keeps_the_symbol(tmp_path):
    """The whole point: survive a restart."""
    p = tmp_path / "w.json"
    Watchlist(p).add("AAPL")
    assert Watchlist(p).symbols() == ["AAPL"]


def test_symbols_come_back_sorted(tmp_path):
    w = Watchlist(tmp_path / "w.json")
    for s in ("TSLA", "AAPL", "MSFT"):
        w.add(s)
    assert w.symbols() == ["AAPL", "MSFT", "TSLA"]


def test_symbols_normalise_so_case_cannot_duplicate_one(tmp_path):
    """Same normalisation as LeaseRegistry and classify(): strip().upper()."""
    w = Watchlist(tmp_path / "w.json")
    assert w.add(" aapl ") is True
    assert w.add("AAPL") is False
    assert w.symbols() == ["AAPL"]


def test_add_reports_whether_it_changed_anything(tmp_path):
    w = Watchlist(tmp_path / "w.json")
    assert w.add("AAPL") is True
    assert w.add("AAPL") is False


def test_remove_reports_whether_it_changed_anything(tmp_path):
    w = Watchlist(tmp_path / "w.json")
    w.add("AAPL")
    assert w.remove("AAPL") is True
    assert w.remove("AAPL") is False
    assert w.symbols() == []


def test_a_corrupt_file_loads_as_empty_and_does_not_raise(tmp_path):
    """A bad file must not stop live-grid from serving. Losing a watchlist is
    recoverable; a container that will not start is not."""
    p = tmp_path / "w.json"
    p.write_text("{ this is not json")
    assert Watchlist(p).symbols() == []


def test_a_json_file_of_the_wrong_shape_loads_as_empty(tmp_path):
    """Valid JSON that is not a list of strings is just as unusable."""
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"symbols": ["AAPL"]}))
    assert Watchlist(p).symbols() == []


def test_the_write_is_atomic_so_a_reader_never_sees_a_partial_file(tmp_path):
    """Written to a temp file in the same directory, then os.replace'd. Checked by
    proving no stray temp file survives and the content is complete."""
    p = tmp_path / "w.json"
    w = Watchlist(p)
    for s in ("AAPL", "MSFT", "TSLA"):
        w.add(s)
    assert json.loads(p.read_text()) == ["AAPL", "MSFT", "TSLA"]
    leftovers = [f.name for f in tmp_path.iterdir() if f.name != "w.json"]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_the_parent_directory_is_created_if_absent(tmp_path):
    """The mount may be empty on first run."""
    p = tmp_path / "sub" / "dir" / "w.json"
    Watchlist(p).add("AAPL")
    assert p.exists()


def test_an_empty_or_blank_symbol_is_rejected(tmp_path):
    w = Watchlist(tmp_path / "w.json")
    assert w.add("   ") is False
    assert w.symbols() == []
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd live-grid && pytest tests/test_watchlist.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.watchlist'`

- [ ] **Step 3: Implement `Watchlist`**

Create `live-grid/app/watchlist.py`:

```python
"""A durable set of subscribed symbols, backed by one JSON file.

Deliberately ignorant of feeds, caps and HTTP. Those live in the route layer,
and keeping them out of here is what lets this be tested without an app.

Stored as a plain JSON array so it can be read, edited or committed by hand.
"""

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PATH = "/data/watchlist.json"


def _normalise(symbol: str) -> str:
    """Same rule as LeaseRegistry.renew and classify(), so one symbol has one form."""
    return str(symbol).strip().upper()


class Watchlist:
    """Symbols pinned by the operator, persisted across restarts."""

    def __init__(self, path):
        self._path = Path(path)
        self._symbols: set[str] = set()
        self.reload()

    def reload(self) -> None:
        """Read the file. A missing, empty, corrupt or wrongly-shaped file is an
        empty watchlist, never an exception: losing the list is recoverable, a
        container that will not start is not."""
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            self._symbols = set()
            return
        except (OSError, ValueError) as exc:
            log.warning("watchlist at %s is unreadable, starting empty: %s", self._path, exc)
            self._symbols = set()
            return
        if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
            log.warning("watchlist at %s is not a list of strings, starting empty", self._path)
            self._symbols = set()
            return
        self._symbols = {_normalise(s) for s in raw if _normalise(s)}

    def symbols(self) -> list[str]:
        return sorted(self._symbols)

    def add(self, symbol: str) -> bool:
        """Add one symbol. False when it was already there or is blank."""
        sym = _normalise(symbol)
        if not sym or sym in self._symbols:
            return False
        self._symbols.add(sym)
        self._save()
        return True

    def remove(self, symbol: str) -> bool:
        """Remove one symbol. False when it was not there."""
        sym = _normalise(symbol)
        if sym not in self._symbols:
            return False
        self._symbols.discard(sym)
        self._save()
        return True

    def _save(self) -> None:
        """Write via a temp file in the SAME directory, then os.replace.

        Same directory because os.replace is only atomic within one filesystem,
        and the watchlist lives on a mount that is not the container's root.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(self.symbols(), handle, indent=2)
            os.replace(tmp, self._path)
        except BaseException:
            # Leave no debris if the write or the replace failed.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `cd live-grid && pytest tests/test_watchlist.py -q`
Expected: PASS (11 tests)

- [ ] **Step 5: Run the whole live-grid suite for regressions**

Run: `cd live-grid && PYTHONFAULTHANDLER=1 pytest -q --capture=sys`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add live-grid/app/watchlist.py live-grid/tests/test_watchlist.py
git commit -m "feat(live-grid): durable watchlist backed by one JSON file"
```

---

### Task 2: The subscription API, and a budget that counts the union

The routes, the cap, and the one addition `LeaseRegistry` needs to make the
budget honest. The union rule in Global Constraints is the whole point of this
task: a symbol that is both pinned and leased is ONE subscription at EODHD, so
summing the two counts would refuse adds while slots were still free.

**Files:**
- Modify: `live-grid/app/leases.py` (add `symbols()` beside `__len__`)
- Modify: `live-grid/app/main.py` (routes, beside the existing `/subscribe`)
- Test: `live-grid/tests/test_subscriptions.py`

**Interfaces:**
- Consumes: `Watchlist(path)` with `symbols()`, `add(symbol) -> bool`, `remove(symbol) -> bool` (Task 1); `LeaseRegistry` from `app/leases.py`; `classify(symbol) -> "us" | "crypto" | "forex"` from `app/classify.py`.
- Produces:
  - `LeaseRegistry.symbols() -> list[str]` — the leased symbols, sorted.
  - `GET /api/subscriptions` -> `{"service": "EODHD", "cap": int, "used": int, "pinned": [...], "leased": [...], "groups": {"Equity": [...], "Crypto": [...], "Forex": [...]}}`
  - `POST /api/subscriptions` body `{"symbol": "AAPL"}` -> 201 with the same payload; 409 already pinned; 422 blank; 507 would exceed the cap.
  - `DELETE /api/subscriptions/{symbol}` -> 200 with the payload; 404 when not pinned.

- [ ] **Step 1: Write the failing tests**

Create `live-grid/tests/test_subscriptions.py`:

```python
"""The subscription API: grouping, and a cap that counts the vendor's view."""

import asyncio

import pytest

from tests.test_main import make_client


def _client(tmp_path, monkeypatch, cap=50):
    monkeypatch.setenv("LIVE_GRID_WATCHLIST", str(tmp_path / "w.json"))
    monkeypatch.setenv("LIVE_GRID_MAX_SYMBOLS", str(cap))
    return make_client()


def test_an_empty_watchlist_reports_the_cap_and_nothing_used(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch).get("/api/subscriptions").json()
    assert body["service"] == "EODHD"
    assert body["cap"] == 50
    assert body["used"] == 0
    assert body["pinned"] == []


def test_adding_a_symbol_pins_it_and_counts_it(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/subscriptions", json={"symbol": "AAPL"}).status_code == 201
    body = client.get("/api/subscriptions").json()
    assert body["pinned"] == ["AAPL"]
    assert body["used"] == 1


def test_symbols_are_grouped_by_feed_under_display_names(tmp_path, monkeypatch):
    """classify() returns us/crypto/forex; the widget shows Equity/Crypto/Forex."""
    client = _client(tmp_path, monkeypatch)
    for s in ("MSFT", "AAPL", "BTC-USD", "EURUSD"):
        client.post("/api/subscriptions", json={"symbol": s})
    groups = client.get("/api/subscriptions").json()["groups"]
    assert groups["Equity"] == ["AAPL", "MSFT"], "alphabetical within a group"
    assert groups["Crypto"] == ["BTC-USD"]
    assert groups["Forex"] == ["EURUSD"]


def test_every_group_is_present_even_when_empty(tmp_path, monkeypatch):
    """The page renders three sections unconditionally; absent keys would break it."""
    groups = _client(tmp_path, monkeypatch).get("/api/subscriptions").json()["groups"]
    assert set(groups) == {"Equity", "Crypto", "Forex"}


def test_adding_a_symbol_twice_is_a_conflict(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/subscriptions", json={"symbol": "AAPL"})
    assert client.post("/api/subscriptions", json={"symbol": "AAPL"}).status_code == 409


def test_a_blank_symbol_is_rejected(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/subscriptions", json={"symbol": "   "}).status_code == 422


def test_the_cap_refuses_an_add_that_would_exceed_it(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, cap=2)
    client.post("/api/subscriptions", json={"symbol": "AAPL"})
    client.post("/api/subscriptions", json={"symbol": "MSFT"})
    r = client.post("/api/subscriptions", json={"symbol": "TSLA"})
    assert r.status_code == 507
    assert "cap" in r.json()["detail"].lower() or "50" in r.json()["detail"] or "2" in r.json()["detail"]
    assert client.get("/api/subscriptions").json()["pinned"] == ["AAPL", "MSFT"]


def test_a_leased_symbol_counts_against_the_cap(tmp_path, monkeypatch):
    """A lease occupies an EODHD slot exactly as a pin does. Ignoring leases would
    let the widget report free capacity the vendor does not have."""
    client = _client(tmp_path, monkeypatch, cap=2)
    client.post("/subscribe", json={"symbols": ["NVDA"], "ttl": 300})
    client.post("/api/subscriptions", json={"symbol": "AAPL"})
    body = client.get("/api/subscriptions").json()
    assert body["leased"] == ["NVDA"]
    assert body["used"] == 2, "one pinned + one leased"
    assert client.post("/api/subscriptions", json={"symbol": "MSFT"}).status_code == 507


def test_a_symbol_both_pinned_and_leased_counts_once(tmp_path, monkeypatch):
    """THE union rule. EODHD sees a SET of symbols per connection, so the same
    symbol pinned and leased is one subscription. Summing would refuse adds while
    slots were still free."""
    client = _client(tmp_path, monkeypatch, cap=2)
    client.post("/api/subscriptions", json={"symbol": "AAPL"})
    client.post("/subscribe", json={"symbols": ["AAPL"], "ttl": 300})
    body = client.get("/api/subscriptions").json()
    assert body["used"] == 1, "AAPL is pinned AND leased, but is one subscription"
    assert client.post("/api/subscriptions", json={"symbol": "MSFT"}).status_code == 201


def test_removing_a_symbol_unpins_it(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/subscriptions", json={"symbol": "AAPL"})
    assert client.delete("/api/subscriptions/AAPL").status_code == 200
    assert client.get("/api/subscriptions").json()["pinned"] == []


def test_removing_something_not_pinned_is_a_404(tmp_path, monkeypatch):
    assert _client(tmp_path, monkeypatch).delete("/api/subscriptions/AAPL").status_code == 404


def test_a_leased_symbol_cannot_be_removed_through_this_api(tmp_path, monkeypatch):
    """Leases lapse on their own TTL; this widget does not own them."""
    client = _client(tmp_path, monkeypatch)
    client.post("/subscribe", json={"symbols": ["NVDA"], "ttl": 300})
    assert client.delete("/api/subscriptions/NVDA").status_code == 404


def test_the_symbol_path_parameter_is_case_insensitive(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/subscriptions", json={"symbol": "AAPL"})
    assert client.delete("/api/subscriptions/aapl").status_code == 200
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd live-grid && pytest tests/test_subscriptions.py -q`
Expected: FAIL — 404 on every route; they do not exist yet

- [ ] **Step 3: Add `symbols()` to `LeaseRegistry`**

In `live-grid/app/leases.py`, immediately after `__len__`:

```python
    def symbols(self) -> list[str]:
        """The leased symbols, sorted.

        The subscription budget needs these by name, not merely counted: a symbol
        that is both pinned and leased is ONE subscription at the vendor, and only
        the names can tell you that.
        """
        return sorted(self._expiry)
```

- [ ] **Step 4: Add the routes**

In `live-grid/app/main.py`, inside `create_app` beside the existing `leases`
construction, add the watchlist and the cap:

```python
    from app.watchlist import DEFAULT_PATH as WATCHLIST_DEFAULT, Watchlist

    watchlist = Watchlist(os.getenv("LIVE_GRID_WATCHLIST", WATCHLIST_DEFAULT))
    max_symbols = int(os.getenv("LIVE_GRID_MAX_SYMBOLS", "50"))
    app.state.watchlist = watchlist
```

Add this helper just above the routes:

```python
    _GROUP_NAMES = {"us": "Equity", "crypto": "Crypto", "forex": "Forex"}

    def _subscription_payload() -> dict:
        """Pinned, leased, and the budget they share.

        `used` is the size of the UNION. EODHD subscribes a SET of symbols per
        connection, so a symbol that is both pinned and leased is one slot, not
        two -- summing would refuse adds while capacity was still free.
        """
        pinned = watchlist.symbols()
        leased = leases.symbols()
        groups: dict[str, list[str]] = {name: [] for name in _GROUP_NAMES.values()}
        for sym in pinned:
            groups[_GROUP_NAMES[classify(sym)]].append(sym)
        return {
            "service": "EODHD",
            "cap": max_symbols,
            "used": len(set(pinned) | set(leased)),
            "pinned": pinned,
            "leased": leased,
            "groups": groups,
        }
```

and the three routes, after `/snapshot`:

```python
    @app.get("/api/subscriptions")
    async def list_subscriptions():
        return _subscription_payload()

    @app.post("/api/subscriptions", status_code=201)
    async def add_subscription(body: dict):
        sym = str(body.get("symbol") or "").strip().upper()
        if not sym:
            raise HTTPException(status_code=422, detail="symbol must be a non-empty string")
        if sym in set(watchlist.symbols()):
            raise HTTPException(status_code=409, detail=f"{sym} is already subscribed")
        # Union again: adding a symbol that is ALREADY leased costs no new slot.
        projected = set(watchlist.symbols()) | set(leases.symbols()) | {sym}
        if len(projected) > max_symbols:
            raise HTTPException(
                status_code=507,
                detail=f"cap of {max_symbols} reached for EODHD; remove a symbol first",
            )
        watchlist.add(sym)
        _apply_watchlist()
        return _subscription_payload()

    @app.delete("/api/subscriptions/{symbol}")
    async def remove_subscription(symbol: str):
        sym = symbol.strip().upper()
        if not watchlist.remove(sym):
            raise HTTPException(status_code=404, detail=f"{sym} is not subscribed")
        _apply_watchlist()
        return _subscription_payload()
```

`_apply_watchlist` is defined in Task 3. For THIS task, define it as a no-op
placeholder immediately above `_subscription_payload` so the routes are testable
in isolation:

```python
    def _apply_watchlist() -> None:
        """Register the watchlist with the feed manager. Filled in by Task 3."""
```

`main.py` does not import from `app.classify` today — `feeds.py` does
(`from app.classify import FEEDS, split_by_feed`). Add a NEW module-level import
to `main.py`:

```python
from app.classify import classify
```

`os` is already imported at module level (line 10); do not add it again.

- [ ] **Step 5: Run the tests and verify they pass**

Run: `cd live-grid && pytest tests/test_subscriptions.py -q`
Expected: PASS (13 tests)

- [ ] **Step 6: Run the whole live-grid suite**

Run: `cd live-grid && PYTHONFAULTHANDLER=1 pytest -q --capture=sys`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add live-grid/app/leases.py live-grid/app/main.py live-grid/tests/test_subscriptions.py
git commit -m "feat(live-grid): subscription API with a union-counted EODHD budget"
```

---

### Task 3: Feed the pinned symbols

Until now the watchlist is a list nobody acts on. This registers it with
`FeedManager` so pinned symbols are actually subscribed — at startup and after
every change — using the same synthetic-id trick the leases use, so no feed logic
changes.

**Files:**
- Modify: `live-grid/app/main.py` (replace the `_apply_watchlist` placeholder; call it in `lifespan`)
- Test: `live-grid/tests/test_subscriptions.py`

**Interfaces:**
- Consumes: `Watchlist.symbols()` (Task 1); `_subscription_payload` and the routes (Task 2); `FeedManager.register(conn_id, symbols)` and `.unregister(conn_id)`, both existing and unchanged.
- Produces: no new public names. Pinned symbols appear in `manager._union(feed)`.

- [ ] **Step 1: Write the failing tests**

Append to `live-grid/tests/test_subscriptions.py`:

```python
def test_a_pinned_symbol_reaches_the_feed(tmp_path, monkeypatch):
    """The point of pinning: the feed must actually want the symbol."""
    client = _client(tmp_path, monkeypatch)
    client.post("/api/subscriptions", json={"symbol": "AAPL"})
    assert "AAPL" in client.app.state.manager._union("us")


def test_unpinning_removes_it_from_the_feed(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/subscriptions", json={"symbol": "AAPL"})
    client.delete("/api/subscriptions/AAPL")
    assert "AAPL" not in client.app.state.manager._union("us")


def test_symbols_already_on_disk_are_fed_at_startup(tmp_path, monkeypatch):
    """A restart must restore the subscriptions, not just the list."""
    import json

    p = tmp_path / "w.json"
    p.write_text(json.dumps(["AAPL", "EURUSD"]))
    monkeypatch.setenv("LIVE_GRID_WATCHLIST", str(p))
    monkeypatch.setenv("LIVE_GRID_MAX_SYMBOLS", "50")
    client = make_client()
    with client:  # entering the context runs lifespan
        manager = client.app.state.manager
        assert "AAPL" in manager._union("us")
        assert "EURUSD" in manager._union("forex")


def test_pinning_does_not_disturb_a_lease_on_another_symbol(tmp_path, monkeypatch):
    """Watchlist and leases are separate _conns entries; _union merges them."""
    client = _client(tmp_path, monkeypatch)
    client.post("/subscribe", json={"symbols": ["NVDA"], "ttl": 300})
    client.post("/api/subscriptions", json={"symbol": "AAPL"})
    union = client.app.state.manager._union("us")
    assert {"AAPL", "NVDA"} <= union
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd live-grid && pytest tests/test_subscriptions.py -q -k "feed or startup or disturb"`
Expected: FAIL — `_apply_watchlist` is still a no-op, so nothing reaches `_union`

- [ ] **Step 3: Implement `_apply_watchlist`**

Replace the placeholder in `live-grid/app/main.py`:

```python
    def _apply_watchlist() -> None:
        """Register every pinned symbol with the feed manager.

        One synthetic id per symbol -- `watchlist:<SYMBOL>` -- mirroring the lease
        registry, because `_sync_feeds` unions symbols across every `_conns` entry
        and does not care which of them came from a websocket. Unregistering the
        symbols that are no longer pinned is what makes a removal take effect.
        """
        wanted = set(watchlist.symbols())
        current = {
            conn_id[len("watchlist:"):]
            for conn_id in list(manager._conns)
            if conn_id.startswith("watchlist:")
        }
        for sym in wanted - current:
            manager.register(f"watchlist:{sym}", [sym])
        for sym in current - wanted:
            manager.unregister(f"watchlist:{sym}")
```

- [ ] **Step 4: Call it at startup**

In `lifespan` in `live-grid/app/main.py`, before the `manager.run()` task is
created:

```python
        # Restore the pinned subscriptions before the drain loop starts, so the
        # first _sync_feeds already has them and a restart does not drop the feed.
        _apply_watchlist()
```

- [ ] **Step 5: Run the tests and verify they pass**

Run: `cd live-grid && pytest tests/test_subscriptions.py -q`
Expected: PASS (17 tests)

- [ ] **Step 6: Run the whole live-grid suite**

Run: `cd live-grid && PYTHONFAULTHANDLER=1 pytest -q --capture=sys`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add live-grid/app/main.py live-grid/tests/test_subscriptions.py
git commit -m "feat(live-grid): feed the pinned watchlist symbols"
```

---

### Task 4: The page, the widget entry, the mount and the docs

The visible half. An `iframe` widget whose endpoint is an HTML page live-grid
serves itself, so the front end needs no changes: `WidgetCard.tsx` already
frames a backend iframe widget's endpoint directly.

**Files:**
- Create: `live-grid/app/static/subscriptions.html`
- Modify: `live-grid/app/main.py` (the page route)
- Modify: `live-grid/widgets.json`
- Modify: `docker-compose.yml` (live-grid's writable mount)
- Modify: `live-grid/README.md`
- Test: `live-grid/tests/test_subscriptions.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: `GET /subscriptions` -> the HTML page.

- [ ] **Step 1: Write the failing tests**

Append to `live-grid/tests/test_subscriptions.py`:

```python
def test_the_page_is_served_as_html(tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch).get("/subscriptions")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<html" in r.text.lower()


def test_the_page_is_self_contained_with_no_external_requests(tmp_path, monkeypatch):
    """It renders inside an iframe in the desktop app, which may be offline. A CDN
    script or webfont would leave the widget blank rather than degraded."""
    body = _client(tmp_path, monkeypatch).get("/subscriptions").text
    for bad in ("http://", "https://", "//cdn", "src=\"//"):
        assert bad not in body, f"page reaches outside for {bad!r}"


def test_the_widget_is_declared_as_an_iframe_pointing_at_the_page(tmp_path, monkeypatch):
    """A backend iframe widget's endpoint IS the URL the front end frames."""
    widgets = _client(tmp_path, monkeypatch).get("/widgets.json").json()
    w = widgets["subscriptions"]
    assert w["type"] == "iframe"
    assert w["endpoint"] == "subscriptions"
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd live-grid && pytest tests/test_subscriptions.py -q -k "page or widget_is_declared"`
Expected: FAIL — 404 for the page, `KeyError: 'subscriptions'` for the widget

- [ ] **Step 3: Create the page**

Create `live-grid/app/static/subscriptions.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Subscriptions</title>
<style>
  :root { color-scheme: light dark; --line: #8883; --dim: #8888; --bad: #c33; }
  body { font: 13px/1.5 system-ui, sans-serif; margin: 0; padding: 12px; }
  header { display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px; }
  h1 { font-size: 14px; margin: 0; font-weight: 600; }
  .bar { flex: 1; height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; }
  .bar > i { display: block; height: 100%; background: currentColor; }
  .count { font-variant-numeric: tabular-nums; }
  form { display: flex; gap: 6px; margin-bottom: 12px; }
  input { flex: 1; padding: 5px 7px; border: 1px solid var(--line); border-radius: 4px;
          background: transparent; color: inherit; font: inherit; }
  button { padding: 5px 10px; border: 1px solid var(--line); border-radius: 4px;
           background: transparent; color: inherit; font: inherit; cursor: pointer; }
  h2 { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
       color: var(--dim); margin: 14px 0 4px; }
  ul { list-style: none; margin: 0; padding: 0; }
  li { display: flex; align-items: center; gap: 8px; padding: 3px 0;
       border-bottom: 1px solid var(--line); }
  li span { flex: 1; font-variant-numeric: tabular-nums; }
  li.leased { color: var(--dim); }
  li.leased em { font-style: normal; font-size: 11px; }
  .err { color: var(--bad); min-height: 1.4em; margin-bottom: 6px; }
  .empty { color: var(--dim); padding: 3px 0; }
</style>
</head>
<body>
<header>
  <h1>EODHD</h1>
  <div class="bar"><i id="fill" style="width:0"></i></div>
  <span class="count" id="count">–</span>
</header>
<p class="err" id="err"></p>
<form id="add">
  <input id="sym" placeholder="Add a symbol (AAPL, BTC-USD, EURUSD)" autocomplete="off">
  <button type="submit">Add</button>
</form>
<div id="groups"></div>
<script>
const $ = (id) => document.getElementById(id);

function render(d) {
  $("count").textContent = `${d.used} / ${d.cap}`;
  $("fill").style.width = `${Math.min(100, (d.used / d.cap) * 100)}%`;
  const leased = new Set(d.leased);
  $("groups").innerHTML = "";
  for (const [name, syms] of Object.entries(d.groups)) {
    const h = document.createElement("h2");
    h.textContent = name;
    $("groups").append(h);
    const ul = document.createElement("ul");
    if (!syms.length) {
      const p = document.createElement("li");
      p.className = "empty";
      p.textContent = "none";
      ul.append(p);
    }
    for (const s of syms) {
      const li = document.createElement("li");
      const label = document.createElement("span");
      label.textContent = s;
      li.append(label);
      const b = document.createElement("button");
      b.textContent = "Remove";
      b.onclick = () => call("DELETE", `/api/subscriptions/${encodeURIComponent(s)}`);
      li.append(b);
      ul.append(li);
    }
    $("groups").append(ul);
  }
  // Leases are shown so the budget adds up, but they are not ours to remove --
  // they lapse on their own TTL.
  const held = d.leased.filter((s) => !d.pinned.includes(s));
  if (held.length) {
    const h = document.createElement("h2");
    h.textContent = "Leased by quotes";
    $("groups").append(h);
    const ul = document.createElement("ul");
    for (const s of held) {
      const li = document.createElement("li");
      li.className = "leased";
      li.innerHTML = `<span></span><em>expires on its own</em>`;
      li.querySelector("span").textContent = s;
      ul.append(li);
    }
    $("groups").append(ul);
  }
}

async function call(method, url, body) {
  $("err").textContent = "";
  try {
    const r = await fetch(url, {
      method,
      headers: body ? { "content-type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const d = await r.json();
    if (!r.ok) { $("err").textContent = d.detail || `HTTP ${r.status}`; return; }
    render(d);
  } catch (e) {
    $("err").textContent = String(e);
  }
}

$("add").onsubmit = (e) => {
  e.preventDefault();
  const v = $("sym").value.trim();
  if (!v) return;
  $("sym").value = "";
  call("POST", "/api/subscriptions", { symbol: v });
};

call("GET", "/api/subscriptions");
setInterval(() => call("GET", "/api/subscriptions"), 15000);
</script>
</body>
</html>
```

- [ ] **Step 4: Add the page route**

In `live-grid/app/main.py`, beside the existing `/demo` route:

```python
    @app.get("/subscriptions", response_class=HTMLResponse)
    async def subscriptions_page():
        return HTMLResponse((_STATIC / "subscriptions.html").read_text())
```

- [ ] **Step 5: Declare the widget**

Add to `live-grid/widgets.json`:

```json
  "subscriptions": {
    "name": "Subscriptions",
    "description": "Manage the EODHD live subscription list: add and remove symbols, grouped by feed, against the 50-symbol budget. Symbols leased by kdb quotes are shown but expire on their own.",
    "category": "Live",
    "type": "iframe",
    "endpoint": "subscriptions",
    "gridData": { "w": 20, "h": 14 }
  }
```

- [ ] **Step 6: Give live-grid a writable mount**

In `docker-compose.yml`, add to live-grid's `volumes` (it currently has only the
read-only kdb licence):

```yaml
      # The subscription watchlist lives here. live-grid otherwise has no
      # writable mount, so without this a restart would silently empty the list.
      - ./live-grid-data:/data
```

- [ ] **Step 7: Run the tests and verify they pass**

Run: `cd live-grid && pytest tests/test_subscriptions.py -q`
Expected: PASS (20 tests)

- [ ] **Step 8: Run every suite**

Run: `cd live-grid && PYTHONFAULTHANDLER=1 pytest -q --capture=sys`
Expected: all pass

- [ ] **Step 9: Document it**

Add to `live-grid/README.md`:

```markdown
### Subscriptions widget

`GET /subscriptions` serves a page, declared in `widgets.json` as an `iframe`
widget, that manages the EODHD live subscription list. Its API:

    GET    /api/subscriptions              current state
    POST   /api/subscriptions              {"symbol": "AAPL"} -> 201
    DELETE /api/subscriptions/{symbol}     -> 200

Pinned symbols persist in `LIVE_GRID_WATCHLIST` (default `/data/watchlist.json`,
which needs the `./live-grid-data:/data` mount) and are re-registered with the
feed manager at startup, so a restart restores the subscriptions rather than just
the list.

`LIVE_GRID_MAX_SYMBOLS` (default 50) caps the account-wide EODHD budget. The
count is the **union** of pinned and leased symbols: a symbol that is both is one
subscription at the vendor, and quote leases occupy the same allowance. An add
that would exceed the cap returns 507. Leased symbols appear in the widget but
cannot be removed there — they lapse on their own TTL.

Tailnet-only, like every route here. Never funnel it.
```

- [ ] **Step 10: Commit**

```bash
git add live-grid/app/static/subscriptions.html live-grid/app/main.py \
        live-grid/widgets.json docker-compose.yml live-grid/README.md \
        live-grid/tests/test_subscriptions.py
git commit -m "feat(live-grid): subscription manager widget"
```

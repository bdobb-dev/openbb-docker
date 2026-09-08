# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Server-layer tests: widgets.json contract, REST seeding, health, key gate.
The SDK is never imported — the seed client is injected."""
import threading
import time

from fastapi.testclient import TestClient

from app.main import create_app


class FakeRest:
    def __init__(self, snaps=None):
        self.snaps = snaps or {}
        self.calls = []

    def get_live_stock_prices(self, ticker):
        self.calls.append(ticker)
        return self.snaps.get(ticker, {"close": "100", "previousClose": "90", "volume": "5"})


class _FakeSnapshotStore:
    """Stands in for KdbStore: no cached snapshot, ever. Keeps quotes.seed()'s
    kdb-first lookup off the real spawn/loopback/host chain -- otherwise a
    request through /live_grid or the websocket's baseline seed pays a
    genuine `connect()` to 127.0.0.1:5000."""

    def read_snapshot(self, symbol, max_age):
        return None

    def write_snapshot(self, symbol, payload):
        pass


def make_client(monkeypatch=None, **kw):
    """`monkeypatch` is optional: pass it (a pytest fixture) for any test that
    exercises REST seeding (`/live_grid`, the websocket's baseline seed) so
    quotes.seed() never reaches a real kdb+ session. Tests that only touch
    `/widgets.json` or `/health` don't need it -- see
    test_health_includes_ticks_key_when_chart_is_enabled_by_default, which
    deliberately keeps the real (never-connected) KdbSession to prove
    `.endpoint` reports None rather than raising."""
    if monkeypatch is not None:
        monkeypatch.setattr("kdb_store.config.resolve_config", lambda: object())
        monkeypatch.setattr("kdb_store.session.KdbSession", lambda config: object())
        monkeypatch.setattr("kdb_store.store.KdbStore", lambda session: _FakeSnapshotStore())
    app = create_app(api_key=kw.pop("api_key", "test-key"),
                     seed_client=kw.pop("seed_client", FakeRest()),
                     client_factory=kw.pop("client_factory", lambda *a, **k: _NullWs()))
    return TestClient(app)


class _NullWs:
    running = False

    def __init__(self):
        # Per instance, deliberately. As a class attribute this list is shared
        # by every client every test builds, so one test's tick is drained by
        # another test's manager.
        self.data_list: list = []

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


def test_widgets_json_declares_the_live_grid_contract():
    body = make_client().get("/widgets.json").json()
    w = body["live_grid"]
    assert w["type"] == "live_grid"
    assert w["wsEndpoint"] == "live_grid_ws"
    assert w["data"]["wsRowIdColumn"] == "symbol"
    fields = [c["field"] for c in w["data"]["table"]["columnsDefs"]]
    assert fields[0] == "symbol"
    # snapshot-only column must never update over the socket
    vol = next(c for c in w["data"]["table"]["columnsDefs"] if c["field"] == "volume")
    assert vol["enableCellChangeWs"] is False


def test_widgets_json_declares_the_live_chart_contract():
    body = make_client().get("/widgets.json").json()
    w = body["live_chart"]
    assert w["type"] == "live_chart"
    assert w["endpoint"] == "series"
    assert w["wsEndpoint"] == "live_grid_ws"
    param_names = [p["paramName"] for p in w["params"]]
    assert param_names == ["symbol", "interval"]
    interval = next(p for p in w["params"] if p["paramName"] == "interval")
    assert [o["value"] for o in interval["options"]] == [
        "1s", "1m", "5m", "15m", "30m", "1h", "1d",
    ]


def test_live_grid_seeds_rows_in_request_order(monkeypatch):
    rest = FakeRest({"AAPL.US": {"close": "150", "previousClose": "100", "volume": "7"}})
    rows = make_client(monkeypatch, seed_client=rest).get("/live_grid?symbol=AAPL,BTC-USD").json()
    assert [r["symbol"] for r in rows] == ["AAPL", "BTC-USD"]
    assert rows[0]["price"] == 150.0
    assert rows[0]["change"] == 50.0
    assert rest.calls == ["AAPL.US", "BTC-USD.CC"]


def test_live_grid_with_no_symbols_is_an_empty_list():
    assert make_client().get("/live_grid").json() == []


def test_missing_api_key_is_a_clear_500_not_a_stacktrace():
    app = create_app(api_key=None, seed_client=None,
                     client_factory=lambda *a, **k: _NullWs())
    resp = TestClient(app).get("/live_grid?symbol=AAPL")
    assert resp.status_code == 500
    assert "EODHD_API_KEY" in resp.json()["detail"]


def test_health_reports_per_feed_state():
    body = make_client().get("/health").json()
    assert body["status"] == "ok"
    assert set(body["feeds"]) == {"us", "crypto", "forex"}


def test_health_omits_ticks_key_when_chart_is_disabled(monkeypatch):
    monkeypatch.setenv("LIVE_GRID_CHART", "false")
    body = make_client().get("/health").json()
    assert "ticks" not in body


def test_health_includes_ticks_key_when_chart_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("LIVE_GRID_CHART", raising=False)
    body = make_client().get("/health").json()
    assert set(body["ticks"]) == {"buffered", "written", "dropped", "endpoint"}
    # A session that has never connected must report None, not raise.
    assert body["ticks"]["endpoint"] is None


def _receive_json(ws, timeout=10.0):
    """`ws.receive_json()` with a deadline.

    starlette's TestClient websocket offers no timeout, so a message that never
    arrives blocks forever. That is not a hypothetical: this test wedged CI for
    ten minutes and was killed by the job's hang guard, and because the wait was
    unbounded the failure carried no message -- only a faulthandler dump.
    A bounded wait turns the same fault into a one-line assertion.

    The reader runs on a daemon thread so a stuck receive cannot keep the
    interpreter alive after the test fails.
    """
    box: dict = {}

    def grab():
        try:
            box["msg"] = ws.receive_json()
        except BaseException as exc:  # noqa: BLE001
            box["err"] = exc

    t = threading.Thread(target=grab, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise AssertionError(f"no websocket message within {timeout}s")
    if "err" in box:
        raise box["err"]
    return box["msg"]


def test_websocket_registers_params_and_streams_dirty_rows(monkeypatch):
    # `with` on the TestClient, not only on the websocket. manager.run() -- the
    # drain loop that marks rows dirty -- is started by the app's lifespan, and
    # starlette runs lifespan only when the client is a context manager. Without
    # it the loop never runs, no feed client is built, and no tick can reach the
    # producer. That is why the previous version of this test poked
    # manager._dirty by hand, and poking it is what made the test wedge CI.
    with make_client(monkeypatch) as client, \
            client.websocket_connect("/live_grid_ws") as ws:
        ws.send_json({"params": {"symbol": "AAPL"}})
        manager = client.app.state.manager
        # Deliver the tick the way the SDK does -- append to the feed client's
        # buffer -- rather than marking manager._dirty from this thread.
        #
        # pop_dirty() snapshots the dirty set and then clears it, as two
        # statements. Both run on the event loop, so production never interleaves
        # them. A mark added from THIS thread, though, can land between the
        # snapshot and the clear: absent from the snapshot, then erased. The row
        # never flushed, receive_json() blocked forever, and CI's hang guard
        # killed the job ten minutes later with no message.
        #
        # Appending to data_list is the supported cross-thread operation -- the
        # SDK thread appends at the tail, the drain loop deletes from the head --
        # and _drain_all() then marks _dirty on the event loop, where it belongs.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if manager._conns and manager._clients.get("us") is not None:
                break
            time.sleep(0.02)
        assert any("AAPL" in by_feed["us"] for by_feed in manager._conns.values())
        feed_client = manager._clients.get("us")
        assert feed_client is not None, "us feed client was never created"
        feed_client.data_list.append({"s": "AAPL", "p": 151.0})
        # The registration's baseline seed may flush AAPL at its snapshot price
        # (100.0) first; the tick above follows it, so consume until it arrives.
        row = None
        for _ in range(10):
            row = _receive_json(ws)
            assert row["symbol"] == "AAPL"
            if row["price"] == 151.0:
                break
        assert row is not None and row["price"] == 151.0

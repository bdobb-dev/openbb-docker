"""Symbol-keyed TTL leases: a subscription that outlives the request."""

from datetime import datetime

from app.leases import LeaseRegistry


class FakeManager:
    def __init__(self):
        self.registered = {}
        self.register_calls = []
        self.unregistered = []

    def register(self, conn_id, symbols):
        self.register_calls.append(conn_id)
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
    assert m.register_calls == ["lease:AAPL"]


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


def test_subscribe_route_rejects_a_non_list_symbols_value():
    """A bare string is iterable -- without a type check {"symbols":"AAPL"}
    silently leases "A", "P", "L" instead of 422ing."""
    from tests.test_main import make_client

    client = make_client()
    resp = client.post("/subscribe", json={"symbols": "AAPL"})
    assert resp.status_code == 422
    manager = client.app.state.manager
    assert "A" not in manager._union("us")


def test_subscribe_route_rejects_a_non_numeric_ttl():
    from tests.test_main import make_client

    client = make_client()
    assert client.post("/subscribe",
                        json={"symbols": ["AAPL"], "ttl": "soon"}).status_code == 422


def test_subscribe_route_puts_the_symbol_into_the_feed_union():
    """The point of the lease: the feed must actually want the symbol."""
    from tests.test_main import make_client

    client = make_client()
    client.post("/subscribe", json={"symbols": ["AAPL"]})
    manager = client.app.state.manager
    assert "AAPL" in manager._union("us")


def test_health_reports_the_active_lease_count():
    from tests.test_main import make_client

    client = make_client()
    assert client.get("/health").json()["leases"] == 0
    client.post("/subscribe", json={"symbols": ["AAPL", "MSFT"]})
    assert client.get("/health").json()["leases"] == 2


def test_snapshot_route_returns_a_delayed_flagged_price():
    from tests.test_main import make_client

    client = make_client()
    body = client.get("/snapshot", params={"symbol": "AAPL"}).json()
    assert body["symbol"] == "AAPL"
    assert body["delayed"] is True, "a REST snapshot is delayed and must say so"
    assert isinstance(body["price"], float)


def test_snapshot_route_returns_a_non_null_prev_close():
    """seed() stashes the vendor's previous close in quotes._prev_close, not
    on the row -- the route must read it from there. FakeRest's default
    payload carries close=100, previousClose=90."""
    from tests.test_main import make_client

    client = make_client()
    body = client.get("/snapshot", params={"symbol": "AAPL"}).json()
    assert body["price"] == 100.0
    assert body["prev_close"] == 90.0
    assert round(body["price"] - body["prev_close"], 2) == 10.0


def test_snapshot_route_404s_when_the_vendor_gives_nothing():
    """A missing snapshot is not something the caller should crash on; the
    fetcher reads 404 as 'no fallback available' and returns no rows."""
    from tests.test_main import make_client

    class Empty:
        def get_live_stock_prices(self, ticker):
            return None

    client = make_client(seed_client=Empty())
    assert client.get("/snapshot", params={"symbol": "ZZZZ"}).status_code == 404


def test_snapshot_route_uses_the_real_client_when_none_was_injected(monkeypatch):
    """Production cold-starts `create_app()` with `seed_client=None`; the
    route must call `_seed_client()` to lazily build the real REST client
    (as /live_grid and the websocket baseline seed already do), not read the
    closure variable directly -- that variant stays `None` forever and every
    quote 404s on `None.get_live_stock_prices(...)`."""
    from tests.test_main import make_client

    class FakeRest:
        def get_live_stock_prices(self, ticker):
            return {"close": "100", "previousClose": "90"}

    monkeypatch.setattr("app.main._rest_client", lambda key: FakeRest())
    client = make_client(seed_client=None)
    resp = client.get("/snapshot", params={"symbol": "AAPL"})
    assert resp.status_code == 200
    assert resp.json()["price"] == 100.0


def test_subscribe_rejects_a_non_finite_ttl():
    """`float("nan")` and `float("inf")` both parse, so the numeric check alone
    is not enough. A NaN expiry never satisfies `exp <= now`, so the sweeper can
    never reclaim that lease -- it pins an EODHD subscription for the life of
    the process. An infinite one does the same."""
    from tests.test_main import make_client

    client = make_client()
    # String forms only. A bare float("nan") cannot be sent at all -- json's
    # encoder rejects it ("Out of range float values are not JSON compliant"),
    # so no compliant client can reach the route that way. `{"ttl": "nan"}` is
    # perfectly valid JSON and float() accepts it, which is the real vector.
    for bad in ("nan", "inf", "-inf", "Infinity"):
        r = client.post("/subscribe", json={"symbols": ["AAPL"], "ttl": bad})
        assert r.status_code == 422, f"ttl={bad!r} was accepted"


def test_subscribe_rejects_a_non_positive_ttl():
    """An already-expired lease cannot be what the caller meant."""
    from tests.test_main import make_client

    client = make_client()
    for bad in (0, -1, -300.5):
        r = client.post("/subscribe", json={"symbols": ["AAPL"], "ttl": bad})
        assert r.status_code == 422, f"ttl={bad!r} was accepted"


def test_subscribe_still_accepts_a_normal_ttl():
    """The guard must not have made the happy path stricter."""
    from tests.test_main import make_client

    client = make_client()
    r = client.post("/subscribe", json={"symbols": ["AAPL"], "ttl": 60})
    assert r.status_code == 200
    datetime.fromisoformat(r.json()["leases"]["AAPL"])

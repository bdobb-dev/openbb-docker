"""The subscription API: grouping, and a cap that counts the vendor's view."""

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


def test_the_widget_is_declared_as_an_iframe_pointing_at_an_absolute_url(tmp_path, monkeypatch):
    """The desktop client passes an iframe widget's endpoint straight to
    `new URL(raw)` with no base -- a relative path is refused, so the served
    spec must carry the full public URL, not the bare route name in the file."""
    monkeypatch.setenv("LIVE_GRID_PUBLIC_URL", "https://openbb.example.ts.net:6903")
    widgets = _client(tmp_path, monkeypatch).get("/widgets.json").json()
    w = widgets["subscriptions"]
    assert w["type"] == "iframe"
    assert w["endpoint"] == "https://openbb.example.ts.net:6903/subscriptions"


def test_a_trailing_slash_on_the_public_url_does_not_double_up(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_GRID_PUBLIC_URL", "https://openbb.example.ts.net:6903/")
    widgets = _client(tmp_path, monkeypatch).get("/widgets.json").json()
    assert widgets["subscriptions"]["endpoint"] == "https://openbb.example.ts.net:6903/subscriptions"


def test_the_widget_is_omitted_when_no_public_url_is_configured(tmp_path, monkeypatch):
    """Advertising a widget that will always render an error is worse than not
    advertising it -- live_grid and everything else must be untouched."""
    monkeypatch.delenv("LIVE_GRID_PUBLIC_URL", raising=False)
    widgets = _client(tmp_path, monkeypatch).get("/widgets.json").json()
    assert "subscriptions" not in widgets
    assert "live_grid" in widgets


def test_a_websocket_registered_symbol_counts_toward_used(tmp_path, monkeypatch):
    """The budget must reflect the vendor's real view: manager._conns holds
    pins, leases AND /live_grid_ws registrations, and all three are subscribed
    at EODHD. Reading only pinned|leased understates `used`."""
    client = _client(tmp_path, monkeypatch)
    client.app.state.manager.register("ws-test", ["NVDA"])
    body = client.get("/api/subscriptions").json()
    assert body["used"] == 1
    assert body["pinned"] == []
    assert body["leased"] == []


def test_a_websocket_registered_symbol_counts_against_the_cap(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, cap=1)
    client.app.state.manager.register("ws-test", ["NVDA"])
    r = client.post("/api/subscriptions", json={"symbol": "AAPL"})
    assert r.status_code == 507


def test_a_watchlist_over_the_cap_is_truncated_at_startup(tmp_path, monkeypatch):
    """The file is hand-editable; the cap must still hold even for symbols
    that were never added through the API."""
    import json

    p = tmp_path / "w.json"
    p.write_text(json.dumps(["AAPL", "MSFT", "TSLA", "NVDA", "AMZN"]))
    monkeypatch.setenv("LIVE_GRID_WATCHLIST", str(p))
    monkeypatch.setenv("LIVE_GRID_MAX_SYMBOLS", "2")
    client = make_client()
    with client:  # entering the context runs lifespan
        manager = client.app.state.manager
        registered = {
            conn_id[len("watchlist:"):]
            for conn_id in manager._conns
            if conn_id.startswith("watchlist:")
        }
        assert len(registered) == 2
        assert registered == {"AAPL", "AMZN"}, "the first 2 sorted, not just the first 2 in the file"


def test_add_with_no_origin_header_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_GRID_PUBLIC_URL", "https://openbb.example.ts.net:6903")
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/subscriptions", json={"symbol": "AAPL"}).status_code == 201


def test_add_with_a_matching_origin_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_GRID_PUBLIC_URL", "https://openbb.example.ts.net:6903")
    client = _client(tmp_path, monkeypatch)
    r = client.post(
        "/api/subscriptions",
        json={"symbol": "AAPL"},
        headers={"Origin": "https://openbb.example.ts.net:6903"},
    )
    assert r.status_code == 201


def test_add_with_a_foreign_origin_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_GRID_PUBLIC_URL", "https://openbb.example.ts.net:6903")
    client = _client(tmp_path, monkeypatch)
    r = client.post(
        "/api/subscriptions",
        json={"symbol": "AAPL"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_delete_with_a_foreign_origin_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_GRID_PUBLIC_URL", "https://openbb.example.ts.net:6903")
    client = _client(tmp_path, monkeypatch)
    client.post("/api/subscriptions", json={"symbol": "AAPL"})
    r = client.delete(
        "/api/subscriptions/AAPL", headers={"Origin": "https://evil.example.com"}
    )
    assert r.status_code == 403
    assert client.get("/api/subscriptions").json()["pinned"] == ["AAPL"]


def test_get_subscriptions_ignores_origin(tmp_path, monkeypatch):
    """The Origin check is only on the two mutating routes."""
    monkeypatch.setenv("LIVE_GRID_PUBLIC_URL", "https://openbb.example.ts.net:6903")
    client = _client(tmp_path, monkeypatch)
    r = client.get("/api/subscriptions", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 200

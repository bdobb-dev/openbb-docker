# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Auth over the real app, including the two surfaces easiest to miss."""

import base64

import pytest

from tests.test_main import make_client


def _hdr(user="alice", pw="s3cret"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "true")
    monkeypatch.setenv("OPENBB_API_USERNAME", "alice")
    monkeypatch.setenv("OPENBB_API_PASSWORD", "s3cret")
    return make_client()


def test_a_rest_route_401s_without_credentials(guarded):
    assert guarded.get("/widgets.json").status_code == 401


def test_the_401_carries_a_www_authenticate_header(guarded):
    """Without it a browser never prompts and a client cannot discover the scheme."""
    assert guarded.get("/widgets.json").headers.get("www-authenticate") == "Basic"


def test_a_rest_route_401s_with_wrong_credentials(guarded):
    assert guarded.get("/widgets.json", headers=_hdr(pw="wrong")).status_code == 401


def test_a_rest_route_succeeds_with_the_right_credentials(guarded):
    assert guarded.get("/widgets.json", headers=_hdr()).status_code == 200


def test_the_subscription_api_is_guarded_too(guarded):
    """It mutates durable state, so it is the one that matters most."""
    assert guarded.get("/api/subscriptions").status_code == 401
    assert guarded.post("/api/subscriptions", json={"symbol": "AAPL"}).status_code == 401


def test_the_websocket_is_refused_without_credentials(guarded):
    """THE trap: Starlette's BaseHTTPMiddleware skips non-http scopes, so an
    HTTP-only guard leaves this wide open while every REST test passes."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with guarded.websocket_connect("/live_grid_ws"):
            pass


def test_the_ta_chart_websocket_is_refused_too(guarded):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with guarded.websocket_connect("/ta_chart_ws?symbol=AAPL&macro=none"):
            pass


def test_the_websocket_connects_with_credentials(guarded):
    """The guard must not break the feature it protects."""
    with guarded.websocket_connect("/live_grid_ws", headers=_hdr()) as ws:
        ws.send_json({"params": {"symbol": "AAPL"}})


def test_a_cors_preflight_succeeds_without_credentials(guarded):
    """A preflight carries no credentials by definition, so this must succeed
    either way. NOTE: this does NOT prove the auth-vs-CORS ordering -- the
    auth middleware forwards real preflights through untouched from any
    position, and CORSMiddleware itself short-circuits real preflights
    without delegating to the wrapped app, so both orderings look identical
    here. See test_an_unauthenticated_cross_origin_request_still_carries_cors_headers
    below for the test that actually distinguishes the orderings."""
    r = guarded.options(
        "/widgets.json",
        headers={
            "Origin": "https://pro.openbb.co",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") is not None


def test_an_unauthenticated_cross_origin_request_still_carries_cors_headers(guarded):
    """The real ordering guard. A preflight cannot detect it: the auth middleware
    passes real preflights through from any position, and CORSMiddleware
    short-circuits them without delegating -- so both orderings look identical
    there.

    A NON-preflight cross-origin request does distinguish them. With auth
    appended (INNERMOST), CORS still wraps the 401 and adds
    Access-Control-Allow-Origin, so a browser sees a usable 401. With auth
    registered via add_middleware (OUTERMOST), the 401 is produced before CORS
    runs and carries no CORS headers, so the browser reports an opaque network
    error and the client cannot tell it needs credentials.
    """
    r = guarded.get("/widgets.json", headers={"Origin": "https://pro.openbb.co"})
    assert r.status_code == 401
    assert r.headers.get("access-control-allow-origin") is not None, (
        "401 lost its CORS headers -- the auth middleware is OUTSIDE CORS"
    )


def test_everything_passes_through_when_auth_is_disabled(monkeypatch):
    """Local development and the existing suite must be unaffected."""
    monkeypatch.delenv("OPENBB_API_AUTH", raising=False)
    client = make_client()
    assert client.get("/widgets.json").status_code == 200
    with client.websocket_connect("/live_grid_ws") as ws:
        ws.send_json({"params": {"symbol": "AAPL"}})


def test_the_subscriptions_page_is_the_one_public_path(guarded):
    """Reversed deliberately (2026-09-04). The original reasoning -- "the
    browser will have credentials, because it authenticated to load the page"
    -- holds for a browser tab and fails for the desktop client, where the
    widget is an IFRAME: a frame navigates itself, cannot attach a header, and
    401s to a blank box before its script runs.

    So the static HTML is public and everything it does is not. The page
    carries no data and no state; it asks its host for credentials over the
    openbb-connect/openbb-auth handshake and sends them on every API call.
    Reaching it at all already requires being on the tailnet, where
    /live_grid_ws serves the same feed to anyone who asks."""
    assert guarded.get("/subscriptions").status_code == 200
    assert guarded.get("/subscriptions", headers=_hdr()).status_code == 200


def test_the_exemption_is_the_page_and_nothing_near_it(guarded):
    """Exact match. The API it calls stays guarded -- that is the whole point
    of exempting only the page -- and a future /subscriptions-admin does not
    inherit anything."""
    assert guarded.get("/api/subscriptions").status_code == 401
    assert guarded.get("/subscriptions-admin").status_code == 401
    assert guarded.get("/subscriptions/anything").status_code == 401


def test_the_page_and_its_api_share_one_origin(guarded):
    """The page fetches /api/subscriptions with a root-relative URL, so the
    browser sends the same credentials it used for the page. If these ever
    diverge in origin, the widget breaks silently for authenticated users."""
    page = guarded.get("/subscriptions", headers=_hdr())
    assert "/api/subscriptions" in page.text
    assert "http://" not in page.text and "https://" not in page.text


def _qs(**kw) -> str:
    """The header's own value, url-encoded -- derived from `_hdr` so the two
    cannot drift apart."""
    import urllib.parse

    return urllib.parse.urlencode(_hdr(**kw))


def test_the_credential_is_accepted_from_the_query(guarded):
    """An iframe issues its own request and cannot attach a header, so the
    query is the only channel it has. Same name, same `Basic <base64>` value."""
    assert guarded.get(f"/widgets.json?{_qs()}").status_code == 200
    assert guarded.get(f"/api/subscriptions?{_qs()}").status_code == 200


def test_a_bad_query_credential_is_still_refused(guarded):
    assert guarded.get("/widgets.json?Authorization=Basic+bm9wZTpub3Bl").status_code == 401
    assert guarded.get("/widgets.json?Authorization=garbage").status_code == 401


def test_a_bad_header_is_not_rescued_by_a_good_query(guarded):
    """A request that supplies a header is judged on it alone -- otherwise a
    stale header could be silently overridden by an appended query string."""
    res = guarded.get(f"/widgets.json?{_qs()}", headers=_hdr(pw="wrong"))
    assert res.status_code == 401


def test_the_websocket_still_takes_the_query_credential(guarded):
    """buildWidgetWsUrl puts it there for the same reason: no headers on a
    browser handshake either."""
    with guarded.websocket_connect(f"/live_grid_ws?{_qs()}") as ws:
        ws.send_json({"params": {"symbol": "AAPL"}})

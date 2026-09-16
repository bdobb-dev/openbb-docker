# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The route through the real OpenBB app, and its absence from widgets.json.

The app loads every installed openbb_core_extension, this one included (the
editable install registers its entry point). No lifespan runs: TestClient is
not used as a context manager, so the CFTC router's startup fetch never fires.
Basic auth is off because OPENBB_API_AUTH is unset in the test container. SEC
is the stand-in from conftest.py."""
from fastapi.testclient import TestClient
from openbb_core.api.rest_api import app
from openbb_platform_api.utils.widgets import build_json

from tests.conftest import APPLE

ROUTE = "/api/v1/fundamentals/fiscal_year_end"
client = TestClient(app)


def test_found_answers_the_month_name(sec_site):
    r = client.get(ROUTE, params={"symbol": "AAPL.US"})
    assert r.status_code == 200
    assert r.json() == {"fiscalYearEnd": "September"}


def test_unknown_ticker_is_404_with_our_detail(sec_site):
    r = client.get(ROUTE, params={"symbol": "ZZZZ"})
    assert r.status_code == 404
    # Our detail, not FastAPI's "Not Found" for an unknown path: the route
    # exists and said no.
    assert r.json() == {"detail": "No fiscal year end for ZZZZ"}


def test_no_user_agent_is_404_without_asking_sec(sec_site, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT")
    r = client.get(ROUTE, params={"symbol": "AAPL"})
    assert r.status_code == 404
    assert r.json() == {"detail": "No fiscal year end for AAPL"}
    assert sec_site.requests == []


def test_upstream_failure_is_502_and_the_next_request_asks_again(sec_site):
    sec_site.pages[APPLE] = 503
    r = client.get(ROUTE, params={"symbol": "AAPL"})
    assert r.status_code == 502
    assert r.json() == {"detail": f"SEC answered 503 for {APPLE}"}
    sec_site.pages[APPLE] = {"fiscalYearEnd": "0926"}
    r = client.get(ROUTE, params={"symbol": "AAPL"})
    assert r.status_code == 200
    assert r.json() == {"fiscalYearEnd": "September"}


def test_route_is_served_but_is_not_a_widget():
    assert ROUTE in app.openapi()["paths"]
    widgets = build_json(app.openapi(), [])
    assert not any(w.get("endpoint") == ROUTE for w in widgets.values())

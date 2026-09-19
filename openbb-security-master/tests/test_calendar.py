# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the /calendar/trading route: security master first, pandas fallback, 404."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from openbb_security_master.calendar import (
    _envelope_from_security_master,
    router,
    trading_calendar,
)

ROW_OPEN = {"calendar_id": "cal_nyse", "exchange_id": "exch_xnys",
            "exchange_name": "NYSE", "mic": "XNYS", "calendar_alias": None,
            "session_date": "2026-06-18", "trade_date": "2026-06-18",
            "timezone": "America/New_York", "session_status": "open",
            "calendar_system": "gregorian", "holiday_family": None,
            "assertion_domain": "market_session", "evidence_status": "market_final",
            "market_open": "2026-06-18T13:30:00Z", "market_close": "2026-06-18T20:00:00Z",
            "break_start": None, "break_end": None, "interruptions": [],
            "holiday_name": None, "special_open": False, "special_close": False,
            "market_effect": "open", "authority_type": "exchange",
            "authority_name": "NYSE", "authority_verified_at": None,
            "source_kind": "exchange_notice", "source_version": "cap_1",
            "rule_id": None, "supersedes_assertion_id": None,
            "effective_from": "2026-01-01T00:00:00Z", "effective_to": None,
            "known_from": "2026-01-01T00:00:00Z", "known_to": None, "capture_id": "cap_1"}
ROW_CLOSED = {**ROW_OPEN, "session_date": "2026-06-19", "trade_date": "2026-06-19",
              "session_status": "closed", "market_open": None, "market_close": None,
              "holiday_name": "Juneteenth National Independence Day",
              "market_effect": "closed"}
ROW_EARLY = {**ROW_OPEN, "session_date": "2026-12-24", "trade_date": "2026-12-24",
             "market_open": "2026-12-24T14:30:00Z", "market_close": "2026-12-24T18:00:00Z",
             "special_close": True, "holiday_name": "Christmas Eve (early close)"}


class _Stub:
    """Records the POST body and answers the canned payload, over real HTTP."""

    def __init__(self, status: int, payload: dict):
        self.calls: list[tuple[str, dict]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append((self.path, body))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def _env_for(stub, monkeypatch):
    monkeypatch.setenv("SECURITY_MASTER_URL", stub.url)
    monkeypatch.setenv("OPENBB_API_USERNAME", "u")
    monkeypatch.setenv("OPENBB_API_PASSWORD", "p")


def test_router_registers_the_plain_get_route():
    routes = [r for r in router.api_router.routes if getattr(r, "methods", None)]
    assert any("GET" in r.methods for r in routes), "the calendar route must be a plain GET"


def test_envelope_from_security_master_projects_the_three_shapes():
    env = _envelope_from_security_master([ROW_OPEN, ROW_CLOSED, ROW_EARLY])
    data = env["data"]
    assert data["Timezone"] == "America/New_York"
    # Modal regular session excludes the special_close row, so Close stays 20:00Z's clock.
    assert data["TradingHours"] == {"Open": "13:30:00", "Close": "20:00:00",
                                    "WorkingDays": "Mon, Tue, Wed, Thu, Fri"}
    assert data["ExchangeHolidays"]["2026-06-19"] == {
        "Holiday": "Juneteenth National Independence Day", "Type": "Official"}
    assert data["ExchangeHolidays"]["2026-12-24"] == {
        "Holiday": "Christmas Eve (early close)", "Type": "EarlyClose", "EarlyClose": "18:00:00"}


def test_route_answers_from_security_master_when_it_has_rows(monkeypatch):
    stub = _Stub(200, {"results": [ROW_OPEN, ROW_CLOSED], "columns": [], "extra": {}})
    _env_for(stub, monkeypatch)
    resp = trading_calendar("XNYS", 2026)
    assert resp.status_code == 200
    body = json.loads(bytes(resp.body))
    assert body["data"]["ExchangeHolidays"]["2026-06-19"]["Type"] == "Official"
    _, posted = stub.calls[0]
    assert posted["parameters"]["include_closed"] is True
    assert posted["parameters"]["mic"] == "XNYS"
    assert posted["parameters"]["start_date"] == "2026-01-01"


def test_route_falls_back_to_exchange_calendars_when_master_is_empty(monkeypatch):
    stub = _Stub(200, {"results": [], "columns": [], "extra": {}})
    _env_for(stub, monkeypatch)
    resp = trading_calendar("XNYS", 2026)
    assert resp.status_code == 200
    body = json.loads(bytes(resp.body))
    data = body["data"]
    assert data["Timezone"] == "America/New_York"
    assert data["TradingHours"]["Open"] == "09:30:00"
    assert data["TradingHours"]["Close"] == "16:00:00"
    holidays = data["ExchangeHolidays"]
    # Official closures the rules know about, including Good Friday —
    # the one the U.S. federal table used by pandas BDay alone would miss.
    assert holidays["2026-01-01"]["Type"] == "Official"
    assert holidays["2026-04-03"]["Type"] == "Official"  # Good Friday
    assert holidays["2026-07-03"]["Type"] == "Official"  # Independence Day, observed
    # Early closes land in exchange-local time: 13:00 New York, not 18:00 UTC.
    assert holidays["2026-11-27"]["Type"] == "EarlyClose"
    assert holidays["2026-11-27"]["EarlyClose"] == "13:00:00"
    assert holidays["2026-12-24"]["EarlyClose"] == "13:00:00"


def test_route_falls_back_when_master_is_unreachable(monkeypatch):
    monkeypatch.setenv("SECURITY_MASTER_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("OPENBB_API_USERNAME", "u")
    monkeypatch.setenv("OPENBB_API_PASSWORD", "p")
    resp = trading_calendar("XNYS", 2026)
    assert resp.status_code == 200
    assert json.loads(bytes(resp.body))["data"]["Timezone"] == "America/New_York"


def test_unknown_exchange_is_the_designed_404(monkeypatch):
    stub = _Stub(200, {"results": [], "columns": [], "extra": {}})
    _env_for(stub, monkeypatch)
    resp = trading_calendar("XXXX", 2026)
    assert resp.status_code == 404


def test_year_outside_the_client_window_is_a_400(monkeypatch):
    stub = _Stub(200, {"results": [], "columns": [], "extra": {}})
    _env_for(stub, monkeypatch)
    assert trading_calendar("XNYS", 1989).status_code == 400
    assert trading_calendar("XNYS", 2041).status_code == 400
    # ...and it never asked the service.
    assert stub.calls == []

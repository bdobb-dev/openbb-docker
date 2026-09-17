# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from openbb_security_master.client import SecurityMasterClient
from openbb_security_master.models import MarketCalendarData, MarketCalendarQueryParams
from openbb_security_master.router import exchange_details, market_calendar, router


class Stub:
    def __init__(self, status=200, body=None):
        self.calls = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                stub.calls.append((self.path, json.loads(self.rfile.read(length) or b"{}")))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


ROW = {"calendar_id": "cal_tadawul", "exchange_id": "exch_xsau", "exchange_name": "Tadawul",
       "mic": "XSAU", "calendar_alias": None, "session_date": "2027-03-11",
       "trade_date": "2027-03-11", "timezone": "Asia/Riyadh", "session_status": "closed",
       "calendar_system": "hijri", "holiday_family": "eid_al_fitr",
       "assertion_domain": "market_session", "evidence_status": "market_final",
       "market_open": None, "market_close": None, "break_start": None, "break_end": None,
       "interruptions": [], "holiday_name": None, "special_open": False, "special_close": False,
       "market_effect": "closed", "authority_type": "exchange", "authority_name": "Tadawul",
       "authority_verified_at": None, "source_kind": "exchange_notice",
       "source_version": "cap_eid_t2", "rule_id": None, "supersedes_assertion_id": None,
       "effective_from": "2027-03-11T00:00:00Z", "effective_to": None,
       "known_from": "2027-03-10T08:00:00Z", "known_to": None, "capture_id": "cap_eid_t2"}
RECEIPT = {"execution_id": "qry_1", "temporal_mode": "known_at", "dependencies": []}


def test_router_registers_three_commands():
    paths = {r.path for r in router.api_router.routes}
    assert {"/market_calendar", "/exchange_details", "/security_master_resolve"} <= paths


def test_market_calendar_delegates_and_keeps_the_receipt_in_extra(monkeypatch):
    stub = Stub(200, {"results": [ROW], "columns": [], "extra": {"security_master_receipt": RECEIPT}})
    monkeypatch.setenv("SECURITY_MASTER_URL", stub.url)
    monkeypatch.setenv("OPENBB_API_USERNAME", "u")
    monkeypatch.setenv("OPENBB_API_PASSWORD", "p")
    params = MarketCalendarQueryParams(calendar_id="cal_tadawul", start_date="2027-03-01",
                                       end_date="2027-03-31", include_closed=True,
                                       knowledge_at="2027-03-10T08:00:00Z")
    obb = market_calendar(**params.model_dump())
    assert isinstance(obb.results[0], MarketCalendarData)
    assert obb.results[0].market_effect == "closed"
    assert obb.extra["security_master_receipt"] == RECEIPT
    path, body = stub.calls[0]
    assert path == "/security-master/v1/odp/models/MarketCalendar/query"
    assert body["context"] == {"mode": "known_at", "known_at": "2027-03-10T08:00:00Z",
                               "effective_at": "2027-03-01T00:00:00Z"}
    assert body["parameters"]["calendar_id"] == "cal_tadawul"


def test_service_error_surfaces_as_openbb_error(monkeypatch):
    stub = Stub(422, {"error": {"code": "TEMPORAL_MODE_UNSUPPORTED", "message": "no",
                                "details": {}, "request_id": "r"}})
    monkeypatch.setenv("SECURITY_MASTER_URL", stub.url)
    from openbb_core.app.model.abstract.error import OpenBBError

    with pytest.raises(OpenBBError, match="TEMPORAL_MODE_UNSUPPORTED"):
        market_calendar(calendar_id="x", start_date="2027-03-01", end_date="2027-03-02")


def test_exchange_details_uses_the_eodhd_client(monkeypatch):
    seen = {}

    def fake_rest_json(client, endpoint, params):
        seen["endpoint"], seen["params"] = endpoint, params
        return {"Code": "US", "ExchangeHolidays": {}}

    monkeypatch.setattr("openbb_security_master.router._eodhd_rest_json", fake_rest_json)
    monkeypatch.setattr("openbb_security_master.router._eodhd_client", lambda: object())
    obb = exchange_details(code="US")
    assert seen["endpoint"] == "exchange-details/US" and obb.results.code == "US"


def test_client_builds_context_from_odp_parameters():
    c = SecurityMasterClient("http://x", "u", "p")
    assert c.context_for(as_of="2027-03-01", knowledge_at=None) == {
        "mode": "effective_on", "effective_at": "2027-03-01T00:00:00Z"}
    assert c.context_for(as_of=None, knowledge_at=None) == {"mode": "current_corrected"}

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The routes through the real OpenBB app, and the widgets.json they produce.

The app loads every installed openbb_core_extension, this one included (the
editable install registers its entry point). No lifespan runs: TestClient is
not used as a context manager, so the CFTC router's startup fetch never fires.
Basic auth is off because OPENBB_API_AUTH is unset in the test container."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from openbb_core.api.rest_api import app
from openbb_platform_api.utils.widgets import build_json

client = TestClient(app)


def test_trading_returns_the_eodhd_v2_document():
    r = client.get("/api/v1/calendar/trading", params={"exchange": "XNYS", "year": 2026})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["Code"] == data["OperatingMIC"] == "XNYS"
    assert data["ExchangeHolidays"]["2026-11-27"]["EarlyClose"] == "13:00:00"
    assert data["ExchangeHolidays"]["2026-07-03"]["Type"] == "Official"


@pytest.mark.parametrize("path", ["/api/v1/calendar/trading", "/api/v1/calendar/trading_table"])
@pytest.mark.parametrize("exchange,year", [("ZZZZ", 2026), ("XNYS", 1989), ("XNYS", 2041)])
def test_missing_calendar_is_404(path, exchange, year):
    r = client.get(path, params={"exchange": exchange, "year": year})
    assert r.status_code == 404
    # Our detail, not FastAPI's "Not Found" for an unknown path: the route
    # exists and said no.
    assert r.json() == {"detail": f"No trading calendar for {exchange} {year}"}


def test_trading_table_serves_rows():
    r = client.get("/api/v1/calendar/trading_table", params={"exchange": "XNYS", "year": 2026})
    assert r.status_code == 200
    rows = r.json()
    assert rows[0] == {
        "date": "2026-01-01",
        "holiday": "New Year's Day",
        "type": "Official",
        "early_close": None,
    }


def test_widgets_json_lists_the_table_and_not_the_data_route():
    widgets = build_json(app.openapi(), [])
    table = widgets["calendar_trading_table_custom_obb"]
    assert table["name"] == "Trading calendar"
    assert table["type"] == "table"
    assert table["endpoint"] == "/api/v1/calendar/trading_table"
    params = {p["paramName"]: p for p in table["params"]}
    assert params["exchange"]["value"] == "XNYS"
    assert [o["value"] for o in params["exchange"]["options"]] == [
        "XNYS", "XLON", "XTSE", "XETR", "XTKS", "XHKG",
    ]
    assert params["year"]["type"] == "number"
    assert params["year"]["value"] == date.today().year
    assert [c["field"] for c in table["data"]["table"]["columnsDefs"]] == [
        "date", "holiday", "type", "early_close",
    ]
    assert not any(w.get("endpoint") == "/api/v1/calendar/trading" for w in widgets.values())

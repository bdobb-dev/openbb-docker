# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The /ta_chart route and macro discovery in /widgets.json."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app


def client():
    return TestClient(create_app(api_key="test-key"))


def test_widgets_json_advertises_the_ta_chart_widget():
    spec = client().get("/widgets.json").json()
    assert spec["ta_chart"]["type"] == "chart"
    assert spec["ta_chart"]["endpoint"] == "ta_chart"


def test_widgets_json_fills_the_macro_dropdown_from_the_macro_directory():
    spec = client().get("/widgets.json").json()
    macro_param = next(p for p in spec["ta_chart"]["params"]
                       if p["paramName"] == "macro")
    values = [o["value"] for o in macro_param["options"]]
    assert "none" in values and "classic-momentum" in values


def test_ta_chart_returns_a_figure_even_with_no_upstream_data():
    response = client().get("/ta_chart", params={"symbol": "AAPL", "macro": "none"})
    assert response.status_code in (200, 502)
    assert "layout" in response.json()


def test_ta_chart_reports_a_bad_macro_in_the_title_not_a_stack_trace():
    response = client().get("/ta_chart", params={"macro": "nope"})
    assert response.status_code == 502
    assert "nope" in response.json()["layout"]["title"]["text"]


def test_the_existing_chart_route_is_untouched():
    assert client().get("/chart", params={"symbol": "AAPL"}).status_code in (200, 502)


def test_health_reports_the_eodhd_call_budget():
    body = client().get("/health").json()
    assert body["ta"]["eodhd_calls"] == 0
    assert body["ta"]["calls_per_indicator"] == 5


def test_health_still_reports_feed_status():
    assert "feeds" in client().get("/health").json()


def test_ta_chart_says_why_it_is_empty_when_bars_are_unavailable():
    response = client().get("/ta_chart", params={"symbol": "AAPL", "macro": "none"})
    assert response.status_code == 200
    assert "bars unavailable" in response.json()["layout"]["title"]["text"]


def test_a_bars_outage_after_rev_0_sends_a_figure_not_a_silently_emptied_delta(monkeypatch):
    """bars_error must force a full figure push -- a delta carries no title,
    so an emptied delta with no explanation is the same silently-blank
    failure /ta_chart was fixed for, one layer down."""
    monkeypatch.setenv("TA_PUSH_INTERVAL_MS", "10")
    calls = {"n": 0}
    bar = {"date": "2024-01-01", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}

    async def fake_build_series(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return [bar], {}
        raise RuntimeError("kdb down")

    monkeypatch.setattr("app.main.build_series", fake_build_series)
    with client().websocket_connect("/ta_chart_ws?symbol=AAPL&macro=none") as ws:
        first = ws.receive_json()
        assert first["type"] == "figure"
        second = ws.receive_json()
        assert second["type"] == "figure"
        assert "bars unavailable" in second["figure"]["layout"]["title"]["text"]


def test_a_degradation_appearing_mid_stream_forces_a_figure_not_a_delta(monkeypatch):
    """Annotations live only in the figure's title, and a delta carries no
    title. Without watching them, an EODHD fetch that starts failing swaps the
    series to local values under a title, frozen at rev 0, still reading eodhd.
    """
    from app import main as main_module
    from app.ta.sources import EodhdSource

    def bar(day):
        return {"date": f"2024-01-{day:02d}", "open": 1.0, "high": 2.0,
                "low": 0.5, "close": 1.5, "adjusted_close": 1.5, "volume": 10}

    pushes = []

    async def fake_series(*args, **kwargs):
        # A new closed bar each push: that is what re-opens the EODHD cache key.
        pushes.append(1)
        return [bar(d) for d in range(2, 6 + len(pushes))], None

    fetches = []

    async def flaky(self, query):
        fetches.append(query)
        if len(fetches) == 1:
            return [{"date": f"2024-01-{d:02d}", "sma": 1.0} for d in range(2, 8)]
        raise RuntimeError("503 Service Unavailable")

    monkeypatch.setattr(main_module, "build_series", fake_series)
    monkeypatch.setattr(EodhdSource, "_http_fetch", flaky)
    monkeypatch.setenv("TA_PUSH_INTERVAL_MS", "0")

    url = "/ta_chart_ws?symbol=AAPL&source=eodhd&indicators=sma:period=3"
    with client().websocket_connect(url) as ws:
        first, second = ws.receive_json(), ws.receive_json()

    assert first["type"] == "figure"
    assert second["type"] == "figure", "a degradation must not arrive as a delta"
    # The human label, not the internal column name: `sma|period=3` is for
    # the dedup pass, and a chart title is read by people.
    title = second["figure"]["layout"]["title"]["text"]
    assert "SMA(3)" in title
    assert "sma|period=3" not in title


def test_ta_chart_anchor_param_adds_an_anchored_vwap_trace():
    response = client().get("/ta_chart", params={"symbol": "AAPL", "macro": "none",
                                                 "anchor": "2026-08-28T14:30"})
    names = [t.get("name") for t in response.json().get("data", [])]
    assert "Anchored VWAP" in names


# This environment has no reachable OpenBB API (see openbb_client.OPENBB_URL,
# http://127.0.0.1:6900) -- fetch_series always fails with a connection
# error, which is exactly the degradation /ta_chart already has a test for.
# The three tests below need bars to actually arrive, so they stand up their
# own deterministic history the way the /ta_chart_ws tests above already do.
def _fake_series_bars(n: int = 40) -> list[dict]:
    bars = []
    price = 100.0
    start = date(2024, 1, 1)
    for i in range(n):
        price += 1.0 if i % 3 else -1.5
        d = (start + timedelta(days=i)).isoformat()
        bars.append({"date": d, "open": price - 0.5, "high": price + 1.0,
                     "low": price - 1.0, "close": price,
                     "adjusted_close": price, "volume": 1000 + i})
    return bars


async def _fake_build_series(*args, **kwargs):
    return _fake_series_bars(), {}


def test_ta_series_ws_first_frame_is_a_full_series_payload(monkeypatch):
    monkeypatch.setattr("app.main.build_series", _fake_build_series)
    with client().websocket_connect(
        "/ta_series_ws?symbol=AAPL&interval=1d&indicators=rsi:period=14"
    ) as ws:
        first = ws.receive_json()
    assert first["type"] == "series"
    assert first["rev"] == 0
    assert first["candles"], "no candles in the seed frame"
    assert any(p["id"] == "price" for p in first["panes"])


def test_ta_series_ws_second_frame_is_a_delta(monkeypatch):
    monkeypatch.setattr("app.main.build_series", _fake_build_series)
    monkeypatch.setenv("TA_PUSH_INTERVAL_MS", "0")
    with client().websocket_connect(
        "/ta_series_ws?symbol=AAPL&interval=1d&indicators=sma:period=20"
    ) as ws:
        ws.receive_json()
        second = ws.receive_json()
    assert second["type"] == "delta"
    assert second["rev"] == 1
    assert "from" in second


def test_ta_series_ws_and_ta_chart_ws_agree_on_values(monkeypatch):
    """One engine, two presenters. If these ever disagree, a second
    implementation has crept in."""
    monkeypatch.setattr("app.main.build_series", _fake_build_series)
    query = "symbol=AAPL&interval=1d&indicators=rsi:period=14"
    with client().websocket_connect(f"/ta_series_ws?{query}") as ws:
        series = ws.receive_json()
    with client().websocket_connect(f"/ta_chart_ws?{query}") as ws:
        figure = ws.receive_json()
    rsi_pane = next(p for p in series["panes"] if p["id"] == "rsi")
    from_series = [point["value"] for point in rsi_pane["series"][0]["data"]]
    from_figure = next(t["y"] for t in figure["figure"]["data"]
                       if t.get("name", "").startswith("RSI"))
    assert from_series == from_figure


def test_ta_series_ws_basis_raw_makes_an_adjusted_indicator_read_raw_close(monkeypatch):
    """Only the bars_to_frame unit test covered `basis` -- it bypasses the
    route wiring entirely. This environment has no reachable OpenBB API, so a
    route test on empty data would pass while proving nothing; stand up
    deterministic bars, same as the tests above, with adjusted close
    deliberately different from raw close (Fix 9)."""
    def bars():
        out = []
        price = 100.0
        start = date(2024, 1, 1)
        for i in range(10):
            price += 1.0
            d = (start + timedelta(days=i)).isoformat()
            out.append({"date": d, "open": price - 0.5, "high": price + 1.0,
                        "low": price - 1.0, "close": price,
                        "adjusted_close": price * 0.5, "volume": 1000})
        return out

    async def fake(*args, **kwargs):
        return bars(), {}

    monkeypatch.setattr("app.main.build_series", fake)
    query = "symbol=AAPL&interval=1d&indicators=sma:period=3"
    with client().websocket_connect(f"/ta_series_ws?{query}") as ws:
        adjusted = ws.receive_json()
    with client().websocket_connect(f"/ta_series_ws?{query}&basis=raw") as ws:
        raw = ws.receive_json()

    def sma_values(payload):
        pane = next(p for p in payload["panes"] if p["id"] == "price")
        return [point["value"] for point in pane["series"][0]["data"]]

    assert sma_values(adjusted) != sma_values(raw)


def test_ta_series_ws_annotation_change_forces_a_series_not_a_delta(monkeypatch):
    """Marks live only on a `series` push -- a delta carries no `marks` key.
    Without watching them, an EODHD fetch that starts failing mid-stream
    would leave a client showing stale marks indefinitely (the /ta_chart_ws
    equivalent of this is test_a_degradation_appearing_mid_stream_forces_a_
    figure_not_a_delta, reused here: same growing-bars/flaky-fetch trick to
    force an EODHD cache miss on push 2)."""
    from app.ta.sources import EodhdSource

    def bar(day):
        return {"date": f"2024-01-{day:02d}", "open": 1.0, "high": 2.0,
                "low": 0.5, "close": 1.5, "adjusted_close": 1.5, "volume": 10}

    pushes = []

    async def fake_series(*args, **kwargs):
        # A new closed bar each push: that is what re-opens the EODHD cache key.
        pushes.append(1)
        return [bar(d) for d in range(2, 6 + len(pushes))], None

    fetches = []

    async def flaky(self, query):
        fetches.append(query)
        if len(fetches) == 1:
            return [{"date": f"2024-01-{d:02d}", "sma": 1.0} for d in range(2, 8)]
        raise RuntimeError("503 Service Unavailable")

    monkeypatch.setattr("app.main.build_series", fake_series)
    monkeypatch.setattr(EodhdSource, "_http_fetch", flaky)
    monkeypatch.setenv("TA_PUSH_INTERVAL_MS", "0")

    url = "/ta_series_ws?symbol=AAPL&source=eodhd&indicators=sma:period=3"
    with client().websocket_connect(url) as ws:
        first, second = ws.receive_json(), ws.receive_json()

    assert first["type"] == "series"
    assert second["type"] == "series", "an annotation change must not arrive as a delta"
    assert second["marks"], "the failed EODHD fetch should have produced a mark"


def test_ta_series_ws_closes_on_an_unknown_macro():
    with pytest.raises(WebSocketDisconnect):
        with client().websocket_connect("/ta_series_ws?symbol=AAPL&macro=nope") as ws:
            ws.receive_json()

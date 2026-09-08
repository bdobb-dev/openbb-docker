# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Chart routes on live-grid, including the tick/history join."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

D = lambda s: datetime.fromisoformat(s)  # noqa: E731


def bar(stamp, close=1.0):
    return {"date": stamp, "open": close, "high": close, "low": close,
            "close": close, "volume": 1.0}


class _NoSpanStore:
    """Stands in for KdbStore: a recorder is wired up, but reports no tick
    span, so build_series falls straight back to history. Keeps this fixture
    off the real spawn/loopback/host chain -- otherwise every test using it
    pays a genuine `connect()` to 127.0.0.1:5000, and on a host where
    something else (e.g. macOS AirPlay) already owns that port, a real
    connect timeout."""

    def tick_span(self, symbol):
        return None

    def prune_ticks(self, cutoff):
        return 0

    def write_ticks(self, frame):
        return 0


@pytest.fixture
def client(monkeypatch):
    async def fake_history(symbol, interval, start, end, provider="kdb"):
        return ([bar("2025-06-10T13:58:00"), bar("2025-06-10T13:59:00")],
                {"cache": "hit", "rows_from_cache": 2, "rows_from_upstream": 0,
                 "gaps_fetched": 0, "upstream_ms": 0.0, "kdb_ms": 1.0})

    monkeypatch.setattr("app.main.fetch_series", fake_history)
    monkeypatch.setattr("kdb_store.config.resolve_config", lambda: object())
    monkeypatch.setattr("kdb_store.session.KdbSession", lambda config: object())
    monkeypatch.setattr("kdb_store.store.KdbStore", lambda session: _NoSpanStore())
    return TestClient(create_app(api_key="test-key"))


def test_series_returns_bars_and_a_cache_block(client):
    body = client.get("/series", params={"symbol": "AAPL"}).json()
    assert "bars" in body and "cache" in body


def test_series_reports_rows_from_ticks(client):
    body = client.get("/series", params={"symbol": "AAPL"}).json()
    assert "rows_from_ticks" in body["cache"]


def test_chart_returns_plotly_figure_json(client):
    body = client.get("/chart", params={"symbol": "AAPL"}).json()
    assert "data" in body and "layout" in body


def test_demo_page_is_served(client):
    res = client.get("/demo")
    assert res.status_code == 200
    assert "plotly_relayout" in res.text


def test_live_grid_widget_is_still_registered(client):
    """Episode 8's widget must survive this change."""
    assert "live_grid" in client.get("/widgets.json").json()


def test_chart_widget_is_registered(client):
    assert any("chart" in key for key in client.get("/widgets.json").json())


def test_series_works_with_no_recorder(monkeypatch):
    """No kdb: history only, and the route must not error.

    Deliberately does not use the `client` fixture: that fixture wires up a
    (mocked) recorder, so its tick_span happens to return None. That is a
    different code path from `recorder is None` -- build_series short-circuits
    on `recorder is None` before ever touching a store. Setting
    LIVE_GRID_CHART=false drives create_app() down the real no-recorder
    branch, so this test exercises what its name says.
    """
    async def fake_history(symbol, interval, start, end, provider="kdb"):
        return ([bar("2025-06-10T13:58:00"), bar("2025-06-10T13:59:00")],
                {"cache": "hit", "rows_from_cache": 2, "rows_from_upstream": 0,
                 "gaps_fetched": 0, "upstream_ms": 0.0, "kdb_ms": 1.0})

    monkeypatch.setattr("app.main.fetch_series", fake_history)
    monkeypatch.setenv("LIVE_GRID_CHART", "false")
    no_recorder_client = TestClient(create_app(api_key="test-key"))
    body = no_recorder_client.get("/series", params={"symbol": "AAPL"}).json()
    assert body["cache"]["rows_from_ticks"] == 0


def _extract_braced_block(text, marker):
    """Return `marker`'s following `{ ... }` block, matched by brace-depth
    counting (the file has no unbalanced braces inside strings/comments, so
    plain counting is safe here -- this is a test-only helper, not a JS
    parser)."""
    start = text.index(marker)
    brace_start = text.index("{", start)
    depth = 0
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start:i + 1]
    raise ValueError(f"unbalanced braces after {marker!r}")


def test_demo_page_binds_relayout_handler_only_after_first_plot(client):
    """Regression test carried over from cache-chart: Plotly only attaches its
    `.on` event-emitter to the chart <div> once that div has been plotted at
    least once (Plotly.newPlot/react). Binding `chart.on(...)` in boot() --
    which runs before any plot exists -- throws a TypeError and leaves the
    page completely blank while every server-side test still passes. This
    page has already been through that bug once; deleting cache-chart must
    not silently drop the regression coverage for it.
    """
    text = client.get("/demo").text

    boot_body = _extract_braced_block(text, "async function boot()")
    assert "chart.on(" not in boot_body, (
        "chart.on(...) is called directly in boot(), which runs before any "
        "plot exists -- on a bare <div>, Plotly has not attached `.on` yet, "
        "so this throws a TypeError and the page never boots"
    )

    draw_body = _extract_braced_block(text, "function draw()")
    plot_positions = [
        i for i in (draw_body.find("Plotly.react(chart"), draw_body.find("Plotly.newPlot(chart"))
        if i != -1
    ]
    on_position = draw_body.find("chart.on(")
    if on_position != -1:
        assert plot_positions and on_position > min(plot_positions), (
            "chart.on(...) appears in draw() before the Plotly plot call -- "
            "it must be bound only after the chart has actually been plotted"
        )


def test_demo_page_registers_relayout_handler_once(client):
    """chart.on(...) must appear exactly once -- re-registering after every
    gap fetch leaks a listener per zoom."""
    text = client.get("/demo").text
    assert text.count('chart.on("plotly_relayout"') == 1


# -- the tick/history join ---------------------------------------------------
#
# The headline of this phase, and previously covered only in its degraded
# (no-recorder) form -- which is why the missing `end` clip went unnoticed.
# These drive the real join through /series with a fake recorder: a known
# tick span and known aggregated bars, so what is under test is the seam
# arithmetic and the clipping, not q.

SPAN = (D("2025-06-10T14:00:30"), D("2025-06-10T14:03:30"))
SEAM = "2025-06-10T14:01:00"

# 14:01 is deliberately present in BOTH: history must lose it to the ticks.
TICK_HISTORY = [bar(D("2025-06-10T13:58:00")), bar(D("2025-06-10T13:59:00")),
                bar(D("2025-06-10T14:00:00")), bar(D("2025-06-10T14:01:00"), close=9.0)]
TICK_BARS = [bar(D("2025-06-10T14:01:00"), close=5.0),
             bar(D("2025-06-10T14:02:00"), close=6.0),
             bar(D("2025-06-10T14:03:00"), close=7.0)]


class FakeStore:
    """Stands in for KdbStore. Records the range each aggregation asked for."""

    def __init__(self):
        self.aggregate_calls = []

    def tick_span(self, symbol):
        return SPAN

    def prune_ticks(self, cutoff):
        return 0

    def write_ticks(self, frame):
        return 0


@pytest.fixture
def tick_client(monkeypatch):
    """live-grid wired to a fake recorder rather than a real q."""
    store = FakeStore()

    async def fake_history(symbol, interval, start, end, provider="kdb"):
        return (list(TICK_HISTORY),
                {"cache": "hit", "rows_from_cache": len(TICK_HISTORY),
                 "rows_from_upstream": 0, "gaps_fetched": 0,
                 "upstream_ms": 0.0, "kdb_ms": 1.0})

    def fake_aggregate_ticks(st, symbol, interval, start, end):
        st.aggregate_calls.append((start, end))
        return [b for b in TICK_BARS if start <= b["date"] <= end]

    monkeypatch.setattr("app.main.fetch_series", fake_history)
    monkeypatch.setattr("kdb_store.aggregate.aggregate_ticks", fake_aggregate_ticks)
    monkeypatch.setattr("kdb_store.config.resolve_config", lambda: object())
    monkeypatch.setattr("kdb_store.session.KdbSession", lambda config: object())
    monkeypatch.setattr("kdb_store.store.KdbStore", lambda session: store)

    client = TestClient(create_app(api_key="test-key"))
    client.store = store
    return client


def _series(client, **params):
    params.setdefault("symbol", "AAPL")
    params.setdefault("interval", "1m")
    return client.get("/series", params=params).json()


def test_the_seam_is_the_first_boundary_at_or_after_the_first_tick(tick_client):
    """14:00:30 is mid-bar -- that bar is missing its own opening trades, so
    ticks may not own it. 14:01 is the first bar they cover whole."""
    body = _series(tick_client, start="2025-06-10", end="2025-06-10")
    assert body["cache"]["seam"] == SEAM


def test_history_at_or_after_the_seam_is_dropped(tick_client):
    """Otherwise 14:01 appears twice, once per source."""
    body = _series(tick_client, start="2025-06-10", end="2025-06-10")
    at_seam = [b for b in body["bars"] if b["date"] == SEAM]
    assert len(at_seam) == 1
    assert at_seam[0]["close"] == 5.0, "the seam bar must come from ticks, not history"


def test_the_join_has_no_duplicate_timestamps_and_is_strictly_increasing(tick_client):
    body = _series(tick_client, start="2025-06-10", end="2025-06-10")
    stamps = [b["date"] for b in body["bars"]]
    assert stamps == sorted(stamps)
    assert len(stamps) == len(set(stamps))
    assert stamps == ["2025-06-10T13:58:00", "2025-06-10T13:59:00",
                      "2025-06-10T14:00:00", SEAM,
                      "2025-06-10T14:02:00", "2025-06-10T14:03:00"]


def test_rows_from_ticks_counts_only_the_tick_derived_bars(tick_client):
    body = _series(tick_client, start="2025-06-10", end="2025-06-10")
    assert body["cache"]["rows_from_ticks"] == len(TICK_BARS)
    assert body["cache"]["rows_from_cache"] == len(TICK_HISTORY)


def test_tick_bars_are_clipped_to_the_requested_end(tick_client):
    """Ticks are only ever as recent as now; the caller's `end` still bounds
    them. Without this the aggregation ran to the end of the tick span and
    returned bars past the window the caller asked for."""
    body = _series(tick_client, start="2025-06-10", end="2025-06-10T14:02:00")
    stamps = [b["date"] for b in body["bars"]]
    assert max(stamps) == "2025-06-10T14:02:00"
    assert body["cache"]["rows_from_ticks"] == 2
    _, hi = tick_client.store.aggregate_calls[-1]
    assert hi == D("2025-06-10T14:02:00"), "the aggregation itself must be clipped"


def test_a_window_that_closed_in_the_past_gets_no_ticks_at_all(tick_client):
    """The reported bug: `?start=2024-01-01&end=2024-06-01` came back with
    today's tick-derived bars appended to a window that ended months earlier."""
    body = _series(tick_client, start="2024-01-01", end="2024-06-01")
    assert body["cache"]["rows_from_ticks"] == 0
    assert body["cache"]["seam"] is None
    assert body["bars"] == [dict(b, date=b["date"].isoformat()) for b in TICK_HISTORY]
    assert tick_client.store.aggregate_calls == [], "no aggregation should have run"


def test_a_date_only_end_covers_that_whole_day(tick_client):
    """A date-only `end` is inclusive of the day -- the historical provider
    returns that day's bar, so midnight would clip the final day's ticks."""
    body = _series(tick_client, start="2025-06-10", end="2025-06-10")
    assert body["cache"]["rows_from_ticks"] == len(TICK_BARS)


def test_history_endpoint_routes_by_asset_class():
    """BTC-USD via equity/price/historical went upstream as BTC-USD.US -> 404;
    each shape has its own Platform route (kdb registers all three fetchers)."""
    from app.openbb_client import history_endpoint

    assert history_endpoint("AAPL") == "equity/price/historical"
    assert history_endpoint("BTC-USD") == "crypto/price/historical"
    assert history_endpoint("EURUSD") == "currency/price/historical"

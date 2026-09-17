# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Live deltas: only revised bars travel, and repainting indicators do not."""

import pytest

from app.ta.figure import delta, trace_index
from app.ta.panes import assign
from app.ta.payload import (
    any_repaints,
    bars_to_frame,
    delta_start,
    parse_indicators,
    revised_from,
)
from app.ta.registry import REGISTRY, resolve
from app.ta.series_payload import series_delta
from app.ta.sources import LocalSource
from tests.ta_helpers import cols, fixture_frame
from tests.test_ta_session import session_bars
from tests.test_ta_sources import THREE_SESSIONS


def test_an_unchanged_series_resends_only_the_forming_bar():
    dates = ["2024-01-01", "2024-01-02"]
    assert revised_from(dates, dates) == len(dates) - 1


def test_a_new_bar_revises_from_the_previous_last_bar():
    """The forming bar is revised and a new one appears: both must travel."""
    before = ["2024-01-01", "2024-01-02"]
    after = ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert revised_from(before, after) == 1


def test_a_first_push_with_no_history_sends_everything():
    assert revised_from([], ["2024-01-01", "2024-01-02"]) == 0


def test_a_shortened_series_resends_from_zero():
    assert revised_from(["a", "b", "c"], ["b", "c"]) == 0


def test_no_tier_one_indicator_repaints():
    assert not any_repaints(assign(None, [resolve(n) for n in REGISTRY]))


def test_a_repainting_indicator_is_detected():
    zigzag = REGISTRY["rsi"]
    REGISTRY["_zz"] = type(zigzag)(**{**zigzag.__dict__, "name": "_zz", "repaints": True})
    try:
        assert any_repaints(assign(None, [resolve("_zz")]))
    finally:
        del REGISTRY["_zz"]


def _bd_frame(sessions):
    """The `sma:period=3bd` request and its computed 1m frame."""
    req = parse_indicators("sma:period=3bd")[0]
    bars = [bar for day, closes in sessions for bar in session_bars(day, closes)]
    frame = LocalSource().series(bars_to_frame(bars), [req], "1m", "AAPL").frame
    return req, frame


def test_a_business_day_window_resends_its_whole_running_session():
    """A `bd` window moves every bar of the running session when a tick
    revises the session's close (Critical 1, review-B3), so a one-bar tail
    delta leaves the rest of the session stale on the client.

    It is not answered by calling the chart repainting, though (Important 1,
    review-final-slice4): the delta stays on and reaches back to the session's
    first bar instead.
    """
    # Three sessions of 10,11,12 / 20,21,22 / 30,31,32, then a fourth bar
    # revises the running session's close to 42.
    running = (*THREE_SESSIONS[:2], ("2024-01-05", [30.0, 31.0, 32.0, 42.0]))
    req, frame = _bd_frame(running)
    panes = assign(None, [req])
    assert not any_repaints(panes)
    column = cols(req)[0]
    # mean(12, 22, 42) lands on the whole running session -- 14:30 through
    # 14:33 -- not only the forming bar a tail delta would resend.
    assert frame[column].to_list()[-4:] == pytest.approx([76 / 3] * 4)

    dates = [str(d) for d in frame["date"].to_list()]
    assert revised_from(dates, dates) == frame.height - 1, "the tail is one bar"
    # ...and the delta covers all four bars that moved.
    assert delta_start(panes, dates, dates, frame, "AAPL") == frame.height - 4


def test_a_business_day_delta_stops_at_the_running_session():
    """The sessions BEFORE the running one are closed and cannot move, so
    they must not travel: this is the whole difference between a bounded
    delta and resending 21,450 bars a second."""
    req, frame = _bd_frame(THREE_SESSIONS)
    panes = assign(None, [req])
    dates = [str(d) for d in frame["date"].to_list()]
    start = delta_start(panes, dates, dates, frame, "AAPL")
    assert start == 6, "the third session's first bar, not the frame's"

    payload = series_delta(frame, panes, start)
    assert payload["from"] == 6
    assert len(payload["candles"]) == 3
    assert all(len(s["data"]) == 3 for pane in payload["panes"] for s in pane["series"])


def test_a_bar_window_delta_is_the_tail_it_always_was():
    """No unit, no clamp: the sessions are irrelevant to a bar window and
    every existing chart keeps the one-bar delta it had."""
    req = parse_indicators("sma:period=3")[0]
    _, frame = _bd_frame(THREE_SESSIONS)
    panes = assign(None, [req])
    dates = [str(d) for d in frame["date"].to_list()]
    assert delta_start(panes, dates, dates, frame, "AAPL") == frame.height - 1


def test_a_delta_of_two_bars_carries_two_points_per_trace():
    frame = fixture_frame()
    panes = assign(None, [resolve("rsi", period=14)])
    from app.ta.compute import compute
    computed = compute(frame, [resolve("rsi", period=14)])
    payload = delta(computed, panes, computed.height - 2)
    assert all(len(t.get("y", t.get("close", []))) == 2
               for t in payload["traces"].values())


def test_trace_indices_in_a_delta_line_up_with_the_figure():
    panes = assign(None, [resolve("macd")])
    from app.ta.compute import compute
    computed = compute(fixture_frame(), [resolve("macd")])
    payload = delta(computed, panes, computed.height - 1)
    assert sorted(int(k) for k in payload["traces"]) == list(range(len(trace_index(panes))))

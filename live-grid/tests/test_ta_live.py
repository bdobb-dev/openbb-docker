"""Live deltas: only revised bars travel, and repainting indicators do not."""

from app.ta.figure import delta, trace_index
from app.ta.panes import assign
from app.ta.payload import any_repaints, revised_from
from app.ta.registry import REGISTRY, resolve
from tests.ta_helpers import fixture_frame


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

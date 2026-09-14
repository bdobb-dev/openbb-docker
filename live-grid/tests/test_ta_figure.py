"""Multi-pane figure assembly and tail deltas."""

from itertools import pairwise

import polars as pl

from app.ta.compute import compute
from app.ta.figure import build_ta_figure, delta, trace_index
from app.ta.panes import assign
from app.ta.registry import resolve
from app.ta.sources import Annotation
from tests.ta_helpers import col, cols, fixture_frame


def built(*reqs):
    panes = assign(None, list(reqs))
    frame = compute(fixture_frame(), list(reqs))
    return frame, panes, build_ta_figure("AAPL", frame, panes)


def test_trace_zero_is_the_candlestick():
    _, _, fig = built(resolve("rsi", period=14))
    assert fig["data"][0]["type"] == "candlestick"
    assert fig["data"][0]["yaxis"] == "y"


def test_each_pane_gets_its_own_yaxis_with_a_domain():
    _, _, fig = built(resolve("rsi", period=14))
    assert "domain" in fig["layout"]["yaxis"]
    assert "domain" in fig["layout"]["yaxis2"]


def test_panes_do_not_overlap_vertically():
    _, _, fig = built(resolve("rsi", period=14), resolve("macd"))
    doms = [fig["layout"][k]["domain"] for k in ("yaxis", "yaxis2", "yaxis3")]
    for upper, lower in pairwise(doms):
        assert lower[1] <= upper[0] + 1e-9


def test_an_overlay_shares_the_price_axis():
    _, _, fig = built(resolve("sma", period=50))
    sma = next(t for t in fig["data"] if t.get("name", "").startswith("SMA"))
    assert sma["yaxis"] == "y"


def test_an_oscillator_uses_its_own_axis():
    _, _, fig = built(resolve("rsi", period=14))
    rsi = next(t for t in fig["data"] if t.get("name", "").startswith("RSI"))
    assert rsi["yaxis"] == "y2"


def test_guides_become_horizontal_shapes_on_the_right_axis():
    _, _, fig = built(resolve("rsi", period=14))
    guides = [s for s in fig["layout"]["shapes"] if s["yref"] == "y2"]
    assert sorted(s["y0"] for s in guides) == [30.0, 70.0]


def test_a_bar_render_becomes_a_bar_trace():
    _, _, fig = built(resolve("volume"))
    assert any(t["type"] == "bar" for t in fig["data"])


def test_only_the_bottom_axis_shows_tick_labels():
    _, _, fig = built(resolve("rsi", period=14))
    assert fig["layout"]["xaxis"]["showticklabels"] is False
    assert fig["layout"]["xaxis2"]["showticklabels"] is True


def test_annotations_appear_in_the_subtitle():
    frame, panes, _ = built(resolve("vwap"))
    fig = build_ta_figure("AAPL", frame, panes,
                          [Annotation(col("vwap"), "local", "no EODHD equivalent")])
    # The human label. This asserted the internal column name until the
    # per-parameter rename made those names like `sma|period=3`, which has
    # no business in text a user reads.
    assert "VWAP" in fig["layout"]["title"]["text"]


def test_annotation_marks_use_human_labels_not_internal_columns():
    """A chart title is read by people.

    Output columns carry a parameter signature (`sma|period=50`) so two
    periods of one indicator cannot collapse onto one column. That suffix is
    for the dedup pass; surfacing it in visible chart text leaks an
    implementation detail at exactly the moment a user is being told
    something went wrong.
    """
    frame, panes, _ = built(resolve("sma", period=50))
    column = panes[0].series[0].column
    assert "|" in column, "guard: this test is meaningless without the suffix"
    fig = build_ta_figure("AAPL", frame, panes,
                          [Annotation(column, "local", "no EODHD equivalent")])
    title = fig["layout"]["title"]["text"]
    assert "SMA(50)" in title
    assert column not in title


def test_the_symbol_appears_in_the_title():
    _, _, fig = built(resolve("rsi", period=14))
    assert "AAPL" in fig["layout"]["title"]["text"]


def test_trace_index_lists_the_candlestick_then_every_series():
    _, panes, _ = built(resolve("macd"))
    assert trace_index(panes)[0] == "__price__"
    assert trace_index(panes)[1:] == cols(resolve("macd"))


def test_a_delta_carries_only_the_requested_tail():
    frame, panes, _ = built(resolve("rsi", period=14))
    d = delta(frame, panes, frame.height - 2)
    assert d["from"] == frame.height - 2
    assert len(d["x"]) == 2
    assert len(d["traces"]["1"]["y"]) == 2


def test_a_delta_includes_the_candlestick_ohlc():
    frame, panes, _ = built(resolve("rsi", period=14))
    d = delta(frame, panes, frame.height - 1)
    assert set(d["traces"]["0"]) == {"open", "high", "low", "close"}


def test_a_nan_in_a_series_serialises_as_null_rather_than_raising():
    """Starlette renders with allow_nan=False: a surviving NaN RAISES.

    So this is not a cosmetic check. If the guard in _column regresses, the
    /ta_chart endpoint returns 500 for any series containing a NaN, rather
    than drawing that bar as a gap.
    """
    import json

    from fastapi.responses import JSONResponse

    frame, panes, _ = built(resolve("rsi", period=14))
    rsi = col("rsi", period=14)
    poisoned = frame.with_columns(
        pl.when(pl.int_range(pl.len()) == 5)
        .then(float("nan"))
        .otherwise(pl.col(rsi))
        .alias(rsi)
    )
    fig = build_ta_figure("AAPL", poisoned, panes)
    body = JSONResponse(fig).body.decode()  # raises if any NaN survived
    assert json.loads(body)["data"][1]["y"][5] is None


def test_an_empty_frame_still_builds_a_valid_figure():
    empty = fixture_frame().head(0)
    panes = assign(None, [resolve("rsi", period=14)])
    fig = build_ta_figure("AAPL", compute(empty, [resolve("rsi", period=14)]), panes)
    assert fig["data"][0]["x"] == []


def test_two_periods_of_one_oscillator_do_not_draw_the_same_series():
    _, panes, fig = built(resolve("rsi", period=14), resolve("rsi", period=2))
    assert len(panes) == 3, [p.id for p in panes]
    fast, slow = fig["data"][1]["y"], fig["data"][2]["y"]
    assert fast != slow


def test_an_inf_in_a_series_serialises_as_null_rather_than_raising():
    """JSONResponse rejects Infinity exactly as it rejects NaN, and that raise
    happens outside the route's try: a blank 500 with no title to say why.
    roc, bandwidth and vwap all divide by something that can be zero."""
    import json

    from fastapi.responses import JSONResponse

    frame, panes, _ = built(resolve("rsi", period=14))
    rsi = col("rsi", period=14)
    poisoned = frame.with_columns(
        pl.when(pl.int_range(pl.len()) == 5)
        .then(float("inf"))
        .otherwise(pl.col(rsi))
        .alias(rsi)
    )
    fig = build_ta_figure("AAPL", poisoned, panes)
    body = JSONResponse(fig).body.decode()  # raises if any inf survived
    assert json.loads(body)["data"][1]["y"][5] is None

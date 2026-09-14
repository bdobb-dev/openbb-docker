"""Pane assignment and domain arithmetic. Pure -- no Plotly, no data."""

import pytest

from app.ta.macros import Macro, PaneSpec
from app.ta.panes import all_reqs, assign, domains
from app.ta.registry import resolve
from tests.ta_helpers import col, cols


def macro_of(*panes):
    return Macro("t", "T", "", list(panes))


def test_manual_mode_puts_overlays_on_price_and_oscillators_below():
    panes = assign(None, [resolve("sma", period=50), resolve("rsi", period=14)])
    assert panes[0].is_price and panes[0].id == "price"
    assert [s.column for s in panes[0].series] == [col("sma", period=50)]
    assert [p.id for p in panes[1:]] == ["rsi"]


def test_manual_mode_always_produces_a_price_pane_even_with_no_overlays():
    panes = assign(None, [resolve("rsi", period=14)])
    assert panes[0].is_price and panes[0].series == []


def test_manual_mode_with_no_picks_is_just_the_price_pane():
    panes = assign(None, [])
    assert len(panes) == 1 and panes[0].is_price


def test_a_macro_defines_pane_order_and_heights():
    macro = macro_of(
        PaneSpec("price", 3.0, [resolve("sma", period=200)]),
        PaneSpec("rsi", 1.0, [resolve("rsi", period=14)], [30.0, 70.0]),
    )
    panes = assign(macro, [])
    assert [(p.id, p.height) for p in panes] == [("price", 3.0), ("rsi", 1.0)]
    assert panes[1].guides == [30.0, 70.0]


def test_a_pick_not_in_the_macro_appends_as_a_new_bottom_pane():
    macro = macro_of(PaneSpec("price", 3.0, [resolve("sma", period=200)]))
    panes = assign(macro, [resolve("cci", period=20)])
    assert [p.id for p in panes] == ["price", "cci"]


def test_a_pick_already_in_the_macro_is_not_duplicated():
    macro = macro_of(
        PaneSpec("price", 3.0, []),
        PaneSpec("rsi", 1.0, [resolve("rsi", period=14)]),
    )
    panes = assign(macro, [resolve("rsi", period=14)])
    assert [p.id for p in panes] == ["price", "rsi"]


def test_the_same_indicator_at_a_different_period_is_a_different_pane():
    macro = macro_of(
        PaneSpec("price", 3.0, []),
        PaneSpec("rsi", 1.0, [resolve("rsi", period=14)]),
    )
    panes = assign(macro, [resolve("rsi", period=2)])
    assert len(panes) == 3


def test_a_multi_output_indicator_contributes_every_series():
    panes = assign(None, [resolve("macd", fast=12, slow=26, signal=9)])
    assert ({s.column for s in panes[1].series}
            == set(cols(resolve("macd", fast=12, slow=26, signal=9))))


def test_domains_span_zero_to_one_top_down():
    panes = assign(None, [resolve("rsi", period=14)])
    got = domains(panes, gap=0.0)
    assert got[0][1] == pytest.approx(1.0)
    assert got[-1][0] == pytest.approx(0.0)
    assert got[0][0] > got[1][1] - 1e-9  # price sits above rsi


def test_domain_heights_follow_the_weights():
    macro = macro_of(PaneSpec("price", 3.0, []), PaneSpec("rsi", 1.0, []))
    tall, short = domains(assign(macro, []), gap=0.0)
    assert (tall[1] - tall[0]) == pytest.approx(3 * (short[1] - short[0]))


def test_gaps_are_subtracted_before_weighting():
    macro = macro_of(PaneSpec("price", 1.0, []), PaneSpec("rsi", 1.0, []))
    got = domains(assign(macro, []), gap=0.1)
    total = sum(hi - lo for lo, hi in got)
    assert total == pytest.approx(0.9)


def test_a_macro_style_overrides_the_registry_render_colour():
    panes = assign(None, [resolve("sma", period=50, style={"color": "#ff0000"})])
    assert panes[0].series[0].render["color"] == "#ff0000"


def test_style_overrides_key_by_key_and_keeps_the_render_type():
    """Recolouring a bar must not turn it into a line."""
    panes = assign(None, [resolve("volume", style={"color": "#ff0000"})])
    series = panes[1].series[0]
    assert series.render["color"] == "#ff0000"
    assert series.render["type"] == "bar"


def test_no_style_leaves_the_registry_render_untouched():
    panes = assign(None, [resolve("sma", period=50)])
    assert panes[0].series[0].render["color"] == "#4c9be8"


def test_all_reqs_deduplicates_across_panes():
    macro = macro_of(
        PaneSpec("price", 3.0, [resolve("bbands", period=20, k=2.0)]),
        PaneSpec("b", 1.0, [resolve("pct_b", period=20, k=2.0)]),
    )
    assert len(all_reqs(assign(macro, []))) == 2


def test_domains_do_not_invert_when_there_are_many_panes():
    """At 0.02 a pane, 60 panes' gaps alone exceed 1.0 and every span comes out
    y0 > y1, which Plotly rejects."""
    panes = assign(None, [resolve("rsi", period=p) for p in range(2, 62)])
    assert len(panes) == 61
    for y0, y1 in domains(panes):
        assert 0.0 <= y0 < y1 <= 1.0

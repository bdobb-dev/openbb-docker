# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Pane assignment and domain arithmetic. Pure -- no Plotly, no data."""

from datetime import date

import pytest

from app.ta.macros import Macro, PaneSpec
from app.ta.panes import all_reqs, assign, domains, shift_times
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


def test_shift_times_zero_offset_is_identity():
    times = [date(2026, 1, d) for d in (1, 2, 3)]
    assert shift_times(times, 0) == times


def test_shift_times_forward_extends_past_the_last_bar():
    times = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    out = shift_times(times, 2)
    assert out[0] == date(2026, 1, 3)
    assert out[1] == date(2026, 1, 4)   # synthesized from the last spacing
    assert out[2] == date(2026, 1, 5)
    assert len(out) == len(times)


def test_shift_times_backward_extends_before_the_first_bar():
    times = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    out = shift_times(times, -2)
    assert out[0] == date(2025, 12, 30)
    assert out[2] == date(2026, 1, 1)


def test_shift_times_on_a_single_bar_cannot_infer_spacing():
    assert shift_times([date(2026, 1, 1)], 2) == [None]


def test_shift_times_infers_step_from_the_smallest_gap_not_the_tail():
    """An irregular series -- e.g. an intraday frame straddling a session
    break -- must not let one big trailing gap poison every synthesized
    timestamp (Fix 8)."""
    times = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 10)]
    out = shift_times(times, 1)
    # The true bar spacing is 1 day; the old last-two-entries rule would have
    # inferred a 7-day step from the tail gap and put this at 2026-01-17.
    assert out[-1] == date(2026, 1, 11)


def test_shift_times_tolerates_a_null_in_the_trailing_positions():
    """bars_to_frame parses dates with strict=False, so a malformed trailing
    date becomes a null. The old `times[-1] - times[-2]` step calculation
    raised TypeError the moment either landed there; this must not."""
    times = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), None]
    out = shift_times(times, 1)
    assert out == [date(2026, 1, 2), date(2026, 1, 3), None, date(2026, 1, 5)]

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Figure construction."""

from app.figure import build_figure

BARS = [
    {"date": "2024-01-02", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
    {"date": "2024-01-03", "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0},
]


def test_figure_has_one_trace_with_every_bar():
    fig = build_figure("AAPL", BARS)
    assert len(fig["data"]) == 1
    assert len(fig["data"][0]["x"]) == 2


def test_symbol_appears_in_the_title():
    assert "AAPL" in fig_title(build_figure("AAPL", BARS))


def fig_title(fig):
    title = fig["layout"]["title"]
    return title["text"] if isinstance(title, dict) else title


def test_empty_bars_still_produce_a_valid_figure():
    fig = build_figure("AAPL", [])
    assert fig["data"][0]["x"] == []

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The series presenter: same engine as the figure, different shape."""

from datetime import date

import polars as pl

from app.ta.panes import Pane, Series
from app.ta.series_payload import build_series_payload, series_delta


def _frame() -> pl.DataFrame:
    return pl.DataFrame({
        "date": [date(2026, 1, d) for d in (1, 2, 3)],
        "open": [1.0, 2.0, 3.0], "high": [1.5, 2.5, 3.5],
        "low": [0.5, 1.5, 2.5], "close": [1.2, 2.2, 3.2],
        "volume": [10.0, 20.0, 30.0],
        "lead": [4.0, 5.0, 6.0], "rsi": [30.0, 50.0, 70.0],
    })


def _panes() -> list[Pane]:
    return [
        Pane("price", 3.0, True,
             [Series("lead", "Span A", {"type": "line", "time_offset": 1})]),
        Pane("rsi", 1.0, False, [Series("rsi", "RSI(14)", {"type": "line"})],
             guides=[30.0, 70.0]),
    ]


def test_candles_carry_ohlc_and_volume_at_bar_times():
    payload = build_series_payload(_frame(), _panes(), "AAPL", "1d · local", [])
    assert payload["candles"][0] == {
        "time": "2026-01-01", "open": 1.0, "high": 1.5, "low": 0.5,
        "close": 1.2, "volume": 10.0,
    }


def test_a_time_offset_series_carries_displaced_times():
    payload = build_series_payload(_frame(), _panes(), "AAPL", "", [])
    lead = payload["panes"][0]["series"][0]["data"]
    assert lead[-1]["time"] == "2026-01-04"
    assert lead[-1]["value"] == 6.0


def test_guides_and_heights_travel_with_their_pane():
    payload = build_series_payload(_frame(), _panes(), "AAPL", "", [])
    assert payload["panes"][1]["guides"] == [30.0, 70.0]
    assert payload["panes"][1]["height"] == 1.0
    assert payload["panes"][0]["domain"][1] == 1.0


def test_non_finite_values_become_none_not_nan():
    frame = _frame().with_columns(pl.Series("rsi", [float("nan"), float("inf"), 70.0]))
    payload = build_series_payload(frame, _panes(), "AAPL", "", [])
    values = [point["value"] for point in payload["panes"][1]["series"][0]["data"]]
    assert values == [None, None, 70.0]


def test_a_delta_carries_only_the_revised_tail():
    delta = series_delta(_frame(), _panes(), 2)
    assert delta["from"] == 2
    assert len(delta["candles"]) == 1
    assert delta["panes"][1]["series"][0]["data"] == [{"time": "2026-01-03", "value": 70.0}]


def test_a_delta_keeps_the_offset_of_an_offset_series():
    delta = series_delta(_frame(), _panes(), 2)
    assert delta["panes"][0]["series"][0]["data"][-1]["time"] == "2026-01-04"

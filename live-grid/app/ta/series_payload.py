# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The series presenter.

`figure.py` renders panes into Plotly traces for the Workspace viewer; this
renders the same panes into raw series for a client that owns its own chart
engine. Both read the frame `build_payload` already computed, so there is no
second implementation of any indicator and the two cannot drift.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import polars as pl

from app.ta.panes import Pane, domains, shift_times

_CANDLE = ("open", "high", "low", "close", "volume")


def _values(frame: pl.DataFrame, name: str) -> list:
    """One column, JSON-safe. Non-finite becomes None -- Starlette renders
    with allow_nan=False and rejects Infinity exactly as it rejects NaN, and
    it would raise OUTSIDE the route's try."""
    if name not in frame.columns:
        return [None] * frame.height
    return [
        None if v is None or (isinstance(v, float) and not math.isfinite(v))
        else float(v)
        for v in frame[name].to_list()
    ]


def _times(frame: pl.DataFrame, offset: int) -> list[str | None]:
    if "date" not in frame.columns:
        return []
    stamps = frame["date"].to_list()
    if offset:
        stamps = shift_times(stamps, offset)
    return [None if s is None else str(s) for s in stamps]


def _series_of(frame: pl.DataFrame, pane: Pane) -> list[dict]:
    out = []
    for series in pane.series:
        render = dict(series.render)
        offset = int(render.pop("time_offset", 0) or 0)
        times = _times(frame, offset)
        values = _values(frame, series.column)
        out.append({
            "column": series.column, "label": series.label, "render": render,
            "data": [{"time": t, "value": v} for t, v in zip(times, values)],
        })
    return out


def _panes_of(
    frame: pl.DataFrame, panes: Sequence[Pane], start: int = 0
) -> list[dict]:
    """`start` slices each series' points AFTER the times are computed.

    Slicing the FRAME first and then displacing would break every offset
    series: shift_times infers spacing from the frame it is given, and a
    one-row tail has no spacing to infer -- so a displaced series' delta
    would carry None for every timestamp.
    """
    spans = domains(list(panes))
    return [
        {"id": pane.id, "height": pane.height, "domain": list(span),
         "guides": list(pane.guides),
         "series": [{**s, "data": s["data"][start:]} for s in _series_of(frame, pane)]}
        for pane, span in zip(panes, spans)
    ]


def _candles(frame: pl.DataFrame) -> list[dict]:
    times = _times(frame, 0)
    columns = {name: _values(frame, name) for name in _CANDLE}
    return [
        {"time": times[i], **{name: columns[name][i] for name in _CANDLE}}
        for i in range(frame.height)
    ]


def build_series_payload(
    frame: pl.DataFrame, panes: Sequence[Pane], symbol: str,
    subtitle: str = "", annotations: Sequence = (),
) -> dict:
    """The full state: every candle and every series, at revision zero."""
    labels = {s.column: s.label for pane in panes for s in pane.series}
    marks = sorted({labels.get(a.column, a.column) for a in annotations})
    return {"symbol": symbol, "subtitle": subtitle, "marks": marks,
            "candles": _candles(frame), "panes": _panes_of(frame, panes)}


def series_delta(
    frame: pl.DataFrame, panes: Sequence[Pane], start_row: int
) -> dict:
    """The tail from `start_row`. Same shape, so a client applies one merge.

    Times come from the FULL frame and the points are sliced afterwards --
    see _panes_of. Candles carry no offset, so slicing the frame for them is
    safe and cheaper.
    """
    start = max(0, min(start_row, frame.height))
    return {"from": start, "candles": _candles(frame.slice(start)),
            "panes": _panes_of(frame, panes, start)}

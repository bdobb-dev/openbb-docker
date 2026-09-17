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


def _series_of(
    frame: pl.DataFrame, pane: Pane,
    annotated: frozenset[str] = frozenset(), source: str = "local",
) -> list[dict]:
    """`annotated` is the set of columns an annotation covers -- i.e. every
    column the chosen source did NOT in the end supply -- and `source` is the
    chart-wide default for a request that named none.

    Both are needed because a series must say two different things: which
    source was ASKED for (`req.source`, so a client can match the series back
    to the study instance that asked) and which one actually SERVED it
    (`render.source`, so the client can label a silent fallback). They differ
    on exactly the fallback paths EodhdSource annotates -- intraday, an
    unmapped indicator, a failed or throttled fetch.
    """
    out = []
    for series in pane.series:
        render = dict(series.render)
        offset = int(render.pop("time_offset", 0) or 0)
        times = _times(frame, offset)
        values = _values(frame, series.column)
        requested = (series.req.source if series.req is not None else None) or source
        render["source"] = (
            "eodhd" if requested == "eodhd" and series.column not in annotated
            else "local"
        )
        out.append({
            "column": series.column, "label": series.label,
            "req": None if series.req is None else {
                "name": series.req.name,
                # `style` is presentation carried from a macro, not part of
                # the request's identity -- a client matching a study to its
                # series must not have to strip it.
                #
                # A parameter that wore a unit goes back in its WIRE form:
                # the client sent `period=3bd` and matches the series on what
                # it sent, so a bare 3 here reads as a different study and the
                # line never finds the instance that asked for it.
                # `:g` and not str(): the legend formats the same value the
                # same way (panes._suffix), and the client matches the series
                # to its study on this text -- `2bd` against `2.0bd` orphans
                # the line (Minor 1). Only a unit-bearing window reaches here,
                # so the format never touches a bare parameter.
                "params": {
                    k: f"{v:g}{series.req.units[k]}" if k in series.req.units else v
                    for k, v in series.req.params.items() if k != "style"
                },
                "source": series.req.source,
            },
            "render": render,
            "data": [{"time": t, "value": v} for t, v in zip(times, values)],
        })
    return out


def _panes_of(
    frame: pl.DataFrame, panes: Sequence[Pane], start: int = 0,
    annotated: frozenset[str] = frozenset(), source: str = "local",
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
         "series": [{**s, "data": s["data"][start:]}
                    for s in _series_of(frame, pane, annotated, source)]}
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
    subtitle: str = "", annotations: Sequence = (), source: str = "local",
) -> dict:
    """The full state: every candle and every series, at revision zero."""
    labels = {s.column: s.label for pane in panes for s in pane.series}
    marks = sorted({labels.get(a.column, a.column) for a in annotations})
    annotated = frozenset(a.column for a in annotations)
    return {"symbol": symbol, "subtitle": subtitle, "marks": marks,
            "candles": _candles(frame),
            "panes": _panes_of(frame, panes, 0, annotated, source)}


def series_delta(
    frame: pl.DataFrame, panes: Sequence[Pane], start_row: int,
    annotations: Sequence = (), source: str = "local",
) -> dict:
    """The tail from `start_row`. Same shape, so a client applies one merge.

    Times come from the FULL frame and the points are sliced afterwards --
    see _panes_of. Candles carry no offset, so slicing the frame for them is
    safe and cheaper.
    """
    start = max(0, min(start_row, frame.height))
    annotated = frozenset(a.column for a in annotations)
    return {"from": start, "candles": _candles(frame.slice(start)),
            "panes": _panes_of(frame, panes, start, annotated, source)}

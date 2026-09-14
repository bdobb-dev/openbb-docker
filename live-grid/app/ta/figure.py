"""Plotly figure JSON. The client renders it -- this service has no chart
backend, exactly like app/figure.py.

Pane N maps to yaxis N+1 ("y", "y2", "y3"...). Trace 0 is always the
candlestick, which is what lets a delta address traces by integer index
without shipping the whole figure again.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import polars as pl

from app.ta.panes import Pane, domains

PRICE = "__price__"


def _axis(index: int) -> str:
    return "y" if index == 0 else f"y{index + 1}"


def _axis_key(index: int, kind: str = "yaxis") -> str:
    return kind if index == 0 else f"{kind}{index + 1}"


def trace_index(panes: Sequence[Pane]) -> list[str]:
    """The column each trace draws, in trace order. Trace 0 is the candlestick."""
    columns = [PRICE]
    for pane in panes:
        columns.extend(s.column for s in pane.series)
    return columns


def _column(frame: pl.DataFrame, name: str) -> list:
    """One column as JSON-safe values.

    Anything non-finite -- NaN or +/-inf alike -- becomes None rather than
    passing through. Starlette renders with `allow_nan=False`, which rejects
    `Infinity` exactly as it rejects `NaN`, so one such value anywhere in a
    series makes the whole response RAISE, and it raises OUTSIDE the route's
    try: a blank 500 with no title to say why. roc, bandwidth and vwap all
    divide by a value that can be zero, so they all reach this boundary.
    Indicators already null their own 0/0 cases, but this is the boundary
    where the damage would actually occur, and a missed `fill_nan` upstream
    should not be able to take the chart down.
    """
    if name not in frame.columns:
        return [None] * frame.height
    return [
        None if v is None or (isinstance(v, float) and not math.isfinite(v)) else float(v)
        for v in frame[name].to_list()
    ]


def _dates(frame: pl.DataFrame) -> list[str]:
    if "date" not in frame.columns:
        return []
    return [None if d is None else str(d) for d in frame["date"].to_list()]


def build_ta_figure(
    symbol: str, frame: pl.DataFrame, panes: Sequence[Pane],
    annotations: Sequence = (), subtitle: str = "",
) -> dict:
    """A stacked multi-pane figure: candlesticks on top, indicators below."""
    x = _dates(frame)
    spans = domains(list(panes))

    data: list[dict] = [{
        "type": "candlestick", "name": symbol, "x": x,
        "open": _column(frame, "open"), "high": _column(frame, "high"),
        "low": _column(frame, "low"), "close": _column(frame, "close"),
        "yaxis": "y", "xaxis": "x",
    }]

    layout: dict = {
        "template": "plotly_dark",
        "margin": {"l": 48, "r": 20, "t": 48, "b": 36},
        "showlegend": True,
        "hovermode": "x unified",
        "shapes": [],
    }

    for index, (pane, (y0, y1)) in enumerate(zip(panes, spans)):
        axis, xaxis = _axis(index), "x" if index == 0 else f"x{index + 1}"
        layout[_axis_key(index)] = {
            "domain": [y0, y1], "anchor": xaxis,
            "title": {"text": "" if pane.is_price else pane.id},
        }
        layout[_axis_key(index, "xaxis")] = {
            "domain": [0.0, 1.0], "anchor": axis,
            "showticklabels": index == len(panes) - 1,
            "rangeslider": {"visible": False},
            **({} if index == 0 else {"matches": "x"}),
        }
        for series in pane.series:
            render = dict(series.render)
            kind = render.pop("type", "line")
            color = render.pop("color", None)
            trace = {
                "name": series.label, "x": x, "y": _column(frame, series.column),
                "yaxis": axis, "xaxis": xaxis,
            }
            if kind == "bar":
                trace["type"] = "bar"
                trace["marker"] = {"color": color}
            else:
                trace["type"] = "scatter"
                trace["mode"] = render.pop("mode", "lines")
                trace["line"] = {"color": color, "width": render.pop("width", 1.4)}
            data.append(trace)
        for guide in pane.guides:
            layout["shapes"].append({
                "type": "line", "xref": "paper", "x0": 0, "x1": 1,
                "yref": axis, "y0": guide, "y1": guide,
                "line": {"color": "#5c6370", "width": 1, "dash": "dot"},
            })

    # Annotations carry the internal column name, which since the
    # per-parameter rename looks like `sma|period=3`. The panes already know
    # the human label for every column, so use it -- a chart title is read by
    # people, not by the dedup pass that needs the suffix.
    labels = {s.column: s.label for pane in panes for s in pane.series}
    marks = ", ".join(sorted({labels.get(a.column, a.column) for a in annotations}))
    bits = [symbol]
    if subtitle:
        bits.append(subtitle)
    if marks:
        bits.append(f"local: {marks}")
    layout["title"] = {"text": "  ·  ".join(bits)}

    return {"data": data, "layout": layout}


def delta(frame: pl.DataFrame, panes: Sequence[Pane], start_row: int) -> dict:
    """The tail from `start_row` onward, addressed by trace index.

    Only revised bars need to travel: a revision to bar t changes bar t alone
    for every causal rolling or ewm indicator. Indicators that repaint are
    excluded from this path by the caller (spec D10).
    """
    start = max(0, min(start_row, frame.height))
    tail = frame.slice(start)
    traces: dict[str, dict] = {
        "0": {
            "open": _column(tail, "open"), "high": _column(tail, "high"),
            "low": _column(tail, "low"), "close": _column(tail, "close"),
        }
    }
    for position, column in enumerate(trace_index(panes)):
        if position == 0:
            continue
        traces[str(position)] = {"y": _column(tail, column)}
    return {"from": start, "x": _dates(tail), "traces": traces}

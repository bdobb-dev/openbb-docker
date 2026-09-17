# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""One builder, two routes.

`/ta_chart` and `/ta_chart_ws` both come through here. That is the reason the
live chart cannot disagree with the static one: there is no second
implementation to drift (spec D9).
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from app.ta.exprs import price_col
from app.ta.figure import build_ta_figure
from app.ta.macros import load_all
from app.ta.panes import Pane, all_reqs, assign
from app.ta.registry import Req, resolve
from app.ta.sources import Annotation, LocalSource, columns_of

_NUMERIC = ("period", "k", "d", "fast", "slow", "signal", "smooth_k",
            "stoch_period", "atr_period", "mult", "acceleration", "maximum",
            "conversion", "base", "span_b", "displacement", "mid",
            "multiplier", "session_shift")


@dataclass(frozen=True)
class ChartParams:
    symbol: str = "AAPL"
    interval: str = "1d"
    source: str = "local"
    macro: str = "none"
    indicators: str = ""
    start: str | None = None
    end: str | None = None
    provider: str = "kdb"
    basis: str = "adjusted"
    # "extended" (every bar) or "regular" (the feed's regular session only).
    # Only the literal "regular" filters; anything else is extended, with no
    # error -- the socket URL is built by a client that may be newer or older
    # than this server, and closing the stream on an unrecognised toggle is
    # worse than showing the whole tape. See app.ta.session.
    session: str = "extended"


#: Units a numeric parameter may wear on the wire. `bd` = business days: the
#: window counts SESSIONS rather than bars and is computed on the session
#: closes (app.ta.session.session_closes). A bare number is bars, as always.
_UNITS = ("bd",)


def _coerce(key: str, raw: str) -> tuple:
    """The parameter's value, and the unit it wore -- `("50bd")` -> `(50, "bd")`.

    Stripped here rather than inside resolve() because a unit is grammar, not
    a parameter: any numeric parameter may wear one, and none of them may
    reach an indicator's build() as a string. An unrecognised suffix is left
    on and float() raises, which is what should happen -- `period=50bx` is a
    typo, and silently reading it as 50 bars draws the wrong line with no
    error anywhere.
    """
    if key not in _NUMERIC:
        return raw, None
    text = raw.strip()
    unit = next((u for u in _UNITS
                 if text.lower().endswith(u) and text[:-len(u)].strip()), None)
    if unit is not None:
        text = text[:-len(unit)].strip()
    value = float(text)
    return (int(value) if value.is_integer() and key != "k" else value), unit


def with_anchor(indicators: str, anchor: str | None) -> str:
    """Fold the widget's `anchor` param into the indicator list.

    A first-class param because Workspace's chart viewer has no click channel
    back to this backend -- anchoring by interaction lives in clients that own
    their renderer (bdobb-v2's real-time chart); here it is declarative. An
    avwap already present in `indicators` wins over the param.
    """
    anchor = (anchor or "").strip()
    if not anchor:
        return indicators
    chunks = (indicators or "").split(",")
    if any(chunk.strip().split(":")[0].strip() == "avwap" for chunk in chunks):
        return indicators
    spec = f"avwap:anchor={anchor}"
    return f"{spec},{indicators}" if indicators else spec


def parse_indicators(raw: str) -> list[Req]:
    """`"rsi:period=14,sma:period=200"` -> resolved requests."""
    reqs: list[Req] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, *fragments = chunk.split(":")
        # a value may itself contain colons (avwap:anchor=2026-08-28T14:30:00);
        # a fragment without "=" belongs to the previous pair's value
        pairs: list[str] = []
        for part in fragments:
            if "=" in part or not pairs:
                pairs.append(part)
            else:
                pairs[-1] += ":" + part
        params = {}
        units: dict[str, str] = {}
        for pair in pairs:
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            key = key.strip()
            params[key], unit = _coerce(key, value.strip())
            if unit is not None:
                units[key] = unit
        # `source` rides in the same colon grammar but is NOT an indicator
        # parameter -- it is the request's own Local/EODHD routing choice. It
        # has to come out before resolve(), which raises on any key the
        # registry does not declare, and it is validated here rather than
        # defaulted: silently reading a typo as "local" would draw a line the
        # payload then labels as the vendor's.
        source = params.pop("source", None)
        if source is not None and source not in ("local", "eodhd"):
            raise ValueError(
                f"source must be 'local' or 'eodhd', got {source!r}")
        # `units` collides with resolve()'s own named argument the same way
        # `source` would -- reject it here instead of letting resolve() raise
        # a "multiple values for argument" TypeError that means nothing to
        # whoever reads the log.
        if "units" in params:
            raise ValueError("'units' is a reserved key, not an indicator parameter")
        reqs.append(resolve(name.strip(), source, units, **params))
    return reqs


def bars_to_frame(bars: list[dict], basis: str = "adjusted") -> pl.DataFrame:
    """Bars from build_series into the frame the engine expects.

    Tick-derived bars have no adjusted close, so raw close stands in. That is
    correct rather than a fudge: intraday ticks are already unadjusted, and the
    alternative is a null column that silently voids every adjusted indicator.
    """
    # `basis` is a param like any other, and every other one in this engine
    # raises on an unknown value (resolve, price_col) rather than silently
    # falling back -- ?basis=raw is the only value besides "adjusted" that
    # means anything here, so reuse price_col's own check instead of letting
    # a typo (?basis=Raw) mean "adjusted" with no error anywhere.
    price_col(basis)
    schema = {"date": pl.Datetime, "open": pl.Float64, "high": pl.Float64,
              "low": pl.Float64, "close": pl.Float64, "adj_close": pl.Float64,
              "volume": pl.Float64, "vwap": pl.Float64}
    if not bars:
        return pl.DataFrame(schema=schema)
    rows = []
    for bar in bars:
        close = bar.get("close")
        # `.get(key, default)` fires only when the key is ABSENT. Providers
        # return an explicit null adjusted_close for instruments that have no
        # adjustment data -- indices, forex, crypto -- and that must fall back
        # too. Without this, all 13 price_basis="adjusted" indicators render
        # blank on those symbols, with no error anywhere to explain it.
        adjusted = bar.get("adjusted_close")
        rows.append({
            # NOT truncated to 10 chars: widgets.json offers 1h/5m/1m and kdb
            # tick bars carry a full timestamp, so a day's worth of intraday
            # bars would all plot at one x. /chart passes timestamps through
            # untouched; this route must not be the sibling that flattens them.
            "date": str(bar.get("date")),
            "open": bar.get("open"), "high": bar.get("high"),
            "low": bar.get("low"), "close": close,
            # basis="raw" makes every price_basis="adjusted" indicator read
            # raw close, without touching a single indicator: they all read
            # the adj_close COLUMN, and this is where that column is decided.
            "adj_close": close if basis == "raw" or adjusted is None else adjusted,
            "volume": bar.get("volume") or 0.0,
            # Only tick-derived bars carry a true trade-weighted vwap; vendor
            # history has no per-trade data, so the column is null there.
            "vwap": bar.get("vwap"),
        })
    # history rows lead with vwap=None; without the override polars infers a
    # Null column and the first tick bar's float then fails to append
    return pl.DataFrame(rows, schema_overrides={"vwap": pl.Float64}).with_columns([
        pl.col("date").str.to_datetime(strict=False),
        pl.col(["open", "high", "low", "close", "adj_close", "volume", "vwap"])
          .cast(pl.Float64, strict=False),
    ])


def chart_subtitle(params: ChartParams) -> str:
    """The line shared by the figure title and the series payload.

    One spelling: `/ta_chart`'s figure and `/ta_series_ws`'s series payload
    used to copy this string independently, and `basis` was added on this
    branch to neither copy.
    """
    return f"{params.interval} · {params.source} · {params.basis}"


async def build_payload(
    params: ChartParams, frame: pl.DataFrame, eodhd_source=None,
) -> tuple[dict, list[Pane], pl.DataFrame, list]:
    """The figure, its panes, the computed frame, and its annotations.

    Annotations come back rather than being folded into the title and dropped:
    the title only ships on a FIGURE push, so a caller that cannot see them
    change cannot know to send one (see main.ta_chart_ws).
    """
    macro = None
    if params.macro and params.macro != "none":
        macros = load_all()
        if params.macro not in macros:
            raise KeyError(f"no such macro {params.macro!r}")
        macro = macros[params.macro]

    panes = assign(macro, parse_indicators(params.indicators))
    reqs = all_reqs(panes)

    # The chart-wide `source` is now only a DEFAULT: each request may name its
    # own (v12.3.0 studies), so the split is per request rather than one
    # branch for the whole chart. Local runs first and the vendor joins onto
    # its frame, so one frame carries both and nothing downstream has to know
    # which engine produced which column.
    def effective(req: Req) -> str:
        return req.source or params.source

    vendor = [r for r in reqs if effective(r) == "eodhd"]
    local = [r for r in reqs if effective(r) != "eodhd"]
    annotations: list = []
    computed = frame
    if local:
        computed = LocalSource().series(
            computed, local, params.interval, params.symbol).frame
    if vendor and eodhd_source is not None:
        last_closed = str(frame["date"][-1]) if frame.height else ""
        result = await eodhd_source.series(
            computed, vendor, params.symbol, params.interval, last_closed
        )
        computed, annotations = result.frame, result.annotations
    elif vendor:
        # No vendor client configured: compute locally and say so, exactly as
        # an intraday request is answered.
        computed = LocalSource().series(
            computed, vendor, params.interval, params.symbol).frame
        annotations = [Annotation(col, "local", "no EODHD source configured")
                       for r in vendor for col in columns_of(r)]

    figure = build_ta_figure(
        params.symbol, computed, panes, annotations, chart_subtitle(params)
    )
    return figure, panes, computed, annotations


def any_repaints(panes: list[Pane]) -> bool:
    """True when a pane holds an indicator whose history can change.

    Tail deltas assume causality: a revision to bar t changes bar t alone. That
    is false for ZigZag, whose pivots move well back into history when a new
    extreme arrives, so such a chart resends in full (spec D10). It is also
    false for a `bd` (business-day) window: a tick to the running session's
    close moves every bar of that session, not only the last one, so a
    unit-bearing request repaints too.
    """
    from app.ta.registry import get

    return any(get(req.name).repaints or req.units
               for pane in panes for req in pane.reqs)


def revised_from(previous_dates: list[str], current_dates: list[str]) -> int:
    """The first row index whose value may have changed since the last push.

    The last bar of the previous push is always included: it was forming, so
    ticks since then have revised it.
    """
    if not previous_dates or len(current_dates) < len(previous_dates):
        return 0
    if current_dates[:len(previous_dates)] != previous_dates:
        return 0
    return max(0, len(previous_dates) - 1)

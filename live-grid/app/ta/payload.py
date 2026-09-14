"""One builder, two routes.

`/ta_chart` and `/ta_chart_ws` both come through here. That is the reason the
live chart cannot disagree with the static one: there is no second
implementation to drift (spec D9).
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from app.ta.figure import build_ta_figure
from app.ta.macros import load_all
from app.ta.panes import Pane, all_reqs, assign
from app.ta.registry import Req, resolve
from app.ta.sources import LocalSource

_NUMERIC = ("period", "k", "d", "fast", "slow", "signal", "smooth_k",
            "stoch_period", "atr_period", "mult", "acceleration", "maximum")


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


def _coerce(key: str, raw: str):
    if key not in _NUMERIC:
        return raw
    value = float(raw)
    return int(value) if value.is_integer() and key != "k" else value


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
        for pair in pairs:
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            params[key.strip()] = _coerce(key.strip(), value.strip())
        reqs.append(resolve(name.strip(), **params))
    return reqs


def bars_to_frame(bars: list[dict]) -> pl.DataFrame:
    """Bars from build_series into the frame the engine expects.

    Tick-derived bars have no adjusted close, so raw close stands in. That is
    correct rather than a fudge: intraday ticks are already unadjusted, and the
    alternative is a null column that silently voids every adjusted indicator.
    """
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
            "adj_close": close if adjusted is None else adjusted,
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

    annotations: list = []
    if params.source == "eodhd" and eodhd_source is not None and reqs:
        last_closed = str(frame["date"][-1]) if frame.height else ""
        result = await eodhd_source.series(
            frame, reqs, params.symbol, params.interval, last_closed
        )
        computed, annotations = result.frame, result.annotations
    else:
        computed = LocalSource().series(frame, reqs).frame

    subtitle = f"{params.interval} · {params.source}"
    figure = build_ta_figure(params.symbol, computed, panes, annotations, subtitle)
    return figure, panes, computed, annotations


def any_repaints(panes: list[Pane]) -> bool:
    """True when a pane holds an indicator whose history can change.

    Tail deltas assume causality: a revision to bar t changes bar t alone. That
    is false for ZigZag, whose pivots move well back into history when a new
    extreme arrives, so such a chart resends in full (spec D10).
    """
    from app.ta.registry import get

    return any(get(req.name).repaints for pane in panes for req in pane.reqs)


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

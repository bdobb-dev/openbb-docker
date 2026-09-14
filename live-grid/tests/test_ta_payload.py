"""The one builder both routes call."""

import pytest

from app.ta.payload import ChartParams, bars_to_frame, build_payload, parse_indicators
from tests.ta_helpers import fixture_frame

BARS = [
    {"date": "2024-01-02", "open": 1.0, "high": 2.0, "low": 0.5,
     "close": 1.5, "adjusted_close": 1.48, "volume": 100},
    {"date": "2024-01-03", "open": 1.5, "high": 2.5, "low": 1.0,
     "close": 2.0, "adjusted_close": 1.97, "volume": 120},
]


def test_parse_indicators_reads_name_and_params():
    reqs = parse_indicators("rsi:period=14,sma:period=200")
    assert [r.name for r in reqs] == ["rsi", "sma"]
    assert reqs[0].params["period"] == 14 and reqs[1].params["period"] == 200


def test_parse_indicators_accepts_a_bare_name():
    assert parse_indicators("obv")[0].name == "obv"


def test_parse_indicators_coerces_floats_and_ints():
    params = parse_indicators("bbands:period=20:k=2.5")[0].params
    assert params["period"] == 20 and params["k"] == 2.5


def test_parse_indicators_ignores_blanks():
    assert parse_indicators(" , rsi , ") == parse_indicators("rsi")


def test_parse_indicators_rejects_an_unknown_name():
    with pytest.raises(KeyError, match="nope"):
        parse_indicators("nope")


def test_bars_to_frame_renames_adjusted_close():
    frame = bars_to_frame(BARS)
    assert "adj_close" in frame.columns and "adjusted_close" not in frame.columns


def test_bars_to_frame_falls_back_when_adjusted_close_is_absent():
    """kdb+ tick-derived bars carry no adjusted close; raw close stands in."""
    raw = [{k: v for k, v in b.items() if k != "adjusted_close"} for b in BARS]
    frame = bars_to_frame(raw)
    assert frame["adj_close"].to_list() == frame["close"].to_list()


def test_bars_to_frame_falls_back_when_adjusted_close_is_an_explicit_null():
    """A present-but-null adjusted_close must fall back exactly like an absent one.

    `.get(k, default)` does not fire on a null value. Providers return null
    adjusted_close for indices, forex and crypto, and 13 of the 22 indicators
    read that column.
    """
    nulled = [{**b, "adjusted_close": None} for b in BARS]
    frame = bars_to_frame(nulled)
    assert frame["adj_close"].to_list() == frame["close"].to_list()


def test_bars_to_frame_on_no_bars_has_the_full_schema():
    frame = bars_to_frame([])
    assert {"date", "open", "high", "low", "close", "adj_close", "volume"} <= set(frame.columns)


async def test_build_payload_from_a_macro_produces_stacked_panes():
    params = ChartParams(symbol="AAPL", macro="classic-momentum")
    fig, panes, _frame, _notes = await build_payload(params, fixture_frame())
    assert [p.id for p in panes] == ["price", "rsi", "macd", "vol"]
    assert fig["data"][0]["type"] == "candlestick"


async def test_build_payload_from_picks_alone_needs_no_macro():
    params = ChartParams(symbol="AAPL", indicators="rsi:period=14")
    _fig, panes, _, _ = await build_payload(params, fixture_frame())
    assert [p.id for p in panes] == ["price", "rsi"]


async def test_an_unknown_macro_name_is_rejected():
    with pytest.raises(KeyError, match="no-such-macro"):
        await build_payload(ChartParams(symbol="AAPL", macro="no-such-macro"),
                            fixture_frame())


async def test_source_local_makes_no_eodhd_calls():
    params = ChartParams(symbol="AAPL", indicators="rsi:period=14", source="local")

    class Boom:
        async def series(self, *a, **k):  # pragma: no cover
            raise AssertionError("source=local must not touch EODHD")

    fig, *_ = await build_payload(params, fixture_frame(), eodhd_source=Boom())
    assert fig["data"][0]["type"] == "candlestick"


def test_intraday_bars_keep_their_time_of_day():
    """widgets.json offers 1h/5m/1m and kdb tick bars carry a full timestamp.
    Truncated to 10 chars, a whole day of them plotted at one x."""
    bars = [{"date": f"2024-01-02T09:{m}:00", "open": 1.0, "high": 2.0,
             "low": 0.5, "close": 1.5, "adjusted_close": 1.5, "volume": 10}
            for m in (30, 35, 40, 45, 50, 55)]
    assert bars_to_frame(bars)["date"].n_unique() == 6


def test_parse_indicators_keeps_colons_inside_a_param_value():
    """avwap anchors are ISO timestamps -- the value's own colons must survive."""
    req = parse_indicators("avwap:anchor=2026-08-28T14:30:00")[0]
    assert req.name == "avwap"
    assert req.params["anchor"] == "2026-08-28T14:30:00"


def test_bars_to_frame_carries_trade_vwap_past_leading_history_nulls():
    """History bars have no vwap; the first tick bar's float must still fit
    (schema inference over leading Nones otherwise types the column Null)."""
    bars = [
        {"date": "2026-08-28T00:00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
        {"date": "2026-08-28T18:00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10, "vwap": 1.25},
    ]
    frame = bars_to_frame(bars)
    assert frame["vwap"].to_list() == [None, 1.25]


def test_with_anchor_folds_the_param_into_the_indicator_list():
    """The widget's anchor param is declarative click-anchoring: Workspace's
    chart viewer has no click channel back to this backend."""
    from app.ta.payload import with_anchor

    assert with_anchor("", "2026-08-28T14:30") == "avwap:anchor=2026-08-28T14:30"
    assert with_anchor("rsi:period=14", "2026-08-28T14:30") == (
        "avwap:anchor=2026-08-28T14:30,rsi:period=14"
    )
    assert with_anchor("rsi:period=14", "") == "rsi:period=14"
    explicit = "avwap:anchor=2026-08-28T09:30,rsi:period=14"
    assert with_anchor(explicit, "2026-08-28T14:30") == explicit  # explicit wins

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Tests for app.studies: the live grid's RSI(14) and anchored-VWAP columns.

Both ride one compute() call over one frame, so the grid's numbers cannot
drift from the chart's -- see the brief for why this task does not transcribe
the supplied Perspective prototype's Cutler RSI or its own kdb websocket.
"""

from datetime import date, datetime, timedelta

from app.studies import WINDOW_DAYS, parse_anchors, studies_for, window_start
from app.ta.compute import compute
from app.ta.payload import bars_to_frame
from app.ta.registry import resolve
from tests.ta_helpers import col


def _tick_bars(
    n: int, shock_at: int | None = None, shock: float = 80.0, base: float = 100.0
) -> list[dict]:
    """`n` one-minute tick-derived bars, each carrying a real trade vwap.

    `shock_at` inserts one large move at that bar -- the fixture the
    Wilder/Cutler test needs, since the two conventions only diverge once a
    single bar's move dwarfs the rest of the window.
    """
    bars = []
    price = base
    start = datetime(2026, 1, 1, 9, 30)
    for i in range(n):
        if shock_at is not None and i == shock_at:
            price += shock
        elif i > 0:
            price += 1.0 if i % 2 == 0 else -0.5
        ts = start + timedelta(minutes=i)
        bars.append({
            "date": ts.isoformat(), "open": price, "high": price, "low": price,
            "close": price, "volume": 100.0, "vwap": price,
        })
    return bars


def test_rsi_and_avwap_ride_the_same_computed_frame():
    """One compute() call, one frame, both columns — so the grid's RSI and
    the chart's RSI cannot disagree, and neither can the two AVWAPs."""
    frame = bars_to_frame(_tick_bars(60))
    reqs = [resolve("rsi", period=14), resolve("avwap", anchor=None)]
    out = compute(frame, reqs)
    assert out[col("rsi", period=14)][-1] is not None
    assert out[col("avwap")][-1] is not None


def test_rsi_is_wilders_not_cutlers():
    """Wilder smooths with alpha = 1/n; Cutler takes a flat mean. On a series
    with one large early move they diverge, which is what this pins — the
    supplied prototype uses Cutler's and this grid must not."""
    frame = bars_to_frame(_tick_bars(60, shock_at=5))
    wilder = compute(frame, [resolve("rsi", period=14)])[col("rsi", period=14)][-1]
    prices = frame["adj_close"].to_list()
    window = prices[-15:]
    gains = sum(max(b - a, 0) for a, b in zip(window, window[1:])) / 14
    losses = sum(max(a - b, 0) for a, b in zip(window, window[1:])) / 14
    cutlers = 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)
    assert abs(wilder - cutlers) > 0.5, "the two conventions must be distinguishable"


def test_a_symbol_with_too_little_history_reports_null_rsi_not_fifty():
    """The prototype returns 50.0 for a flat or short window. A null says
    "unknown"; 50 says "neutral", and a scanner sorted on RSI would rank
    every cold symbol in the middle of the pack."""
    assert studies_for("NEW", bars=[]) == {
        "symbol": "NEW", "rsi": None, "avwap": None, "avwap_dev": None,
        "vwap_start": None,
    }


def test_studies_for_matches_the_raw_compute_and_fills_avwap_dev():
    """studies_for is not a second implementation: it must return exactly
    what compute() over the same frame produces, plus the signed deviation
    the grid's bar needs and the raw compute() call does not provide."""
    bars = _tick_bars(60)
    frame = bars_to_frame(bars)
    reqs = [resolve("rsi", period=14), resolve("avwap", anchor=None)]
    out = compute(frame, reqs)
    expected_rsi = out[col("rsi", period=14)][-1]
    expected_avwap = out[col("avwap")][-1]
    expected_price = out["close"][-1]

    row = studies_for("AAPL", bars)

    assert row["symbol"] == "AAPL"
    assert row["rsi"] == expected_rsi
    assert row["avwap"] == expected_avwap
    assert row["avwap_dev"] == (expected_price - expected_avwap) / expected_avwap


def test_rsi_is_null_until_a_full_period_of_bars():
    """Wilder is an EWM, not a rolling window: `ewm_mean(alpha=1/n)` has no
    min_periods and yields a confident number from the SECOND bar, where
    `rolling_mean(14)` would stay null until the fourteenth.

    Found against the live stack, not in a fixture: the studies route asked
    for `1m` bars, the vendor returned DAILY history because kdb held no
    recorded ticks for the symbol, a five-day window therefore delivered four
    bars, and RSI(14) reported 78.5 off them. A plausible, authoritative,
    meaningless number is exactly what the null discipline exists to stop.
    """
    assert studies_for("SHORT", _tick_bars(4))["rsi"] is None
    assert studies_for("EDGE", _tick_bars(14))["rsi"] is None, "period bars is not enough"
    assert studies_for("OK", _tick_bars(15))["rsi"] is not None


def test_the_anchor_moves_the_anchored_vwap():
    """An anchored VWAP without an anchor is not one -- it is a cumulative
    mean from wherever the caller's window happens to start, so it moves when
    the window rolls and is not comparable between two symbols or two days.
    """
    bars = _tick_bars(60, shock_at=5)
    early = studies_for("X", bars, anchor="2026-01-01T09:30:00")["avwap"]
    late = studies_for("X", bars, anchor="2026-01-01T10:15:00")["avwap"]
    assert early is not None and late is not None
    assert early != late, "the anchor must select which trades are averaged"


def test_an_anchor_after_the_last_bar_yields_no_avwap():
    """Nothing has traded since, so there is nothing to average -- null, not
    the last price and not zero."""
    bars = _tick_bars(30)
    assert studies_for("X", bars, anchor="2030-01-01T00:00:00")["avwap"] is None


# --- parse_anchors: the Anchor column's wire format ---------------------


def test_parse_anchors_empty_is_all_default():
    assert parse_anchors("", "D") == ("D", {})


def test_parse_anchors_bare_iso_overrides_every_symbol():
    """One unkeyed timestamp is the grid-wide anchor -- "anchor everything at
    the open" is one cell edited, not fifty."""
    assert parse_anchors("2026-09-04T14:00:00", "D") == ("2026-09-04T14:00:00", {})


def test_parse_anchors_keyed_pairs_are_per_symbol():
    got = parse_anchors("NVDA=2026-09-04T14:00:00,BTC-USD=2026-09-04T13:30:00", "D")
    assert got == (
        "D",
        {"NVDA": "2026-09-04T14:00:00", "BTC-USD": "2026-09-04T13:30:00"},
    )


def test_parse_anchors_mixes_a_default_with_overrides():
    fallback, over = parse_anchors("2026-09-04T14:00:00,NVDA=2026-09-04T18:00:00", "D")
    assert fallback == "2026-09-04T14:00:00"
    assert over == {"NVDA": "2026-09-04T18:00:00"}


def test_parse_anchors_uppercases_the_symbol():
    """The grid's own symbols are upper-cased by _parse_symbols, so a lookup
    keyed on a lower-case cell would silently miss and use the default."""
    assert parse_anchors("nvda=2026-09-04T14:00:00", "D")[1] == {
        "NVDA": "2026-09-04T14:00:00"
    }


def test_parse_anchors_drops_an_unparseable_timestamp():
    """A typo in one row's cell must not blank the whole grid, and must not
    reach polars -- a bad anchor raises inside compute(), which is outside
    the per-symbol try, so it would 500 the entire request."""
    fallback, over = parse_anchors("NVDA=not-a-time,AAPL=2026-09-04T14:00:00", "D")
    assert fallback == "D"
    assert over == {"AAPL": "2026-09-04T14:00:00"}


def test_parse_anchors_drops_an_unparseable_bare_chunk():
    assert parse_anchors("garbage", "D") == ("D", {})


def test_parse_anchors_accepts_a_trailing_z():
    """The client sends UTC; `toISOString()` ends in Z, which
    datetime.fromisoformat rejected before 3.11 and _avwap_anchor rewrites."""
    assert parse_anchors("NVDA=2026-09-04T14:00:00Z", "D")[1] == {
        "NVDA": "2026-09-04T14:00:00Z"
    }


def test_parse_anchors_ignores_blank_chunks():
    """An empty cell contributes nothing -- the client sends only the rows
    that were edited, but a stray comma must not become a symbol."""
    assert parse_anchors(",NVDA=2026-09-04T14:00:00,,", "D")[1] == {
        "NVDA": "2026-09-04T14:00:00"
    }


def test_parse_anchors_last_wins_on_a_repeated_symbol():
    assert parse_anchors("NVDA=2026-09-04T14:00:00,NVDA=2026-09-04T15:00:00", "D")[1] == {
        "NVDA": "2026-09-04T15:00:00"
    }


# --- window_start: the fetch window has to reach past the anchor ------------

TODAY = date(2026, 9, 4)


def test_window_start_defaults_to_the_rsi_warmup_floor():
    assert window_start(f"{TODAY}T14:00:00", TODAY) == str(TODAY - timedelta(days=WINDOW_DAYS))


def test_window_start_stretches_back_to_cover_an_older_anchor():
    """An anchor before every bar fetched filters nothing, so the AVWAP would
    report a cumulative mean from the window's first bar while the column
    still claimed the anchor the user typed -- the same authoritative-and-
    wrong failure as an RSI off four bars."""
    assert window_start("2026-01-15T14:00:00", TODAY) == "2026-01-15"


def test_window_start_keeps_the_floor_for_a_recent_anchor():
    """Today's open must not shrink the window below RSI(14)'s warmup."""
    assert window_start(f"{TODAY}T00:00:00", TODAY) == str(TODAY - timedelta(days=WINDOW_DAYS))


def test_window_start_survives_an_unparseable_anchor():
    assert window_start("not-a-time", TODAY) == str(TODAY - timedelta(days=WINDOW_DAYS))


def test_window_start_handles_a_tz_aware_anchor():
    """A client sending `...Z` must widen the window the same as a naive one."""
    assert window_start("2026-01-15T14:00:00Z", TODAY) == "2026-01-15"


# --- vwap_start: the payload echoes the anchor it actually used -------------


def test_studies_echoes_the_anchor_in_vwap_start():
    """The cell must show what the server COMPUTED with, not what the client
    believes it asked for -- otherwise a dropped or defaulted anchor leaves
    the column claiming a time the number beside it never used."""
    anchor = "2026-01-01T09:35:00"
    got = studies_for("NVDA", _tick_bars(30), anchor)
    assert got["vwap_start"] == anchor


def test_blank_row_still_carries_vwap_start():
    """A symbol with no bars keeps every key, so `tableColumns` cannot drop
    the column on the first render."""
    got = studies_for("NVDA", [], "2026-01-01T09:35:00")
    assert got == {
        "symbol": "NVDA",
        "rsi": None,
        "avwap": None,
        "avwap_dev": None,
        "vwap_start": "2026-01-01T09:35:00",
    }


def test_vwap_start_is_none_when_unanchored():
    """No anchor is not the same as an anchor at the epoch -- the column
    renders blank rather than asserting a start it does not have."""
    assert studies_for("NVDA", _tick_bars(30))["vwap_start"] is None

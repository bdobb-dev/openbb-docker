# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Two-pass evaluation: shared sub-series are materialised exactly once."""

from datetime import datetime

import polars as pl
import pytest

from app.ta.compute import collect_bases, compute, compute_with_bases, session_columns
from app.ta.registry import Indicator, Req, get, resolve
from tests.ta_helpers import col, cols, fixture_frame


def test_two_indicators_sharing_a_base_collect_it_once():
    reqs = [resolve("bbands", period=20, k=2.0), resolve("sma", period=20)]
    bases = collect_bases(reqs)
    assert sorted(bases) == ["sma:adj_close:20", "std:adj_close:20"]


def test_differing_periods_are_not_collapsed():
    bases = collect_bases([resolve("sma", period=20), resolve("sma", period=50)])
    assert sorted(bases) == ["sma:adj_close:20", "sma:adj_close:50"]


def test_differing_price_basis_is_not_collapsed():
    """ATR reads raw, SMA reads adjusted. Merging them would be silently wrong."""
    bases = collect_bases([resolve("atr", period=14), resolve("sma", period=14)])
    assert "tr:raw:0" in bases and "sma:adj_close:14" in bases


def test_base_columns_are_dropped_from_the_public_result():
    out = compute(fixture_frame(), [resolve("bbands", period=20, k=2.0)])
    assert [c for c in out.columns if ":" in c] == []
    assert set(cols(resolve("bbands", period=20, k=2.0))) <= set(out.columns)


def test_original_bar_columns_survive():
    out = compute(fixture_frame(), [resolve("rsi", period=14)])
    assert {"date", "open", "high", "low", "close", "adj_close", "volume"} <= set(out.columns)


def test_empty_request_list_returns_the_frame_unchanged():
    df = fixture_frame()
    assert compute(df, []).columns == df.columns


def test_duplicate_requests_do_not_produce_duplicate_columns():
    out = compute(fixture_frame(), [resolve("sma", period=50), resolve("sma", period=50)])
    assert out.columns.count(col("sma", period=50)) == 1


def test_compute_with_bases_exposes_the_intermediate_columns():
    _, bases = compute_with_bases(fixture_frame(), [resolve("bbands", period=20, k=2.0)])
    assert len(bases) == 2


def test_a_frame_missing_adj_close_fails_loudly():
    df = fixture_frame().drop("adj_close")
    with pytest.raises(pl.exceptions.ColumnNotFoundError):
        compute(df, [resolve("sma", period=50)])


def test_two_periods_of_one_indicator_are_two_distinct_columns():
    """The 50/200 cross. Columns were named per indicator, requests dedup per
    (indicator, params), so the 200 was dropped as a duplicate of the 50."""
    out = compute(fixture_frame(), [resolve("sma", period=50), resolve("sma", period=200)])
    made = [c for c in out.columns if c.startswith("sma")]
    assert len(made) == 2, made
    fifty, two_hundred = (out[c].to_list() for c in made)
    assert fifty != two_hundred


def _fake_sessioned() -> Indicator:
    """A sessioned indicator that is NOT registered.

    REGISTRY is module-global, so registering a test-only indicator would
    leak into every other test module's view of it -- including the
    convention sweep. session_columns takes the Indicator directly, so it
    needs no registration.
    """
    return Indicator(
        name="_test_session", label="Test session",
        params={"anchor": "1d", "session_shift": 1},
        pane="price", price_basis="raw", convention="test only",
        deps=lambda p: [],
        build=lambda p, b: [((pl.col("H") + pl.col("L") + pl.col("C")) / 3).alias("mid")],
        render={"mid": {"type": "line", "color": None}},
        sessioned=True,
        session_agg=lambda p: {
            "H": pl.col("high").max(), "L": pl.col("low").min(),
            "C": pl.col("close").last(),
        },
    )


def _intraday(days: int = 3, per_day: int = 8) -> pl.DataFrame:
    rows = []
    for day in range(1, days + 1):
        for step in range(per_day):
            price = 100.0 + day * 10 + step
            rows.append({
                "date": datetime(2026, 1, day, 9 + step),
                "open": price, "high": price + 1, "low": price - 1,
                "close": price, "adj_close": price,
                "volume": 100.0, "vwap": price,
            })
    return pl.DataFrame(rows)


def test_session_columns_holds_the_prior_sessions_values_flat():
    ind = _fake_sessioned()
    out = session_columns(_intraday(), ind, Req("_test_session", dict(ind.params)))
    mid = out["mid"]
    # Day 1 has no prior session.
    assert all(v is None for v in mid[:8])
    # Day 2 holds one value across all eight of its bars.
    assert len(set(mid[8:16])) == 1 and mid[8] is not None
    # Day 3 holds a different one.
    assert mid[16] != mid[8]


def test_session_columns_returns_one_value_per_row_for_an_ascending_frame():
    frame = _intraday()
    ind = _fake_sessioned()
    out = session_columns(frame, ind, Req("_test_session", dict(ind.params)))
    assert len(out["mid"]) == frame.height


def test_session_columns_raises_rather_than_silently_mis_assign_an_unsorted_frame():
    """The returned lists are assigned straight onto the caller's frame by row
    position, so they must align with ITS rows. session_columns does not sort
    internally -- group_by_dynamic refuses unsorted input and raises here. If
    an internal sort were ever added, this call would succeed instead, and the
    now-reordered lists would be assigned onto the frame's original (unsorted)
    row order, silently mis-assigning every value."""
    frame = _intraday().reverse()
    ind = _fake_sessioned()
    with pytest.raises(pl.exceptions.InvalidOperationError, match="not sorted"):
        session_columns(frame, ind, Req("_test_session", dict(ind.params)))


def test_a_plain_indicator_declares_sessioned_false():
    assert get("sma").sessioned is False
    assert get("sma").session_agg is None


def test_session_columns_raises_when_build_omits_a_promised_render_column():
    """render promises 'mid' and 'phantom'; build only produces 'mid'. A
    silently-dropped output means a chart series never appears with no signal
    as to why -- this must fail loudly instead."""
    ind = _fake_sessioned()
    broken = Indicator(
        name="_test_session_broken", label="Broken", params=ind.params,
        pane=ind.pane, price_basis=ind.price_basis, convention=ind.convention,
        deps=ind.deps, build=ind.build,
        render={**ind.render, "phantom": {"type": "line", "color": None}},
        sessioned=True, session_agg=ind.session_agg,
    )
    with pytest.raises(ValueError, match="phantom"):
        session_columns(_intraday(), broken, Req("_test_session_broken", dict(broken.params)))

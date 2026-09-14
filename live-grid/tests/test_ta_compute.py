"""Two-pass evaluation: shared sub-series are materialised exactly once."""

import polars as pl
import pytest

from app.ta.compute import collect_bases, compute, compute_with_bases
from app.ta.registry import resolve
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

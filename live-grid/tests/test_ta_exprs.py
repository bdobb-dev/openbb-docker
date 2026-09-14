"""The Polars expression vocabulary: Base keys and the primitives they build."""

import polars as pl
import pytest

from app.ta.exprs import Base, price_col, true_range
from tests.ta_helpers import fixture_frame


def test_base_key_is_stable_and_distinguishes_price_basis():
    assert Base("ewm", "close", 12).key == "ewm:close:12"
    assert Base("ewm", "adj_close", 12).key != Base("ewm", "close", 12).key


def test_price_col_maps_basis_to_column_name():
    assert price_col("adjusted") == "adj_close"
    assert price_col("raw") == "close"


def test_price_col_rejects_an_unknown_basis():
    with pytest.raises(ValueError, match="price_basis"):
        price_col("split_adjusted")


def test_sma_matches_a_hand_rolled_mean():
    df = fixture_frame()
    out = df.with_columns(Base("sma", "close", 5).expr().alias("x"))
    closes = df["close"].to_list()
    assert out["x"][4] == pytest.approx(sum(closes[:5]) / 5, rel=1e-12)
    assert out["x"][3] is None  # warmup


def test_wilder_is_alpha_one_over_n_not_span():
    df = fixture_frame()
    out = df.with_columns([
        Base("wilder", "close", 14).expr().alias("w"),
        pl.col("close").ewm_mean(span=14, adjust=False).alias("span14"),
    ])
    assert out["w"][100] != pytest.approx(out["span14"][100], rel=1e-6)


def test_std_uses_population_ddof_zero():
    df = fixture_frame()
    out = df.with_columns([
        Base("std", "close", 20).expr().alias("pop"),
        pl.col("close").rolling_std(20, ddof=1).alias("sample"),
    ])
    assert out["pop"][50] < out["sample"][50]


def test_true_range_is_the_max_of_the_three_ranges():
    df = fixture_frame().with_columns(true_range().alias("tr"))
    row, prev = df.row(50, named=True), df.row(49, named=True)
    expected = max(row["high"] - row["low"],
                   abs(row["high"] - prev["close"]),
                   abs(row["low"] - prev["close"]))
    assert df["tr"][50] == pytest.approx(expected, rel=1e-12)


def test_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown Base kind"):
        Base("kalman", "close", 5).expr()

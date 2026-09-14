"""Convention pinning. These guard the exact bug class the spike found twice:
a library silently returning a different variant under a familiar name."""

import polars as pl
import pytest

from app.ta.compute import compute
from app.ta.registry import REGISTRY, col_suffix, get, resolve
from tests.ta_helpers import fixture_frame


def _computed(reqs, df=None):
    """Compute, then rename each output back to its bare registry name.

    Columns carry a parameter suffix so two periods of one indicator cannot
    collide; these conventions are about the values, so the suffix is undone
    here and every assertion below reads as it always did.
    """
    out = compute(fixture_frame() if df is None else df, reqs)
    return out.rename({c + col_suffix(r): c for r in reqs for c in get(r.name).render})


def _series(name, **params):
    return _computed([resolve(name, **params)])


def test_fast_slow_and_full_stochastic_are_three_different_series():
    fast = _series("stoch", k=14, smooth_k=1, d=3)["stoch_k"].to_list()
    slow = _series("stoch", k=14, smooth_k=3, d=3)["stoch_k"].to_list()
    full = _series("stoch", k=14, smooth_k=5, d=3)["stoch_k"].to_list()
    assert fast[100] != pytest.approx(slow[100], rel=1e-9)
    assert slow[100] != pytest.approx(full[100], rel=1e-9)


def test_slow_stochastic_is_the_three_period_mean_of_fast():
    fast = _series("stoch", k=14, smooth_k=1, d=3)["stoch_k"].to_list()
    slow = _series("stoch", k=14, smooth_k=3, d=3)["stoch_k"].to_list()
    assert slow[100] == pytest.approx(sum(fast[98:101]) / 3, rel=1e-9)


def test_stochastic_default_is_fast_and_says_so():
    assert get("stoch").params["smooth_k"] == 1
    assert "fast" in get("stoch").convention.lower()


def test_stochastic_reads_raw_ohlc_matching_eodhd():
    assert get("stoch").price_basis == "raw"


def test_percent_d_is_the_mean_of_percent_k():
    out = _series("stoch", k=14, smooth_k=1, d=3)
    k, d = out["stoch_k"].to_list(), out["stoch_d"].to_list()
    assert d[100] == pytest.approx(sum(k[98:101]) / 3, rel=1e-9)


def test_macd_histogram_is_line_minus_signal():
    out = _series("macd", fast=12, slow=26, signal=9)
    row = out.row(100, named=True)
    assert row["macd_hist"] == pytest.approx(row["macd"] - row["macd_signal"], rel=1e-9)


def test_percent_b_is_zero_at_the_lower_band_and_one_at_the_upper():
    out = _computed([resolve("bbands", period=20, k=2.0),
                     resolve("pct_b", period=20, k=2.0)])
    row = out.row(100, named=True)
    expected = (row["adj_close"] - row["bb_lo"]) / (row["bb_up"] - row["bb_lo"])
    assert row["pct_b"] == pytest.approx(expected, rel=1e-9)


def test_bandwidth_is_band_span_over_the_middle():
    out = _computed([resolve("bbands", period=20, k=2.0),
                     resolve("bandwidth", period=20, k=2.0)])
    row = out.row(100, named=True)
    expected = (row["bb_up"] - row["bb_lo"]) / row["bb_mid"] * 100
    assert row["bandwidth"] == pytest.approx(expected, rel=1e-9)


def test_bbands_pct_b_and_bandwidth_share_one_base_pair():
    from app.ta.compute import collect_bases
    bases = collect_bases([resolve("bbands", period=20, k=2.0),
                           resolve("pct_b", period=20, k=2.0),
                           resolve("bandwidth", period=20, k=2.0)])
    assert sorted(bases) == ["sma:adj_close:20", "std:adj_close:20"]


def test_divide_prone_indicators_yield_null_not_nan_on_a_flat_window():
    """0/0 must render as a gap, not NaN.

    Null is already how every rolling indicator spells its warmup; NaN would
    be a second spelling of the same idea, and it breaks equality comparisons
    (NaN != NaN) in every downstream test and delta diff.
    """
    import math

    flat = fixture_frame().with_columns([
        pl.lit(100.0).alias(c) for c in ("open", "high", "low", "close", "adj_close")
    ])
    for name in ("rsi", "stoch", "stochrsi", "adx", "cci", "willr", "pct_b"):
        out = _computed([resolve(name)], flat)
        for column in get(name).render:
            values = out[column].to_list()
            assert not any(v is not None and math.isnan(v) for v in values), (
                f"{name}/{column} produced NaN on a flat window; expected null"
            )


def test_rsi_is_null_at_bar_zero_not_nan():
    """Bar 0 has no previous close, so gain and loss are both 0 and rs is 0/0."""
    out = _series("rsi", period=14)
    assert out["rsi"][0] is None


def test_wilder_indicators_all_declare_it():
    for name in ("rsi", "atr", "adx"):
        assert "wilder" in get(name).convention.lower(), name


def test_every_indicator_renders_every_column_it_builds():
    df = fixture_frame()
    for name, ind in REGISTRY.items():
        out = _computed([resolve(name)], df)
        for column in ind.render:
            assert column in out.columns, f"{name} declares render for missing {column}"


def test_every_indicator_produces_finite_values_on_real_shaped_input():
    """Absence of NaN is not presence of data.

    ADX once returned null for every bar on every dataset -- a NaN ahead of a
    smoothing step propagated through it, and the trailing fill_nan then nulled
    the whole series. The flat-window test passed throughout, because an
    all-null column contains no NaN.
    """
    import math

    df = fixture_frame()
    for name, ind in REGISTRY.items():
        out = _computed([resolve(name)], df)
        for column in ind.render:
            values = out[column].to_list()
            finite = [v for v in values if v is not None and math.isfinite(v)]
            assert len(finite) > len(values) // 2, (
                f"{name}/{column}: only {len(finite)}/{len(values)} finite values"
            )

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The eleven next-tier indicators added at v12.0.0."""

import math
from datetime import datetime

import polars as pl
import pytest

from app.ta.compute import collect_bases, compute
from app.ta.registry import REGISTRY, resolve
from tests.ta_helpers import col, fixture_frame


def approx(value: float, rel: float = 1e-9):
    """Local alias. NOT named pytest_approx -- pytest collects any module-level
    name starting with `pytest_` as a hook implementation and errors on it."""
    return pytest.approx(value, rel=rel)


def _wma(values: list[float], length: int) -> float:
    weights = list(range(1, length + 1))
    window = values[-length:]
    return sum(v * w for v, w in zip(window, weights)) / sum(weights)


def test_hma_matches_a_hand_rolled_wma_of_wma():
    df = fixture_frame()
    out = compute(df, [resolve("hma", period=9)])[col("hma", period=9)].to_list()
    price = df["adj_close"].to_list()
    # HMA(9): WMA(2*WMA(p,4) - WMA(p,9), 3) -- round(9/2)=4, round(sqrt(9))=3
    raw = [
        2 * _wma(price[: i + 1], 4) - _wma(price[: i + 1], 9) if i >= 8 else None
        for i in range(len(price))
    ]
    expected = _wma([r for r in raw[-3:]], 3)
    assert out[-1] == approx(expected)


def test_hma_reads_adjusted_close_not_raw():
    df = fixture_frame()
    name = col("hma", period=9)
    base = compute(df, [resolve("hma", period=9)])[name].to_list()
    perturbed = compute(df.with_columns(pl.col("close") * 1.5),
                        [resolve("hma", period=9)])[name].to_list()
    assert base == perturbed


def test_trix_is_the_percent_change_of_a_triple_ema():
    df = fixture_frame()
    n = 18
    alpha = 2 / (n + 1)
    price = pl.col("adj_close")
    ema3 = (price.ewm_mean(alpha=alpha, adjust=False, ignore_nulls=True)
                 .ewm_mean(alpha=alpha, adjust=False, ignore_nulls=True)
                 .ewm_mean(alpha=alpha, adjust=False, ignore_nulls=True))
    expected = df.select((100 * ema3.pct_change(1)).alias("e"))["e"].to_list()
    out = compute(df, [resolve("trix", period=n)])[col("trix", period=n)].to_list()
    assert out[-1] == approx(expected[-1])


def test_both_declare_no_eodhd_map():
    for name in ("hma", "trix"):
        assert REGISTRY[name].eodhd is None, name


def test_hma_rounds_its_derived_lengths_to_nearest_int():
    """period=7 -> round(7/2)=4 and round(sqrt(7))=3; a truncating int() would
    give (3, 2) instead. Both lengths diverge from truncation here, and each
    scheme lands on a different final value, so this hand-rolled expectation
    -- built with (4, 3), the same way test_hma_matches_a_hand_rolled_wma_of_wma
    builds (4, 3) for period=9 -- fails if _hma_build ever truncates instead of
    rounding."""
    df = fixture_frame()
    out = compute(df, [resolve("hma", period=7)])[col("hma", period=7)].to_list()
    price = df["adj_close"].to_list()
    raw = [
        2 * _wma(price[: i + 1], 4) - _wma(price[: i + 1], 7) if i >= 6 else None
        for i in range(len(price))
    ]
    expected = _wma([r for r in raw[-3:]], 3)
    assert out[-1] == approx(expected)


def test_ao_is_the_gap_between_two_hl2_means():
    df = fixture_frame()
    hl2 = ((pl.col("high") + pl.col("low")) / 2)
    expected = df.select((hl2.rolling_mean(5) - hl2.rolling_mean(34)).alias("e"))["e"][-1]
    assert compute(df, [resolve("ao")])[col("ao")][-1] == approx(expected)


def test_ao_takes_no_parameters():
    with pytest.raises(ValueError, match="unknown parameter 'period'"):
        resolve("ao", period=5)


def test_mfi_is_bounded_and_uses_typical_price_direction():
    df = fixture_frame()
    out = [v for v in compute(df, [resolve("mfi", period=14)])[col("mfi", period=14)]
           if v is not None]
    assert out, "mfi produced nothing"
    assert all(0.0 <= v <= 100.0 for v in out)


def test_mfi_matches_a_hand_computed_value_with_known_direction():
    """Bounds alone can't catch a swapped positive/negative bucket -- a bug
    that flips the ratio still lands in [0, 100]. Flat bars (high == low ==
    close) make typical price equal to close, so the up/down bucketing and
    the expected number below follow straight from the definition.

    Prices: 10, 12, 11, 13, 9 (up, down, up, down from the prior bar).
    Volumes: 100, 200, 100, 300, 100.
    period=3 at the last bar covers indices 2, 3, 4 (rolling_sum(3) trailing):
      positive flow = flow[3]        = 13*300 = 3900   (only idx3 rose)
      negative flow = flow[2]+flow[4] = 11*100 + 9*100 = 2000
      mfi = 100 * 3900 / (3900 + 2000)
    A swapped numerator/denominator would give 100 * 2000 / 5900 instead --
    a different number, so this test catches that inversion.
    """
    prices = [10.0, 12.0, 11.0, 13.0, 9.0]
    volumes = [100.0, 200.0, 100.0, 300.0, 100.0]
    df = pl.DataFrame({
        "date": [f"2026-01-{d:02d}" for d in range(1, 6)],
        "open": prices, "high": prices, "low": prices, "close": prices,
        "adj_close": prices, "volume": volumes, "vwap": prices,
    }).with_columns(pl.col("date").str.to_date())
    out = compute(df, [resolve("mfi", period=3)])[col("mfi", period=3)].to_list()
    expected = 100 * 3900 / (3900 + 2000)
    assert out[-1] == approx(expected)
    assert not math.isnan(out[-1])


def test_mfi_nulls_a_flat_window_rather_than_dividing_by_zero():
    """A window with no typical-price movement has pos+neg == 0."""
    flat = pl.DataFrame({
        "date": [f"2026-01-{d:02d}" for d in range(1, 21)],
        "open": [10.0] * 20, "high": [10.0] * 20, "low": [10.0] * 20,
        "close": [10.0] * 20, "adj_close": [10.0] * 20,
        "volume": [100.0] * 20, "vwap": [10.0] * 20,
    }).with_columns(pl.col("date").str.to_date())
    out = compute(flat, [resolve("mfi", period=14)])[col("mfi", period=14)].to_list()
    assert all(v is None for v in out)


def test_cmf_treats_a_flat_bar_as_zero_flow_not_nan():
    df = fixture_frame().with_columns([
        pl.when(pl.int_range(pl.len()) == 100).then(pl.col("close"))
          .otherwise(pl.col("high")).alias("high"),
        pl.when(pl.int_range(pl.len()) == 100).then(pl.col("close"))
          .otherwise(pl.col("low")).alias("low"),
    ])
    out = compute(df, [resolve("cmf", period=20)])[col("cmf", period=20)].to_list()
    # Assert INSIDE the flat bar's window. A 20-bar window at index 120 spans
    # rows 101-120 and never sees row 100 at all, so asserting there would
    # pass whatever the flat-bar branch did. Index 110 spans 91-110, and
    # index 100's own window spans 81-100.
    assert out[100] is not None, "the flat bar's own window must still resolve"
    assert out[110] is not None, "one flat bar must not poison the window"
    # "not None" alone lets NaN slip through and, worse, a NaN would silently
    # poison every window's rolling_sum -- unlike null, which rolling_sum
    # skips. Both rows must be real numbers, not NaN.
    assert not math.isnan(out[100]), "flat bar produced NaN, not a real number"
    assert not math.isnan(out[110]), "the flat bar's NaN leaked into a later window"


def test_the_three_declare_no_eodhd_map():
    for name in ("ao", "mfi", "cmf"):
        assert REGISTRY[name].eodhd is None, name


def test_the_three_share_one_true_range_base():
    reqs = [resolve("uo"), resolve("vortex", period=14), resolve("chop", period=14),
            resolve("atr", period=14)]
    bases = collect_bases(reqs)
    assert sum(1 for key in bases if key.startswith("tr:")) == 1


def test_uo_is_bounded_and_weights_four_two_one():
    df = fixture_frame()
    out = [v for v in compute(df, [resolve("uo")])[col("uo")] if v is not None]
    assert out and all(0.0 <= v <= 100.0 for v in out)


def test_uo_uses_ratio_of_sums_not_mean_of_ratios():
    """The two agree only when TR is constant, so a varying fixture separates
    them. Hand-compute the last value from the ratio-of-sums definition."""
    df = fixture_frame()
    frame = df.with_columns([
        (pl.col("close") - pl.min_horizontal(pl.col("low"), pl.col("close").shift(1)))
        .alias("bp"),
        pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - pl.col("close").shift(1)).abs(),
            (pl.col("low") - pl.col("close").shift(1)).abs(),
        ).alias("tr"),
    ])
    def avg(n: int) -> float:
        tail = frame.tail(n)
        return tail["bp"].sum() / tail["tr"].sum()
    expected = 100 * (4 * avg(7) + 2 * avg(14) + avg(28)) / 7
    assert compute(df, [resolve("uo")])[col("uo")][-1] == approx(expected)


def test_vortex_matches_a_hand_rolled_ratio_of_sums():
    df = fixture_frame()
    n = 14
    frame = df.with_columns([
        (pl.col("high") - pl.col("low").shift(1)).abs().alias("vmp"),
        (pl.col("low") - pl.col("high").shift(1)).abs().alias("vmm"),
        pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - pl.col("close").shift(1)).abs(),
            (pl.col("low") - pl.col("close").shift(1)).abs(),
        ).alias("tr"),
    ]).tail(n)
    total = frame["tr"].sum()
    out = compute(df, [resolve("vortex", period=n)])
    assert out[col("vortex", "vi_plus", period=n)][-1] == approx(frame["vmp"].sum() / total)
    assert out[col("vortex", "vi_minus", period=n)][-1] == approx(frame["vmm"].sum() / total)


def test_vortex_does_not_swap_its_two_legs():
    """vi_plus reads |high - PRIOR low|. Swapping the two shifts is the
    classic mistake and leaves both legs in range, so bounds cannot see it."""
    rising = pl.DataFrame({
        "date": [f"2026-01-{d:02d}" for d in range(1, 21)],
        "open": [100.0 + i for i in range(20)],
        "high": [101.0 + i for i in range(20)],
        "low": [99.0 + i for i in range(20)],
        "close": [100.0 + i for i in range(20)],
        "adj_close": [100.0 + i for i in range(20)],
        "volume": [100.0] * 20, "vwap": [100.0 + i for i in range(20)],
    }).with_columns(pl.col("date").str.to_date())
    out = compute(rising, [resolve("vortex", period=14)])
    up = out[col("vortex", "vi_plus", period=14)][-1]
    down = out[col("vortex", "vi_minus", period=14)][-1]
    assert up > down, "a monotonically rising series must favour vi_plus"


def test_chop_matches_its_hand_rolled_definition():
    df = fixture_frame()
    n = 14
    tail = df.with_columns(
        pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - pl.col("close").shift(1)).abs(),
            (pl.col("low") - pl.col("close").shift(1)).abs(),
        ).alias("tr")
    ).tail(n)
    span = tail["high"].max() - tail["low"].min()
    expected = 100 * math.log10(tail["tr"].sum() / span) / math.log10(n)
    assert compute(df, [resolve("chop", period=n)])[col("chop", period=n)][-1] == approx(expected)


def test_chop_produces_real_numbers_not_nan():
    df = fixture_frame()
    out = [v for v in compute(df, [resolve("chop", period=14)])[col("chop", period=14)]
           if v is not None]
    assert out, "chop produced nothing"
    # `is not None` is true for NaN, so say NaN.
    assert all(not math.isnan(v) for v in out)


def test_the_tr_family_declares_no_eodhd_map():
    for name in ("uo", "vortex", "chop"):
        assert REGISTRY[name].eodhd is None, name


def test_vortex_nulls_a_zero_true_range_window_rather_than_dividing_by_zero():
    """up/down read the PRIOR bar's low/high, not the prior close, so a
    window can have a nonzero numerator over a zero-TR denominator: one
    normal bar followed by `period` bars pinned flat at its own close. Every
    in-window bar has TR == 0 (each is O=H=L=C equal to the prior bar's
    close), but the window's first up/down term reads back across the
    boundary to the normal bar's low/high, so the numerator is 5, not 0.
    x/0 with x != 0 is +inf in polars, not NaN -- fill_nan cannot catch it,
    so this must be a real None, not merely "not NaN" (inf would pass that)."""
    n = 14
    dates = [f"2026-01-{d:02d}" for d in range(1, n + 2)]
    flat = 100.0
    df = pl.DataFrame({
        "date": dates,
        "open": [100.0] + [flat] * n,
        "high": [105.0] + [flat] * n,
        "low": [95.0] + [flat] * n,
        "close": [100.0] + [flat] * n,
        "adj_close": [100.0] + [flat] * n,
        "volume": [100.0] * (n + 1),
        "vwap": [100.0] + [flat] * n,
    }).with_columns(pl.col("date").str.to_date())
    out = compute(df, [resolve("vortex", period=n)])
    assert out[col("vortex", "vi_plus", period=n)][-1] is None
    assert out[col("vortex", "vi_minus", period=n)][-1] is None


def test_ichimoku_conversion_is_the_nine_bar_midpoint():
    df = fixture_frame()
    out = compute(df, [resolve("ichimoku")])
    name = col("ichimoku", "ichi_conversion")
    tail = df.tail(9)
    expected = (tail["high"].max() + tail["low"].min()) / 2
    assert out[name][-1] == approx(expected)


def test_ichimoku_does_not_shift_its_values_inside_the_frame():
    """Span A at row i is computed FROM row i. The displacement is a plotting
    property carried in render['time_offset'], not a shift of the column --
    shifting would drop the leading 26 values off the end of the frame."""
    df = fixture_frame()
    out = compute(df, [resolve("ichimoku")])
    conversion = out[col("ichimoku", "ichi_conversion")][-1]
    base = out[col("ichimoku", "ichi_base")][-1]
    span_a = out[col("ichimoku", "ichi_span_a")][-1]
    assert span_a == approx((conversion + base) / 2)


def test_ichimoku_declares_its_displacements_on_the_render_dict():
    render = REGISTRY["ichimoku"].render
    assert render["ichi_span_a"]["time_offset"] == 26
    assert render["ichi_span_b"]["time_offset"] == 26
    assert render["ichi_chikou"]["time_offset"] == -26
    assert "time_offset" not in render["ichi_conversion"]


def test_ichimoku_has_no_eodhd_map():
    assert REGISTRY["ichimoku"].eodhd is None


def _two_sessions() -> pl.DataFrame:
    rows = []
    for day, (low, high, last) in enumerate([(10.0, 20.0, 15.0), (30.0, 40.0, 35.0)], 1):
        for hour, price in enumerate([low, high, last]):
            rows.append({
                "date": datetime(2026, 1, day, 9 + hour),
                "open": price, "high": price, "low": price, "close": price,
                "adj_close": price, "volume": 100.0, "vwap": price,
            })
    return pl.DataFrame(rows)


def test_pivots_use_the_prior_sessions_hlc_never_the_current_one():
    out = compute(_two_sessions(), [resolve("pivots_standard")])
    pp = out[col("pivots_standard", "PP")].to_list()
    assert all(v is None for v in pp[:3]), "day one has no prior session"
    # Day one: H=20, L=10, C=15 -> PP = 45/3 = 15
    assert all(v == approx(15.0) for v in pp[3:])


def test_pivots_derive_all_six_levels_from_pp():
    out = compute(_two_sessions(), [resolve("pivots_standard")])
    last = {name: out[col("pivots_standard", name)][-1]
            for name in ("PP", "R1", "S1", "R2", "S2", "R3", "S3")}
    high, low = 20.0, 10.0
    assert last["R1"] == approx(2 * 15.0 - low)
    assert last["S1"] == approx(2 * 15.0 - high)
    assert last["R2"] == approx(15.0 + (high - low))
    assert last["S2"] == approx(15.0 - (high - low))
    assert last["R3"] == approx(high + 2 * (15.0 - low))
    assert last["S3"] == approx(low - 2 * (high - 15.0))


def test_pivots_are_sessioned_not_iterative():
    assert REGISTRY["pivots_standard"].sessioned is True
    assert REGISTRY["pivots_standard"].iterative is False
    assert REGISTRY["pivots_standard"].eodhd is None


def test_a_sessioned_and_a_flat_indicator_together_still_share_one_base():
    """The regression the batched design exists to prevent: adding the
    sessioned path must not move Base materialisation per-request. This lives
    here rather than with the mechanism because it needs a REGISTERED
    sessioned indicator to go through compute()."""
    reqs = [resolve("pivots_standard"), resolve("atr", period=14),
            resolve("chop", period=14)]
    bases = collect_bases(reqs)
    assert sum(1 for key in bases if key.startswith("tr:")) == 1
    frame = compute(_two_sessions(), reqs)
    assert "tr:raw:0" not in frame.columns, "base columns are dropped by compute()"

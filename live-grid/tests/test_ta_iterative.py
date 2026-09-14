"""Path-dependent indicators: SAR cannot be a vectorised expression."""

from itertools import pairwise

from app.ta.compute import compute
from app.ta.iterative import parabolic_sar
from app.ta.registry import get, resolve
from tests.ta_helpers import col, fixture_frame


def test_sar_is_marked_iterative_not_vectorised():
    assert get("sar").iterative is True


def test_sar_produces_one_value_per_bar_after_the_first():
    df = fixture_frame()
    out = parabolic_sar(df, {"acceleration": 0.02, "maximum": 0.2})
    assert len(out["sar"]) == df.height
    assert out["sar"][0] is None
    assert all(v is not None for v in out["sar"][1:])


def test_sar_flows_through_compute_like_any_other_indicator():
    out = compute(fixture_frame(), [resolve("sar")])
    assert col("sar") in out.columns
    assert out.height == fixture_frame().height


def test_sar_stays_within_the_recent_price_range():
    df = fixture_frame()
    out = compute(df, [resolve("sar")])
    lo, hi = df["low"].min(), df["high"].max()
    vals = [v for v in out[col("sar")].to_list() if v is not None]
    assert min(vals) >= lo * 0.9 and max(vals) <= hi * 1.1


def test_a_faster_acceleration_flips_at_least_as_often():
    df = fixture_frame()

    def flips(accel):
        sar = parabolic_sar(df, {"acceleration": accel, "maximum": 0.2})["sar"]
        closes = df["close"].to_list()
        side = [c > s for c, s in zip(closes[1:], sar[1:])]
        return sum(a != b for a, b in pairwise(side))

    # Strict: `>=` also passes when `acceleration` is ignored entirely.
    assert flips(0.05) > flips(0.01)


def test_sar_combines_with_vectorised_indicators_in_one_call():
    out = compute(fixture_frame(), [resolve("sar"), resolve("rsi", period=14)])
    assert {col("sar"), col("rsi", period=14)} <= set(out.columns)


def test_an_empty_frame_yields_no_values():
    empty = fixture_frame().head(0)
    assert parabolic_sar(empty, {"acceleration": 0.02, "maximum": 0.2}) == {"sar": []}

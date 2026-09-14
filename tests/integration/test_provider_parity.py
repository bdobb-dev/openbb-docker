"""provider="arcticdb" must return the same 1-minute bars tick-lab computes.

Runs INSIDE the Platform image (it needs openbb + the arcticdb extension) and
asserts against the same golden CSV tick-lab asserts against. Requires a
reachable ArcticDB store configured through ARCTICDB_S3_* and the fixture
ticks already loaded into library 'ticks' as symbol 'MSFT'.

See tests/integration/README.md for the full setup (loading fixtures, then
bind-mounting this checkout and installing pytest for a one-off run --
neither the repo nor pytest ships in the image).

The provider does NO session filtering -- unlike tick-lab's own
`to_minute_bars(..., session="regular")` default, it returns every bar,
extended hours included. That is the "all" shape, so this compares against
golden_1m_bars_all.csv (5 bars: the pre-market 13:15Z print and the 20:00Z
closing print included), not golden_1m_bars.csv (3 bars, regular session
only) -- comparing against the regular-session golden here can never pass,
since the provider has no session parameter to filter with.
"""

import os
from pathlib import Path

import pandas as pd
import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("TICK_LAB_TEST_S3") != "1",
    reason="needs a reachable ArcticDB store; see tests/integration/README.md",
)

GOLDEN = Path(__file__).resolve().parents[2] / "tick-lab/tests/fixtures/golden_1m_bars_all.csv"


def load_golden() -> pd.DataFrame:
    golden = pd.read_csv(GOLDEN, index_col="timestamp")
    golden.index = pd.DatetimeIndex(golden.index).tz_convert("UTC")
    # Clear the index name exactly as tick-lab's own load_golden does. Both
    # sides must read this shared artifact identically -- that is the whole
    # point of pinning them to one file -- and a difference here would only
    # surface later, as a puzzling failure in whichever side gets extended
    # first to compare indexes rather than positions.
    golden.index.name = None
    return golden


def test_provider_returns_the_golden_bars():
    from openbb import obb

    result = obb.equity.price.historical(
        "MSFT",
        provider="arcticdb",
        library="ticks",
        interval="1m",
        start_date="2023-05-12",
        end_date="2023-05-12",
    )
    got = result.to_df()

    golden = load_golden()
    assert len(got) == len(golden), (
        f"provider returned {len(got)} bars, golden has {len(golden)}"
    )
    for column in ("open", "high", "low", "close", "volume"):
        pd.testing.assert_series_equal(
            got[column].reset_index(drop=True),
            golden[column].reset_index(drop=True),
            check_names=False,
            check_dtype=False,
        )

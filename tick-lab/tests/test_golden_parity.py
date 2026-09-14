"""tick-lab's roll-up must match the committed golden bars.

The same CSV is asserted against by tests/integration/test_provider_parity.py,
which runs provider="arcticdb" inside the Platform image. Pinning both sides to
one artifact is what makes the two environments comparable without either
importing the other.
"""

from pathlib import Path

import pandas as pd

from tick_lab.firstrate import parse
from tick_lab.rollup import to_minute_bars

FIXTURES = Path(__file__).parent / "fixtures"


def load_golden() -> pd.DataFrame:
    golden = pd.read_csv(FIXTURES / "golden_1m_bars.csv", index_col="timestamp")
    golden.index = pd.DatetimeIndex(golden.index).tz_convert("UTC")
    golden.index.name = None
    return golden


def load_golden_all() -> pd.DataFrame:
    golden = pd.read_csv(FIXTURES / "golden_1m_bars_all.csv", index_col="timestamp")
    golden.index = pd.DatetimeIndex(golden.index).tz_convert("UTC")
    golden.index.name = None
    return golden


def test_rollup_matches_the_golden_bars():
    _, trades = parse((FIXTURES / "MSFT_trades_sample.txt").read_text())
    bars = to_minute_bars(trades, session="regular")
    bars.index.name = None
    pd.testing.assert_frame_equal(bars, load_golden(), check_like=True)


def test_golden_excludes_premarket_and_the_closing_print():
    golden = load_golden()
    assert pd.Timestamp("2023-05-12 13:15:00Z") not in golden.index
    assert pd.Timestamp("2023-05-12 20:00:00Z") not in golden.index


def test_rollup_session_all_matches_the_golden_bars():
    """provider="arcticdb" does no session filtering, so it produces the
    'all' shape, not the 'regular' one -- this pins that shape to its own
    golden (golden_1m_bars_all.csv), the same file
    tests/integration/test_provider_parity.py compares against.
    """
    _, trades = parse((FIXTURES / "MSFT_trades_sample.txt").read_text())
    bars = to_minute_bars(trades, session="all")
    bars.index.name = None
    pd.testing.assert_frame_equal(bars, load_golden_all(), check_like=True)


def test_golden_all_includes_premarket_and_the_closing_print():
    golden = load_golden_all()
    assert pd.Timestamp("2023-05-12 13:15:00Z") in golden.index
    assert pd.Timestamp("2023-05-12 20:00:00Z") in golden.index
    assert len(golden) == len(load_golden()) + 2

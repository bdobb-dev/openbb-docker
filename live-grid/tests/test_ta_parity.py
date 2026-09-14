"""Local compute against EODHD's own numbers.

This is the oracle that makes hand-written indicators defensible. It is
deselected by default (`addopts = -m 'not network'`); run it deliberately:

    pytest tests/test_ta_parity.py -m network -v

Tolerance is 2e-4, not 1e-9: EODHD rounds its JSON to four decimals, which is
the measured error floor (spec S7, D13).
"""

import os
import statistics

import pytest

from app.ta.registry import REGISTRY, col_suffix, resolve
from app.ta.sources import EodhdSource, LocalSource, eodhd_query

pytestmark = pytest.mark.network

SYMBOL = "SPY.US"
TOLERANCE = 2e-4
RATE_BASED = {"sar": (0.99, 1e-6), "stochrsi": (0.99, 1e-5), "macd": (0.98, 1e-5)}
# EodhdSource sends no from/to, so EODHD computes over its FULL history and its
# indicators have long since converged, while ours start cold at the window's
# first bar. Triple-smoothed indicators converge far slower than the default
# warmup allows. Measured for adx: 120 bars -> max 1.98e-05 over comparable
# bars, but in the test's own alignment its first compared bar sits at
# 1.49e-03, decaying to 2e-5 later. A longer warmup is the honest fix; a rate
# assertion would also excuse genuine mid-series drift.
WARMUP = {"adx": 300}
DEFAULT_WARMUP = 120
CASES = [
    ("sma", {"period": 50}, ["sma"]),
    ("ema", {"period": 20}, ["ema"]),
    ("wma", {"period": 20}, ["wma"]),
    ("rsi", {"period": 14}, ["rsi"]),
    ("bbands", {"period": 20, "k": 2.0}, ["bb_up", "bb_mid", "bb_lo"]),
    ("atr", {"period": 14}, ["atr"]),
    ("macd", {"fast": 12, "slow": 26, "signal": 9}, ["macd", "macd_signal"]),
    ("stoch", {"k": 14, "smooth_k": 3, "d": 3}, ["stoch_k", "stoch_d"]),
    ("adx", {"period": 14}, ["adx"]),
    ("stddev", {"period": 20}, ["stddev"]),
    # SAR is the only hand-written imperative algorithm in the codebase; every
    # other indicator is a Polars primitive a reader can check by inspection.
    # It therefore needs this oracle more than anything else here, not less.
    ("sar", {"acceleration": 0.02, "maximum": 0.2}, ["sar"]),
    # stochrsi's scaling convention is unverified -- its own EodhdMap note says
    # so. This is where that gets settled.
    ("stochrsi", {"period": 14}, ["stochrsi"]),
]


@pytest.fixture(scope="module")
def api_key():
    key = os.getenv("EODHD_API_KEY")
    if not key:
        pytest.skip("EODHD_API_KEY not set")
    return key


@pytest.fixture(scope="module")
async def bars(api_key):
    """Two years of SPY daily bars straight from EODHD's EOD endpoint."""
    import httpx
    import polars as pl

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            f"https://eodhd.com/api/eod/{SYMBOL}",
            params={"from": "2023-01-01", "api_token": api_key, "fmt": "json"},
        )
        response.raise_for_status()
        rows = response.json()
    return pl.DataFrame([
        {"date": r["date"], "open": r["open"], "high": r["high"], "low": r["low"],
         "close": r["close"], "adj_close": r["adjusted_close"], "volume": float(r["volume"])}
        for r in rows
    # Datetime, not Date: that is what payload.bars_to_frame produces, and an
    # oracle built on a different shape from production stops testing the real
    # path -- including EodhdSource._join's date-part join key.
    ]).with_columns(pl.col("date").str.to_datetime(strict=False))


@pytest.mark.parametrize("name,params,columns", CASES, ids=[c[0] for c in CASES])
async def test_local_matches_eodhd(api_key, bars, name, params, columns):
    req = resolve(name, **params)
    # Output columns are named per (indicator, params); `columns` holds the
    # bare registry names, which is what the assertion messages should read.
    suffix = col_suffix(req)
    wanted = [c + suffix for c in columns]
    local = LocalSource().series(bars, [req]).frame
    remote = await EodhdSource(api_key).series(
        bars.drop([c for c in wanted if c in bars.columns]),
        [req], SYMBOL, "1d", str(bars["date"][-1]),
    )
    for column, actual in zip(columns, wanted):
        mine = local[actual].to_list()
        theirs = remote.frame[actual].to_list()
        warmup = WARMUP.get(name, DEFAULT_WARMUP)
        pairs = [(a, b) for a, b in zip(mine, theirs)
                 if a is not None and b is not None][warmup:]
        assert len(pairs) > 200, f"{name}/{column}: too few overlapping points"
        rels = [abs(a - b) / max(abs(b), 1e-9) for a, b in pairs]
        if name in RATE_BASED:
            min_rate, max_median = RATE_BASED[name]
            rate = sum(r < TOLERANCE for r in rels) / len(rels)
            median = statistics.median(rels)
            assert rate >= min_rate, (
                f"{name}/{column} only {rate:.1%} of bars within {TOLERANCE}"
            )
            assert median < max_median, (
                f"{name}/{column} median relative error {median:.2e} suggests "
                f"systematic drift, not isolated path-dependence"
            )
        else:
            worst = max(rels)
            assert worst < TOLERANCE, f"{name}/{column} max relative error {worst:.2e}"


def test_every_mapped_indicator_is_covered_by_a_parity_case():
    """A new EODHD mapping without a parity case is an untested claim."""
    mapped = {n for n, i in REGISTRY.items() if i.eodhd is not None}
    covered = {c[0] for c in CASES}
    assert mapped - covered == set(), sorted(mapped - covered)


def test_query_mapping_is_exercised_for_every_case():
    for name, params, _ in CASES:
        assert eodhd_query(resolve(name, **params))["function"]

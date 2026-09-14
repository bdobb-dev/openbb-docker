"""Shared loaders for TA tests."""

from pathlib import Path

import polars as pl

from app.ta.registry import col_suffix, get, resolve

FIXTURE = Path(__file__).parent / "fixtures" / "ohlcv.csv"


def fixture_frame() -> pl.DataFrame:
    """The committed 300-bar fixture, typed the way the engine expects.

    The csv predates the per-bar trade `vwap` column bars_to_frame now always
    carries, so a synthetic one is derived here -- it stands in for the value
    q records from real trades, it is not the indicator's formula.
    """
    return pl.read_csv(FIXTURE).with_columns([
        pl.col("date").str.to_date(),
        pl.col(["open", "high", "low", "close", "adj_close", "volume"]).cast(pl.Float64),
    ]).with_columns(
        ((pl.col("high") + pl.col("low") + pl.col("close")) / 3).alias("vwap")
    )


def cols(req) -> list[str]:
    """The columns this request lands in, suffixed by its parameters."""
    return [c + col_suffix(req) for c in get(req.name).render]


def col(name: str, base: str | None = None, **params) -> str:
    """One indicator output column: `col("sma", period=200)` -> sma|period=200."""
    return (base or next(iter(get(name).render))) + col_suffix(resolve(name, **params))

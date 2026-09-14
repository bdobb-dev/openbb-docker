"""The Polars expression vocabulary.

A `Base` is a shared sub-series that more than one indicator may need -- a
20-period mean of close, Wilder's average of true range. Indicators declare the
Bases they depend on and the compute pass materialises each one exactly once.

That deduplication is deliberate rather than delegated: Polars' common
subexpression elimination does fire on these subtrees, but measured at only
1.20x against the 2.06x available from declaring them (spec S3).

`col` carries the price basis in its name -- "close" vs "adj_close" -- so two
indicators reading different bases produce different Base keys and are never
collapsed into one. EODHD switches basis per indicator (spec S5), so this is
load-bearing, not tidiness.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

_BASIS_TO_COL = {"adjusted": "adj_close", "raw": "close"}


def price_col(basis: str) -> str:
    """The frame column an indicator with this price basis reads."""
    try:
        return _BASIS_TO_COL[basis]
    except KeyError:
        raise ValueError(
            f"unknown price_basis {basis!r}; expected one of {sorted(_BASIS_TO_COL)}"
        ) from None


def true_range() -> pl.Expr:
    """Wilder's true range, always on raw OHLC."""
    prev_close = pl.col("close").shift(1)
    return pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - prev_close).abs(),
        (pl.col("low") - prev_close).abs(),
    )


@dataclass(frozen=True)
class Base:
    """A deduplicable sub-series. `kind` selects the primitive, `col` the input."""

    kind: str
    col: str
    period: int

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.col}:{self.period}"

    def expr(self) -> pl.Expr:
        if self.kind == "tr":
            return true_range()
        c = pl.col(self.col)
        if self.kind == "sma":
            return c.rolling_mean(self.period)
        if self.kind == "ewm":
            return c.ewm_mean(span=self.period, adjust=False)
        if self.kind == "wilder":
            # Wilder's smoothing is alpha = 1/n, NOT span = n. RSI, ATR and ADX
            # all read wrong -- plausibly, not obviously -- if this slips.
            return c.ewm_mean(alpha=1 / self.period, adjust=False, ignore_nulls=True)
        if self.kind == "std":
            # Population, matching EODHD's Bollinger bands (spec S5).
            return c.rolling_std(self.period, ddof=0)
        if self.kind == "max":
            return c.rolling_max(self.period)
        if self.kind == "min":
            return c.rolling_min(self.period)
        raise ValueError(f"unknown Base kind {self.kind!r}")

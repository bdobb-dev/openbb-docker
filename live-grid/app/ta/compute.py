"""Evaluation in two passes.

Pass one materialises every distinct `Base` exactly once. Pass two evaluates
the indicator expressions, which reference those Bases by column name.

Doing this by hand rather than leaving it to Polars' optimiser is worth about
1.7x on a macro whose indicators share moving averages -- CSE captures 1.20x of
an available 2.06x (spec S3, D3).
"""

from __future__ import annotations

import polars as pl

from app.ta.exprs import Base
from app.ta.iterative import ITERATIVE
from app.ta.registry import Req, col_suffix, get


def collect_bases(reqs: list[Req]) -> dict[str, Base]:
    """Every Base the requests need, unique by key."""
    bases: dict[str, Base] = {}
    for req in reqs:
        for base in get(req.name).deps(req.params):
            bases[base.key] = base
    return bases


def compute_with_bases(
    df: pl.DataFrame, reqs: list[Req]
) -> tuple[pl.DataFrame, dict[str, Base]]:
    """Evaluate `reqs` against `df`, keeping the intermediate Base columns."""
    if not reqs:
        return df, {}
    bases = collect_bases(reqs)
    frame = df
    if bases:
        frame = frame.with_columns([b.expr().alias(b.key) for b in bases.values()])

    seen: set[str] = set()
    exprs: list[pl.Expr] = []
    for req in reqs:
        suffix = col_suffix(req)
        for expr in get(req.name).build(req.params, bases):
            name = expr.meta.output_name() + suffix
            if name in seen:
                continue  # the same indicator, same params, requested twice
            seen.add(name)
            exprs.append(expr.alias(name))
    if exprs:
        frame = frame.with_columns(exprs)

    # Path-dependent indicators run after the vectorised pass; they read the
    # frame rather than contributing expressions to it.
    for req in reqs:
        if not get(req.name).iterative:
            continue
        suffix = col_suffix(req)
        for column, values in ITERATIVE[req.name](frame, req.params).items():
            column += suffix
            if column not in frame.columns:
                frame = frame.with_columns(pl.Series(column, values, dtype=pl.Float64))

    return frame, bases


def compute(df: pl.DataFrame, reqs: list[Req]) -> pl.DataFrame:
    """Evaluate `reqs` against `df`. Base columns are an implementation detail."""
    frame, bases = compute_with_bases(df, reqs)
    return frame.drop([k for k in bases if k in frame.columns])

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

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
from app.ta.registry import Indicator, Req, col_suffix, get


def collect_bases(reqs: list[Req]) -> dict[str, Base]:
    """Every Base the requests need, unique by key."""
    bases: dict[str, Base] = {}
    for req in reqs:
        for base in get(req.name).deps(req.params):
            bases[base.key] = base
    return bases


def session_columns(
    frame: pl.DataFrame, ind: Indicator, req: Req
) -> dict[str, list]:
    """One value per session, held flat across every bar inside it.

    The frame is expected ASCENDING by date, which every other path in this
    engine already assumes -- parabolic_sar walks it in order too. Do NOT
    sort here: the returned lists are assigned straight onto the caller's
    frame by row position, so reordering them would mis-assign every value.
    group_by_dynamic requires the same ordering and raises without it.

    shift() is what makes today read YESTERDAY's aggregates. Without it every
    level is computed from the session it is drawn on, which is not a
    forecast, it is a lookahead.
    """
    every = str(req.params.get("anchor", "1d"))
    back = int(req.params.get("session_shift", 1))
    aggregates = ind.session_agg(req.params)

    sessions = (
        frame.group_by_dynamic("date", every=every, closed="left")
        .agg(**aggregates)
        .with_columns(**{name: pl.col(name).shift(back) for name in aggregates})
    )
    sessions = sessions.with_columns(ind.build(req.params, {}))
    missing = [name for name in ind.render if name not in sessions.columns]
    if missing:
        raise ValueError(
            f"{ind.name!r}: session build did not produce column(s) {missing} "
            f"promised by render"
        )
    outputs = list(ind.render)
    joined = frame.select("date").join_asof(
        sessions.select(["date", *outputs]), on="date", strategy="backward",
    )
    return {name: joined[name].to_list() for name in outputs}


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
        indicator = get(req.name)
        if indicator.sessioned:
            continue  # sessioned builds run against the session frame, below
        suffix = col_suffix(req)
        for expr in indicator.build(req.params, bases):
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

    # Session-anchored indicators run last, for the same reason the iterative
    # ones do: they read the frame rather than contributing expressions to it.
    for req in reqs:
        indicator = get(req.name)
        if not indicator.sessioned:
            continue
        suffix = col_suffix(req)
        for column, values in session_columns(frame, indicator, req).items():
            column += suffix
            if column not in frame.columns:
                frame = frame.with_columns(pl.Series(column, values, dtype=pl.Float64))

    return frame, bases


def compute(df: pl.DataFrame, reqs: list[Req]) -> pl.DataFrame:
    """Evaluate `reqs` against `df`. Base columns are an implementation detail."""
    frame, bases = compute_with_bases(df, reqs)
    return frame.drop([k for k in bases if k in frame.columns])

"""Bitemporal contract resolver and provenance for tick-vault query results
(task-8 brief; episode spec Sec 3, controller ruling).

`query_ticks(con, root, ...)` is the single public entry point client code
(backtests, notebooks, ad-hoc research) should use to read ticks out of the
lake: it resolves which of the four temporal modes a call is in (`GOLD`,
`PIT_KNOWLEDGE`, `BITEMPORAL`, `EFFECTIVE_ONLY`) from the `as_of`/`effective`
parameters, dispatches to the right `tick_vault.temporal` macro
(`latest_ticks()` / `pit_ticks(as_of)` / `pit_ticks_policy(as_of, policy)`),
optionally resolves vendor symbols to `listing_id`s via
`tick_vault.reference_queries.pit_listing(...)`, and returns a
`ContractResult` bundling the resulting `pandas.DataFrame` together with a
`provenance` dict that pins every version/policy string a downstream
backtest needs to record for reproducibility (per baseline
Sec 17/`ops.backtest_run_manifest`).

Mode resolution (episode spec Sec 3, controller ruling - the brief's
`query_ticks` signature lacked a `root` parameter; adding one is required so
this module can pin Delta table versions via `deltalake.DeltaTable(path).
version()`, which needs the table's on-disk path):

  - neither `as_of` nor `effective` given -> `GOLD`: `latest_ticks()`,
    current identity/universe (no PIT resolution - `pit_listing`'s
    `decision_ts` defaults to the far-future sentinel, so the *current*
    assignment wins).
  - `as_of` only -> `PIT_KNOWLEDGE`: `pit_ticks(as_of)`, or
    `pit_ticks_policy(as_of, availability_policy)` when
    `availability_policy != 'AS_INGESTED_LOCAL_V1'`. Each returned tick row
    already carries the `listing_id`/`instrument_id` that was correct as of
    that row's own `trade_date` (identity is a property of the versioned
    silver row, not something this resolver re-derives per row) - hence
    "per-row identity effective at the row's own trade_date".
  - both given -> `BITEMPORAL`: same data-availability macro as
    `PIT_KNOWLEDGE` (governed by `as_of`), but any symbol filtering is
    resolved against the universe/identity as it stood at `effective`
    (`pit_listing`'s `market_date` argument), not `end`.
  - `effective` only -> `EFFECTIVE_ONLY`: `latest_ticks()` (latest/most-
    corrected data), but identity/universe resolved as it stood at
    `effective` with the far-future decision-ts sentinel (i.e. "the lake's
    current belief about what was effective on `effective`", mirroring
    `current_sp500`/`pit_listing` with a maximal `decision_ts`).

Symbol resolution: for each requested symbol, `pit_listing(symbol,
market_date, decision_ts)` is called once, where `market_date = effective`
if `effective` is given else `end`, and `decision_ts = as_of` if `as_of` is
given else the far-future sentinel `TIMESTAMP '9999-12-31'`. A symbol that
resolves to no listing contributes a `"unresolved symbol: <symbol>"` warning
and is excluded from the `listing_id IN (...)` filter (not an error).

Validation: a future `as_of` (`as_of > now` UTC) is a `ValueError` - this is
checked by the pure `validate_as_of(as_of, now)` before anything else runs,
so it never depends on duckdb/deltalake being importable. An `as_of` earlier
than every tick's `available_at_ts` (a cheap `MIN(available_at_ts)` check
against `silver_us_trade_tick_version`) is NOT an error: it produces an
empty result plus a `"as_of predates earliest capture"` warning.

`duckdb` and `deltalake` are imported lazily (inside `query_ticks` and its
helpers) so this module - including the pure `resolve_mode`/`validate_as_of`
helpers - stays importable without either installed, matching the
`tick_vault.temporal` / `tick_vault.reference_queries` pattern (Tasks 6-7).
`pandas` is not imported at module scope here - the `df` field is typed by
docstring/comment only, matching this module's own lazy-import discipline;
`tick_vault.tick_parser` (which does import pandas at module scope) is not
imported by this module at all, only its `PARSER_VERSION` string constant.

This module is written test-first against deferred tests
(`tick-vault/tests/deferred/test_contract.py`, requiring duckdb/deltalake/
pyarrow/pytest - UNVERIFIED until those are installed); the only test
runnable in this sandbox (`tick-vault/tests/test_contract_static.py`)
py-compiles the module, imports it bare, and exercises `resolve_mode`/
`validate_as_of` directly.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from tick_vault.sl_conditions import DECODE_VERSION as _SL_DECODE_VERSION
from tick_vault.temporal import (
    POLICY_AS_INGESTED_LOCAL,
    POLICY_HISTORICAL_VENDOR_FINAL,
    SEQUENCE_POLICY as _SEQUENCE_POLICY,
)
from tick_vault.tick_parser import PARSER_VERSION as _PARSER_VERSION

# The four temporal modes `query_ticks` can resolve to. Kept as
# module-level string constants (rather than only inline literals) so
# `resolve_mode`'s return values and this set are the single source of
# truth, and so the static test can discover the exact string set from
# source without importing duckdb.
MODE_GOLD = "GOLD"
MODE_PIT_KNOWLEDGE = "PIT_KNOWLEDGE"
MODE_BITEMPORAL = "BITEMPORAL"
MODE_EFFECTIVE_ONLY = "EFFECTIVE_ONLY"

MODES = frozenset({MODE_GOLD, MODE_PIT_KNOWLEDGE, MODE_BITEMPORAL, MODE_EFFECTIVE_ONLY})

# Sentinel `decision_ts` used for `pit_listing(...)` calls when no `as_of`
# was given - i.e. "resolve against whatever is knowable right now", which
# is what `GOLD` and `EFFECTIVE_ONLY` mode want.
FAR_FUTURE_DECISION_TS = dt.datetime(9999, 12, 31, tzinfo=dt.timezone.utc)

# Delta tables whose version is always pinned in `provenance["delta_versions"]`,
# per the episode spec/controller ruling - these are the tables every mode's
# query can touch (tick data, index membership, symbol identity).
_PINNED_TABLES = (
    "silver.us_trade_tick_version",
    "silver.index_membership_version",
    "silver.identifier_assignment_version",
)


@dataclass
class ContractResult:
    """The result of a `query_ticks(...)` call: the tick rows plus enough
    provenance metadata to reproduce the query later (baseline Sec 17)."""

    df: "pandas.DataFrame"
    provenance: dict = field(default_factory=dict)


def resolve_mode(as_of, effective) -> str:
    """Pure mode resolution: which of the four temporal modes a
    `(as_of, effective)` pair selects. No I/O, no duckdb/deltalake -
    directly unit-testable without either installed."""
    if as_of is None and effective is None:
        return MODE_GOLD
    if as_of is not None and effective is None:
        return MODE_PIT_KNOWLEDGE
    if as_of is not None and effective is not None:
        return MODE_BITEMPORAL
    return MODE_EFFECTIVE_ONLY


def validate_as_of(as_of, now) -> None:
    """Raise `ValueError` if `as_of` is in the future relative to `now`.

    Pure and side-effect-free - both `as_of` and `now` are passed in (no
    hidden `datetime.now()` call) so this is directly unit-testable.
    `as_of=None` (no PIT requested) never raises.
    """
    if as_of is not None and as_of > now:
        raise ValueError(
            f"as_of ({as_of!r}) is in the future relative to now ({now!r})"
        )


def _pinned_delta_versions(root: str) -> dict:
    """Pin `deltalake.DeltaTable(path).version()` for every table in
    `_PINNED_TABLES`, keyed by their `SCHEMAS`-registry dotted name."""
    from deltalake import DeltaTable

    versions = {}
    for name in _PINNED_TABLES:
        layer, table = name.split(".", 1)
        path = f"{root}/{layer}/{table}"
        versions[name] = DeltaTable(path).version()
    return versions


def _build_provenance(
    *, mode: str, availability_policy: str, warnings: list, root: str, origin_flags: list
) -> dict:
    return {
        "mode": mode,
        "availability_policy_version": availability_policy,
        "sequence_policy_version": _SEQUENCE_POLICY,
        "sl_decode_version": _SL_DECODE_VERSION,
        "parser_version": _PARSER_VERSION,
        "delta_versions": _pinned_delta_versions(root),
        "warnings": list(warnings),
        "origin_flags": origin_flags,
    }


def _resolve_symbol_listing_ids(con, symbols, *, market_date, decision_ts, warnings: list) -> list:
    """Resolve each vendor symbol to its `listing_id`(s) via `pit_listing`,
    per-symbol. A symbol resolving to nothing appends an
    `"unresolved symbol: <symbol>"` warning and contributes no ids."""
    listing_ids: list = []
    for symbol in symbols:
        rows = con.execute(
            "SELECT listing_id FROM pit_listing(?, ?, ?)",
            [symbol, market_date, decision_ts],
        ).fetchall()
        if not rows:
            warnings.append(f"unresolved symbol: {symbol}")
            continue
        listing_ids.extend(row[0] for row in rows if row[0] is not None)
    return listing_ids


def query_ticks(
    con,
    root: str,
    *,
    symbols=None,
    start,
    end,
    as_of=None,
    effective=None,
    availability_policy: str = POLICY_AS_INGESTED_LOCAL,
) -> ContractResult:
    """Resolve and run a temporal tick query against the lake rooted at
    `root`, using the DuckDB connection `con`.

    `con` must be a DuckDB connection; this function (re-)runs
    `tick_vault.temporal.attach`/`install_macros` and
    `tick_vault.reference_queries.install_reference_macros` against it
    (all `CREATE OR REPLACE`, so this is safe to call repeatedly / on a
    connection some other caller has already set up).

    `start`/`end` bound `trade_date` (inclusive). `as_of`/`effective`
    select the temporal mode per this module's docstring. `symbols`, if
    given, is a vendor-symbol allowlist resolved via `pit_listing`.
    """
    from tick_vault.temporal import attach, install_macros
    from tick_vault.reference_queries import install_reference_macros

    now = dt.datetime.now(dt.timezone.utc)
    validate_as_of(as_of, now)

    mode = resolve_mode(as_of, effective)
    warnings: list = []

    attach(con, root)
    install_macros(con)
    install_reference_macros(con)

    if mode == MODE_GOLD and availability_policy != POLICY_AS_INGESTED_LOCAL:
        # GOLD mode always reads latest_ticks() - availability_policy only
        # governs which PIT-availability macro a PIT/BITEMPORAL query uses,
        # so a non-default policy passed alongside GOLD mode has no effect.
        # Surface that rather than silently ignoring it.
        warnings.append("availability_policy ignored in GOLD mode")

    if symbols is not None and len(symbols) == 0:
        # Explicit empty symbol list: nothing was requested, so nothing can
        # match - return an empty result with a warning rather than falling
        # through to the "no filter at all" behavior `None` gets.
        warnings.append("no symbols requested")
        df = con.execute(
            "SELECT * FROM silver_us_trade_tick_version WHERE FALSE"
        ).df()
        provenance = _build_provenance(
            mode=mode,
            availability_policy=availability_policy,
            warnings=warnings,
            root=root,
            origin_flags=[],
        )
        return ContractResult(df=df, provenance=provenance)

    # Cheap pre-capture check: an as_of earlier than the floor at which any
    # data could possibly be visible can never see any data - short-circuit
    # to an empty result with a warning rather than running the (still-
    # correct, but pointless) macro query. The floor itself depends on
    # availability_policy: under 'AS_INGESTED_LOCAL_V1' it's the earliest
    # actually-stored `available_at_ts`; under 'HISTORICAL_VENDOR_FINAL_V1'
    # availability is SIMULATED (never stored) as
    # `MIN(trade_date) + INTERVAL 1 DAY` at 08:00:00 UTC (mirroring
    # `tick_vault.temporal.pit_ticks_policy`'s own simulated-availability
    # formula) - using the stored `available_at_ts` floor here would wrongly
    # suppress data that policy considers available.
    if as_of is not None:
        if availability_policy == POLICY_HISTORICAL_VENDOR_FINAL:
            min_trade_date = con.execute(
                "SELECT MIN(trade_date) FROM silver_us_trade_tick_version"
            ).fetchone()[0]
            floor = (
                dt.datetime.combine(
                    min_trade_date, dt.time(8, 0), tzinfo=dt.timezone.utc
                )
                + dt.timedelta(days=1)
                if min_trade_date is not None
                else None
            )
            floor_warning = "as_of predates simulated availability floor"
        else:
            floor = con.execute(
                "SELECT MIN(available_at_ts) FROM silver_us_trade_tick_version"
            ).fetchone()[0]
            floor_warning = "as_of predates earliest capture"

        if floor is not None and as_of < floor:
            warnings.append(floor_warning)
            df = con.execute(
                "SELECT * FROM silver_us_trade_tick_version WHERE FALSE"
            ).df()
            provenance = _build_provenance(
                mode=mode,
                availability_policy=availability_policy,
                warnings=warnings,
                root=root,
                origin_flags=[],
            )
            return ContractResult(df=df, provenance=provenance)

    # Base relation: which macro supplies the (revision-resolved) tick rows.
    if mode in (MODE_GOLD, MODE_EFFECTIVE_ONLY):
        base_sql = "SELECT * FROM latest_ticks()"
        base_params: list = []
    elif availability_policy == POLICY_AS_INGESTED_LOCAL:
        base_sql = "SELECT * FROM pit_ticks(?)"
        base_params = [as_of]
    else:
        base_sql = "SELECT * FROM pit_ticks_policy(?, ?)"
        base_params = [as_of, availability_policy]

    # Symbol resolution: identity/universe is evaluated at `effective` if
    # given, else `end`; knowledge-time is `as_of` if given, else the
    # far-future sentinel (i.e. "whatever is knowable now").
    listing_ids = None
    if symbols:
        market_date = effective if effective is not None else end
        decision_ts = as_of if as_of is not None else FAR_FUTURE_DECISION_TS
        listing_ids = _resolve_symbol_listing_ids(
            con, symbols, market_date=market_date, decision_ts=decision_ts, warnings=warnings
        )

    sql = f"WITH base AS ({base_sql}) SELECT * FROM base WHERE trade_date BETWEEN ? AND ?"
    params = list(base_params) + [start, end]

    if symbols:
        if listing_ids:
            placeholders = ", ".join("?" for _ in listing_ids)
            sql += f" AND listing_id IN ({placeholders})"
            params.extend(listing_ids)
        else:
            # every requested symbol was unresolved - no rows can qualify.
            sql += " AND FALSE"

    df = con.execute(sql, params).df()

    origin_flags = (
        sorted(v for v in df["origin"].dropna().unique().tolist())
        if "origin" in df.columns
        else []
    )

    provenance = _build_provenance(
        mode=mode,
        availability_policy=availability_policy,
        warnings=warnings,
        root=root,
        origin_flags=origin_flags,
    )
    return ContractResult(df=df, provenance=provenance)

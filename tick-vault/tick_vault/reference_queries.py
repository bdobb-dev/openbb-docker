"""Point-in-time reference macros (DuckDB) for S&P 500 membership and
vendor-symbol identity resolution.

Transcribed from baseline design doc Sec 8.4's `pit_sp500_membership`
macro (bitemporal membership: effective interval on `market_date`,
knowledge-time visibility via `available_at_ts <= decision_ts` and an
open `system_to_ts`, restricted to `resolution_status = 'RESOLVED'`), and
extended per the task-7 brief to:

  - `pit_listing(symbol, market_date, decision_ts)`: the identity-side
    analogue of `pit_sp500_membership` - resolves a vendor symbol to
    `(listing_id, instrument_id)` via `silver.identifier_assignment_
    version`, using the same effective-interval + knowledge-time predicate
    shape, against `id_namespace IN ('EODHD_SYMBOL', 'TICKER')` rows whose
    `id_value` matches the requested symbol.
  - `current_sp500(market_date)`: the "effective-only" mode explicitly
    contemplated by the task-7 brief - the same effective-interval and
    `resolution_status = 'RESOLVED'` predicates as `pit_sp500_membership`,
    but with NO knowledge-time predicate at all (no `available_at_ts`, no
    `system_to_ts` check) - i.e. "what does the lake say right now about
    membership effective on `market_date`", not "what was knowable as of
    some `decision_ts`".

`duckdb` is imported lazily (inside `install_reference_macros`) so this
module can be imported without duckdb installed - duckdb is not
installable in the authoring sandbox (network blocked), matching the
`tick_vault.temporal` pattern (Task 6). This module is written test-first
against deferred tests (`tick-vault/tests/deferred/test_reference_
queries.py`); the only test runnable in this sandbox (`tick-vault/tests/
test_reference_sql_static.py`) checks this file's source text statically,
without importing duckdb.

No logic beyond DuckDB table-macro registration lives here by design.
"""
from __future__ import annotations


def install_reference_macros(con) -> None:
    """Register `pit_sp500_membership`, `pit_listing`, and `current_sp500`
    as DuckDB table macros against `silver_index_membership_version` and
    `silver_identifier_assignment_version`.

    Requires `tick_vault.temporal.attach()` to have already registered
    those views (i.e. `attach(con, root)` must run first).
    """
    import duckdb  # noqa: F401  (imported lazily; ensures duckdb is present)

    # Baseline Sec 8.4, transcribed verbatim: DISTINCT (listing_id,
    # instrument_id) pairs from silver.index_membership_version where
    # market_date falls within [membership_effective_from,
    # membership_effective_to), the row was knowable by decision_ts
    # (available_at_ts <= decision_ts, system_to_ts open at decision_ts),
    # index_id = 'idx_sp500', and resolution_status = 'RESOLVED'.
    con.execute(
        """
        CREATE OR REPLACE MACRO pit_sp500_membership(
          market_date,
          decision_ts
        ) AS TABLE
        SELECT DISTINCT
          m.listing_id,
          m.instrument_id
        FROM silver_index_membership_version AS m
        WHERE m.index_id = 'idx_sp500'
          AND m.membership_effective_from <= market_date
          AND (
            m.membership_effective_to IS NULL
            OR m.membership_effective_to > market_date
          )
          AND m.available_at_ts <= decision_ts
          AND (
            m.system_to_ts IS NULL
            OR m.system_to_ts > decision_ts
          )
          AND m.resolution_status = 'RESOLVED';
        """
    )

    # `pit_listing(symbol, market_date, decision_ts)`: identity-side analogue
    # of pit_sp500_membership. Resolves a vendor symbol to (listing_id,
    # instrument_id) via silver.identifier_assignment_version, restricted to
    # id_namespace IN ('EODHD_SYMBOL', 'TICKER') rows whose id_value matches
    # the requested symbol, with the same effective-interval (against
    # market_date at 00:00 UTC) and knowledge-time (available_at_ts <=
    # decision_ts, system_to_ts open at decision_ts) predicates.
    con.execute(
        """
        CREATE OR REPLACE MACRO pit_listing(
          symbol,
          market_date,
          decision_ts
        ) AS TABLE
        SELECT DISTINCT
          a.listing_id,
          a.instrument_id
        FROM silver_identifier_assignment_version AS a
        WHERE a.id_namespace IN ('EODHD_SYMBOL', 'TICKER')
          AND a.id_value = symbol
          AND (
            a.effective_from_ts IS NULL
            OR a.effective_from_ts <= CAST(market_date AS TIMESTAMP WITH TIME ZONE)
          )
          AND (
            a.effective_to_ts IS NULL
            OR a.effective_to_ts > CAST(market_date AS TIMESTAMP WITH TIME ZONE)
          )
          AND a.available_at_ts <= decision_ts
          AND (
            a.system_to_ts IS NULL
            OR a.system_to_ts > decision_ts
          );
        """
    )

    # `current_sp500(market_date)`: the effective-only mode - same
    # effective-interval and resolution_status = 'RESOLVED' predicates as
    # pit_sp500_membership, but deliberately WITHOUT any knowledge-time
    # predicate (no available_at_ts, no system_to_ts check) - "what the lake
    # currently says was effective on market_date", not a point-in-time
    # knowledge reconstruction.
    con.execute(
        """
        CREATE OR REPLACE MACRO current_sp500(
          market_date
        ) AS TABLE
        SELECT DISTINCT
          m.listing_id,
          m.instrument_id
        FROM silver_index_membership_version AS m
        WHERE m.index_id = 'idx_sp500'
          AND m.membership_effective_from <= market_date
          AND (
            m.membership_effective_to IS NULL
            OR m.membership_effective_to > market_date
          )
          AND m.resolution_status = 'RESOLVED';
        """
    )

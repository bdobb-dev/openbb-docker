"""Temporal tick selection macros (DuckDB) for the tick-vault gold layer.

Transcribed from baseline design doc Sec 13.3's window-pattern selection
rule (row_number() over logical_tick_id, availability filter, then
anti-join tombstones whose cancellation is visible) and Sec 12.3's
synthetic tape ordering, plus the tick-vault episode spec's binding
semantics for `latest_ticks()` / `pit_ticks()` / `pit_ticks_policy()`
(task-6 brief and controller ruling):

  - `latest_ticks()`: per `logical_tick_id`, the row with the highest
    `revision_number`, EXCLUDING logical ticks whose latest revision is a
    tombstone (`is_cancelled`).
  - `pit_ticks(as_of)`: among versions with `available_at_ts <= as_of`,
    the highest `revision_number` per `logical_tick_id`; if that chosen
    version `is_cancelled`, the tick is excluded (a cancellation visible by
    `as_of` removes the tick; a cancellation not yet visible leaves the
    prior revision serving).
  - `pit_ticks_policy(as_of, policy)`:
      * `'AS_INGESTED_LOCAL_V1'` -> identical to `pit_ticks(as_of)`.
      * `'HISTORICAL_VENDOR_FINAL_V1'` -> availability is SIMULATED, never
        stored, as `trade_date + INTERVAL 1 DAY` at 08:00:00 UTC compared
        against `as_of`; revision selection is otherwise the same highest-
        `revision_number`-per-`logical_tick_id` / tombstone-exclusion rule.

`duckdb` is imported lazily (inside functions) so this module can be
imported without duckdb installed - duckdb is not installable in the
authoring sandbox (network blocked). This module is written test-first
against deferred tests (`tick-vault/tests/deferred/test_temporal.py`); the
only test runnable in this sandbox
(`tick-vault/tests/test_temporal_sql_static.py`) checks this file's source
text statically, without importing duckdb.

No logic beyond DuckDB view/macro registration lives here by design.
"""
from __future__ import annotations

# Baseline Sec 17 / episode spec: the sequencing policy used to derive
# `gold.us_trade_tape.synthetic_tape_seq` (baseline Sec 12.3).
SEQUENCE_POLICY = "EODHD_TS_MS_THEN_SOURCE_ORDER_V1"

# Availability policy version strings (silver.data_availability_policy /
# ops.backtest_run_manifest.availability_policy_version), per episode spec
# Sec 4.2 and task-6 brief.
POLICY_AS_INGESTED_LOCAL = "AS_INGESTED_LOCAL_V1"
POLICY_HISTORICAL_VENDOR_FINAL = "HISTORICAL_VENDOR_FINAL_V1"


def attach(con, root: str) -> None:
    """Register every table in `SCHEMAS` as a DuckDB view over `delta_scan`.

    Each `<schema>.<table>` registry entry (e.g. `silver.us_trade_tick_
    version`) is registered as a view named `<schema>_<table>` (underscores,
    e.g. `silver_us_trade_tick_version`) pointing at
    `delta_scan('<root>/<schema>/<table>')`, per the task-6 brief.
    """
    from tick_vault.schemas import SCHEMAS  # lazy: pulls in pyarrow

    for name in SCHEMAS:
        layer, table = name.split(".", 1)
        view_name = f"{layer}_{table}"
        table_path = f"{root}/{layer}/{table}"
        con.execute(
            f"CREATE OR REPLACE VIEW {view_name} AS "
            f"SELECT * FROM delta_scan('{table_path}')"
        )


def install_macros(con) -> None:
    """Create the `tape_order`, `latest_ticks`, `pit_ticks`, and
    `pit_ticks_policy` macros against `silver_us_trade_tick_version`.

    Requires `attach()` to have already registered
    `silver_us_trade_tick_version` (and, for `tape_order`'s optional venue
    priority join, whatever venue-priority relation the caller passes in).
    """
    import duckdb  # noqa: F401  (imported lazily; ensures duckdb is present)

    # Baseline Sec 12.3's synthetic tape ordering: ORDER BY (trade_ts_ms,
    # vendor_sequence_no nulls-last presence rule, vendor_sequence_no,
    # venue_priority, observed_at_ts, source_page_ordinal,
    # source_row_ordinal, tick_version_id). `venue_priority` is resolved via
    # COALESCE against an optional joined relation (default: an empty
    # derived table, so priority defaults to 0), kept simple per the
    # controller ruling.
    con.execute(
        """
        CREATE OR REPLACE MACRO tape_order(
            venue_priority_relation := (
                SELECT NULL::VARCHAR AS venue_id, 0 AS venue_priority
                WHERE FALSE
            )
        ) AS TABLE
        SELECT
            t.* EXCLUDE (venue_priority),
            COALESCE(vp.venue_priority, 0) AS venue_priority
        FROM (SELECT *, NULL AS venue_priority FROM silver_us_trade_tick_version) AS t
        LEFT JOIN venue_priority_relation AS vp
            ON vp.venue_id = t.venue_id
        ORDER BY
            t.trade_ts_ms,
            (t.vendor_sequence_no IS NULL),
            t.vendor_sequence_no,
            COALESCE(vp.venue_priority, 0),
            t.observed_at_ts,
            t.source_page_ordinal,
            t.source_row_ordinal,
            t.tick_version_id;
        """
    )

    # `latest_ticks()`: highest revision_number per logical_tick_id via a
    # row_number() window, then exclude logical ticks whose *latest*
    # revision is a tombstone (is_cancelled).
    con.execute(
        """
        CREATE OR REPLACE MACRO latest_ticks() AS TABLE
        WITH ranked AS (
            SELECT
                t.*,
                row_number() OVER (
                    PARTITION BY t.logical_tick_id
                    ORDER BY t.revision_number DESC
                ) AS rn
            FROM silver_us_trade_tick_version AS t
        )
        SELECT * EXCLUDE (rn)
        FROM ranked
        WHERE rn = 1
          AND COALESCE(is_cancelled, FALSE) = FALSE;
        """
    )

    # `pit_ticks(as_of)`: among versions with available_at_ts <= as_of,
    # highest revision_number per logical_tick_id (row_number window over
    # the availability-filtered set), then exclude the tick if the chosen
    # (as-of-visible) revision is a tombstone.
    con.execute(
        """
        CREATE OR REPLACE MACRO pit_ticks(as_of) AS TABLE
        WITH visible AS (
            SELECT *
            FROM silver_us_trade_tick_version
            WHERE available_at_ts <= as_of
        ),
        ranked AS (
            SELECT
                v.*,
                row_number() OVER (
                    PARTITION BY v.logical_tick_id
                    ORDER BY v.revision_number DESC
                ) AS rn
            FROM visible AS v
        )
        SELECT * EXCLUDE (rn)
        FROM ranked
        WHERE rn = 1
          AND COALESCE(is_cancelled, FALSE) = FALSE;
        """
    )

    # `pit_ticks_policy(as_of, policy)`:
    #   'AS_INGESTED_LOCAL_V1'        -> identical to pit_ticks(as_of).
    #   'HISTORICAL_VENDOR_FINAL_V1'  -> availability SIMULATED (never
    #       stored) as trade_date + INTERVAL 1 DAY at 08:00:00 UTC,
    #       compared against as_of; same highest-revision-per-
    #       logical_tick_id / tombstone-exclusion selection.
    con.execute(
        """
        CREATE OR REPLACE MACRO pit_ticks_policy(as_of, policy) AS TABLE
        WITH visible AS (
            SELECT *
            FROM silver_us_trade_tick_version
            WHERE
                (
                    policy = 'AS_INGESTED_LOCAL_V1'
                    AND available_at_ts <= as_of
                )
                OR
                (
                    policy = 'HISTORICAL_VENDOR_FINAL_V1'
                    AND (
                        CAST(trade_date AS TIMESTAMP WITH TIME ZONE)
                        + INTERVAL 1 DAY
                        + INTERVAL '08:00:00' HOUR TO SECOND
                    ) <= as_of
                )
        ),
        ranked AS (
            SELECT
                v.*,
                row_number() OVER (
                    PARTITION BY v.logical_tick_id
                    ORDER BY v.revision_number DESC
                ) AS rn
            FROM visible AS v
        )
        SELECT * EXCLUDE (rn)
        FROM ranked
        WHERE rn = 1
          AND COALESCE(is_cancelled, FALSE) = FALSE;
        """
    )

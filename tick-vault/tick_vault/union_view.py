"""Transitional union-view routing over silver ticks and legacy `ticks_sip`
(task-9 brief; episode spec Sec 4, preflight controller ruling).

During the migration off the legacy per-symbol `ticks_sip/<SYM>` Delta
tables (one table per symbol, partitioned by `day` (string `YYYY-MM-DD`),
columns `date` (naive-UTC timestamp index), `price`, `size`, `mkt`,
`sub_mkt`, `seq`, `sl` - the layout `scripts/sip_backfill.py` wrote), a
symbol's data is served from two places depending on calendar week:

  - weeks that have been re-downloaded and versioned into
    `silver.us_trade_tick_version` are served from silver (full
    bitemporal/PIT machinery, `origin='CAPTURE'` etc.);
  - weeks that have NOT yet been re-downloaded are still served from the
    legacy table, tagged `origin='LEGACY_UNVERSIONED'`.

The boundary is per-symbol: `watermarks: dict[str, datetime.date]` maps
`SYMBOL -> first re-downloaded week-monday`. Dates `>= watermark[symbol]`
are served from silver; dates `< watermark[symbol]` are served from
legacy. A symbol absent from `watermarks` has not been re-downloaded at
all and is served entirely from legacy (there is nothing for it in
silver yet - `silver.us_trade_tick_version` is only ever written for
symbols under active watermark tracking, so this is not a special case,
just the natural result of the join in `vault_trade_tick` below).

`install_union_view(con, root, legacy_root, watermarks)` registers:

  1. `union_watermarks(symbol VARCHAR, watermark DATE)` - a TEMP VIEW built
     from the `watermarks` dict via a `VALUES` list (an always-empty
     `WHERE FALSE` view when the dict is empty), mirroring
     `tick_vault.temporal.install_macros`'s `venue_priority` convention-
     table pattern (fix-round note in that module: DuckDB macros can't
     bind a relation-valued parameter, so real registered views are used
     instead).
  2. `legacy_trade_tick` - a view unioning one `delta_scan(...)` per
     symbol in `watermarks`, each projected into the exact column set of
     `silver.us_trade_tick_version` (NULL where legacy has no equivalent
     column) with the vendor symbol injected as a literal. A symbol whose
     legacy table path (`<legacy_root>/<SYMBOL>`) does not exist on disk
     is skipped (not every watermarked symbol necessarily has legacy
     history - e.g. a symbol added to coverage only after the migration
     started); skipped symbols are recorded on the returned handle
     (`UnionViewHandle.skipped_symbols`), not raised as an error.
  3. `vault_trade_tick` - `UNION ALL` of silver rows with
     `trade_date >= watermark` (joined to `union_watermarks` via
     `vendor_request_symbol`, which every silver row carries - a symbol
     absent from `union_watermarks` contributes zero silver rows, which
     is exactly "serve entirely from legacy") and legacy rows with
     `trade_date < watermark` (or all legacy rows for a symbol without a
     matching watermark row, which cannot occur here since
     `legacy_trade_tick` is only ever built from `watermarks`' own keys,
     but the `COALESCE` keeps the view correct even if that invariant is
     ever relaxed).

`install_union_view` returns a `UnionViewHandle` bundling the watermarks
dict actually installed plus the list of skipped symbols; its
`union_provenance(symbol)` method is the "displacement frontier" report
the task-9 brief asks for (kept as a handle method rather than module-
level state, per the controller ruling, so multiple `install_union_view`
calls against different connections/roots in the same process don't
clobber each other).

Union-mode PIT semantics (preflight controller ruling): `vault_trade_tick`
itself carries NO revision-resolution or availability filtering - it is
the raw row-level union. `tick_vault.contract.query_ticks` is responsible
for applying the same "highest revision_number per logical_tick_id,
excluding a tombstoned latest revision" selection rule
(`tick_vault.temporal.latest_ticks`/`pit_ticks`/`pit_ticks_policy`'s own
rule) on top of `vault_trade_tick`, via parallel SQL kept in
`contract.py` itself (not by rewriting `tick_vault.temporal`'s macros,
which target `silver_us_trade_tick_version` directly and have no
knowledge of the union). The PIT-mode rule for legacy
(`origin='LEGACY_UNVERSIONED'`) rows specifically:

  - `'AS_INGESTED_LOCAL_V1'`: legacy rows are NEVER visible under this
    policy, at any `as_of`. They have no real capture timestamp
    (`available_at_ts IS NULL` - nothing was ever actually ingested with
    a recorded arrival time), so "as-ingested" knowledge-time semantics
    have nothing honest to say about them; the correct answer is an
    empty contribution from legacy, not an unbounded/always-available one.
  - `'HISTORICAL_VENDOR_FINAL_V1'`: legacy rows ARE visible once the same
    SIMULATED availability floor silver rows use under this policy
    (`trade_date + INTERVAL 1 DAY` at `08:00:00` UTC `<= as_of`) is
    crossed - the simulation is a statement about when the market/vendor
    print became knowable in principle, which applies identically
    regardless of which system captured the row.
  - `latest`/`GOLD`/`EFFECTIVE_ONLY` modes (no `as_of`): legacy rows serve
    as-is, with no availability gating at all (mirroring
    `latest_ticks()`'s "no `as_of`, so no availability filter" behavior).

`duckdb` is imported lazily (inside `install_union_view` and its
helpers), matching the `tick_vault.temporal` / `tick_vault.reference_
queries` / `tick_vault.contract` lazy-import discipline, so this module
stays importable without duckdb installed. This module is written test-
first against deferred tests (`tick-vault/tests/deferred/
test_union_view.py`, requiring duckdb/deltalake/pyarrow/pytest -
UNVERIFIED until those are installed); the only test runnable in this
sandbox (`tick-vault/tests/test_union_view_static.py`) py-compiles the
module, imports it bare, and checks its source text statically.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Legacy rows always carry this origin literal - "unversioned" because the
# legacy `ticks_sip` tables have no revision/correction/tombstone tracking
# at all (unlike silver.us_trade_tick_version's bitemporal versioning).
LEGACY_ORIGIN = "LEGACY_UNVERSIONED"

# The full column set of `silver.us_trade_tick_version`
# (tick_vault.schemas._SILVER_US_TRADE_TICK_VERSION), in schema order.
# `vault_trade_tick` and `legacy_trade_tick` both project into exactly
# this column list so `UNION ALL` lines up positionally and by name.
SILVER_COLUMNS = [
    "tick_version_id",
    "logical_tick_id",
    "source_system",
    "source_capture_id",
    "source_page_ordinal",
    "source_row_ordinal",
    "listing_id",
    "instrument_id",
    "vendor_request_symbol",
    "eodhd_exchange_code",
    "trade_date",
    "trade_ts_ms",
    "trade_ts",
    "venue_code_raw",
    "sub_mkt_raw",
    "session_seq",
    "venue_id",
    "mic",
    "price_venue_type",
    "price",
    "size",
    "sale_condition_raw",
    "sale_condition_flags",
    "origin",
    "vendor_trade_id",
    "vendor_sequence_no",
    "observed_at_ts",
    "committed_at_ts",
    "available_at_ts",
    "revision_number",
    "is_correction",
    "is_cancelled",
    "is_late_add",
    "supersedes_tick_version_id",
    "payload_hash",
    "record_hash",
    "parser_version",
]


@dataclass
class UnionViewHandle:
    """The result of `install_union_view(...)`: the watermarks that were
    actually installed, the symbols whose legacy table was skipped (path
    did not exist), plus the `union_provenance` reporting method.

    A handle (rather than module-level state) so multiple
    `install_union_view` calls - e.g. against different connections/roots
    in the same process, or a rollback re-install with a new watermarks
    dict - never clobber each other's provenance.
    """

    watermarks: dict = field(default_factory=dict)
    skipped_symbols: list = field(default_factory=list)

    def union_provenance(self, symbol: str) -> dict:
        """The "displacement frontier" report for `symbol`: the watermark
        date (or `None` if the symbol has no watermark at all), and a
        `serving` summary of which source(s) actually contribute rows for
        it right now.

          - a watermark is set: `{"silver_from": watermark,
            "legacy_before": watermark}` - both sources contribute,
            split at the watermark.
          - no watermark, but the symbol's legacy table was skipped
            (path did not exist): `"silver_only"` - there is no legacy
            history behind the (nonexistent) watermark, so only whatever
            silver has (if anything) is real.
          - no watermark, legacy table present (or never attempted):
            `"legacy_only"` - the symbol has not been re-downloaded, so
            silver contributes nothing for it (per `vault_trade_tick`'s
            join semantics) and legacy serves everything.
        """
        watermark = self.watermarks.get(symbol)
        if watermark is not None:
            serving = {"silver_from": watermark, "legacy_before": watermark}
        elif symbol in self.skipped_symbols:
            serving = "silver_only"
        else:
            serving = "legacy_only"
        return {"symbol": symbol, "frontier": watermark, "serving": serving}


def _install_watermarks_view(con, watermarks: dict) -> None:
    """Register `union_watermarks(symbol VARCHAR, watermark DATE)` as a
    TEMP VIEW - a real relation (not a macro parameter; see module
    docstring / `tick_vault.temporal.install_macros`'s `venue_priority`
    precedent) so it can be joined against directly."""
    if watermarks:
        values_sql = ", ".join(
            f"({symbol!r}, DATE '{watermark.isoformat()}')"
            for symbol, watermark in watermarks.items()
        )
        con.execute(
            f"""
            CREATE OR REPLACE TEMP VIEW union_watermarks(symbol, watermark) AS
            SELECT * FROM (VALUES {values_sql});
            """
        )
    else:
        con.execute(
            """
            CREATE OR REPLACE TEMP VIEW union_watermarks(symbol, watermark) AS
            SELECT NULL::VARCHAR AS symbol, NULL::DATE AS watermark
            WHERE FALSE;
            """
        )


def _legacy_symbol_select(legacy_root: str, symbol: str) -> str:
    """The projection of one legacy `<legacy_root>/<symbol>` Delta table
    into `SILVER_COLUMNS`, with the vendor symbol injected as a literal
    (the legacy table has no symbol column of its own - it's implicit in
    the table's path) and every silver-only column NULLed out."""
    path = f"{legacy_root}/{symbol}"
    return f"""
        SELECT
            NULL::VARCHAR AS tick_version_id,
            {symbol!r} || '|' || CAST(CAST(day AS DATE) AS VARCHAR) || '|' || CAST(seq AS VARCHAR) AS logical_tick_id,
            'LEGACY_TICKS_SIP' AS source_system,
            NULL::VARCHAR AS source_capture_id,
            NULL::BIGINT AS source_page_ordinal,
            seq AS source_row_ordinal,
            NULL::VARCHAR AS listing_id,
            NULL::VARCHAR AS instrument_id,
            {symbol!r} AS vendor_request_symbol,
            NULL::VARCHAR AS eodhd_exchange_code,
            CAST(day AS DATE) AS trade_date,
            epoch_ms(date) AS trade_ts_ms,
            date AS trade_ts,
            mkt AS venue_code_raw,
            sub_mkt AS sub_mkt_raw,
            seq AS session_seq,
            NULL::VARCHAR AS venue_id,
            NULL::VARCHAR AS mic,
            NULL::VARCHAR AS price_venue_type,
            price AS price,
            size AS size,
            sl AS sale_condition_raw,
            NULL::VARCHAR[] AS sale_condition_flags,
            {LEGACY_ORIGIN!r} AS origin,
            NULL::VARCHAR AS vendor_trade_id,
            NULL::BIGINT AS vendor_sequence_no,
            date AS observed_at_ts,
            date AS committed_at_ts,
            NULL::TIMESTAMP AS available_at_ts,
            1 AS revision_number,
            FALSE AS is_correction,
            FALSE AS is_cancelled,
            FALSE AS is_late_add,
            NULL::VARCHAR AS supersedes_tick_version_id,
            NULL::VARCHAR AS payload_hash,
            NULL::VARCHAR AS record_hash,
            NULL::VARCHAR AS parser_version
        FROM delta_scan({path!r})
    """


def _install_legacy_trade_tick_view(con, legacy_root: str, watermarks: dict) -> list:
    """Register `legacy_trade_tick` as the `UNION ALL` of every watermarked
    symbol's legacy table (skipping - and returning - symbols whose table
    path does not exist). Only symbols present in `watermarks` are
    considered: that dict is the only symbol universe `install_union_view`
    is given."""
    skipped: list = []
    selects: list = []
    for symbol in watermarks:
        path = os.path.join(legacy_root, symbol)
        if not os.path.exists(path):
            skipped.append(symbol)
            continue
        selects.append(_legacy_symbol_select(legacy_root, symbol))

    if selects:
        union_sql = "\nUNION ALL\n".join(selects)
    else:
        null_cols = ", ".join(f"NULL AS {c}" for c in SILVER_COLUMNS)
        union_sql = f"SELECT {null_cols} WHERE FALSE"

    con.execute(f"CREATE OR REPLACE VIEW legacy_trade_tick AS {union_sql}")
    return skipped


def _install_vault_trade_tick_view(con) -> None:
    """Register `vault_trade_tick`: silver rows at/after each symbol's
    watermark UNION ALL legacy rows before it (see module docstring for
    the full join-semantics explanation, including why a symbol absent
    from `union_watermarks` contributes zero silver rows)."""
    cols = ", ".join(SILVER_COLUMNS)
    con.execute(
        f"""
        CREATE OR REPLACE VIEW vault_trade_tick AS
        SELECT {cols}
        FROM silver_us_trade_tick_version AS s
        JOIN union_watermarks AS w ON w.symbol = s.vendor_request_symbol
        WHERE s.trade_date >= w.watermark
        UNION ALL
        SELECT {cols}
        FROM legacy_trade_tick AS s
        LEFT JOIN union_watermarks AS w ON w.symbol = s.vendor_request_symbol
        WHERE s.trade_date < COALESCE(w.watermark, DATE '9999-12-31');
        """
    )


def install_union_view(con, root: str, legacy_root: str, watermarks: dict) -> UnionViewHandle:
    """Register `union_watermarks`, `legacy_trade_tick`, and
    `vault_trade_tick` against `con`.

    Requires `tick_vault.temporal.attach(con, root)` to have already run
    (registers `silver_us_trade_tick_version`, which `vault_trade_tick`
    reads). `root` is accepted (rather than assumed already attached) to
    make the dependency explicit in the signature, matching `tick_vault.
    contract.query_ticks`'s own `(con, root, ...)` shape, even though this
    function does not itself call `attach` - it only reads the view
    `attach` already registered.

    Returns a `UnionViewHandle` (watermarks actually installed + skipped
    symbols + `union_provenance`) - see `UnionViewHandle` docstring.
    """
    import duckdb  # noqa: F401  (imported lazily; ensures duckdb is present)

    watermarks = dict(watermarks or {})
    _install_watermarks_view(con, watermarks)
    skipped = _install_legacy_trade_tick_view(con, legacy_root, watermarks)
    _install_vault_trade_tick_view(con)
    return UnionViewHandle(watermarks=watermarks, skipped_symbols=skipped)

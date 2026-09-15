"""Silver tick Delta writer (task-2 brief; tick-vault temporal design §7).

Turns `tick_vault.tick_parser.parse_tick_payload`'s (and
`tick_vault.diffing.diff_window`'s) pandas DataFrames into rows actually
written to `silver.us_trade_tick_version` / `silver.tick_correction_event`.

`tick_parser`'s module docstring flags that its 29-column `COLUMNS` frame
leaves four `silver.us_trade_tick_version` NOT NULL columns unset:
`source_system`, `vendor_request_symbol`, `price_venue_type` (missing
entirely from `COLUMNS`) and `committed_at_ts` (present but always `None`,
since the parser has no knowledge-time to stamp it with - that's this
writer's job, at write time). `complete_tick_frame` below is the pure
logic that fills those in; it's exercised directly (no pyarrow/deltalake)
by `tests/test_tick_writer_core.py`.

It also converts `price` from the parser's raw float/int to
`decimal.Decimal`, via a `str(...)` round-trip rather than
`decimal.Decimal(float_value)` directly: constructing a Decimal straight
from a float captures the float's exact (but visually surprising) binary
value (e.g. `Decimal(150.26)` -> `Decimal('150.259999999999990905052982...')`),
whereas `Decimal(str(150.26))` gives `Decimal('150.26')` - the shortest
decimal string that round-trips back to the same float, i.e. "repr-exact".
`silver.us_trade_tick_version.price` is `decimal128(24, 10)`
(`tick_vault.schemas`), and `pa.Table.from_pylist` (used by
`tick_vault.capture.append_rows`) casts a Python `decimal.Decimal` into
that type directly - no further `pa.compute` cast is needed.

Column ordering doesn't matter for `append_rows`: `pa.Table.from_pylist`
is given the target `pa.schema` explicitly and looks up each field by
name in every record dict, so extra/missing dict keys are handled by the
schema itself (missing -> null, extra -> ignored) - this module only needs
to *set* the right dict keys, not reindex the whole frame's column list.

This module imports bare (no pyarrow/deltalake at module scope, matching
`tick_vault.capture`'s pattern) so it stays importable in the
pyarrow/deltalake-less authoring sandbox; the Delta-backed
`write_tick_versions`/`write_correction_events` lazy-import
`tick_vault.capture.append_rows` (which itself lazy-imports
pyarrow/deltalake/schemas) inside the function body.

Brief deviation: the brief's `write_correction_events(root, events_df,
committed_at)` signature includes a `committed_at` parameter, by analogy
with `write_tick_versions`. But `tick_vault.diffing.EVENT_COLUMNS` (and
`tick_vault.schemas._SILVER_TICK_CORRECTION_EVENT`, whose docstring says
its column list "matches `tick_vault.diffing.EVENT_COLUMNS` exactly") has
no `committed_at_ts` column at all - correction events are stamped with
`observed_at_ts` only, already filled by `diff_window`. There is nothing
for a `committed_at` argument to do, so it's dropped here rather than
accepted and silently ignored.
"""
from __future__ import annotations

import datetime as dt
import decimal

import pandas as pd

# NOT NULL columns on silver.us_trade_tick_version that
# tick_vault.tick_parser.parse_tick_payload's COLUMNS does not populate.
FILLED_COLUMNS = (
    "source_system",
    "vendor_request_symbol",
    "price_venue_type",
    "committed_at_ts",
)


def _to_decimal(value) -> "decimal.Decimal | None":
    """`value` (a parser-produced float/int/None) -> `decimal.Decimal` (or
    `None`), via a `str(...)` round-trip - see module docstring for why."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, decimal.Decimal):
        return value
    return decimal.Decimal(str(value))


def complete_tick_frame(
    df: pd.DataFrame,
    *,
    source_system: str,
    vendor_request_symbol: str,
    price_venue_type: str,
    committed_at: dt.datetime,
) -> pd.DataFrame:
    """Pure completion logic: fills the four NOT NULL columns the parser
    leaves out and converts `price` to `decimal.Decimal`.

    Returns a new DataFrame; `df` is never mutated in place. Safe to call
    on the frame from either `parse_tick_payload` (fresh capture) or
    `diffing.DiffResult.new_versions` (corrections/late-adds/tombstones) -
    both are shaped per `tick_vault.tick_parser.COLUMNS`.
    """
    out = df.copy()
    out["source_system"] = source_system
    out["vendor_request_symbol"] = vendor_request_symbol
    out["price_venue_type"] = price_venue_type
    out["committed_at_ts"] = committed_at
    if "price" in out.columns:
        out["price"] = out["price"].map(_to_decimal)
    return out


def write_tick_versions(
    root: str,
    df: pd.DataFrame,
    *,
    source_system: str = "EODHD",
    vendor_request_symbol: str,
    price_venue_type: str = "CONSOLIDATED_US",
    committed_at: dt.datetime,
) -> int:
    """Complete `df` (parser/diff-shaped) and append it to
    `silver.us_trade_tick_version`, partitioned by `trade_date`. Returns
    the number of rows written. Never replaces existing data - every call
    is an append (`tick_vault.capture.append_rows`'s `mode="append"`), so
    calling this twice with the same `df` duplicates rows; corrections/
    late-adds/cancellations are new rows by construction (see
    `tick_vault.diffing`), not overwrites of old ones.
    """
    from tick_vault.capture import append_rows

    completed = complete_tick_frame(
        df,
        source_system=source_system,
        vendor_request_symbol=vendor_request_symbol,
        price_venue_type=price_venue_type,
        committed_at=committed_at,
    )
    append_rows(root, "silver.us_trade_tick_version", completed)
    return len(completed)


def write_correction_events(root: str, events_df: pd.DataFrame) -> int:
    """Append `events_df` (`tick_vault.diffing.DiffResult.events`-shaped,
    i.e. `diffing.EVENT_COLUMNS`) to `silver.tick_correction_event`.
    Returns the number of rows written.

    See the module docstring for why this does not take a `committed_at`
    argument despite the brief's proposed signature: the target schema has
    no `committed_at_ts` column to fill.
    """
    from tick_vault.capture import append_rows

    append_rows(root, "silver.tick_correction_event", events_df)
    return len(events_df)

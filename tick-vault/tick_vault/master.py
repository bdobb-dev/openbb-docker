"""Security master builder (task-4 brief): allocates and dates identifier
assignments over `silver.instrument` / `silver.listing_version` /
`silver.identifier_assignment_version`, from Task-3's parsed EODHD
reference frames (`tick_vault.eodhd_reference.parse_symbols`/
`parse_changes`/`parse_fundamentals`).

Design seam (binding, per task brief): `MasterBuilder(root)` reads its
in-memory view of "current" identifier assignments through an injectable
`assignments_reader(root) -> pandas.DataFrame` (default: a lazy
`deltalake` read of `silver.identifier_assignment_version`). Tests supply
a fake reader over a plain in-memory `pandas.DataFrame`, which keeps every
bit of resolution/derivation logic in this module pure-pandas and
runnable in the authoring sandbox (no pyarrow/deltalake installed here).

This module extends that seam with a second, symmetric injection point -
a `writer(root, table, df)` callable (default: a lazy
`tick_vault.capture.append_rows`) - so the *write* side of
`upsert_from_symbols`/`apply_symbol_changes`/`attach_issue_ids` is equally
testable without deltalake: `tests/test_master_core.py` injects a fake
writer that appends into an in-memory `dict[str, DataFrame]`, and a fake
reader that reads back out of that same dict, so a test can run these
methods twice in a row (idempotency) or run `upsert_from_symbols` then
`apply_symbol_changes` then `resolve_symbol_at` against the resulting
state - all in pure pandas. Production code (and
`tests/deferred/test_master.py`) uses the defaults, which are real Delta
reads/writes. Neither default import lives at module scope (matching
`tick_vault.capture`/`tick_vault.tick_writer`'s no-pyarrow-at-import
pattern): they're lazy-imported inside `_default_assignments_reader`/
`_default_writer`.

Append-only convention, WITH ONE BINDING EXCEPTION: system-time closure
-------------------------------------------------------------------------
Every other Delta writer in this repo (`tick_vault.capture.append_rows`,
`tick_vault.tick_writer`, `tick_vault.diffing`) only ever appends - a
correction is a brand-new row carrying a `supersedes_*_id` pointer back
at the row it replaces, never an in-place update of the old row. This
module follows that same convention for the *business* history of an
identifier assignment (`apply_symbol_changes`): the old code's identity
is not mutated - the SAME id_value/instrument/listing gets a new version
row appended, with `effective_to_ts` set to the change date and
`supersedes_assignment_version_id` pointing at the row it replaces.

However (controller ruling, binding - fix-round on this module): a
superseded assignment row's Delta-native `system_to_ts` (system/knowledge
time, baseline design doc §4.1's bitemporal model) IS explicitly updated
in place when it is superseded - `system_to_ts = observed_at` of the
capture that superseded it - via a targeted `deltalake.DeltaTable.update`
call scoped to that one row's `assignment_version_id` (see
`_default_system_closer` below). This is a deliberate, narrow exception
to the append-only convention, confined to silver *METADATA version*
tables like `silver.identifier_assignment_version` (never tick/fact
data, which stays append-only): the DuckDB point-in-time layer
(`tick_vault.reference_queries.pit_listing`) has no notion of a
`supersedes_assignment_version_id` chain - it filters purely on the
row's own effective interval plus `system_to_ts IS NULL OR system_to_ts
> decision_ts`. Without physically closing the superseded row's
`system_to_ts`, that row's `effective_to_ts` is left untouched (open-
ended, by design - only the *new* close-row carries the closing
`effective_to_ts`) and so it would satisfy `pit_listing`'s effective-
interval predicate forever, and after a ticker-reuse-shaped sequence
`pit_listing` could return more than one listing for the same symbol.
Setting `system_to_ts` is therefore not optional bookkeeping; it is what
keeps the DuckDB PIT resolution layer single-valued.

`_current_rows` (below) is the read-side counterpart on the Python side:
it now filters purely on `system_to_ts IS NULL` (mirroring
`pit_listing`), NOT on whether a row is referenced by some other row's
`supersedes_assignment_version_id` - that pointer is retained purely as
provenance (which row a given version replaced), never as the read-path
signal for "is this row current". This keeps the Python-side notion of
"current" and the DuckDB-side notion of "current" identical.

Knowledge-time rule (binding, per task brief)
-----------------------------------------------
Every row this module writes stamps `available_at_ts` = the source
capture's `observed_at` (the caller-supplied knowledge time), with
`system_from_ts` set to the same value and `system_to_ts` left `None`
(this module never closes a row's system-time span - see above).
`confidence` is `decimal128(5, 4)` in the registered schema, so every
confidence value here is a `decimal.Decimal`, never a Python float (CI
lesson from earlier tasks: constructing a Delta decimal column from a
`pandas`/`pyarrow` float silently loses precision or fails to cast).
`effective_from_ts`/`effective_to_ts` are UTC timestamps; a bare
`datetime.date` `d` (e.g. `changes_df["date"]`, from
`tick_vault.eodhd_reference.parse_changes`) is converted via
`datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc)`,
never a naive/local datetime.

`attach_issue_ids` scope ruling (per task brief)
----------------------------------------------------
CUSIP and ISIN assignment rows are attached at INSTRUMENT scope
(`instrument_id` set, `listing_id` left `None`) - baseline design doc
§9.2 treats CUSIP/ISIN as security/instrument-level identifiers, not
listing/venue-level ones. SEC CIK rows are also written with
`instrument_id` set and `registrant_id` left `None`, even though
`silver.identifier_assignment_version.registrant_id` is the "natural"
home for a CIK: `silver.registrant` (the table `registrant_id` would
reference) is not populated by anything in this episode, so there is no
`registrant_id` to point at yet. This is a deliberate, documented
placement, not an oversight - a later EDGAR-ingestion episode is expected
to mint `silver.registrant` rows and re-home these `SEC_CIK` assignments
onto `registrant_id` (via a new version row, per the append-only
convention above, superseding the `instrument_id`-scoped one).

Review-queue / data-quality-issue usage
-----------------------------------------
`ops.openfigi_resolution_queue` rows (reason codes `UNKNOWN_OLD_CODE`,
`AMBIGUOUS_MATCH`, `NEEDS_FIGI`) are appended - never a guess - for:
  - `apply_symbol_changes`: an old code with no currently-open EODHD_SYMBOL
    assignment (`UNKNOWN_OLD_CODE`), or more than one such open assignment
    (`AMBIGUOUS_MATCH`, which also logs an `ops.data_quality_issue` row -
    this table has no other producer in this module, since every other
    path here is either a clean allocation or an unambiguous close/open).
  - `attach_issue_ids`: fundamentals for a code with no currently-open
    EODHD_SYMBOL assignment yet, i.e. issuer identifiers arriving ahead of
    the symbol-list upsert that would create the instrument to attach them
    to (`NEEDS_FIGI` - the closest fit of the three fixed reason codes:
    this instrument's identity isn't resolved yet, so it can't be handed
    to FIGI-level resolution either).
This module has no `queue_id`/`issue_id`-column "reason" field to spend on
a bespoke code, so the fixed `status` column on
`ops.openfigi_resolution_queue` doubles as the reason code.

ID prefixes
------------
`tick_vault.ids.new_id`: `instrument_id` -> `"ins"`, `listing_id` and
`listing_version_id` -> `"lst"` (two different IDs, same category
prefix), everything else this module mints (identifier-assignment
version rows, resolution-queue rows, data-quality-issue rows) -> `"wrk"`
(the catch-all "work-item/record" prefix; none of `PREFIXES` is a closer
fit for a bitemporal version row or a queue/issue row).
"""
from __future__ import annotations

import datetime as dt
import decimal
import json

import pandas as pd

from tick_vault.ids import new_id

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

AUTO_CONFIDENCE = decimal.Decimal("0.9000")
DEFAULT_PRICE_VENUE_TYPE = "CONSOLIDATED_US"
SOURCE_SYSTEM = "eodhd"
VERIFICATION_STATUS_UNVERIFIED = "UNVERIFIED"

NAMESPACE_EODHD_SYMBOL = "EODHD_SYMBOL"
NAMESPACE_TICKER = "TICKER"
NAMESPACE_CUSIP = "CUSIP"
NAMESPACE_ISIN = "ISIN"
NAMESPACE_SEC_CIK = "SEC_CIK"

REASON_UNKNOWN_OLD_CODE = "UNKNOWN_OLD_CODE"
REASON_AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
REASON_NEEDS_FIGI = "NEEDS_FIGI"

# Column lists, transcribed from tick_vault.schemas (which this module
# cannot import at module scope - it imports bare pyarrow - see module
# docstring); order doesn't matter for pa.Table.from_pylist(schema=...)
# (tick_vault.tick_writer's docstring), only presence/spelling does.
_ASSIGNMENT_COLUMNS = [
    "assignment_version_id", "issuer_id", "registrant_id", "instrument_id",
    "listing_id", "id_namespace", "id_value", "normalized_id_value",
    "effective_from_ts", "effective_to_ts", "observed_at_ts",
    "available_at_ts", "system_from_ts", "system_to_ts", "source_system",
    "source_capture_id", "verification_status", "confidence",
    "supersedes_assignment_version_id",
]

_INSTRUMENT_COLUMNS = [
    "instrument_id", "issuer_id", "registrant_id", "instrument_type",
    "share_class", "country_of_incorporation", "primary_currency",
    "created_at_ts", "retired_at_ts", "status", "source_system",
]

_LISTING_COLUMNS = [
    "listing_version_id", "listing_id", "instrument_id", "ticker",
    "eodhd_symbol", "eodhd_exchange_code", "venue_id", "mic",
    "operating_mic", "exchange_name", "listing_currency", "venue_figi",
    "composite_figi", "share_class_figi", "price_venue_type",
    "effective_from_ts", "effective_to_ts", "observed_at_ts",
    "available_at_ts", "system_from_ts", "system_to_ts", "source_system",
    "source_capture_id", "confidence", "verification_status",
]

_QUEUE_COLUMNS = [
    "queue_id", "instrument_id", "listing_id", "id_namespace", "id_value",
    "exchange_code", "status", "attempts", "priority", "last_error",
    "created_at_ts", "updated_at_ts", "resolved_figi_assignment_version_id",
]

_DQ_COLUMNS = [
    "issue_id", "check_name", "severity", "trade_date", "listing_id",
    "instrument_id", "source_capture_id", "details_json", "status",
    "detected_at_ts", "resolved_at_ts", "ingestion_run_id",
]

# fundamentals_df column -> (id_namespace,) mapping for attach_issue_ids.
_ISSUE_ID_FIELDS = (
    ("cusip", NAMESPACE_CUSIP),
    ("isin", NAMESPACE_ISIN),
    ("cik", NAMESPACE_SEC_CIK),
)


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------

def _is_missing(value) -> bool:
    """`True` for `None`, NaN/NaT, and the empty string."""
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return value == ""


def _to_utc_ts(value):
    """Coerce `value` (None / `datetime.date` / `datetime.datetime` /
    `pandas.Timestamp`) into a tz-aware UTC `datetime.datetime`, or `None`.

    A bare `datetime.date` `d` (e.g. a parsed `changes_df["date"]`) becomes
    `datetime.datetime(d.year, d.month, d.day, tzinfo=UTC)` - midnight UTC
    on that date, per the task brief's knowledge-time rule. A naive
    `datetime.datetime` is assumed to already be UTC and is simply
    stamped with the UTC tzinfo (never treated as local time).
    """
    if _is_missing(value):
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)
    if hasattr(value, "to_pydatetime"):
        return _to_utc_ts(value.to_pydatetime())
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc)
    return value


def _normalize(value) -> "str | None":
    if _is_missing(value):
        return None
    return str(value).strip().upper()


def _empty(columns: list) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def _row(columns: list, **overrides) -> dict:
    row = {c: None for c in columns}
    for key, value in overrides.items():
        if key in row:
            row[key] = value
    return row


# ---------------------------------------------------------------------------
# reading "current" assignment state
# ---------------------------------------------------------------------------

def _current_rows(assignments_df: "pd.DataFrame | None") -> pd.DataFrame:
    """The currently-open rows: `system_to_ts IS NULL`, exactly mirroring
    `tick_vault.reference_queries.pit_listing`'s "current" predicate. See
    the module docstring's "Append-only convention, WITH ONE BINDING
    EXCEPTION" section - `supersedes_assignment_version_id` is provenance
    only and plays no part in this filter.
    """
    if assignments_df is None or assignments_df.empty:
        return _empty(_ASSIGNMENT_COLUMNS)
    if "system_to_ts" in assignments_df.columns:
        mask = assignments_df["system_to_ts"].isna()
    else:
        mask = pd.Series(True, index=assignments_df.index)
    return assignments_df[mask]


def _open_assignments_for_code(
    current: pd.DataFrame, code, namespace: str = NAMESPACE_EODHD_SYMBOL
) -> pd.DataFrame:
    """Currently-open (`effective_to_ts` null) rows for `code` within the
    already-`_current_rows`-filtered frame `current`."""
    if current.empty:
        return current
    matches = current[(current["id_namespace"] == namespace) & (current["id_value"] == code)]
    if matches.empty:
        return matches
    return matches[matches["effective_to_ts"].isna()]


def _resolve_covering_row(
    assignments_df: "pd.DataFrame | None",
    code,
    at_date,
    namespaces=(NAMESPACE_EODHD_SYMBOL, NAMESPACE_TICKER),
):
    """Shared lookup behind `resolve_listing_at`/
    `resolve_listing_and_instrument_at`: among the current
    (non-superseded) assignment rows in `namespaces` whose `id_value ==
    code`, returns the full row (a `pandas.Series`) of whichever one's
    effective interval `[effective_from_ts, effective_to_ts)` covers
    `at_date` (a `None` bound is open on that side), or `None` if
    unresolvable. Two disjoint windows for the same `code` pointing at
    different listings (ticker reuse) each only match their own date
    range, so this resolves correctly by date alone; if more than one
    covering row is somehow found (a genuine overlap - not expected from
    this module's own writers), the most-recently-asserted one (highest
    `system_from_ts`, falling back to `observed_at_ts`) wins.
    """
    if _is_missing(code):
        return None
    at_ts = _to_utc_ts(at_date)
    current = _current_rows(assignments_df)
    if current.empty:
        return None
    candidates = current[
        current["id_namespace"].isin(namespaces) & (current["id_value"] == code)
    ]
    if candidates.empty:
        return None

    def _covers(row) -> bool:
        efrom = _to_utc_ts(row["effective_from_ts"])
        eto = _to_utc_ts(row["effective_to_ts"])
        if efrom is not None and efrom > at_ts:
            return False
        if eto is not None and eto <= at_ts:
            return False
        return True

    covering = candidates[candidates.apply(_covers, axis=1)]
    if covering.empty:
        return None
    sort_cols = [c for c in ("system_from_ts", "observed_at_ts") if c in covering.columns]
    if sort_cols:
        covering = covering.sort_values(by=sort_cols)
    return covering.iloc[-1]


def resolve_listing_at(
    assignments_df: "pd.DataFrame | None",
    code,
    at_date,
    namespaces=(NAMESPACE_EODHD_SYMBOL, NAMESPACE_TICKER),
) -> "str | None":
    """`code` -> `listing_id` as of `at_date`, or `None` if unresolvable.

    Pure function over an assignments frame shaped like
    `silver.identifier_assignment_version` (see `_ASSIGNMENT_COLUMNS`);
    see `_resolve_covering_row` for the matching/tie-break rule.
    """
    row = _resolve_covering_row(assignments_df, code, at_date, namespaces)
    if row is None:
        return None
    return row["listing_id"]


def resolve_listing_and_instrument_at(
    assignments_df: "pd.DataFrame | None",
    code,
    at_date,
    namespaces=(NAMESPACE_EODHD_SYMBOL, NAMESPACE_TICKER),
) -> "tuple[str, str] | None":
    """`code` -> `(listing_id, instrument_id)` as of `at_date`, or `None`
    if unresolvable - the two-value counterpart to `resolve_listing_at`
    consumed by `tick_vault.membership.build_membership`'s injected
    `resolver` seam (which needs both ids, not just `listing_id`).

    Same matching/tie-break rule as `resolve_listing_at` (see
    `_resolve_covering_row`); additionally returns `None` (rather than a
    tuple with a missing half) when the covering row's `instrument_id` is
    itself missing/null, since the caller's contract is "a complete pair,
    or nothing".
    """
    row = _resolve_covering_row(assignments_df, code, at_date, namespaces)
    if row is None:
        return None
    instrument_id = row["instrument_id"]
    if _is_missing(instrument_id):
        return None
    return (row["listing_id"], instrument_id)


# ---------------------------------------------------------------------------
# pure compute functions (assignments snapshot + input frame -> new rows)
# ---------------------------------------------------------------------------

def compute_symbol_upsert(
    assignments_df: "pd.DataFrame | None",
    symbols_df: pd.DataFrame,
    capture_id: str,
    observed_at,
) -> "dict[str, pd.DataFrame]":
    """For each vendor `code` in `symbols_df` (shaped like
    `tick_vault.eodhd_reference.parse_symbols`'s output: `code, name,
    exchange, type, isin`) not already carrying a currently-open
    `EODHD_SYMBOL` assignment, allocate a new `silver.instrument` +
    `silver.listing_version` row and an `EODHD_SYMBOL` assignment row.
    `effective_from_ts=None` ("since listing start", per the task brief)
    UNLESS the normalized code has EVER been assigned before (any prior
    row in `assignments_df` for that normalized code, system-closed or
    effective-closed - i.e. a reused ticker) - see the controller ruling
    below. Idempotent: a code with an existing open assignment (whether
    already in `assignments_df`, or created earlier within this same
    call, for a duplicate row) yields no new rows for that code.

    Reused-code `effective_from_ts` (controller ruling, fix-round 2):
    an unbounded `effective_from_ts=None` on a REUSED code's new
    assignment makes `pit_listing` (baseline Sec 4.1's bitemporal DuckDB
    layer) multi-valued for historical queries - a `market_date` inside
    the OLD assignment's effective window, combined with a `decision_ts`
    AFTER the reuse was captured, would satisfy both the old close-row's
    effective interval (system-closed, but `system_to_ts` plays no part
    in `pit_listing`'s effective-interval predicate) AND the new row's
    unbounded-from-below interval. So: if the assignments frame shows ANY
    prior assignment for that normalized code (regardless of state -
    system-closed via `system_to_ts`, or effective-closed via
    `effective_to_ts`, or even currently open, though that path is
    already excluded by `known_codes` above), the new assignment's
    `effective_from_ts` is bounded at `observed_at` - the best available
    bound given only vendor symbol-list snapshots (a documented
    approximation; a later Phase-0/vendor symbol-change-history episode
    may refine this to the actual reuse date once that data is
    available). A code with NO prior assignment at all (first-ever
    listing) keeps `effective_from_ts=None`, unchanged.
    """
    observed_at_ts = _to_utc_ts(observed_at)
    current = _current_rows(assignments_df)
    known_codes = set()
    if not current.empty:
        existing = current[current["id_namespace"] == NAMESPACE_EODHD_SYMBOL]
        known_codes = set(existing.loc[existing["effective_to_ts"].isna(), "id_value"])

    # Any EODHD_SYMBOL assignment ever written (open or closed, system or
    # effective) for a given normalized code, across the FULL history -
    # not just `current` - used to detect ticker reuse above.
    ever_assigned_normalized_codes = set()
    if assignments_df is not None and not assignments_df.empty:
        all_eodhd = assignments_df[assignments_df["id_namespace"] == NAMESPACE_EODHD_SYMBOL]
        ever_assigned_normalized_codes = set(all_eodhd["normalized_id_value"].dropna())

    instrument_rows, listing_rows, assignment_rows = [], [], []
    for _, r in symbols_df.iterrows():
        code = r.get("code")
        if _is_missing(code) or code in known_codes:
            continue
        instrument_id = new_id("ins")
        listing_id = new_id("lst")
        listing_version_id = new_id("lst")
        assignment_version_id = new_id("wrk")

        instrument_type = r.get("type")
        if _is_missing(instrument_type):
            # silver.instrument.instrument_type is non-null; default to
            # "UNKNOWN" rather than writing a null into a non-null column
            # (minor fix from review).
            instrument_type = "UNKNOWN"

        # Reused-code bound (controller ruling, fix-round 2 - see
        # docstring above): a normalized code with ANY prior assignment
        # gets effective_from_ts=observed_at instead of the unbounded
        # None a first-ever code gets.
        normalized_code = _normalize(code)
        assignment_effective_from_ts = (
            observed_at_ts
            if normalized_code in ever_assigned_normalized_codes
            else None
        )
        instrument_rows.append(_row(
            _INSTRUMENT_COLUMNS,
            instrument_id=instrument_id,
            instrument_type=instrument_type,
            created_at_ts=observed_at_ts,
            status="ACTIVE",
            source_system=SOURCE_SYSTEM,
        ))
        listing_rows.append(_row(
            _LISTING_COLUMNS,
            listing_version_id=listing_version_id,
            listing_id=listing_id,
            instrument_id=instrument_id,
            ticker=code,
            eodhd_symbol=code,
            eodhd_exchange_code=r.get("exchange"),
            price_venue_type=DEFAULT_PRICE_VENUE_TYPE,
            observed_at_ts=observed_at_ts,
            available_at_ts=observed_at_ts,
            system_from_ts=observed_at_ts,
            source_system=SOURCE_SYSTEM,
            source_capture_id=capture_id,
            confidence=AUTO_CONFIDENCE,
            verification_status=VERIFICATION_STATUS_UNVERIFIED,
        ))
        assignment_rows.append(_row(
            _ASSIGNMENT_COLUMNS,
            assignment_version_id=assignment_version_id,
            instrument_id=instrument_id,
            listing_id=listing_id,
            id_namespace=NAMESPACE_EODHD_SYMBOL,
            id_value=code,
            normalized_id_value=normalized_code,
            effective_from_ts=assignment_effective_from_ts,
            observed_at_ts=observed_at_ts,
            available_at_ts=observed_at_ts,
            system_from_ts=observed_at_ts,
            source_system=SOURCE_SYSTEM,
            source_capture_id=capture_id,
            verification_status=VERIFICATION_STATUS_UNVERIFIED,
            confidence=AUTO_CONFIDENCE,
        ))
        known_codes.add(code)
        ever_assigned_normalized_codes.add(normalized_code)

    return {
        "instrument": pd.DataFrame(instrument_rows, columns=_INSTRUMENT_COLUMNS),
        "listing_version": pd.DataFrame(listing_rows, columns=_LISTING_COLUMNS),
        "identifier_assignment_version": pd.DataFrame(assignment_rows, columns=_ASSIGNMENT_COLUMNS),
    }


def compute_symbol_changes(
    assignments_df: "pd.DataFrame | None",
    changes_df: pd.DataFrame,
    capture_id: str,
    observed_at,
) -> "dict[str, pd.DataFrame]":
    """For each `old -> new` row in `changes_df` (shaped like
    `tick_vault.eodhd_reference.parse_changes`'s output: `old, new, date`):
    close the `old` code's currently-open `EODHD_SYMBOL` assignment
    (append a new version of that same identity with `effective_to_ts =
    date`, `supersedes_assignment_version_id` pointing at the row it
    closes) and open a brand new `EODHD_SYMBOL` assignment for `new`,
    to the SAME `listing_id`/`instrument_id`, `effective_from_ts = date`
    (baseline §9.1).

    An `old` code with no currently-open assignment is queued
    (`UNKNOWN_OLD_CODE`), never guessed. More than one currently-open
    assignment for `old` is queued (`AMBIGUOUS_MATCH`) and also logged to
    `ops.data_quality_issue` - resolving which one is correct needs human
    or FIGI-backed review, not a guess either.

    Also returns `"system_closes"`: a plain `list[tuple[str,
    datetime.datetime]]` of `(assignment_version_id, system_to_ts)`
    instructions - one per row actually superseded above - for the
    caller (`MasterBuilder.apply_symbol_changes`) to apply as a targeted
    Delta `UPDATE` against the superseded row (see the module docstring's
    binding controller ruling). This function stays pure/Delta-free: it
    only *describes* the closes, it never performs one - but it does
    apply each close to its own local `working` copy of the assignments
    frame (system_to_ts only, never a physical mutation of the input
    `assignments_df`) so that a second `old -> new` row in the same
    `changes_df` batch, chained off the first, sees the correct
    currently-open state without needing the real Delta UPDATE to have
    happened yet.
    """
    observed_at_ts = _to_utc_ts(observed_at)
    working = assignments_df.copy() if assignments_df is not None else _empty(_ASSIGNMENT_COLUMNS)

    new_assignment_rows, queue_rows, dq_rows, system_closes = [], [], [], []
    for _, r in changes_df.iterrows():
        old_code = r.get("old")
        new_code = r.get("new")
        change_date = r.get("date")
        if _is_missing(old_code) or _is_missing(new_code):
            continue
        change_ts = _to_utc_ts(change_date)
        current = _current_rows(working)
        matches = _open_assignments_for_code(current, old_code, NAMESPACE_EODHD_SYMBOL)

        if matches.empty:
            queue_rows.append(_row(
                _QUEUE_COLUMNS,
                queue_id=new_id("wrk"),
                id_namespace=NAMESPACE_EODHD_SYMBOL,
                id_value=old_code,
                status=REASON_UNKNOWN_OLD_CODE,
                attempts=0,
                priority=5,
                last_error=(
                    f"symbol change {old_code!r} -> {new_code!r} on "
                    f"{change_date}: no open EODHD_SYMBOL assignment for "
                    f"{old_code!r}"
                ),
                created_at_ts=observed_at_ts,
                updated_at_ts=observed_at_ts,
            ))
            continue

        if len(matches) > 1:
            dq_rows.append(_row(
                _DQ_COLUMNS,
                issue_id=new_id("wrk"),
                check_name="AMBIGUOUS_SYMBOL_ASSIGNMENT",
                severity="WARN",
                details_json=json.dumps({
                    "old_code": old_code, "new_code": new_code,
                    "candidate_count": int(len(matches)),
                }),
                status="OPEN",
                detected_at_ts=observed_at_ts,
            ))
            queue_rows.append(_row(
                _QUEUE_COLUMNS,
                queue_id=new_id("wrk"),
                id_namespace=NAMESPACE_EODHD_SYMBOL,
                id_value=old_code,
                status=REASON_AMBIGUOUS_MATCH,
                attempts=0,
                priority=5,
                last_error=f"{len(matches)} open EODHD_SYMBOL assignments for {old_code!r}",
                created_at_ts=observed_at_ts,
                updated_at_ts=observed_at_ts,
            ))
            continue

        old_row = matches.iloc[0]
        listing_id = old_row["listing_id"]
        instrument_id = old_row["instrument_id"]

        close_row = {col: old_row.get(col) for col in _ASSIGNMENT_COLUMNS}
        close_row["assignment_version_id"] = new_id("wrk")
        close_row["effective_to_ts"] = change_ts
        close_row["observed_at_ts"] = observed_at_ts
        close_row["available_at_ts"] = observed_at_ts
        close_row["system_from_ts"] = observed_at_ts
        close_row["system_to_ts"] = None
        close_row["source_capture_id"] = capture_id
        close_row["verification_status"] = VERIFICATION_STATUS_UNVERIFIED
        close_row["confidence"] = AUTO_CONFIDENCE
        close_row["supersedes_assignment_version_id"] = old_row["assignment_version_id"]

        open_row = _row(
            _ASSIGNMENT_COLUMNS,
            assignment_version_id=new_id("wrk"),
            instrument_id=instrument_id,
            listing_id=listing_id,
            id_namespace=NAMESPACE_EODHD_SYMBOL,
            id_value=new_code,
            normalized_id_value=_normalize(new_code),
            effective_from_ts=change_ts,
            observed_at_ts=observed_at_ts,
            available_at_ts=observed_at_ts,
            system_from_ts=observed_at_ts,
            source_system=SOURCE_SYSTEM,
            source_capture_id=capture_id,
            verification_status=VERIFICATION_STATUS_UNVERIFIED,
            confidence=AUTO_CONFIDENCE,
        )

        new_assignment_rows.append(close_row)
        new_assignment_rows.append(open_row)
        working = pd.concat(
            [working, pd.DataFrame([close_row, open_row], columns=_ASSIGNMENT_COLUMNS)],
            ignore_index=True,
        )

        # System-close the superseded row: system_to_ts = observed_at (the
        # closing knowledge time), applied here only to this function's own
        # local `working` copy (so later rows in this same batch see the
        # correct currently-open state), and recorded as an explicit
        # instruction for the caller to apply against the real Delta row.
        old_version_id = old_row["assignment_version_id"]
        working.loc[
            working["assignment_version_id"] == old_version_id, "system_to_ts"
        ] = observed_at_ts
        system_closes.append((old_version_id, observed_at_ts))

    return {
        "identifier_assignment_version": pd.DataFrame(new_assignment_rows, columns=_ASSIGNMENT_COLUMNS),
        "openfigi_resolution_queue": pd.DataFrame(queue_rows, columns=_QUEUE_COLUMNS),
        "data_quality_issue": pd.DataFrame(dq_rows, columns=_DQ_COLUMNS),
        "system_closes": system_closes,
    }


def compute_attach_issue_ids(
    assignments_df: "pd.DataFrame | None",
    fundamentals_df: pd.DataFrame,
    capture_id: str,
    observed_at,
) -> "dict[str, pd.DataFrame]":
    """For each row of `fundamentals_df` (shaped like
    `tick_vault.eodhd_reference.parse_fundamentals`'s output: `code,
    cusip, isin, cik, name, type, share_class`), resolve `code` to its
    currently-open `EODHD_SYMBOL` assignment's `instrument_id`, then
    attach any present `cusip`/`isin`/`cik` as `CUSIP`/`ISIN`/`SEC_CIK`
    assignment rows scoped to that `instrument_id` (`listing_id=None`,
    and for `SEC_CIK`, `registrant_id=None` too - see the module
    docstring's scope ruling). Idempotent per `(namespace, instrument_id,
    id_value)`. A `code` with no currently-open `EODHD_SYMBOL` assignment
    yet is queued (`NEEDS_FIGI`) rather than skipped silently.
    """
    observed_at_ts = _to_utc_ts(observed_at)
    current = _current_rows(assignments_df)

    already = set()
    if not current.empty:
        scoped = current[current["id_namespace"].isin([NAMESPACE_CUSIP, NAMESPACE_ISIN, NAMESPACE_SEC_CIK])]
        for _, row in scoped.iterrows():
            already.add((row["id_namespace"], row["instrument_id"], row["id_value"]))

    assignment_rows, queue_rows = [], []
    for _, r in fundamentals_df.iterrows():
        code = r.get("code")
        if _is_missing(code):
            continue
        matches = _open_assignments_for_code(current, code, NAMESPACE_EODHD_SYMBOL)
        if matches.empty:
            queue_rows.append(_row(
                _QUEUE_COLUMNS,
                queue_id=new_id("wrk"),
                id_namespace=NAMESPACE_EODHD_SYMBOL,
                id_value=code,
                status=REASON_NEEDS_FIGI,
                attempts=0,
                priority=5,
                last_error=(
                    f"fundamentals for {code!r} arrived with no open "
                    "EODHD_SYMBOL assignment (instrument not yet minted)"
                ),
                created_at_ts=observed_at_ts,
                updated_at_ts=observed_at_ts,
            ))
            continue

        instrument_id = matches.iloc[0]["instrument_id"]
        for field, namespace in _ISSUE_ID_FIELDS:
            value = r.get(field)
            if _is_missing(value):
                continue
            key = (namespace, instrument_id, value)
            if key in already:
                continue
            assignment_rows.append(_row(
                _ASSIGNMENT_COLUMNS,
                assignment_version_id=new_id("wrk"),
                instrument_id=instrument_id,
                id_namespace=namespace,
                id_value=value,
                normalized_id_value=_normalize(value),
                observed_at_ts=observed_at_ts,
                available_at_ts=observed_at_ts,
                system_from_ts=observed_at_ts,
                source_system=SOURCE_SYSTEM,
                source_capture_id=capture_id,
                verification_status=VERIFICATION_STATUS_UNVERIFIED,
                confidence=AUTO_CONFIDENCE,
            ))
            already.add(key)

    return {
        "identifier_assignment_version": pd.DataFrame(assignment_rows, columns=_ASSIGNMENT_COLUMNS),
        "openfigi_resolution_queue": pd.DataFrame(queue_rows, columns=_QUEUE_COLUMNS),
    }


# ---------------------------------------------------------------------------
# Delta-backed defaults (lazy pyarrow/deltalake imports)
# ---------------------------------------------------------------------------

def _default_assignments_reader(root: str) -> pd.DataFrame:
    from deltalake import DeltaTable

    return DeltaTable(f"{root}/silver/identifier_assignment_version").to_pandas()


def _default_writer(root: str, table: str, df: pd.DataFrame) -> None:
    from tick_vault.capture import append_rows

    append_rows(root, table, df)


def _quote_sql_literal(value: str) -> str:
    """Escape a single-quoted SQL string literal (doubling embedded `'`s).
    `assignment_version_id` values are our own `new_id("wrk")` hex ids, so
    this is defense-in-depth, not a real injection surface."""
    return value.replace("'", "''")


def _default_system_closer(root: str, table: str, closes: "list[tuple[str, object]]") -> None:
    """Real Delta implementation of the `system_closer` seam (binding
    controller ruling - see module docstring): for each `(assignment_
    version_id, system_to_ts)` instruction, issue a targeted
    `DeltaTable.update` against exactly that row, setting `system_to_ts`.

    Uses the `updates={"col": "<sql expr>"}` + `predicate="<sql expr>"`
    form of `DeltaTable.update` (present in `deltalake>=0.16`; still
    present as of `deltalake>=1.0`). NOTE (unverified in this sandbox -
    no deltalake installed - flag for CI): if a future `deltalake`
    version renames/changes this signature, `tests/deferred/
    test_master.py::test_apply_symbol_changes_system_closes_superseded_row`
    is what will catch it under `pytest`.
    """
    from deltalake import DeltaTable

    layer, name = table.split(".", 1)
    dt_table = DeltaTable(f"{root}/{layer}/{name}")
    for assignment_version_id, system_to_ts in closes:
        ts = _to_utc_ts(system_to_ts)
        ts_literal = ts.strftime("%Y-%m-%d %H:%M:%S.%f")
        dt_table.update(
            updates={"system_to_ts": f"TIMESTAMP '{ts_literal}'"},
            predicate=(
                "assignment_version_id = "
                f"'{_quote_sql_literal(assignment_version_id)}'"
            ),
        )


# ---------------------------------------------------------------------------
# MasterBuilder
# ---------------------------------------------------------------------------

class MasterBuilder:
    """Security-master builder: allocates/dates `silver.instrument`,
    `silver.listing_version`, and `silver.identifier_assignment_version`
    rows from Task-3's parsed EODHD reference frames.

    `assignments_reader(root) -> pandas.DataFrame`, `writer(root, table,
    df) -> None`, and `system_closer(root, table, closes) -> None` are all
    injectable (see module docstring); the defaults are real Delta reads/
    writes/targeted-updates. The identifier-assignment table IS the
    persistent ID registry - there is no separate side-registry file.
    """

    def __init__(self, root: str, *, assignments_reader=None, writer=None, system_closer=None):
        self.root = root
        self._assignments_reader = assignments_reader or _default_assignments_reader
        self._writer = writer or _default_writer
        self._system_closer = system_closer or _default_system_closer

    def _read_assignments(self) -> pd.DataFrame:
        return self._assignments_reader(self.root)

    def _write(self, table: str, df: "pd.DataFrame | None") -> int:
        if df is None or df.empty:
            return 0
        self._writer(self.root, table, df)
        return len(df)

    def upsert_from_symbols(self, symbols_df: pd.DataFrame, capture_id: str, observed_at) -> "dict[str, pd.DataFrame]":
        result = compute_symbol_upsert(self._read_assignments(), symbols_df, capture_id, observed_at)
        self._write("silver.instrument", result["instrument"])
        self._write("silver.listing_version", result["listing_version"])
        self._write("silver.identifier_assignment_version", result["identifier_assignment_version"])
        return result

    def apply_symbol_changes(self, changes_df: pd.DataFrame, capture_id: str, observed_at) -> "dict[str, pd.DataFrame]":
        result = compute_symbol_changes(self._read_assignments(), changes_df, capture_id, observed_at)
        self._write("silver.identifier_assignment_version", result["identifier_assignment_version"])
        self._write("ops.openfigi_resolution_queue", result["openfigi_resolution_queue"])
        self._write("ops.data_quality_issue", result["data_quality_issue"])
        closes = result.get("system_closes") or []
        if closes:
            # Binding controller ruling: system-close each superseded row
            # (system_to_ts = observed_at) so the DuckDB PIT layer
            # (`reference_queries.pit_listing`) - which knows nothing of
            # `supersedes_assignment_version_id` chains - stops matching a
            # symbol's old identity forever. See module docstring.
            self._system_closer(self.root, "silver.identifier_assignment_version", closes)
        return result

    def attach_issue_ids(self, fundamentals_df: pd.DataFrame, capture_id: str, observed_at) -> "dict[str, pd.DataFrame]":
        result = compute_attach_issue_ids(self._read_assignments(), fundamentals_df, capture_id, observed_at)
        self._write("silver.identifier_assignment_version", result["identifier_assignment_version"])
        self._write("ops.openfigi_resolution_queue", result["openfigi_resolution_queue"])
        return result

    def resolve_symbol_at(self, code, date) -> "str | None":
        """`code` -> `listing_id` as of `date` (see `resolve_listing_at`),
        used by the manifest generator before DuckDB is warm."""
        return resolve_listing_at(self._read_assignments(), code, date)

    def resolve_listing_and_instrument_at(self, code, date) -> "tuple[str, str] | None":
        """`code` -> `(listing_id, instrument_id)` as of `date` (see
        `resolve_listing_and_instrument_at` module function): the named
        resolver adapter `tick_vault.membership.build_membership`'s
        injected `resolver(code, date)` seam is meant to be bound to (per
        that module's docstring - it never constructs a `MasterBuilder`
        itself)."""
        return resolve_listing_and_instrument_at(self._read_assignments(), code, date)

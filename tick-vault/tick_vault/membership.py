"""Bitemporal S&P 500 membership interval builder (task-5 brief).

Reads Task-3's parsed index-components frame
(`tick_vault.eodhd_reference.parse_components`'s output: `code, name,
start_date, end_date, is_active, sector`) and writes `silver.
index_membership_version` rows (baseline §8.3), resolving each vendor
constituent code to a dated `(listing_id, instrument_id)` pair.

Design seam (binding, matching Task 4's `tick_vault.master` pattern):
this module never imports `tick_vault.master.MasterBuilder` - that would
invert the intended dependency direction (membership building depends on
security-master resolution, not the other way around, and the two stay
decoupled at the module level). Instead `build_membership` takes an
injectable `resolver(code, date) -> tuple[listing_id, instrument_id] |
None` callable; in production this is a *bound method* the caller
supplies (typically `MasterBuilder(root).resolve_listing_and_instrument_
at`, or an equivalent adapter), never something this module constructs
itself. Tests supply a plain fake function.

Two more injectable seams mirror `tick_vault.master`'s:
  - `memberships_reader(root) -> pandas.DataFrame`: the current contents
    of `silver.index_membership_version` (default: a lazy `deltalake`
    read), used only for idempotency dedup (see below) - never for
    overlap detection, which is scoped to the CURRENT `components_df`
    batch only.
  - `writer(root, table, df) -> None`: appends rows (default: a lazy
    `tick_vault.capture.append_rows`).
Neither default import lives at module scope, matching `tick_vault.
master`/`tick_vault.capture`'s no-pyarrow-at-import convention: this
module is importable and its pure logic runnable in a sandbox with no
pyarrow/deltalake installed (`tests/test_membership_core.py`). The real
Delta round trip is `tests/deferred/test_membership.py` (pytest).

Semantics (baseline §8.3, binding - task-5 brief + controller ruling)
-----------------------------------------------------------------------
- Each vendor code is resolved via `resolver(code, start_date)` - i.e.
  at the constituent's OWN start date, not "today" or the capture's
  `observed_at` - EXCEPT when `start_date` is missing (a Components-only
  row with no known join date): `membership_effective_from` is a
  non-nullable `date32` column, so it can never be written as bare
  `None`. Per controller ruling (fix-round), such a row is instead
  resolved via `resolver(code, observed_at.date())` (the capture's OWN
  knowledge time standing in for the unknown start), and
  `membership_effective_from` is likewise bounded at `observed_at.date()`
  - whether or not that resolution attempt succeeds. Either way the row
  is tagged `inclusion_reason='START_DATE_UNKNOWN_BOUNDED_AT_OBSERVATION'`
  (`INCLUSION_REASON_START_UNKNOWN`) and exactly one
  `ops.data_quality_issue` row (`check_name='MEMBERSHIP_START_UNKNOWN'`)
  is appended, so a later Phase-0 episode (with access to real
  constituent-history data) can refine the bound. If that
  observation-date resolution attempt itself returns `None`, the row is
  UNRESOLVED (same as the ordinary "unknown code" case below) but STILL
  carries `membership_effective_from=observed_at.date()` and the same
  `inclusion_reason` - never a bare `None` into the column.
- RESOLVED: `listing_id`/`instrument_id` set from the resolver,
  `resolution_status='RESOLVED'`, `confidence=AUTO_CONFIDENCE`
  (`Decimal("0.9000")`, same fixed auto-resolution confidence
  `tick_vault.master` uses for its own auto-allocated rows).
- UNRESOLVED: `resolver(...)` returned `None` (unknown code, or no
  `start_date` to resolve at). `listing_id`/`instrument_id` stay `None`;
  the vendor's own `source_constituent_code`/`source_name` are retained
  verbatim (never dropped) so the row is still a legible historical
  fact, just not yet tied to a security-master identity.
  `resolution_status='UNRESOLVED'`. `confidence=UNRESOLVED_CONFIDENCE`
  (`Decimal("0.0000")` - there is no resolution to be confident about;
  this is a deliberate, documented choice, distinct from
  `AUTO_CONFIDENCE`, since the schema's `confidence` column is
  non-nullable).
- Overlap: among ROWS THAT RESOLVED in this same call, grouped by
  `(index_id, listing_id)`, if two intervals `[membership_effective_from,
  membership_effective_to]` overlap UNDER INCLUSIVE `effective_to`
  SEMANTICS (per `MEMBERSHIP_BOUNDARY_V1` below, `effective_to` is the
  LAST included session, not an exclusive/day-after boundary - so two
  same-listing intervals that merely TOUCH at a shared boundary date DO
  count as overlapping; a `None` bound is open on that side), BOTH rows
  are written (never silently dropped) but the LATER-STARTING one (by
  `membership_effective_from`; a `None` start sorts first, i.e. is never
  "later") is written with `resolution_status='AMBIGUOUS'` instead of
  `'RESOLVED'`. Exactly one `ops.data_quality_issue` row
  (`check_name='MEMBERSHIP_OVERLAP'`) is appended describing the
  conflicting pair, UNLESS both participating rows already existed
  (matched an already-written row's dedup key) BEFORE this call - i.e.
  the issue is only (re-)emitted when at least one of the two rows is
  newly written in THIS run, so an idempotent re-run over the same
  `components_df` does not keep re-appending the same issue row forever
  (controller ruling, fix-round). Detection is scoped to the current
  `components_df` batch only - it does not re-read previously-written
  membership rows (those are assumed already reconciled when they were
  written).
- `membership_effective_from`/`membership_effective_to` are the vendor's
  own `start_date`/`end_date`, passed through unchanged (as
  `datetime.date`, per the schema's `date32` columns) under the
  `MEMBERSHIP_BOUNDARY_V1` policy (see the module constant below) - no
  session-shifting or off-by-one adjustment is applied here.
- Idempotency: a candidate row is skipped (not re-written) if
  `memberships_reader(root)` already contains a row with the same
  `(index_id, source_constituent_code, membership_effective_from,
  membership_effective_to, listing_id)` tuple (the `listing_id` from
  resolution is part of the key, so a byte-identical fact - same vendor
  interval resolving to the same listing, or the same vendor interval
  staying unresolved with `listing_id=None` both times - produces no new
  row on a re-run; this key deliberately does NOT include
  `resolution_status`, so a re-run that recomputes the exact same
  overlap classification is correctly deduped too).
- Knowledge time: `observed_at_ts`/`available_at_ts`/`system_from_ts` are
  all the caller-supplied `observed_at` (the capture's knowledge time);
  `system_to_ts` is always `None` (this module never closes a row - no
  supersession is implemented here; `supersedes_membership_version_id`
  is always `None`).
- `index_id='idx_sp500'`, `index_vendor_symbol='GSPC.INDX'` (module
  constants `INDEX_ID`/`INDEX_VENDOR_SYMBOL`), used as defaults for every
  row this module writes.

`MEMBERSHIP_BOUNDARY_V1`
------------------------
A named policy-version constant, NOT a column on any row (the schema has
no such column) - it exists to be exported into backtest run manifests
(`ops.backtest_run_manifest`, a later episode) so a future consumer can
tell which boundary interpretation a given membership snapshot was built
under, without polluting `silver.index_membership_version`'s rows with a
free-text field the schema was never given. The policy it names, verbatim
per the task brief: a constituent is included EFFECTIVE AT that session's
OPEN on `membership_effective_from`, and its LAST included session is
`membership_effective_to` itself (i.e. `membership_effective_to` is the
last member session, not an exclusive/day-after boundary) - this is a
documentation-only convention for how callers should interpret the two
DATE columns; this module does not enforce or adjust dates to match it,
since the vendor's `start_date`/`end_date` are passed through unchanged
(see above).
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
from dataclasses import dataclass

import pandas as pd

from tick_vault.ids import new_id

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

INDEX_ID = "idx_sp500"
INDEX_VENDOR_SYMBOL = "GSPC.INDX"
SOURCE_SYSTEM = "eodhd"

AUTO_CONFIDENCE = decimal.Decimal("0.9000")
UNRESOLVED_CONFIDENCE = decimal.Decimal("0.0000")

RESOLUTION_RESOLVED = "RESOLVED"
RESOLUTION_UNRESOLVED = "UNRESOLVED"
RESOLUTION_AMBIGUOUS = "AMBIGUOUS"

DQ_CHECK_MEMBERSHIP_OVERLAP = "MEMBERSHIP_OVERLAP"
DQ_CHECK_MEMBERSHIP_START_UNKNOWN = "MEMBERSHIP_START_UNKNOWN"

# Controller ruling (fix-round): `membership_effective_from` is a
# non-nullable date32 column, so a Components-only row with no vendor
# `start_date` can never get a bare `None` written into it. Instead its
# interval is bounded at the capture's OWN knowledge time
# (`observed_at.date()`), regardless of whether the code goes on to
# resolve or not, and the row is tagged with this `inclusion_reason` so a
# later Phase-0 episode (which has access to real corporate-action/
# constituent-history data) can refine the bound. See `compute_membership`.
INCLUSION_REASON_START_UNKNOWN = "START_DATE_UNKNOWN_BOUNDED_AT_OBSERVATION"

# Boundary policy version (see module docstring): join effective at that
# session's open; leave date = last member session. Exported for backtest
# manifests, never stored on a membership row.
MEMBERSHIP_BOUNDARY_V1 = "MEMBERSHIP_BOUNDARY_V1"

_MEMBERSHIP_COLUMNS = [
    "membership_version_id", "index_id", "index_vendor_symbol", "listing_id",
    "instrument_id", "source_constituent_code", "source_exchange_code",
    "source_name", "membership_effective_from", "membership_effective_to",
    "inclusion_reason", "exclusion_reason", "index_weight", "observed_at_ts",
    "available_at_ts", "system_from_ts", "system_to_ts", "source_system",
    "source_capture_id", "resolution_status", "confidence",
    "supersedes_membership_version_id",
]

_DQ_COLUMNS = [
    "issue_id", "check_name", "severity", "trade_date", "listing_id",
    "instrument_id", "source_capture_id", "details_json", "status",
    "detected_at_ts", "resolved_at_ts", "ingestion_run_id",
]


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
    """Coerce `value` into a tz-aware UTC `datetime.datetime`, or `None`
    (see `tick_vault.master._to_utc_ts`, duplicated here to keep this
    module import-independent of `tick_vault.master`)."""
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


def _to_date(value) -> "dt.date | None":
    """Coerce `value` (`None` / `datetime.date` / `datetime.datetime` /
    `pandas.Timestamp`) into a bare `datetime.date`, or `None` - for the
    schema's `date32` `membership_effective_from`/`_to` columns."""
    if _is_missing(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().date()
    if isinstance(value, dt.date):
        return value
    return value


def _empty(columns: list) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def _row(columns: list, **overrides) -> dict:
    row = {c: None for c in columns}
    for key, value in overrides.items():
        if key in row:
            row[key] = value
    return row


_MIN_DATE = dt.date.min


def _sort_key(value: "dt.date | None") -> dt.date:
    """`None` (an open/unbounded start) sorts first - never "later" than
    any real date - per the overlap rule's "later-starting" tie-break."""
    return _MIN_DATE if value is None else value


def _intervals_overlap(from_a, to_a, from_b, to_b) -> bool:
    """`[from_a, to_a] overlaps [from_b, to_b]` under `MEMBERSHIP_BOUNDARY_
    V1`'s INCLUSIVE `effective_to` semantics (controller ruling, fix-
    round): `effective_to` is the LAST included session, not an
    exclusive/day-after boundary (see the module docstring), so two
    same-listing intervals that merely TOUCH at a shared boundary date
    (`to_a == from_b`) both claim that same session and DO overlap - this
    is a half-open-looking signature (`[from_a, to_a)`-style args) but the
    comparison is deliberately `<` rather than `<=` to get inclusive-
    endpoint behavior. A `None` bound is open on that side (unbounded
    start/end). No overlap iff one interval ends (strictly) before the
    other starts."""
    if to_a is not None and from_b is not None and to_a < from_b:
        return False
    if to_b is not None and from_a is not None and to_b < from_a:
        return False
    return True


@dataclass(frozen=True)
class MembershipReport:
    resolved: int
    ambiguous: int
    unresolved: int
    written: int


# ---------------------------------------------------------------------------
# pure compute core
# ---------------------------------------------------------------------------

def _existing_keys(memberships_df: "pd.DataFrame | None") -> set:
    if memberships_df is None or memberships_df.empty:
        return set()
    keys = set()
    for _, r in memberships_df.iterrows():
        keys.add((
            r.get("index_id"),
            r.get("source_constituent_code"),
            _to_date(r.get("membership_effective_from")),
            _to_date(r.get("membership_effective_to")),
            r.get("listing_id"),
        ))
    return keys


def compute_membership(
    existing_df: "pd.DataFrame | None",
    components_df: pd.DataFrame,
    resolver,
    capture_id: str,
    observed_at,
    *,
    index_id: str = INDEX_ID,
    index_vendor_symbol: str = INDEX_VENDOR_SYMBOL,
) -> dict:
    """Pure compute core: `components_df` (Task-3's `parse_components`
    shape) + an injected `resolver(code, date) -> (listing_id,
    instrument_id) | None` -> new `silver.index_membership_version` /
    `ops.data_quality_issue` rows, plus a `MembershipReport`.

    `existing_df` (shaped like `silver.index_membership_version`, or
    `None`/empty) is consulted ONLY for idempotency dedup (see module
    docstring); it plays no part in overlap detection, which is scoped to
    `components_df` alone.
    """
    observed_ts = _to_utc_ts(observed_at)
    existing_keys = _existing_keys(existing_df)
    # Snapshot BEFORE this run mutates `existing_keys` (the write loop
    # below adds each newly-written row's key as it goes) - used to tell
    # whether a candidate participating in an overlap was already written
    # on some prior run (see the overlap dq-dedup fix below).
    existing_keys_snapshot = set(existing_keys)

    # The capture's own knowledge-time date - the bound used for
    # Components-only rows with no vendor `start_date` (controller ruling,
    # see `INCLUSION_REASON_START_UNKNOWN`'s docstring above), since
    # `membership_effective_from` is a non-nullable column and can never
    # be written as bare `None`.
    observed_date = observed_ts.date() if observed_ts is not None else _to_date(observed_at)

    candidates = []
    start_unknown_dq_rows = []
    for _, r in components_df.iterrows():
        code = r.get("code")
        if _is_missing(code):
            continue
        start_date = _to_date(r.get("start_date"))
        end_date = _to_date(r.get("end_date"))

        start_unknown = start_date is None
        resolve_at_date = observed_date if start_unknown else start_date
        resolved = resolver(code, resolve_at_date) if resolve_at_date is not None else None
        if resolved is not None:
            listing_id, instrument_id = resolved
            status = RESOLUTION_RESOLVED
            confidence = AUTO_CONFIDENCE
        else:
            listing_id, instrument_id = None, None
            status = RESOLUTION_UNRESOLVED
            confidence = UNRESOLVED_CONFIDENCE

        effective_from = observed_date if start_unknown else start_date
        inclusion_reason = INCLUSION_REASON_START_UNKNOWN if start_unknown else None

        candidates.append({
            "code": code,
            "exchange_code": r.get("exchange_code") if "exchange_code" in components_df.columns else None,
            "name": r.get("name"),
            "start_date": effective_from,
            "end_date": end_date,
            "listing_id": listing_id,
            "instrument_id": instrument_id,
            "status": status,
            "confidence": confidence,
            "inclusion_reason": inclusion_reason,
        })

        if start_unknown:
            start_unknown_dq_rows.append(_row(
                _DQ_COLUMNS,
                issue_id=new_id("wrk"),
                check_name=DQ_CHECK_MEMBERSHIP_START_UNKNOWN,
                severity="WARN",
                listing_id=listing_id,
                instrument_id=instrument_id,
                source_capture_id=capture_id,
                details_json=json.dumps({
                    "index_id": index_id,
                    "code": code,
                    "bounded_effective_from": effective_from.isoformat() if effective_from else None,
                    "resolution_status": status,
                }),
                status="OPEN",
                detected_at_ts=observed_ts,
            ))

    # Overlap detection: scoped to resolved candidates in THIS batch,
    # grouped by (index_id, listing_id) - index_id is fixed per call, so
    # effectively grouped by listing_id.
    by_listing: "dict[str, list[int]]" = {}
    for i, c in enumerate(candidates):
        if c["status"] != RESOLUTION_RESOLVED:
            continue
        by_listing.setdefault(c["listing_id"], []).append(i)

    def _candidate_key(c) -> tuple:
        return (index_id, c["code"], c["start_date"], c["end_date"], c["listing_id"])

    dq_rows = []
    for listing_id, idxs in by_listing.items():
        idxs.sort(key=lambda i: _sort_key(candidates[i]["start_date"]))
        accepted: "list[int]" = []  # indices of intervals accepted so far (non-ambiguous)
        for i in idxs:
            cur = candidates[i]
            conflict = None
            for j in accepted:
                other = candidates[j]
                if _intervals_overlap(
                    other["start_date"], other["end_date"],
                    cur["start_date"], cur["end_date"],
                ):
                    conflict = other
                    break
            if conflict is not None:
                candidates[i]["status"] = RESOLUTION_AMBIGUOUS
                # Controller ruling (fix-round): only emit the overlap
                # quality-issue row if at least one of the two
                # participating candidates is a row NEWLY WRITTEN in
                # THIS run (i.e. its dedup key wasn't already present in
                # `memberships_reader`'s snapshot before this call) - an
                # idempotent re-run over the exact same `components_df`
                # re-derives the identical AMBIGUOUS/RESOLVED
                # classification (see below) but must not re-append a
                # duplicate issue row for a conflict that was already
                # reported and written on a prior run.
                if (
                    _candidate_key(cur) not in existing_keys_snapshot
                    or _candidate_key(conflict) not in existing_keys_snapshot
                ):
                    dq_rows.append(_row(
                        _DQ_COLUMNS,
                        issue_id=new_id("wrk"),
                        check_name=DQ_CHECK_MEMBERSHIP_OVERLAP,
                        severity="WARN",
                        listing_id=listing_id,
                        instrument_id=cur["instrument_id"],
                        source_capture_id=capture_id,
                        details_json=json.dumps({
                            "index_id": index_id,
                            "listing_id": listing_id,
                            "code_a": conflict["code"],
                            "start_a": conflict["start_date"].isoformat() if conflict["start_date"] else None,
                            "end_a": conflict["end_date"].isoformat() if conflict["end_date"] else None,
                            "code_b": cur["code"],
                            "start_b": cur["start_date"].isoformat() if cur["start_date"] else None,
                            "end_b": cur["end_date"].isoformat() if cur["end_date"] else None,
                        }),
                        status="OPEN",
                        detected_at_ts=observed_ts,
                    ))
            else:
                accepted.append(i)

    resolved_count = sum(1 for c in candidates if c["status"] == RESOLUTION_RESOLVED)
    ambiguous_count = sum(1 for c in candidates if c["status"] == RESOLUTION_AMBIGUOUS)
    unresolved_count = sum(1 for c in candidates if c["status"] == RESOLUTION_UNRESOLVED)

    membership_rows = []
    written = 0
    for c in candidates:
        key = (index_id, c["code"], c["start_date"], c["end_date"], c["listing_id"])
        if key in existing_keys:
            continue
        membership_rows.append(_row(
            _MEMBERSHIP_COLUMNS,
            membership_version_id=new_id("mem"),
            index_id=index_id,
            index_vendor_symbol=index_vendor_symbol,
            listing_id=c["listing_id"],
            instrument_id=c["instrument_id"],
            source_constituent_code=c["code"],
            source_exchange_code=c["exchange_code"],
            source_name=c["name"],
            membership_effective_from=c["start_date"],
            membership_effective_to=c["end_date"],
            inclusion_reason=c["inclusion_reason"],
            observed_at_ts=observed_ts,
            available_at_ts=observed_ts,
            system_from_ts=observed_ts,
            source_system=SOURCE_SYSTEM,
            source_capture_id=capture_id,
            resolution_status=c["status"],
            confidence=c["confidence"],
        ))
        written += 1
        # Guard the in-call key set too, so a duplicate row within the
        # SAME components_df batch (shouldn't happen given parse_
        # components's per-code dedup, but defensive) doesn't double-write.
        existing_keys.add(key)

    all_dq_rows = start_unknown_dq_rows + dq_rows

    return {
        "index_membership_version": pd.DataFrame(membership_rows, columns=_MEMBERSHIP_COLUMNS),
        "data_quality_issue": pd.DataFrame(all_dq_rows, columns=_DQ_COLUMNS),
        "report": MembershipReport(
            resolved=resolved_count,
            ambiguous=ambiguous_count,
            unresolved=unresolved_count,
            written=written,
        ),
    }


# ---------------------------------------------------------------------------
# Delta-backed defaults (lazy pyarrow/deltalake imports)
# ---------------------------------------------------------------------------

def _default_memberships_reader(root: str) -> pd.DataFrame:
    from deltalake import DeltaTable

    return DeltaTable(f"{root}/silver/index_membership_version").to_pandas()


def _default_writer(root: str, table: str, df: pd.DataFrame) -> None:
    from tick_vault.capture import append_rows

    append_rows(root, table, df)


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def build_membership(
    root: str,
    components_df: pd.DataFrame,
    *,
    resolver,
    capture_id: str,
    observed_at,
    memberships_reader=None,
    writer=None,
    index_id: str = INDEX_ID,
    index_vendor_symbol: str = INDEX_VENDOR_SYMBOL,
    boundary_policy: str = MEMBERSHIP_BOUNDARY_V1,
) -> MembershipReport:
    """Build and write `silver.index_membership_version` rows (+ any
    `ops.data_quality_issue` overlap rows) from `components_df` (Task-3's
    `parse_components` shape).

    `resolver(code, date) -> (listing_id, instrument_id) | None` is
    REQUIRED and must be supplied by the caller (e.g. a bound method on a
    Task-4 `MasterBuilder`) - this module never constructs one itself
    (see module docstring's dependency-inversion note).

    `memberships_reader`/`writer` default to real Delta reads/appends
    (lazy-imported); tests inject fakes over an in-memory `dict[str,
    DataFrame]` (see `tests/test_membership_core.py`).

    `boundary_policy` is accepted for forward-compatibility with backtest
    manifest generation (see `MEMBERSHIP_BOUNDARY_V1`'s docstring) - it is
    not currently interpreted here (this module always uses
    MEMBERSHIP_BOUNDARY_V1 semantics; passing a different value is not
    validated, since there is no second policy implemented yet).
    """
    reader = memberships_reader or _default_memberships_reader
    write = writer or _default_writer

    existing_df = reader(root)
    result = compute_membership(
        existing_df,
        components_df,
        resolver,
        capture_id,
        observed_at,
        index_id=index_id,
        index_vendor_symbol=index_vendor_symbol,
    )

    membership_rows = result["index_membership_version"]
    if membership_rows is not None and not membership_rows.empty:
        write(root, "silver.index_membership_version", membership_rows)

    dq_rows = result["data_quality_issue"]
    if dq_rows is not None and not dq_rows.empty:
        write(root, "ops.data_quality_issue", dq_rows)

    return result["report"]

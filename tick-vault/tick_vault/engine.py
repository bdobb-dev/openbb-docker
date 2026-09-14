"""Manifest generator + re-backfill fetch core (task-7 brief).

The heart of the re-backfill engine: turns bitemporal membership/identity
state into `ops.backfill_manifest` work items (`generate_manifest` /
`ManifestBuilder`), and turns one manifest item into real bronze captures
+ silver tick rows (`fetch_week`), replaying `scripts/sip_backfill.py`'s
proven operational core (span-halving, 429 budget backoff, boundary-second
dedup) re-hosted on the medallion path (`tick_vault.capture`/
`tick_vault.tick_parser`/`tick_vault.tick_writer`).

Design seams (binding, matching task-4/5/6's `MasterBuilder`/
`build_membership`/FIGI pattern)
-------------------------------------------------------------------------
Every read of persistent state is an injectable `*_reader(root) -> pandas
DataFrame` callable (`membership_reader`, `assignments_reader`,
`manifest_reader`), and every write is an injectable `writer(root, table,
df) -> None` (or, for the manifest specifically, `manifest_writer`,
matching the "ops state is mutable, overwrite/append via a seam" pattern
already used for `ops.openfigi_resolution_queue` in `tick_vault.master`/
`tick_vault.figi`). None of the defaults are imported at module scope -
`deltalake`/`pyarrow` stay lazy-imported inside the `_default_*` functions
- so this module (and its pure logic: `generate_manifest`, `symbol_at`,
`Budget`, `SpanMemory`, `build_tick_url`, `dedup_ticks`, `FetchResult`)
imports and runs bare in the pyarrow/deltalake-less authoring sandbox
(`tests/test_engine_core.py`, no pytest). The Delta-backed shells
(`ManifestBuilder`, and `fetch_week`'s *default* `writer`/
`existing_ticks_reader`/`verifier`, which a caller may leave un-injected)
are exercised for real Delta round-tripping only in `tests/deferred/
test_engine.py` (pytest, real tmp_path lake, `capture.FakeTransport`).

`symbol_at` - the missing inverse of `tick_vault.master.resolve_listing_at`
-------------------------------------------------------------------------
The manifest needs, for a given `listing_id` and week, "what vendor symbol
was this listing trading under, as of that date" - the INVERSE direction
of `master.resolve_listing_at`/`resolve_symbol_at` (which go `code ->
listing_id`). `tick_vault.master` has no such function, and per this
module's task brief either module was an acceptable home for it; it lives
here (not added to `tick_vault.master`) to avoid growing that module's
surface for a lookup only the manifest generator needs, and because
`tick_vault.membership` already sets the precedent of duplicating
`master`'s small `_to_utc_ts`/`_is_missing`/"current row" helpers locally
rather than importing them (see `membership.py`'s module docstring) - this
module follows that same convention rather than reaching into `master`'s
private (underscore-prefixed) helpers.

`ops.backfill_manifest` schema deviations from the task brief (binding -
this module follows the REGISTERED schema, `tick_vault.schemas.
_OPS_BACKFILL_MANIFEST`, over the brief's looser prose description)
-------------------------------------------------------------------------
1. No `wall_minutes`/`details_json` columns exist on `ops.
   backfill_manifest` (verified against `tick_vault.schemas.py`, the only
   place the table's columns are actually declared). `FetchResult.
   wall_minutes` is therefore a return value for the caller's own
   logging/ops-status purposes only - it is NEVER persisted onto a
   manifest row. Likewise `SpanMemory`'s per-symbol working-span state is
   NOT serialized into any manifest row's (nonexistent) `details_json`;
   `SpanMemory.to_json`/`from_json` exist so a caller can persist it
   wherever it likes (a small sidecar JSON file, an `ops.*` table added by
   a later episode, ...) - this module takes no position on where.
2. `vendor_symbol_at_date` is declared `nullable=False`. A `BLOCKED_
   IDENTITY` row (symbol unresolvable) is nonetheless represented with
   `vendor_symbol_at_date=None` in `generate_manifest`'s RETURNED
   DataFrame (matching the task brief's explicit "with symbol None", and
   letting a runnable test assert `row["vendor_symbol_at_date"] is None`
   without touching pyarrow) - but `ManifestBuilder.generate`, which
   actually appends the frame to the real Delta table, coerces that
   `None` to `""` (empty string) immediately before writing, since a
   `None` would fail `pa.Table.from_pylist`'s exact-schema cast against a
   non-nullable string column. `""` is unambiguous here: no real vendor
   symbol is ever the empty string.
3. `attempts`/`priority` are declared `int32`; this module always writes
   plain Python `int`s into those columns; the actual int32 narrowing
   happens later, in `pa.Table.from_pylist(schema=...)` at write time
   (same convention `tick_vault.master`/`tick_vault.membership` already
   follow for their own `int32` columns).

`fetch_week`'s 5xx/timeout-halving rule (binding simplification, task
brief's own words)
-------------------------------------------------------------------------
The task brief's "status 5xx AND elapsed>60s" trigger is explicitly given
a documented simplification in the same sentence: "treat any 5xx on a
>1-day window as halve-signal, 5xx on <=1-day window after 3 tries ->
window FAILED". This module implements exactly that simplification and
nothing more - no per-call elapsed-time tracking is threaded through
`Transport`/`FakeTransport` at all (a real elapsed-time signal was
considered and dropped: it would have required a new response shape
`(status, body, elapsed)` on a transport wrapper, which the task brief
flags should NOT touch `tick_vault.capture.FakeTransport`'s existing
`(status, body)` contract; since the window-size rule alone fully decides
every one of the brief's 8 named test scenarios, adding an unused
elapsed-time seam would be complexity with no behavioral payoff. If a
later episode needs the real elapsed-time trigger, `capture.FakeTransport`
still doesn't need to change - a purpose-built transport wrapper local to
this module's own tests would.)

Re-run idempotency: append-only, NOT delete-and-rewrite (binding
controller ruling, documented deviation from the plan's prose)
-------------------------------------------------------------------------
The task brief's plan text describes re-running an already-`COMPLETE`
tranche as "delete-and-rewrite its partition slice". `silver.
us_trade_tick_version` is append-only by construction everywhere else in
this codebase (`tick_vault.tick_writer`'s own docstring: "Never replaces
existing data - every call is an append... calling this twice with the
same `df` duplicates rows") - introducing a delete here would be the only
place in the whole pipeline that mutates/removes committed silver rows,
which breaks the bitemporal audit trail (`available_at_ts`/
`system_from_ts`/`system_to_ts`) every other writer in this repo is
built to preserve. Ruling: `fetch_week` instead implements re-run
idempotency via a `dedup`, not a `delete`: given an injectable
`existing_ticks_reader(root, listing_id) -> pandas.DataFrame` (rows
already in `silver.us_trade_tick_version` for that listing, shaped like
`tick_parser.COLUMNS`), any freshly-parsed logical tick whose `(trade_
ts_ms, session_seq)` key already exists in that frame is dropped BEFORE
`write_tick_versions` is called - so a re-run of a `COMPLETE` week that
hits the exact same vendor data appends zero new rows (idempotent by
omission, never by physically removing what's already there). A tick
whose (ts, seq) key already exists but whose VALUE differs (a genuine
vendor correction/late-add) is deliberately left alone here too - that is
Task-8's `diffing.diff_window` correction machinery's job, not this
fetch/write path's; `fetch_week` only ever writes brand-new logical
ticks, never a correction row.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass

import pandas as pd

from tick_vault.ids import new_id
from tick_vault.tick_parser import parse_tick_payload
from tick_vault.tick_writer import write_tick_versions

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

ENGINE_PARSER_VERSION = "TICK_ENGINE_FETCH_V1"
AVAILABILITY_POLICY_VERSION = "BACKFILL_MANIFEST_V1"
DEFAULT_SNAPSHOT_ID = "adhoc"

STATUS_PENDING = "PENDING"
STATUS_BLOCKED_IDENTITY = "BLOCKED_IDENTITY"
STATUS_COMPLETE = "COMPLETE"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAILED = "FAILED"

MISSING_PRIORITY_RANK = 999999

_IDENTITY_NAMESPACES = ("EODHD_SYMBOL", "TICKER")

MANIFEST_COLUMNS = [
    "work_id", "listing_id", "week_monday", "instrument_id",
    "vendor_symbol_at_date", "eodhd_exchange_code",
    "request_from_sec", "request_to_sec",
    "membership_snapshot_id", "identity_snapshot_id",
    "availability_policy_version",
    "status", "attempts", "priority",
    "completed_at_ts", "last_error",
    "created_at_ts", "updated_at_ts",
]

# sip_backfill semantics (carried over verbatim, per the episode plan):
# 30-minute waits, 12 consecutive waits (== 6 hours of sleeping) before
# giving up.
DEFAULT_WAIT_SECONDS = 1800
DEFAULT_MAX_CONSECUTIVE_WAITS = 12

# Per-symbol working span, in days (sip_backfill semantics).
DEFAULT_SPAN_DAYS = 7.0
MIN_SPAN_DAYS = 0.5

# EODHD's tick-by-tick endpoint (task-7 brief; unverified against a live
# response, same "URL verification caveat" as tick_vault.eodhd_reference's
# `_ENDPOINTS`). `{token}` is the raw api key, substituted at call time by
# the caller of `build_tick_url`, never stored.
TICK_ENDPOINT_TEMPLATE = "https://eodhd.com/api/ticks?s={symbol}&from={frm}&to={to}&api_token={token}"
_REDACTED = "REDACTED"


# ---------------------------------------------------------------------------
# small shared helpers (duplicated from tick_vault.master/membership by the
# same convention those two modules already use for each other - see
# module docstring)
# ---------------------------------------------------------------------------

def _is_missing(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return value == ""


def _to_date(value) -> "dt.date | None":
    if _is_missing(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().date()
    if isinstance(value, dt.date):
        return value
    return value


def _to_utc_ts(value) -> "dt.datetime | None":
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


def _item_get(item, key, default=None):
    """`item` is either a plain `dict` or a `pandas.Series` (one manifest
    row) - both support `.get(key, default)`, so this is mostly a
    single choke point to document that either shape is accepted."""
    return item.get(key, default)


def _empty(columns: list) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


# ---------------------------------------------------------------------------
# symbol_at: the inverse of tick_vault.master.resolve_listing_at
# ---------------------------------------------------------------------------

def _current_assignment_rows(assignments_df: "pd.DataFrame | None") -> pd.DataFrame:
    """Mirrors `tick_vault.master._current_rows`: currently-open rows
    (`system_to_ts IS NULL`)."""
    if assignments_df is None or assignments_df.empty:
        return _empty(list(assignments_df.columns) if assignments_df is not None else [])
    if "system_to_ts" in assignments_df.columns:
        mask = assignments_df["system_to_ts"].isna()
    else:
        mask = pd.Series(True, index=assignments_df.index)
    return assignments_df[mask]


def symbol_at(
    assignments_df: "pd.DataFrame | None",
    listing_id: str,
    at_date,
    namespaces=_IDENTITY_NAMESPACES,
) -> "str | None":
    """`listing_id` -> the vendor symbol code it traded under as of
    `at_date`, or `None` if unresolvable.

    The INVERSE lookup direction of `tick_vault.master.resolve_listing_at`
    (which goes `code -> listing_id`): here we already know the
    `listing_id` (from membership) and want "what code identified it on
    this date" so the manifest can build a request URL. Same matching/
    tie-break rule as `master._resolve_covering_row`: among the current
    (non-system-closed) assignment rows in `namespaces` for this
    `listing_id`, whichever one's effective interval `[effective_from_ts,
    effective_to_ts)` covers `at_date` wins; ties broken by the
    most-recently-asserted row (highest `system_from_ts`, falling back to
    `observed_at_ts`).
    """
    if _is_missing(listing_id):
        return None
    current = _current_assignment_rows(assignments_df)
    if current.empty:
        return None
    candidates = current[
        current["id_namespace"].isin(namespaces) & (current["listing_id"] == listing_id)
    ]
    if candidates.empty:
        return None

    at_ts = _to_utc_ts(at_date)

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
    return covering.iloc[-1]["id_value"]


# ---------------------------------------------------------------------------
# generate_manifest (pure)
# ---------------------------------------------------------------------------

def _week_bounds_utc(week_monday: dt.date) -> "tuple[int, int]":
    """`(request_from_sec, request_to_sec)` for the ISO week starting
    `week_monday`: Monday 00:00:00 UTC .. Saturday 00:00:00 UTC minus one
    second (the task brief's inclusive-boundary rule - EODHD's `from`/`to`
    are both inclusive, so ending one second before Saturday midnight
    avoids double-serving that shared boundary second across two
    consecutive weekly manifest items; `fetch_week` additionally dedups
    `(ts, seq)` across windows anyway, as defense in depth)."""
    monday_utc = dt.datetime(week_monday.year, week_monday.month, week_monday.day, tzinfo=dt.timezone.utc)
    saturday_utc = monday_utc + dt.timedelta(days=5)
    from_sec = int(monday_utc.timestamp())
    to_sec = int(saturday_utc.timestamp()) - 1
    return from_sec, to_sec


def _intervals_overlap(from_a, to_a, from_b, to_b) -> bool:
    """`[from_a, to_a] overlaps [from_b, to_b]`, inclusive on both ends, a
    `None` bound open on that side. Mirrors `tick_vault.membership.
    _intervals_overlap`'s inclusive-`effective_to` semantics."""
    if to_a is not None and from_b is not None and to_a < from_b:
        return False
    if to_b is not None and from_a is not None and to_b < from_a:
        return False
    return True


def _existing_manifest_keys(existing_manifest_df: "pd.DataFrame | None") -> set:
    if existing_manifest_df is None or existing_manifest_df.empty:
        return set()
    keys = set()
    for _, r in existing_manifest_df.iterrows():
        keys.add((r.get("listing_id"), _to_date(r.get("week_monday"))))
    return keys


def generate_manifest(
    membership_df: "pd.DataFrame | None",
    assignments_df: "pd.DataFrame | None",
    first_week,
    last_week,
    *,
    priority_caps: "dict[str, int] | None" = None,
    existing_manifest_df: "pd.DataFrame | None" = None,
    membership_snapshot_id: str = DEFAULT_SNAPSHOT_ID,
    identity_snapshot_id: str = DEFAULT_SNAPSHOT_ID,
    availability_policy_version: str = AVAILABILITY_POLICY_VERSION,
    created_at=None,
) -> pd.DataFrame:
    """Pure compute core: for each Monday week in `[first_week, last_week]`
    and each listing whose membership tenure overlaps that week AT ALL
    (a mid-week add/delete gets the whole week - `membership_df`'s
    RESOLVED intervals for that listing are checked for ANY overlap with
    the week's `[week_monday, week_monday+6]` calendar span, which is
    mathematically equivalent to checking the *union* of that listing's
    resolved intervals for overlap), returns one NEW `ops.backfill_
    manifest`-shaped row (see `MANIFEST_COLUMNS`).

    `membership_df` is `silver.index_membership_version`-shaped (task-5's
    `tick_vault.membership` output): only `resolution_status == 'RESOLVED'`
    rows are considered - an `AMBIGUOUS`/`UNRESOLVED` membership fact has
    no reliable `listing_id` to backfill against. `assignments_df` is
    `silver.identifier_assignment_version`-shaped (task-4's `tick_vault.
    master` output), consulted via `symbol_at` to resolve each listing's
    vendor symbol AT the week.

    `vendor_symbol_at_date` resolution first tries `symbol_at(assignments,
    listing_id, week_monday)`; if that fails, it falls back to trying each
    of the other 6 days of the week (Tue..Sun) in order before giving up -
    a listing whose identifier-assignment row's `effective_from_ts` lands
    mid-week (e.g. a Wednesday IPO/relist) would otherwise be wrongly
    BLOCKED just because Monday itself predates the assignment. A listing
    that still can't be resolved on any day of the week gets `status=
    'BLOCKED_IDENTITY'`, `vendor_symbol_at_date=None` (see module
    docstring's schema-deviation note #2 for how the shell persists that);
    otherwise `status='PENDING'`.

    `priority` is `priority_caps.get(vendor_symbol_at_date, 999999)` (a
    market-cap-descending rank map the caller supplies; a `BLOCKED_
    IDENTITY` row has no symbol to rank, so it always gets the same
    lowest-priority fallback).

    Idempotent: a `(listing_id, week_monday)` pair already present in
    `existing_manifest_df` is skipped - never re-emitted, never updated.
    """
    priority_caps = priority_caps or {}
    created_at_ts = _to_utc_ts(created_at) if created_at is not None else dt.datetime.now(dt.timezone.utc)
    existing_keys = _existing_manifest_keys(existing_manifest_df)

    if membership_df is None or membership_df.empty:
        resolved = _empty(["listing_id", "instrument_id", "membership_effective_from", "membership_effective_to"])
    else:
        resolved = membership_df[membership_df["resolution_status"] == "RESOLVED"]

    first = _to_date(first_week)
    last = _to_date(last_week)
    if first is None or last is None:
        return _empty(MANIFEST_COLUMNS)

    rows = []
    week_monday = first
    while week_monday <= last:
        week_end = week_monday + dt.timedelta(days=6)
        from_sec, to_sec = _week_bounds_utc(week_monday)

        listing_instrument: "dict[str, object]" = {}
        for _, r in resolved.iterrows():
            listing_id = r.get("listing_id")
            if _is_missing(listing_id):
                continue
            interval_from = _to_date(r.get("membership_effective_from"))
            interval_to = _to_date(r.get("membership_effective_to"))
            if _intervals_overlap(interval_from, interval_to, week_monday, week_end):
                listing_instrument.setdefault(listing_id, r.get("instrument_id"))

        for listing_id in sorted(listing_instrument):
            key = (listing_id, week_monday)
            if key in existing_keys:
                continue

            symbol = None
            for offset in range(7):
                candidate_date = week_monday + dt.timedelta(days=offset)
                symbol = symbol_at(assignments_df, listing_id, candidate_date)
                if symbol is not None:
                    break

            if symbol is None:
                status = STATUS_BLOCKED_IDENTITY
                priority = MISSING_PRIORITY_RANK
            else:
                status = STATUS_PENDING
                priority = priority_caps.get(symbol, MISSING_PRIORITY_RANK)

            rows.append({
                "work_id": new_id("wrk"),
                "listing_id": listing_id,
                "week_monday": week_monday,
                "instrument_id": listing_instrument.get(listing_id),
                "vendor_symbol_at_date": symbol,
                "eodhd_exchange_code": None,
                "request_from_sec": from_sec,
                "request_to_sec": to_sec,
                "membership_snapshot_id": membership_snapshot_id,
                "identity_snapshot_id": identity_snapshot_id,
                "availability_policy_version": availability_policy_version,
                "status": status,
                "attempts": 0,
                "priority": priority,
                "completed_at_ts": None,
                "last_error": None,
                "created_at_ts": created_at_ts,
                "updated_at_ts": created_at_ts,
            })

        week_monday = week_monday + dt.timedelta(days=7)

    return pd.DataFrame(rows, columns=MANIFEST_COLUMNS)


# ---------------------------------------------------------------------------
# Budget (429 backoff, sip_backfill semantics)
# ---------------------------------------------------------------------------

class BudgetExhausted(Exception):
    """Raised by `Budget.wait()` once more than `max_consecutive_waits`
    429s have been seen in a row with no intervening success."""


class Budget:
    """sip_backfill's 429-backoff budget: each `wait()` call (one per 429
    response) sleeps `wait_seconds` (default 1800s/30min) via an
    injectable `sleeper` (defaults to `time.sleep`, never actually called
    in tests). After `max_consecutive_waits` (default 12, i.e. 6 hours of
    total sleeping) consecutive waits with no success in between, the
    NEXT `wait()` call raises `BudgetExhausted` instead of sleeping again.
    `reset()` (called by a caller on any successful/non-429 response)
    zeroes the counter, so bursts of 429s are only fatal if they never let
    up for 12 straight waits.
    """

    def __init__(
        self,
        *,
        wait_seconds: int = DEFAULT_WAIT_SECONDS,
        max_consecutive_waits: int = DEFAULT_MAX_CONSECUTIVE_WAITS,
        sleeper=None,
    ):
        self.wait_seconds = wait_seconds
        self.max_consecutive_waits = max_consecutive_waits
        self._sleeper = sleeper or time.sleep
        self.consecutive_waits = 0

    def wait(self) -> None:
        self.consecutive_waits += 1
        if self.consecutive_waits > self.max_consecutive_waits:
            raise BudgetExhausted(
                f"budget exhausted: {self.consecutive_waits - 1} consecutive "
                f"429 waits (limit {self.max_consecutive_waits})"
            )
        self._sleeper(self.wait_seconds)

    def reset(self) -> None:
        self.consecutive_waits = 0


# ---------------------------------------------------------------------------
# SpanMemory (per-symbol working span, sip_backfill semantics)
# ---------------------------------------------------------------------------

class SpanMemory:
    """Per-symbol working request-window span, in days. Starts every
    unseen symbol at `default_span_days` (7.0); `halve(symbol)` halves its
    current span, floored at `min_span_days` (0.5), and remembers the new
    value for that symbol going forward (`span(symbol)` after a halve
    returns the halved value, not the default). Pure/in-memory; `to_json`/
    `from_json` let a caller persist and restore the whole per-symbol map
    across process runs (see module docstring's schema-deviation note #1
    for why that persistence is NOT wired into `ops.backfill_manifest`
    itself).
    """

    def __init__(
        self,
        spans: "dict[str, float] | None" = None,
        *,
        default_span_days: float = DEFAULT_SPAN_DAYS,
        min_span_days: float = MIN_SPAN_DAYS,
    ):
        self._spans: "dict[str, float]" = dict(spans or {})
        self.default_span_days = default_span_days
        self.min_span_days = min_span_days

    def span(self, symbol: str) -> float:
        return self._spans.get(symbol, self.default_span_days)

    def halve(self, symbol: str) -> float:
        new_span = max(self.min_span_days, self.span(symbol) / 2.0)
        self._spans[symbol] = new_span
        return new_span

    def set_span(self, symbol: str, span_days: float) -> None:
        self._spans[symbol] = span_days

    def to_json(self) -> str:
        return json.dumps(self._spans, sort_keys=True)

    @classmethod
    def from_json(cls, payload: "str | None", **kwargs) -> "SpanMemory":
        spans = json.loads(payload) if payload else {}
        return cls(spans, **kwargs)


# ---------------------------------------------------------------------------
# build_tick_url / windowing / dedup (pure)
# ---------------------------------------------------------------------------

def build_tick_url(symbol: str, frm: int, to: int, token: str) -> str:
    """The EODHD tick-by-tick endpoint URL for `symbol` over `[frm, to]`
    (unix seconds, BOTH ends inclusive per the vendor's semantics).
    `token` is the raw api key, substituted directly - callers must keep
    it out of any captured `request_params` dict (pass `"REDACTED"`
    there instead), matching `tick_vault.eodhd_reference`'s API-key-
    hygiene convention.
    """
    return TICK_ENDPOINT_TEMPLATE.format(symbol=symbol, frm=frm, to=to, token=token)


def _window_span_seconds(span_days: float) -> int:
    return max(int(round(span_days * 86400)), 1)


def request_windows(from_sec: int, to_sec: int, span_days: float) -> "list[tuple[int, int]]":
    """`[from_sec, to_sec]` (inclusive) sliced into consecutive `span_days`
    -wide inclusive-end windows: `(start, min(start+span-1, to_sec))`,
    `start` stepping to `end+1` each time, ending exactly at `to_sec`."""
    span_seconds = _window_span_seconds(span_days)
    windows = []
    start = from_sec
    while start <= to_sec:
        end = min(start + span_seconds - 1, to_sec)
        windows.append((start, end))
        start = end + 1
    return windows


def dedup_ticks(frames: "list[pd.DataFrame]") -> pd.DataFrame:
    """Concatenate `frames` (each shaped like `tick_vault.tick_parser.
    COLUMNS`, one per fetched window/page) and drop duplicate `(trade_
    ts_ms, session_seq)` keys, keeping the first occurrence - the
    boundary-second dedup rule (task brief): even though `fetch_week`'s
    windows are built to avoid re-requesting a shared boundary second (see
    `_week_bounds_utc`), this is defense in depth against a vendor payload
    that still repeats it across two window responses.
    """
    if not frames:
        from tick_vault.tick_parser import COLUMNS
        return _empty(COLUMNS)
    combined = pd.concat(frames, ignore_index=True)
    if combined.empty:
        return combined
    combined = combined.drop_duplicates(subset=["trade_ts_ms", "session_seq"], keep="first")
    return combined.reset_index(drop=True)


def _existing_tick_keys(existing_df: "pd.DataFrame | None") -> set:
    if existing_df is None or existing_df.empty:
        return set()
    return set(zip(existing_df["trade_ts_ms"], existing_df["session_seq"]))


def _drop_already_written(df: pd.DataFrame, existing_keys: set) -> pd.DataFrame:
    if not existing_keys or df.empty:
        return df
    mask = [
        (ts, seq) not in existing_keys
        for ts, seq in zip(df["trade_ts_ms"], df["session_seq"])
    ]
    return df[mask].reset_index(drop=True)


# ---------------------------------------------------------------------------
# FetchResult / fetch_week
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FetchResult:
    status: str  # COMPLETE | PARTIAL | FAILED
    rows_written: int
    captures: int
    wall_minutes: float
    error: "str | None" = None


def _default_verify(root: str, listing_id: str, written_df: pd.DataFrame) -> "tuple[bool, int]":
    """Default (no-op-read-back) verifier: since no real reader is
    injected, this only checks the in-memory frame's OWN `(trade_date,
    session_seq)` uniqueness (guaranteed already by `dedup_ticks`, so this
    default effectively always passes - it exists so `fetch_week` always
    has a verifier to call, and so a caller with a real Delta lake can
    override it with an actual read-back-and-count check, per the task
    brief)."""
    if written_df.empty:
        return True, 0
    dup = written_df.duplicated(subset=["trade_date", "session_seq"]).any()
    return (not dup), len(written_df)


def fetch_week(
    transport,
    store,
    root: str,
    item,
    *,
    span_memory: SpanMemory,
    budget: Budget,
    now: dt.datetime,
    api_token: str = _REDACTED,
    ingestion_run_id: "str | None" = None,
    writer=None,
    existing_ticks_reader=None,
    verifier=None,
    monotonic=None,
    source_system: str = "EODHD",
    price_venue_type: str = "CONSOLIDATED_US",
    max_window_retries: int = 3,
) -> FetchResult:
    """Fetch, capture, parse, dedup, and write one manifest item's tick
    week - the sip_backfill core re-hosted on the medallion path.

    `item` is one `ops.backfill_manifest`-shaped row (`dict` or `pandas.
    Series`): `listing_id`, `instrument_id`, `vendor_symbol_at_date`,
    `request_from_sec`, `request_to_sec` are read from it.

    `store` is duck-typed to `tick_vault.capture.CaptureStore`'s
    `.record(...)` method (NOT `.fetch_and_capture` - see the inline note
    below for why); production callers pass a real `CaptureStore(root)`,
    tests pass a fake with a compatible `.record(...)`.

    `writer(root, df, *, vendor_request_symbol, committed_at,
    source_system, price_venue_type) -> int` defaults to `tick_vault.
    tick_writer.write_tick_versions`; injectable so runnable (pyarrow/
    deltalake-less) tests can supply a fake that appends into an in-memory
    list instead of a real Delta table.

    `existing_ticks_reader(root, listing_id) -> pandas.DataFrame | None`
    - when given, its rows' `(trade_ts_ms, session_seq)` keys are treated
    as already-written and any freshly-parsed tick with the same key is
    dropped before `writer` is called (append-only re-run idempotency -
    see module docstring). `None` (the default) means "assume nothing
    already written" - the ordinary first-run path.

    `verifier(root, listing_id, written_df) -> (ok: bool, row_count: int)`
    defaults to `_default_verify` (an in-memory-only uniqueness check);
    inject a real read-back-and-count check against Delta for production/
    deferred-pytest use.

    Returns a `FetchResult`; never raises for ordinary HTTP failures (5xx,
    unexpected statuses) - those are reported as `PARTIAL`/`FAILED` with
    `error` set. A `Budget.wait()` -> `BudgetExhausted` (12 consecutive
    429s with no success) IS caught here too and reported as `FAILED`,
    since it is still just "this item didn't complete", not a
    caller-fatal condition - callers processing a manifest queue,
    presumably one item at a time, generally want to move on to the next
    item rather than crash the whole batch on one stuck symbol.
    """
    listing_id = _item_get(item, "listing_id")
    instrument_id = _item_get(item, "instrument_id")
    symbol = _item_get(item, "vendor_symbol_at_date")
    from_sec = int(_item_get(item, "request_from_sec"))
    to_sec = int(_item_get(item, "request_to_sec"))
    ingestion_run_id = ingestion_run_id or new_id("run")
    writer = writer or write_tick_versions
    verifier = verifier or _default_verify
    monotonic_fn = monotonic or time.monotonic

    if _is_missing(symbol):
        return FetchResult(
            status=STATUS_FAILED,
            rows_written=0,
            captures=0,
            wall_minutes=0.0,
            error="no vendor_symbol_at_date on this item (BLOCKED_IDENTITY, cannot fetch)",
        )

    started_monotonic = monotonic_fn()

    existing_keys: set = set()
    if existing_ticks_reader is not None:
        existing_keys = _existing_tick_keys(existing_ticks_reader(root, listing_id))

    frames: "list[pd.DataFrame]" = []
    captures = 0
    page_ordinal = 0
    failed_windows: "list[tuple[int, int, str]]" = []

    try:
        window_start = from_sec
        window_end = None
        while window_start <= to_sec:
            span_days = span_memory.span(symbol)
            window_end = min(window_start + _window_span_seconds(span_days) - 1, to_sec)
            window_attempts = 0

            while True:
                url = build_tick_url(symbol, window_start, window_end, api_token)
                # NOTE: this calls transport.get directly (not `store.
                # fetch_and_capture`, which ALSO calls transport.get
                # internally) so we can branch on the raw HTTP status
                # (429 vs 5xx vs 2xx) before deciding how to capture it -
                # `fetch_and_capture` collapses any non-2xx into a single
                # `payload_bytes=None`, which loses exactly the
                # distinction this loop needs. The capture row this
                # produces (via `store.record(...)`) is byte-identical to
                # what `fetch_and_capture` would have written for the same
                # transport response.
                status, body = transport.get(url, timeout=30)
                is_success = 200 <= status < 300
                request_params = {
                    "s": symbol, "from": window_start, "to": window_end,
                    "api_token": _REDACTED,
                }
                record = store.record(
                    table="bronze.eodhd_tick_capture",
                    endpoint="tick_backfill",
                    request_params=request_params,
                    http_status=status,
                    payload_bytes=body if is_success else None,
                    error_body=None if is_success else body,
                    observed_at=now,
                    ingestion_run_id=ingestion_run_id,
                    parser_version=ENGINE_PARSER_VERSION,
                    extra_cols={
                        "request_symbol": symbol,
                        "request_from_sec": window_start,
                        "request_to_sec": window_end,
                    },
                    source_system=source_system,
                )
                captures += 1

                if is_success:
                    budget.reset()
                    payload = json.loads(body) if body else []
                    frames.append(parse_tick_payload(
                        payload,
                        capture_id=record.capture_id,
                        listing_id=listing_id,
                        instrument_id=instrument_id,
                        observed_at=now,
                        page_ordinal=page_ordinal,
                    ))
                    page_ordinal += 1
                    break

                if status == 429:
                    budget.wait()
                    continue  # retry the same window, doesn't count as a window attempt

                if 500 <= status < 600:
                    window_days = (window_end - window_start + 1) / 86400.0
                    if window_days > 1.0:
                        span_memory.halve(symbol)
                        window_end = min(
                            window_start + _window_span_seconds(span_memory.span(symbol)) - 1,
                            to_sec,
                        )
                        window_attempts = 0
                        continue
                    window_attempts += 1
                    if window_attempts >= max_window_retries:
                        failed_windows.append(
                            (window_start, window_end, f"HTTP {status} after {window_attempts} attempts")
                        )
                        break
                    continue

                # Unexpected non-2xx/429/5xx status: fail this window
                # immediately, no retry.
                failed_windows.append((window_start, window_end, f"HTTP {status}"))
                break

            window_start = window_end + 1
    except BudgetExhausted as exc:
        wall_minutes = (monotonic_fn() - started_monotonic) / 60.0
        return FetchResult(
            status=STATUS_FAILED,
            rows_written=0,
            captures=captures,
            wall_minutes=wall_minutes,
            error=str(exc),
        )

    combined = dedup_ticks(frames)
    combined = _drop_already_written(combined, existing_keys)

    rows_written = 0
    if not combined.empty:
        rows_written = writer(
            root,
            combined,
            vendor_request_symbol=symbol,
            committed_at=now,
            source_system=source_system,
            price_venue_type=price_venue_type,
        )

    verify_ok, _verified_count = verifier(root, listing_id, combined)

    wall_minutes = (monotonic_fn() - started_monotonic) / 60.0
    error = None
    if failed_windows:
        error = "; ".join(f"[{s}, {e}]: {msg}" for s, e, msg in failed_windows)
        status = STATUS_FAILED if combined.empty and rows_written == 0 else STATUS_PARTIAL
    elif not verify_ok:
        status = STATUS_PARTIAL
        error = "verification failed: duplicate (trade_date, session_seq) in written frame"
    else:
        status = STATUS_COMPLETE

    return FetchResult(
        status=status,
        rows_written=rows_written,
        captures=captures,
        wall_minutes=wall_minutes,
        error=error,
    )


def update_manifest_row(row: dict, result: FetchResult, *, completed_at) -> dict:
    """Pure helper: apply a `FetchResult` to one manifest row's mutable
    fields (`status`, `attempts` (incremented), `last_error`, `updated_
    at_ts`, and `completed_at_ts` when the result is `COMPLETE`). Does not
    touch `wall_minutes` - see module docstring's schema-deviation note #1
    (there is no such column to write it into); the caller is responsible
    for logging/emitting `FetchResult.wall_minutes` itself if it wants
    that number recorded somewhere.
    """
    completed_ts = _to_utc_ts(completed_at)
    updated = dict(row)
    updated["status"] = result.status
    updated["attempts"] = int(row.get("attempts") or 0) + 1
    updated["last_error"] = result.error
    updated["updated_at_ts"] = completed_ts
    if result.status == STATUS_COMPLETE:
        updated["completed_at_ts"] = completed_ts
    return updated


# ---------------------------------------------------------------------------
# Delta-backed defaults (lazy pyarrow/deltalake imports)
# ---------------------------------------------------------------------------

def _default_membership_reader(root: str) -> pd.DataFrame:
    from deltalake import DeltaTable

    return DeltaTable(f"{root}/silver/index_membership_version").to_pandas()


def _default_assignments_reader(root: str) -> pd.DataFrame:
    from deltalake import DeltaTable

    return DeltaTable(f"{root}/silver/identifier_assignment_version").to_pandas()


def _default_manifest_reader(root: str) -> pd.DataFrame:
    from deltalake import DeltaTable

    return DeltaTable(f"{root}/ops/backfill_manifest").to_pandas()


def _default_manifest_writer(root: str, table: str, df: pd.DataFrame) -> None:
    from tick_vault.capture import append_rows

    append_rows(root, table, df)


# ---------------------------------------------------------------------------
# ManifestBuilder
# ---------------------------------------------------------------------------

class ManifestBuilder:
    """Delta-backed shell around `generate_manifest`: reads membership/
    identity/existing-manifest state through injectable readers, computes
    the new rows with the pure core, and appends them (never overwrites -
    `ops.backfill_manifest` grows monotonically, mutable ops STATE that
    later gets its `status`/`attempts`/etc. columns updated in place by a
    later episode's queue-runner, not by this module, which only ever
    appends brand-new work items).

    Every reader/writer is injectable, matching `tick_vault.master.
    MasterBuilder`'s seam pattern; defaults are real Delta reads/appends,
    none imported at module scope.
    """

    def __init__(
        self,
        root: str,
        *,
        membership_reader=None,
        assignments_reader=None,
        manifest_reader=None,
        manifest_writer=None,
    ):
        self.root = root
        self._membership_reader = membership_reader or _default_membership_reader
        self._assignments_reader = assignments_reader or _default_assignments_reader
        self._manifest_reader = manifest_reader or _default_manifest_reader
        self._manifest_writer = manifest_writer or _default_manifest_writer

    def generate(self, first_week, last_week, *, priority_caps=None, **kwargs) -> pd.DataFrame:
        membership_df = self._membership_reader(self.root)
        assignments_df = self._assignments_reader(self.root)
        existing_df = self._manifest_reader(self.root)

        new_rows = generate_manifest(
            membership_df,
            assignments_df,
            first_week,
            last_week,
            priority_caps=priority_caps,
            existing_manifest_df=existing_df,
            **kwargs,
        )
        if not new_rows.empty:
            persisted = new_rows.copy()
            # Schema-deviation note #2 (module docstring): vendor_symbol_
            # at_date is NOT NULL; a BLOCKED_IDENTITY row's None becomes
            # "" only in the persisted frame, never in generate_manifest's
            # own pure return value.
            persisted["vendor_symbol_at_date"] = persisted["vendor_symbol_at_date"].where(
                persisted["vendor_symbol_at_date"].notna(), ""
            )
            self._manifest_writer(self.root, "ops.backfill_manifest", persisted)
        return new_rows

"""Diff-aware settle and daily passes (task-8 brief; tick-vault temporal
design §7).

`settle_pass` re-fetches one manifest item's window through the exact same
raw-fetch core `fetch_week` uses (`tick_vault.engine._fetch_raw_week` -
same span-halving/429-budget/boundary-second-dedup windows, real bronze
captures), diffs the fresh incoming parse against a caller-supplied frame
of the CURRENT latest non-cancelled silver versions for that (listing,
week) via `tick_vault.diffing.diff_window`, and appends ONLY the delta
(new tick versions + correction events) - it never rewrites or deletes an
existing silver row (`tick_vault.tick_writer`'s append-only contract,
same as `fetch_week`'s own write path).

`existing_latest_reader(root, item) -> pandas.DataFrame` is a seam: in
production this is a DuckDB query against `silver.us_trade_tick_version`'s
`latest_ticks()` view (`tick_vault.temporal`), filtered to this item's
`(listing_id, week)` - Plan-3 wires that real reader up. Here it is just
an injected callable so this module (and its runnable tests) never needs
a real Delta/DuckDB lake.

Choosing `revealing_capture_id` (binding choice, documented per the
task-8 preflight ruling)
-------------------------------------------------------------------------
`diffing.diff_window` wants exactly one `revealing_capture_id` to stamp
onto every new version/event it emits - "which capture revealed this
version" (see `diffing`'s own docstring). A settle fetch may issue several
HTTP requests (one per span-halved window, plus 429 retries and failed
5xx attempts along the way) and therefore writes several bronze capture
rows. This module picks the FIRST capture that came back with a 2xx
status as `revealing_capture_id` - "the settle fetch, as a whole event,
revealed this state; here is the earliest evidence of it" - rather than,
say, the last successful capture (which would arbitrarily favor whichever
window happened to be fetched last) or a synthetic new id (which would
have no bronze row backing it as provenance at all). Concretely this is
implemented by wrapping `store` in a tiny local `_CaptureIdTracker` that
delegates every `.record(...)` call through to the real store unchanged,
while remembering the first `capture_id` whose `http_status` was a 2xx -
`_fetch_raw_week`'s own return contract (`(parsed_df, captures, error)`,
task-8 preflight ruling) has no room for a capture-id list, so this is the
non-invasive way to recover it without changing that shared function's
signature.

Failure handling: a settle fetch with ANY failed window (`error` is not
`None` from `_fetch_raw_week`) is never diffed at all - `settle_pass`
returns `FAILED` immediately, writes nothing. Diffing a PARTIAL incoming
window against the full existing latest frame would be actively wrong: a
logical tick omitted only because its window's HTTP call failed would look
identical, to `diff_window`, to a logical tick the vendor genuinely
revealed as cancelled - `diff_window` has no way to tell "vendor says this
trade never happened" apart from "we simply failed to re-fetch it". Only a
clean, complete re-fetch of the whole window is safe to diff.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from tick_vault import diffing
from tick_vault.engine import _REDACTED, Budget, SpanMemory, _fetch_raw_week
from tick_vault.tick_writer import write_correction_events, write_tick_versions

STATUS_SETTLED = "SETTLED"
STATUS_NO_CHANGES = "NO_CHANGES"
STATUS_FAILED = "FAILED"


def _item_get(item, key, default=None):
    """`item` is either a plain `dict` or a `pandas.Series` (one manifest
    row) - both support `.get(key, default)` (same convention as
    `tick_vault.engine._item_get`)."""
    return item.get(key, default)


@dataclass(frozen=True)
class SettleResult:
    status: str  # SETTLED | NO_CHANGES | FAILED
    new_versions: int
    cancellations: int
    late_adds: int
    revisions: int
    captures: int
    error: "str | None" = None


class _CaptureIdTracker:
    """Thin `store` wrapper: delegates every call through unchanged, while
    remembering the `capture_id` of the FIRST call to `.record(...)` whose
    `http_status` was a 2xx (see module docstring's "choosing
    revealing_capture_id" note). `_fetch_raw_week` only ever calls
    `.record(...)` (never `.fetch_and_capture`, per `tick_vault.engine`'s
    own note), so that is the only method this needs to intercept."""

    def __init__(self, store):
        self._store = store
        self.first_success_capture_id: "str | None" = None

    def record(self, **kwargs):
        record = self._store.record(**kwargs)
        status = kwargs.get("http_status")
        if (
            self.first_success_capture_id is None
            and status is not None
            and 200 <= status < 300
        ):
            self.first_success_capture_id = record.capture_id
        return record

    def __getattr__(self, name):
        return getattr(self._store, name)


def settle_pass(
    transport,
    store,
    root: str,
    item,
    *,
    existing_latest_reader,
    span_memory: SpanMemory,
    budget: Budget,
    token: str = _REDACTED,
    writer=None,
    events_writer=None,
    now: "dt.datetime | None" = None,
    source_system: str = "EODHD",
    price_venue_type: str = "CONSOLIDATED_US",
) -> SettleResult:
    """Re-fetch `item`'s window, diff it against the current silver latest,
    and append only the delta.

    1. Re-fetch via `engine._fetch_raw_week` (real bronze captures - the
       REVEALING captures for this settle).
    2. `existing = existing_latest_reader(root, item)` - the caller-
       supplied frame of CURRENT latest non-cancelled silver versions for
       this item's `(listing, week)` (a seam; see module docstring).
    3. `diffing.diff_window(existing, incoming,
       revealing_capture_id=<first successful capture of this settle
       fetch>, observed_at=now)`.
    4. Append `diff.new_versions` via `tick_vault.tick_writer.
       write_tick_versions` (append-only; `complete_tick_frame` only fills
       `source_system`/`vendor_request_symbol`/`price_venue_type`/
       `committed_at_ts` - it never touches `available_at_ts`, which stays
       `diff_window`'s `observed_at` - the settle's OWN knowledge time,
       not the original trade's).
    5. Append `diff.events` via `write_correction_events`.
    6. Return counts. An empty diff (nothing changed) short-circuits at
       step 3/4 with `status=NO_CHANGES` and neither `writer` nor
       `events_writer` is called at all.

    A failed re-fetch (`_fetch_raw_week` reports a non-`None` error - any
    window failed, including a `BudgetExhausted`) short-circuits before
    even reading `existing`/diffing: `status=FAILED`, zero writes, the
    error message recorded on the result (see module docstring for why a
    partial incoming window is never safe to diff).
    """
    writer = writer or write_tick_versions
    events_writer = events_writer or write_correction_events
    now = now if now is not None else dt.datetime.now(dt.timezone.utc)

    tracker = _CaptureIdTracker(store)
    incoming, captures, error = _fetch_raw_week(
        transport, tracker, item,
        span_memory=span_memory, budget=budget, token=token, now=now,
    )

    if error is not None:
        return SettleResult(
            status=STATUS_FAILED,
            new_versions=0,
            cancellations=0,
            late_adds=0,
            revisions=0,
            captures=captures,
            error=error,
        )

    revealing_capture_id = tracker.first_success_capture_id
    if revealing_capture_id is None:
        # No window was even attempted (e.g. a zero-length request range)
        # and no error was raised either - nothing revealed, nothing to
        # diff against. Treated the same as an empty diff.
        return SettleResult(
            status=STATUS_NO_CHANGES,
            new_versions=0,
            cancellations=0,
            late_adds=0,
            revisions=0,
            captures=captures,
            error=None,
        )

    existing = existing_latest_reader(root, item)
    if existing is None:
        from tick_vault.tick_parser import COLUMNS

        existing = pd.DataFrame(columns=COLUMNS)

    diff = diffing.diff_window(
        existing, incoming,
        revealing_capture_id=revealing_capture_id,
        observed_at=now,
    )

    if diff.new_versions.empty:
        return SettleResult(
            status=STATUS_NO_CHANGES,
            new_versions=0,
            cancellations=0,
            late_adds=0,
            revisions=0,
            captures=captures,
            error=None,
        )

    vendor_symbol = _item_get(item, "vendor_symbol_at_date")
    writer(
        root,
        diff.new_versions,
        vendor_request_symbol=vendor_symbol,
        committed_at=now,
        source_system=source_system,
        price_venue_type=price_venue_type,
    )
    if not diff.events.empty:
        events_writer(root, diff.events)

    new_rows = diff.new_versions.to_dict("records")
    revisions = sum(1 for r in new_rows if r["is_correction"])
    cancellations = sum(1 for r in new_rows if r["is_cancelled"])
    late_adds = sum(1 for r in new_rows if r["is_late_add"])

    return SettleResult(
        status=STATUS_SETTLED,
        new_versions=len(new_rows),
        cancellations=cancellations,
        late_adds=late_adds,
        revisions=revisions,
        captures=captures,
        error=None,
    )


def daily_pass(
    transport,
    store,
    root: str,
    items,
    *,
    existing_latest_reader,
    span_memory: SpanMemory,
    budget: Budget,
    token: str = _REDACTED,
    writer=None,
    events_writer=None,
    now: "dt.datetime | None" = None,
    source_system: str = "EODHD",
    price_venue_type: str = "CONSOLIDATED_US",
) -> "list[SettleResult]":
    """Run `settle_pass` over `items` (one manifest-shaped row per
    (listing, week) needing a settle sweep - the caller/Task-10's loop
    builds this list to cover the trailing 7 days for current index
    members). Same mechanics, one `SettleResult` per item, in order.

    Late prints surface as `late_adds` and corrections as `revisions` on
    each item's own `SettleResult` - that semantics comes for free from
    `diffing.diff_window`, `daily_pass` adds nothing beyond "loop over
    items and call `settle_pass`" (a single `now`/`span_memory`/`budget`
    are shared across every item in the sweep, exactly as a caller passing
    them once here would expect).
    
    Per-item exception isolation: if `settle_pass` or any of its dependencies
    (including `existing_latest_reader`, `writer`, or `events_writer`) raises
    an exception for a particular item, that item's result is recorded as
    FAILED with an error message, and processing continues with the next item.
    """
    now = now if now is not None else dt.datetime.now(dt.timezone.utc)
    results = []
    for item in items:
        try:
            results.append(
                settle_pass(
                    transport, store, root, item,
                    existing_latest_reader=existing_latest_reader,
                    span_memory=span_memory, budget=budget, token=token,
                    writer=writer, events_writer=events_writer, now=now,
                    source_system=source_system, price_venue_type=price_venue_type,
                )
            )
        except Exception as e:
            results.append(
                SettleResult(
                    status=STATUS_FAILED,
                    new_versions=0,
                    cancellations=0,
                    late_adds=0,
                    revisions=0,
                    captures=0,
                    error=f"{type(e).__name__}: {e}",
                )
            )
    return results

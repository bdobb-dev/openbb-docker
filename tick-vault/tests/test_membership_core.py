"""Runnable (pyarrow/deltalake/pytest-free) tests for
`tick_vault.membership` (task-5 brief).

`tick_vault.membership` imports bare (no pyarrow/deltalake at module
scope), so it's importable/runnable here even without those installed.
Mirrors `tests/test_master_core.py`'s `_FakeLake` pattern: a fake
`memberships_reader`/`writer` pair backed by an in-memory
`dict[str, pandas.DataFrame]`, and a fake `resolver(code, date) ->
(listing_id, instrument_id) | None` function (never a real
`MasterBuilder`, per the module's dependency-inversion rule).

Real Delta round trip: `tests/deferred/test_membership.py` (pytest).
"""
import datetime as dt
import decimal

import pandas as pd

from tick_vault.membership import (
    AUTO_CONFIDENCE,
    INCLUSION_REASON_START_UNKNOWN,
    INDEX_ID,
    INDEX_VENDOR_SYMBOL,
    MEMBERSHIP_BOUNDARY_V1,
    RESOLUTION_AMBIGUOUS,
    RESOLUTION_RESOLVED,
    RESOLUTION_UNRESOLVED,
    UNRESOLVED_CONFIDENCE,
    _MEMBERSHIP_COLUMNS,
    build_membership,
)

OBS1 = dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc)
OBS2 = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc)
CAP1 = "cap_components1"
CAP2 = "cap_components2"


class _FakeLake:
    def __init__(self):
        self.tables: "dict[str, pd.DataFrame]" = {}

    def reader(self, root):
        return self.tables.get(
            "silver.index_membership_version",
            pd.DataFrame(columns=_MEMBERSHIP_COLUMNS),
        )

    def writer(self, root, table, df):
        existing = self.tables.get(table)
        self.tables[table] = (
            pd.concat([existing, df], ignore_index=True) if existing is not None else df.copy()
        )

    def get(self, table):
        return self.tables.get(table, pd.DataFrame())


def _components_df(rows):
    return pd.DataFrame(rows, columns=["code", "name", "start_date", "end_date", "is_active", "sector"])


def _fake_resolver(mapping):
    """`mapping`: `{code: [(listing_id, instrument_id, on_or_after_date), ...]}`
    sorted oldest-first; returns the last entry whose `on_or_after_date <=
    date` (a tiny stand-in for a "resolve at date" security master)."""
    def resolver(code, date):
        entries = mapping.get(code)
        if not entries or date is None:
            return None
        match = None
        for listing_id, instrument_id, on_or_after in entries:
            if on_or_after <= date:
                match = (listing_id, instrument_id)
        return match
    return resolver


# ---------------------------------------------------------------------------
# 1. resolved tenure row shape
# ---------------------------------------------------------------------------

def test_resolved_tenure_row_shape():
    lake = _FakeLake()
    resolver = _fake_resolver({"AAPL": [("lst_aapl", "ins_aapl", dt.date(1990, 1, 1))]})
    components = _components_df([
        {"code": "AAPL", "name": "Apple Inc", "start_date": dt.date(2000, 1, 1),
         "end_date": None, "is_active": True, "sector": "Technology"},
    ])

    report = build_membership(
        "fake_root", components, resolver=resolver, capture_id=CAP1, observed_at=OBS1,
        memberships_reader=lake.reader, writer=lake.writer,
    )

    assert report.resolved == 1
    assert report.ambiguous == 0
    assert report.unresolved == 0
    assert report.written == 1

    rows = lake.get("silver.index_membership_version")
    assert len(rows) == 1
    row = rows.iloc[0]
    assert row["membership_version_id"].startswith("mem_")
    assert row["index_id"] == INDEX_ID
    assert row["index_vendor_symbol"] == INDEX_VENDOR_SYMBOL
    assert row["listing_id"] == "lst_aapl"
    assert row["instrument_id"] == "ins_aapl"
    assert row["source_constituent_code"] == "AAPL"
    assert row["source_name"] == "Apple Inc"
    assert row["membership_effective_from"] == dt.date(2000, 1, 1)
    assert row["membership_effective_to"] is None
    assert row["resolution_status"] == RESOLUTION_RESOLVED
    assert isinstance(row["confidence"], decimal.Decimal)
    assert row["confidence"] == AUTO_CONFIDENCE
    assert row["observed_at_ts"] == OBS1
    assert row["available_at_ts"] == OBS1
    assert row["system_from_ts"] == OBS1
    assert row["system_to_ts"] is None
    assert row["source_capture_id"] == CAP1
    assert row["supersedes_membership_version_id"] is None


# ---------------------------------------------------------------------------
# 2. delisted member resolved via historical resolver
# ---------------------------------------------------------------------------

def test_delisted_member_resolves_via_historical_resolver():
    lake = _FakeLake()
    # "ENRN" was only ever a listing between 1990 and its 2001 delisting;
    # the fake resolver only knows about it via its historical window.
    resolver = _fake_resolver({"ENRN": [("lst_enrn", "ins_enrn", dt.date(1990, 1, 1))]})
    components = _components_df([
        {"code": "ENRN", "name": "Enron Corp", "start_date": dt.date(1995, 1, 1),
         "end_date": dt.date(2001, 12, 1), "is_active": False, "sector": "Energy"},
    ])

    report = build_membership(
        "fake_root", components, resolver=resolver, capture_id=CAP1, observed_at=OBS1,
        memberships_reader=lake.reader, writer=lake.writer,
    )

    assert report.resolved == 1
    row = lake.get("silver.index_membership_version").iloc[0]
    assert row["listing_id"] == "lst_enrn"
    assert row["resolution_status"] == RESOLUTION_RESOLVED
    assert row["membership_effective_from"] == dt.date(1995, 1, 1)
    assert row["membership_effective_to"] == dt.date(2001, 12, 1)


# ---------------------------------------------------------------------------
# 3. unknown code -> UNRESOLVED, source text retained
# ---------------------------------------------------------------------------

def test_unknown_code_unresolved_retains_source_text():
    lake = _FakeLake()
    resolver = _fake_resolver({})  # nothing resolves
    components = _components_df([
        {"code": "GHOST", "name": "Ghost Corp", "start_date": dt.date(2005, 1, 1),
         "end_date": None, "is_active": True, "sector": "Unknown"},
    ])

    report = build_membership(
        "fake_root", components, resolver=resolver, capture_id=CAP1, observed_at=OBS1,
        memberships_reader=lake.reader, writer=lake.writer,
    )

    assert report.resolved == 0
    assert report.unresolved == 1
    assert report.written == 1

    row = lake.get("silver.index_membership_version").iloc[0]
    assert row["listing_id"] is None
    assert row["instrument_id"] is None
    assert row["resolution_status"] == RESOLUTION_UNRESOLVED
    assert row["source_constituent_code"] == "GHOST"
    assert row["source_name"] == "Ghost Corp"
    assert row["confidence"] == UNRESOLVED_CONFIDENCE


# ---------------------------------------------------------------------------
# 4. overlap -> both rows + AMBIGUOUS + quality-issue frame
# ---------------------------------------------------------------------------

def test_overlap_writes_both_rows_flags_later_and_logs_quality_issue():
    lake = _FakeLake()
    # Two different vendor codes for a corporate-action-shaped scenario
    # both resolving to the SAME listing_id, with overlapping intervals.
    resolver = _fake_resolver({
        "OLDX": [("lst_shared", "ins_shared", dt.date(1990, 1, 1))],
        "NEWX": [("lst_shared", "ins_shared", dt.date(1990, 1, 1))],
    })
    components = _components_df([
        {"code": "OLDX", "name": "Old Co", "start_date": dt.date(2000, 1, 1),
         "end_date": dt.date(2010, 1, 1), "is_active": False, "sector": "Industrials"},
        {"code": "NEWX", "name": "New Co", "start_date": dt.date(2005, 1, 1),
         "end_date": None, "is_active": True, "sector": "Industrials"},
    ])

    report = build_membership(
        "fake_root", components, resolver=resolver, capture_id=CAP1, observed_at=OBS1,
        memberships_reader=lake.reader, writer=lake.writer,
    )

    assert report.resolved == 1
    assert report.ambiguous == 1
    assert report.unresolved == 0
    assert report.written == 2

    rows = lake.get("silver.index_membership_version")
    assert len(rows) == 2
    by_code = {r["source_constituent_code"]: r for _, r in rows.iterrows()}
    assert by_code["OLDX"]["resolution_status"] == RESOLUTION_RESOLVED
    assert by_code["NEWX"]["resolution_status"] == RESOLUTION_AMBIGUOUS  # later-starting

    dq = lake.get("ops.data_quality_issue")
    assert len(dq) == 1
    issue = dq.iloc[0]
    assert issue["check_name"] == "MEMBERSHIP_OVERLAP"
    assert issue["listing_id"] == "lst_shared"
    assert issue["issue_id"].startswith("wrk_")
    assert issue["status"] == "OPEN"
    assert "OLDX" in issue["details_json"]
    assert "NEWX" in issue["details_json"]


# ---------------------------------------------------------------------------
# 5. re-run idempotent
# ---------------------------------------------------------------------------

def test_rerun_idempotent_zero_new_rows():
    lake = _FakeLake()
    resolver = _fake_resolver({"AAPL": [("lst_aapl", "ins_aapl", dt.date(1990, 1, 1))]})
    components = _components_df([
        {"code": "AAPL", "name": "Apple Inc", "start_date": dt.date(2000, 1, 1),
         "end_date": None, "is_active": True, "sector": "Technology"},
    ])

    first = build_membership(
        "fake_root", components, resolver=resolver, capture_id=CAP1, observed_at=OBS1,
        memberships_reader=lake.reader, writer=lake.writer,
    )
    second = build_membership(
        "fake_root", components, resolver=resolver, capture_id=CAP2, observed_at=OBS2,
        memberships_reader=lake.reader, writer=lake.writer,
    )

    assert first.written == 1
    assert second.written == 0
    assert second.resolved == 1  # still classified, just not re-written
    assert len(lake.get("silver.index_membership_version")) == 1


def test_rerun_idempotent_with_fake_reader_over_first_runs_output():
    """Same idempotency check, but explicitly exercising the brief's
    "fake reader returns first run's rows -> zero new" phrasing: a fresh
    lake object primed directly with the first call's output frame,
    passed in as the SECOND call's reader."""
    resolver = _fake_resolver({"MSFT": [("lst_msft", "ins_msft", dt.date(1990, 1, 1))]})
    components = _components_df([
        {"code": "MSFT", "name": "Microsoft", "start_date": dt.date(1999, 1, 1),
         "end_date": None, "is_active": True, "sector": "Technology"},
    ])

    lake = _FakeLake()
    build_membership(
        "fake_root", components, resolver=resolver, capture_id=CAP1, observed_at=OBS1,
        memberships_reader=lake.reader, writer=lake.writer,
    )
    first_run_rows = lake.get("silver.index_membership_version")

    def fixed_reader(root):
        return first_run_rows

    calls = []

    def counting_writer(root, table, df):
        calls.append((table, len(df)))

    report = build_membership(
        "fake_root", components, resolver=resolver, capture_id=CAP2, observed_at=OBS2,
        memberships_reader=fixed_reader, writer=counting_writer,
    )
    assert report.written == 0
    assert calls == []  # writer never invoked with an empty frame


# ---------------------------------------------------------------------------
# 6. boundary policy constant
# ---------------------------------------------------------------------------

def test_boundary_policy_constant_exported():
    assert MEMBERSHIP_BOUNDARY_V1 == "MEMBERSHIP_BOUNDARY_V1"


# ---------------------------------------------------------------------------
# 7. inclusive-boundary overlap (controller ruling, fix-round)
# ---------------------------------------------------------------------------

def test_touching_endpoints_same_listing_flagged_as_overlap():
    """`MEMBERSHIP_BOUNDARY_V1` documents an INCLUSIVE `effective_to` (the
    last member session, not an exclusive/day-after boundary), so two
    same-listing intervals that merely TOUCH at a shared boundary date -
    one ending 2010-01-01, the next starting exactly 2010-01-01 - both
    claim that same session and MUST be flagged as an overlap, not treated
    as adjacent/non-overlapping."""
    lake = _FakeLake()
    resolver = _fake_resolver({
        "OLDX": [("lst_shared", "ins_shared", dt.date(1990, 1, 1))],
        "NEWX": [("lst_shared", "ins_shared", dt.date(1990, 1, 1))],
    })
    components = _components_df([
        {"code": "OLDX", "name": "Old Co", "start_date": dt.date(2000, 1, 1),
         "end_date": dt.date(2010, 1, 1), "is_active": False, "sector": "Industrials"},
        {"code": "NEWX", "name": "New Co", "start_date": dt.date(2010, 1, 1),
         "end_date": None, "is_active": True, "sector": "Industrials"},
    ])

    report = build_membership(
        "fake_root", components, resolver=resolver, capture_id=CAP1, observed_at=OBS1,
        memberships_reader=lake.reader, writer=lake.writer,
    )

    assert report.resolved == 1
    assert report.ambiguous == 1

    rows = lake.get("silver.index_membership_version")
    by_code = {r["source_constituent_code"]: r for _, r in rows.iterrows()}
    assert by_code["OLDX"]["resolution_status"] == RESOLUTION_RESOLVED
    assert by_code["NEWX"]["resolution_status"] == RESOLUTION_AMBIGUOUS

    dq = lake.get("ops.data_quality_issue")
    overlap_issues = dq[dq["check_name"] == "MEMBERSHIP_OVERLAP"]
    assert len(overlap_issues) == 1


# ---------------------------------------------------------------------------
# 8. missing start_date -> bounded at observed_at, never a bare None
# ---------------------------------------------------------------------------

def test_missing_start_date_resolves_bounds_effective_from_at_observation():
    lake = _FakeLake()
    # The resolver only knows AAPL as of observed_at's date (2026-01-10),
    # not any earlier - matching a Components-only row with no start_date.
    resolver = _fake_resolver({"AAPL": [("lst_aapl", "ins_aapl", dt.date(2026, 1, 10))]})
    components = _components_df([
        {"code": "AAPL", "name": "Apple Inc", "start_date": None,
         "end_date": None, "is_active": True, "sector": "Technology"},
    ])

    report = build_membership(
        "fake_root", components, resolver=resolver, capture_id=CAP1, observed_at=OBS1,
        memberships_reader=lake.reader, writer=lake.writer,
    )

    assert report.resolved == 1
    assert report.unresolved == 0

    row = lake.get("silver.index_membership_version").iloc[0]
    assert row["membership_effective_from"] == OBS1.date()
    assert row["membership_effective_from"] is not None
    assert row["resolution_status"] == RESOLUTION_RESOLVED
    assert row["inclusion_reason"] == INCLUSION_REASON_START_UNKNOWN

    dq = lake.get("ops.data_quality_issue")
    start_unknown_issues = dq[dq["check_name"] == "MEMBERSHIP_START_UNKNOWN"]
    assert len(start_unknown_issues) == 1


def test_missing_start_date_unresolved_still_bounds_effective_from():
    lake = _FakeLake()
    resolver = _fake_resolver({})  # nothing resolves, even at observed_at
    components = _components_df([
        {"code": "GHOST", "name": "Ghost Corp", "start_date": None,
         "end_date": None, "is_active": True, "sector": "Unknown"},
    ])

    report = build_membership(
        "fake_root", components, resolver=resolver, capture_id=CAP1, observed_at=OBS1,
        memberships_reader=lake.reader, writer=lake.writer,
    )

    assert report.resolved == 0
    assert report.unresolved == 1

    row = lake.get("silver.index_membership_version").iloc[0]
    # Never a bare None into the non-nullable column, even when unresolved.
    assert row["membership_effective_from"] == OBS1.date()
    assert row["membership_effective_from"] is not None
    assert row["resolution_status"] == RESOLUTION_UNRESOLVED
    assert row["listing_id"] is None
    assert row["inclusion_reason"] == INCLUSION_REASON_START_UNKNOWN

    dq = lake.get("ops.data_quality_issue")
    start_unknown_issues = dq[dq["check_name"] == "MEMBERSHIP_START_UNKNOWN"]
    assert len(start_unknown_issues) == 1


# ---------------------------------------------------------------------------
# 9. overlap quality-issue not re-emitted on an idempotent re-run
# ---------------------------------------------------------------------------

def test_overlap_issue_not_reemitted_on_idempotent_rerun():
    lake = _FakeLake()
    resolver = _fake_resolver({
        "OLDX": [("lst_shared", "ins_shared", dt.date(1990, 1, 1))],
        "NEWX": [("lst_shared", "ins_shared", dt.date(1990, 1, 1))],
    })
    components = _components_df([
        {"code": "OLDX", "name": "Old Co", "start_date": dt.date(2000, 1, 1),
         "end_date": dt.date(2010, 1, 1), "is_active": False, "sector": "Industrials"},
        {"code": "NEWX", "name": "New Co", "start_date": dt.date(2005, 1, 1),
         "end_date": None, "is_active": True, "sector": "Industrials"},
    ])

    first = build_membership(
        "fake_root", components, resolver=resolver, capture_id=CAP1, observed_at=OBS1,
        memberships_reader=lake.reader, writer=lake.writer,
    )
    assert first.written == 2
    assert len(lake.get("ops.data_quality_issue")) == 1

    second = build_membership(
        "fake_root", components, resolver=resolver, capture_id=CAP2, observed_at=OBS2,
        memberships_reader=lake.reader, writer=lake.writer,
    )

    # Same batch, re-run: zero NEW membership rows, and the overlap issue
    # is not re-appended (still exactly one issue row total).
    assert second.written == 0
    assert second.ambiguous == 1  # still classified as ambiguous
    assert len(lake.get("silver.index_membership_version")) == 2
    assert len(lake.get("ops.data_quality_issue")) == 1

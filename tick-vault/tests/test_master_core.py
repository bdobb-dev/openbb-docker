"""Runnable (pyarrow/deltalake/pytest-free) tests for
`tick_vault.master` (task-4 brief).

`tick_vault.master` imports bare (no pyarrow/deltalake at module scope -
those are lazy-imported inside `_default_assignments_reader`/
`_default_writer`, neither of which this file exercises), so it's
importable and runnable here even though pyarrow/deltalake are not
installed in this sandbox.

`_FakeLake` below plays both halves of `MasterBuilder`'s injectable seam:
`reader(root)` returns whatever's currently in `self.tables[table]` (an
in-memory `dict[str, pandas.DataFrame]`), and `writer(root, table, df)`
appends into it - so a `MasterBuilder(root, assignments_reader=lake.reader,
writer=lake.writer)` round-trips through the SAME in-memory state a real
Delta lake would give it (a re-run sees its own prior writes), entirely in
pandas. This is what makes the task brief's six named scenarios (new-
symbol allocation shapes, idempotency, BK->BNY same-listing close/open,
unknown-old-code queue row, ticker-reuse resolve-by-date, CUSIP-at-
instrument-scope) all runnable here rather than deferred.

The real Delta round-trip (writes actually land in a lake, re-read via
`MasterBuilder`'s *default* reader/writer) is covered separately by
`tests/deferred/test_master.py` (pytest, needs deltalake/pyarrow).
"""
import datetime as dt

import pandas as pd

from tick_vault.master import (
    AUTO_CONFIDENCE,
    NAMESPACE_CUSIP,
    NAMESPACE_EODHD_SYMBOL,
    NAMESPACE_ISIN,
    NAMESPACE_SEC_CIK,
    REASON_UNKNOWN_OLD_CODE,
    MasterBuilder,
    _ASSIGNMENT_COLUMNS,
    resolve_listing_at,
)

OBS1 = dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc)
OBS2 = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc)
CAP1 = "cap_symbols1"
CAP2 = "cap_changes1"


class _FakeLake:
    """In-memory stand-in for the Delta lake `MasterBuilder` reads/writes.

    `reader` always returns the current `silver.identifier_assignment_
    version` table (creating it with the right columns if absent);
    `writer` appends `df` onto whatever table `table` currently holds;
    `system_closer` plays the role of the real `DeltaTable.update`
    default (see `tick_vault.master._default_system_closer`) by mutating
    the in-memory table's `system_to_ts` for each `(assignment_version_id,
    system_to_ts)` instruction - so a re-read via `reader` sees exactly
    the system-closed state a real Delta `UPDATE` would have left behind
    (this is what makes the fake usable to test the controller-ruling fix
    without deltalake installed). It also records every call it receives
    in `closer_calls`, so tests can assert the close actually happened.
    """

    def __init__(self):
        self.tables: dict[str, pd.DataFrame] = {}
        self.closer_calls: list[tuple[str, list]] = []

    def reader(self, root):
        return self.tables.get(
            "silver.identifier_assignment_version",
            pd.DataFrame(columns=_ASSIGNMENT_COLUMNS),
        )

    def writer(self, root, table, df):
        existing = self.tables.get(table)
        self.tables[table] = (
            pd.concat([existing, df], ignore_index=True) if existing is not None else df.copy()
        )

    def system_closer(self, root, table, closes):
        self.closer_calls.append((table, list(closes)))
        frame = self.tables.get(table)
        if frame is None or frame.empty:
            return
        for assignment_version_id, system_to_ts in closes:
            frame.loc[
                frame["assignment_version_id"] == assignment_version_id, "system_to_ts"
            ] = system_to_ts
        self.tables[table] = frame

    def get(self, table):
        return self.tables.get(table, pd.DataFrame())


def _builder(lake):
    return MasterBuilder(
        "fake_root",
        assignments_reader=lake.reader,
        writer=lake.writer,
        system_closer=lake.system_closer,
    )


def _symbols_df(rows):
    return pd.DataFrame(rows, columns=["code", "name", "exchange", "type", "isin"])


def _changes_df(rows):
    return pd.DataFrame(rows, columns=["old", "new", "date"])


def _fundamentals_df(rows):
    return pd.DataFrame(rows, columns=["code", "cusip", "isin", "cik", "name", "type", "share_class"])


# ---------------------------------------------------------------------------
# 1. new-symbol allocation shapes
# ---------------------------------------------------------------------------

def test_new_symbol_allocates_instrument_listing_assignment():
    lake = _FakeLake()
    mb = _builder(lake)
    symbols = _symbols_df([
        {"code": "IBM", "name": "IBM Corp", "exchange": "US", "type": "Common Stock", "isin": "US4592001014"},
    ])

    result = mb.upsert_from_symbols(symbols, CAP1, OBS1)

    assert len(result["instrument"]) == 1
    assert len(result["listing_version"]) == 1
    assert len(result["identifier_assignment_version"]) == 1

    inst = result["instrument"].iloc[0]
    assert inst["instrument_id"].startswith("ins_")
    assert inst["instrument_type"] == "Common Stock"
    assert inst["status"] == "ACTIVE"
    assert inst["source_system"] == "eodhd"

    lst = result["listing_version"].iloc[0]
    assert lst["listing_id"].startswith("lst_")
    assert lst["listing_version_id"].startswith("lst_")
    assert lst["instrument_id"] == inst["instrument_id"]
    assert lst["ticker"] == "IBM"
    assert lst["eodhd_symbol"] == "IBM"
    assert lst["effective_from_ts"] is None
    assert lst["available_at_ts"] == OBS1
    assert lst["system_from_ts"] == OBS1
    assert lst["system_to_ts"] is None
    assert lst["confidence"] == AUTO_CONFIDENCE
    assert lst["verification_status"] == "UNVERIFIED"

    asn = result["identifier_assignment_version"].iloc[0]
    assert asn["assignment_version_id"].startswith("wrk_")
    assert asn["instrument_id"] == inst["instrument_id"]
    assert asn["listing_id"] == lst["listing_id"]
    assert asn["id_namespace"] == NAMESPACE_EODHD_SYMBOL
    assert asn["id_value"] == "IBM"
    assert asn["normalized_id_value"] == "IBM"
    assert asn["effective_from_ts"] is None
    assert asn["effective_to_ts"] is None
    assert asn["available_at_ts"] == OBS1
    assert asn["confidence"] == AUTO_CONFIDENCE

    # actually landed in the fake lake
    assert len(lake.get("silver.instrument")) == 1
    assert len(lake.get("silver.listing_version")) == 1
    assert len(lake.get("silver.identifier_assignment_version")) == 1


# ---------------------------------------------------------------------------
# 2. idempotency
# ---------------------------------------------------------------------------

def test_upsert_idempotent():
    lake = _FakeLake()
    mb = _builder(lake)
    symbols = _symbols_df([{"code": "MSFT", "name": "Microsoft", "exchange": "US", "type": "Common Stock", "isin": None}])

    first = mb.upsert_from_symbols(symbols, CAP1, OBS1)
    second = mb.upsert_from_symbols(symbols, CAP2, OBS2)

    assert len(first["identifier_assignment_version"]) == 1
    assert len(second["identifier_assignment_version"]) == 0
    assert len(second["instrument"]) == 0
    assert len(second["listing_version"]) == 0

    assert len(lake.get("silver.instrument")) == 1
    assert len(lake.get("silver.listing_version")) == 1
    assert len(lake.get("silver.identifier_assignment_version")) == 1


# ---------------------------------------------------------------------------
# 3. BK -> BNY: close old, open new to same listing
# ---------------------------------------------------------------------------

def test_symbol_change_closes_old_opens_new_same_listing():
    lake = _FakeLake()
    mb = _builder(lake)
    symbols = _symbols_df([{"code": "BK", "name": "Bank of NY", "exchange": "US", "type": "Common Stock", "isin": None}])
    upsert_result = mb.upsert_from_symbols(symbols, CAP1, OBS1)
    lst = upsert_result["listing_version"].iloc[0]["listing_id"]
    old_assignment_id = upsert_result["identifier_assignment_version"].iloc[0]["assignment_version_id"]

    change_date = dt.date(2026, 6, 15)
    changes = _changes_df([{"old": "BK", "new": "BNY", "date": change_date}])
    change_result = mb.apply_symbol_changes(changes, CAP2, OBS2)

    assert len(change_result["identifier_assignment_version"]) == 2
    assert len(change_result["openfigi_resolution_queue"]) == 0
    assert len(change_result["data_quality_issue"]) == 0

    # controller-ruling fix: the superseded (original BK) row must come
    # back with an explicit system-close instruction, and the injected
    # system_closer must actually have been invoked with it.
    assert change_result["system_closes"] == [(old_assignment_id, OBS2)]
    assert lake.closer_calls == [
        ("silver.identifier_assignment_version", [(old_assignment_id, OBS2)])
    ]
    table = lake.get("silver.identifier_assignment_version")
    old_row = table[table["assignment_version_id"] == old_assignment_id].iloc[0]
    assert old_row["system_to_ts"] == OBS2

    assert mb.resolve_symbol_at("BK", dt.date(2026, 5, 1)) == lst
    assert mb.resolve_symbol_at("BNY", dt.date(2026, 8, 1)) == lst
    assert mb.resolve_symbol_at("BK", dt.date(2026, 8, 1)) is None
    # boundary: the change date itself belongs to the new code
    assert mb.resolve_symbol_at("BNY", change_date) == lst
    assert mb.resolve_symbol_at("BK", change_date) is None


def test_symbol_change_twice_same_code_chains_system_closes():
    """A code changed twice (A -> B -> C) must system-close BOTH
    superseded rows, and a second `apply_symbol_changes` call must be
    able to find B's (not A's) currently-open row - i.e. the within-batch
    and across-call system_to_ts bookkeeping both hold under the new
    system_to_ts-based `_current_rows` filter (no more reliance on
    `supersedes_assignment_version_id` for "is this current")."""
    lake = _FakeLake()
    mb = _builder(lake)
    symbols = _symbols_df([{"code": "A", "name": "A Corp", "exchange": "US", "type": "Common Stock", "isin": None}])
    mb.upsert_from_symbols(symbols, CAP1, OBS1)

    d1 = dt.date(2026, 3, 1)
    mb.apply_symbol_changes(_changes_df([{"old": "A", "new": "B", "date": d1}]), CAP2, OBS2)

    OBS3 = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    d2 = dt.date(2026, 9, 15)
    result2 = mb.apply_symbol_changes(_changes_df([{"old": "B", "new": "C", "date": d2}]), "cap_changes2", OBS3)

    assert len(result2["identifier_assignment_version"]) == 2
    assert len(result2["openfigi_resolution_queue"]) == 0

    assert mb.resolve_symbol_at("A", dt.date(2026, 2, 1)) is not None
    assert mb.resolve_symbol_at("A", dt.date(2026, 6, 1)) is None
    assert mb.resolve_symbol_at("B", dt.date(2026, 6, 1)) is not None
    assert mb.resolve_symbol_at("B", dt.date(2026, 10, 1)) is None
    assert mb.resolve_symbol_at("C", dt.date(2026, 10, 1)) is not None

    table = lake.get("silver.identifier_assignment_version")
    closed = table[table["system_to_ts"].notna()]
    assert len(closed) == 2  # A's original row, and B's open_row-turned-superseded row


# ---------------------------------------------------------------------------
# 4. unknown old code -> queued, not guessed
# ---------------------------------------------------------------------------

def test_unknown_old_code_queued_not_guessed():
    lake = _FakeLake()
    mb = _builder(lake)
    changes = _changes_df([{"old": "GHOST", "new": "GHOST2", "date": dt.date(2026, 3, 1)}])

    result = mb.apply_symbol_changes(changes, CAP2, OBS2)

    assert len(result["identifier_assignment_version"]) == 0
    assert len(result["openfigi_resolution_queue"]) == 1
    q = result["openfigi_resolution_queue"].iloc[0]
    assert q["status"] == REASON_UNKNOWN_OLD_CODE
    assert q["id_value"] == "GHOST"
    assert q["queue_id"].startswith("wrk_")
    assert q["instrument_id"] is None
    assert q["listing_id"] is None

    assert mb.resolve_symbol_at("GHOST", dt.date(2026, 5, 1)) is None
    assert mb.resolve_symbol_at("GHOST2", dt.date(2026, 5, 1)) is None


# ---------------------------------------------------------------------------
# 5. ticker reuse: two disjoint windows, distinct listings, resolve by date
# ---------------------------------------------------------------------------

def test_ticker_reuse_two_listings():
    assignments = pd.DataFrame([
        {
            "assignment_version_id": "wrk_old", "issuer_id": None, "registrant_id": None,
            "instrument_id": "ins_old", "listing_id": "lst_old",
            "id_namespace": NAMESPACE_EODHD_SYMBOL, "id_value": "X", "normalized_id_value": "X",
            "effective_from_ts": dt.datetime(2010, 1, 1, tzinfo=dt.timezone.utc),
            "effective_to_ts": dt.datetime(2017, 6, 1, tzinfo=dt.timezone.utc),
            "observed_at_ts": dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc),
            "available_at_ts": dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc),
            "system_from_ts": dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc),
            "system_to_ts": None, "source_system": "eodhd", "source_capture_id": "cap_a",
            "verification_status": "UNVERIFIED", "confidence": AUTO_CONFIDENCE,
            "supersedes_assignment_version_id": None,
        },
        {
            "assignment_version_id": "wrk_new", "issuer_id": None, "registrant_id": None,
            "instrument_id": "ins_new", "listing_id": "lst_new",
            "id_namespace": NAMESPACE_EODHD_SYMBOL, "id_value": "X", "normalized_id_value": "X",
            "effective_from_ts": dt.datetime(2020, 3, 1, tzinfo=dt.timezone.utc),
            "effective_to_ts": None,
            "observed_at_ts": dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc),
            "available_at_ts": dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc),
            "system_from_ts": dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc),
            "system_to_ts": None, "source_system": "eodhd", "source_capture_id": "cap_b",
            "verification_status": "UNVERIFIED", "confidence": AUTO_CONFIDENCE,
            "supersedes_assignment_version_id": None,
        },
    ], columns=_ASSIGNMENT_COLUMNS)

    assert resolve_listing_at(assignments, "X", dt.date(2015, 1, 1)) == "lst_old"
    assert resolve_listing_at(assignments, "X", dt.date(2021, 1, 1)) == "lst_new"
    assert resolve_listing_at(assignments, "X", dt.date(2018, 6, 1)) is None  # gap between windows


# ---------------------------------------------------------------------------
# 6. CUSIP attached at instrument scope
# ---------------------------------------------------------------------------

def test_cusip_attached_at_instrument_scope():
    lake = _FakeLake()
    mb = _builder(lake)
    symbols = _symbols_df([{"code": "IBM", "name": "IBM Corp", "exchange": "US", "type": "Common Stock", "isin": None}])
    upsert_result = mb.upsert_from_symbols(symbols, CAP1, OBS1)
    instrument_id = upsert_result["instrument"].iloc[0]["instrument_id"]

    fundamentals = _fundamentals_df([{
        "code": "IBM", "cusip": "459200101", "isin": "US4592001014", "cik": "0000051143",
        "name": "IBM Corp", "type": "Common Stock", "share_class": None,
    }])
    result = mb.attach_issue_ids(fundamentals, CAP2, OBS2)

    rows = result["identifier_assignment_version"]
    assert len(rows) == 3
    by_ns = {r["id_namespace"]: r for _, r in rows.iterrows()}

    cusip_row = by_ns[NAMESPACE_CUSIP]
    assert cusip_row["instrument_id"] == instrument_id
    assert cusip_row["listing_id"] is None
    assert cusip_row["id_value"] == "459200101"

    isin_row = by_ns[NAMESPACE_ISIN]
    assert isin_row["instrument_id"] == instrument_id
    assert isin_row["listing_id"] is None

    cik_row = by_ns[NAMESPACE_SEC_CIK]
    assert cik_row["instrument_id"] == instrument_id
    assert cik_row["listing_id"] is None
    assert cik_row["registrant_id"] is None

    assert len(result["openfigi_resolution_queue"]) == 0

    # idempotent re-attach: no duplicate rows
    again = mb.attach_issue_ids(fundamentals, CAP2, OBS2)
    assert len(again["identifier_assignment_version"]) == 0

"""Deferred tests for tick_vault.reference_queries (task-7 brief, Step 1).

Requires duckdb, deltalake, pyarrow, pytest - none of which are installable
in the authoring sandbox (network blocked). Placed under tests/deferred/ so
the sandbox mini-runner (tests/run_tests.py, which os.listdir()s tests/
non-recursively for test_*.py) does not pick these up. Run with:

    pytest tick-vault/tests

once the dependencies are available. UNVERIFIED until then.

Seeding (per task-7 brief and the controller ruling) models a
`silver.index_membership_version` + `silver.identifier_assignment_version`
lake, all rows re-downloaded (and thus made knowable) on 2026-09-01
(`available_at_ts = 2026-09-01`, `system_to_ts = NULL`, i.e. currently
open):

  membership (index_id = 'idx_sp500'):
    - mv_aapl:  lst_aapl / inst_aapl, member 2015-01-01 -> NULL (still a
                member), RESOLVED.
    - mv_twtr:  lst_twtr / inst_twtr, member 2018-06-01 -> 2022-10-27
                (delisted; membership mapping retained), RESOLVED.
    - mv_lstnew: lst_new / inst_new, member 2020-01-01 -> NULL (the "2020
                joiner" that must be excluded from a 2017 universe), the
                same listing that ticker "X" is reassigned to below.
    - mv_ambig: lst_ambig / inst_ambig, member 2016-01-01 -> NULL, but
                resolution_status = 'AMBIGUOUS' - must never be returned
                by the macro, though it is visible via a direct query on
                the raw `silver_index_membership_version` view.

  identifiers (ticker reuse):
    - ia_x_old: id_namespace='TICKER', id_value='X' -> lst_old / inst_old,
                effective 2010-01-01 -> 2017-06-01.
    - ia_x_new: id_namespace='TICKER', id_value='X' -> lst_new / inst_new,
                effective 2020-03-01 -> NULL.
"""
import datetime as dt
import decimal

import duckdb
import pyarrow as pa
import pytest
from deltalake import write_deltalake

from tick_vault.schemas import SCHEMAS, create_all
from tick_vault.temporal import attach
from tick_vault.reference_queries import install_reference_macros

_AVAILABLE_AT = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.timezone.utc)


def _ts(y, m, d, hh=0, mm=0, ss=0):
    return dt.datetime(y, m, d, hh, mm, ss, tzinfo=dt.timezone.utc)


def _date(y, m, d):
    return dt.date(y, m, d)


def _membership_row(**overrides) -> dict:
    row = {
        "membership_version_id": "mv_default",
        "index_id": "idx_sp500",
        "index_vendor_symbol": "GSPC.INDX",
        "listing_id": None,
        "instrument_id": None,
        "source_constituent_code": "DEFAULT",
        "source_exchange_code": "US",
        "source_name": None,
        "membership_effective_from": _date(2000, 1, 1),
        "membership_effective_to": None,
        "inclusion_reason": None,
        "exclusion_reason": None,
        "index_weight": None,
        "observed_at_ts": _AVAILABLE_AT,
        "available_at_ts": _AVAILABLE_AT,
        "system_from_ts": _AVAILABLE_AT,
        "system_to_ts": None,
        "source_system": "eodhd",
        "source_capture_id": "cap_test",
        "resolution_status": "RESOLVED",
        "confidence": decimal.Decimal("1.0000"),
        "supersedes_membership_version_id": None,
    }
    row.update(overrides)
    return row


def _identifier_row(**overrides) -> dict:
    row = {
        "assignment_version_id": "ia_default",
        "issuer_id": None,
        "registrant_id": None,
        "instrument_id": None,
        "listing_id": None,
        "id_namespace": "TICKER",
        "id_value": "DEFAULT",
        "normalized_id_value": "DEFAULT",
        "effective_from_ts": None,
        "effective_to_ts": None,
        "observed_at_ts": _AVAILABLE_AT,
        "available_at_ts": _AVAILABLE_AT,
        "system_from_ts": _AVAILABLE_AT,
        "system_to_ts": None,
        "source_system": "eodhd",
        "source_capture_id": "cap_test",
        "verification_status": "VERIFIED",
        "confidence": decimal.Decimal("1.0000"),
        "supersedes_assignment_version_id": None,
    }
    row.update(overrides)
    return row


def _seed_reference_lake(tmp_path) -> str:
    root = str(tmp_path / "lake")
    create_all(root)

    membership_rows = [
        _membership_row(
            membership_version_id="mv_aapl",
            listing_id="lst_aapl",
            instrument_id="inst_aapl",
            source_constituent_code="AAPL",
            source_name="Apple Inc.",
            membership_effective_from=_date(2015, 1, 1),
            membership_effective_to=None,
        ),
        _membership_row(
            membership_version_id="mv_twtr",
            listing_id="lst_twtr",
            instrument_id="inst_twtr",
            source_constituent_code="TWTR",
            source_name="Twitter, Inc.",
            membership_effective_from=_date(2018, 6, 1),
            membership_effective_to=_date(2022, 10, 27),
        ),
        _membership_row(
            membership_version_id="mv_lstnew",
            listing_id="lst_new",
            instrument_id="inst_new",
            source_constituent_code="X",
            source_name="New Joiner Co.",
            membership_effective_from=_date(2020, 1, 1),
            membership_effective_to=None,
        ),
        _membership_row(
            membership_version_id="mv_ambig",
            listing_id="lst_ambig",
            instrument_id="inst_ambig",
            source_constituent_code="AMBIG",
            source_name="Ambiguous Co.",
            membership_effective_from=_date(2016, 1, 1),
            membership_effective_to=None,
            resolution_status="AMBIGUOUS",
        ),
    ]
    membership_table = pa.Table.from_pylist(
        membership_rows, schema=SCHEMAS["silver.index_membership_version"]
    )
    write_deltalake(
        f"{root}/silver/index_membership_version", membership_table, mode="append"
    )

    identifier_rows = [
        _identifier_row(
            assignment_version_id="ia_x_old",
            listing_id="lst_old",
            instrument_id="inst_old",
            id_namespace="TICKER",
            id_value="X",
            normalized_id_value="X",
            effective_from_ts=_ts(2010, 1, 1),
            effective_to_ts=_ts(2017, 6, 1),
        ),
        _identifier_row(
            assignment_version_id="ia_x_new",
            listing_id="lst_new",
            instrument_id="inst_new",
            id_namespace="TICKER",
            id_value="X",
            normalized_id_value="X",
            effective_from_ts=_ts(2020, 3, 1),
            effective_to_ts=None,
        ),
    ]
    identifier_table = pa.Table.from_pylist(
        identifier_rows, schema=SCHEMAS["silver.identifier_assignment_version"]
    )
    write_deltalake(
        f"{root}/silver/identifier_assignment_version",
        identifier_table,
        mode="append",
    )

    return root


@pytest.fixture
def lake(tmp_path):
    root = _seed_reference_lake(tmp_path)
    con = duckdb.connect()
    attach(con, root)
    install_reference_macros(con)
    yield con
    con.close()


def q(con, sql, params=None):
    cur = con.execute(sql, params) if params else con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _members(con, market_date, decision_ts):
    rows = q(
        con,
        "SELECT listing_id, instrument_id FROM pit_sp500_membership("
        f"DATE '{market_date}', TIMESTAMP '{decision_ts}')",
    )
    return {r["listing_id"] for r in rows}


def _resolve(con, symbol, market_date, decision_ts):
    rows = q(
        con,
        f"SELECT listing_id, instrument_id FROM pit_listing("
        f"'{symbol}', DATE '{market_date}', TIMESTAMP '{decision_ts}')",
    )
    assert len(rows) == 1, f"expected exactly one resolution, got {rows}"
    return rows[0]["listing_id"]


_DECISION_AFTER_INGEST = "2026-09-05 00:00:00+00"


def test_2017_universe_excludes_2020_joiner(lake):
    members = _members(lake, "2017-03-06", _DECISION_AFTER_INGEST)
    assert "lst_new" not in members
    assert "lst_aapl" in members


def test_delisted_member_present_within_tenure(lake):
    members = _members(lake, "2021-06-01", _DECISION_AFTER_INGEST)
    assert "lst_twtr" in members


def test_reused_ticker_resolves_by_date(lake):
    assert _resolve(lake, "X", "2015-06-01", _DECISION_AFTER_INGEST) == "lst_old"
    assert _resolve(lake, "X", "2021-06-01", _DECISION_AFTER_INGEST) == "lst_new"


def test_ambiguous_membership_excluded_but_countable(lake):
    members = _members(lake, "2021-06-01", _DECISION_AFTER_INGEST)
    assert "lst_ambig" not in members

    direct_rows = q(
        lake,
        "SELECT listing_id, resolution_status FROM silver_index_membership_version "
        "WHERE resolution_status = 'AMBIGUOUS'",
    )
    assert {r["listing_id"] for r in direct_rows} == {"lst_ambig"}


def test_membership_invisible_before_ingestion(lake):
    rows = q(
        lake,
        "SELECT * FROM pit_sp500_membership("
        "DATE '2017-03-06', TIMESTAMP '2017-03-06 21:00:00+00')",
    )
    assert rows == []

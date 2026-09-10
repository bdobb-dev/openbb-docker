"""Deferred tests for tick_vault.contract (task-8 brief, controller ruling).

Requires duckdb, deltalake, pyarrow, pytest - none of which are installable
in the authoring sandbox (network blocked). Placed under tests/deferred/ so
the sandbox mini-runner (tests/run_tests.py, which os.listdir()s tests/
non-recursively for test_*.py) does not pick these up. Run with:

    pytest tick-vault/tests

once the dependencies are available. UNVERIFIED until then.

Seeding (controller ruling): reuses the `_tick(...)` row-builder from
`tests/deferred/test_temporal.py` and the `_identifier_row(...)` row-builder
from `tests/deferred/test_reference_queries.py` when importable as sibling
modules (pytest's rootdir-relative import puts `tests/deferred/` on
`sys.path` since it has no `__init__.py`); falls back to minimal duplicated
builders otherwise so this file has no hard import-order dependency on the
other two deferred test modules.

`_seed_contract_lake` builds ONE lake (a single `create_all(root)` call,
since `_seed_lake`/`_seed_reference_lake` each call `create_all` themselves
and would clobber each other's rows if both were called against the same
root) containing:

  - `silver.us_trade_tick_version`: two logical ticks, both
    `trade_date = 2021-11-05`:
      * tick A (`listing_id = "lst_old"`): rev 1 price 100.0, available
        2026-01-10 (`_AVAILABLE_EARLY`); rev 2 (correction) price 101.0,
        available 2026-02-01 (`_AVAILABLE_LATE`).
      * tick B (`listing_id = "lst_new"`): rev 1 price 200.0, available
        2026-01-10.
  - `silver.identifier_assignment_version`: vendor symbol "X" reused
    across two listings (mirrors the task-7 ticker-reuse scenario), both
    rows knowable from `_IDENTITY_AVAILABLE` (2000-01-01, i.e. always
    knowable at every `as_of`/`decision_ts` this file uses):
      * `id_value="X"` -> `listing_id="lst_old"`,
        effective 2010-01-01 -> 2017-06-01.
      * `id_value="X"` -> `listing_id="lst_new"`,
        effective 2020-03-01 -> NULL.
"""
import datetime as dt
import decimal

import duckdb
import pyarrow as pa
import pytest
from deltalake import write_deltalake

from tick_vault.schemas import SCHEMAS, create_all
from tick_vault.contract import (
    MODE_BITEMPORAL,
    MODE_EFFECTIVE_ONLY,
    MODE_GOLD,
    MODE_PIT_KNOWLEDGE,
    query_ticks,
)
from tick_vault.sl_conditions import DECODE_VERSION
from tick_vault.tick_parser import PARSER_VERSION
from tick_vault.temporal import (
    POLICY_HISTORICAL_VENDOR_FINAL,
    SEQUENCE_POLICY,
)

try:
    from test_temporal import _tick
except ImportError:

    def _tick(**overrides) -> dict:
        row = {
            "tick_version_id": overrides.get("tick_version_id", "tkv_default"),
            "logical_tick_id": overrides.get("logical_tick_id", "ltk_default"),
            "source_system": "eodhd",
            "source_capture_id": "cap_test",
            "source_page_ordinal": None,
            "source_row_ordinal": 0,
            "listing_id": None,
            "instrument_id": None,
            "vendor_request_symbol": "TEST",
            "eodhd_exchange_code": None,
            "trade_date": dt.date(2021, 11, 5),
            "trade_ts_ms": 1636122600000,
            "trade_ts": dt.datetime(2021, 11, 5, 14, 30, 0, tzinfo=dt.timezone.utc),
            "venue_code_raw": None,
            "sub_mkt_raw": None,
            "session_seq": None,
            "venue_id": None,
            "mic": None,
            "price_venue_type": "consolidated",
            "price": decimal.Decimal("0.0"),
            "size": None,
            "sale_condition_raw": None,
            "sale_condition_flags": None,
            "origin": None,
            "vendor_trade_id": None,
            "vendor_sequence_no": None,
            "observed_at_ts": dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc),
            "committed_at_ts": dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc),
            "available_at_ts": dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc),
            "revision_number": 1,
            "is_correction": False,
            "is_cancelled": False,
            "is_late_add": False,
            "supersedes_tick_version_id": None,
            "payload_hash": "hash",
            "record_hash": "hash",
            "parser_version": "v1",
        }
        row.update(overrides)
        return row


try:
    from test_reference_queries import _identifier_row
except ImportError:

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
            "observed_at_ts": _IDENTITY_AVAILABLE,
            "available_at_ts": _IDENTITY_AVAILABLE,
            "system_from_ts": _IDENTITY_AVAILABLE,
            "system_to_ts": None,
            "source_system": "eodhd",
            "source_capture_id": "cap_test",
            "verification_status": "VERIFIED",
            "confidence": "1.0000",
            "supersedes_assignment_version_id": None,
        }
        row.update(overrides)
        return row


_TRADE_DATE = dt.date(2021, 11, 5)
_AVAILABLE_EARLY = dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc)
_AVAILABLE_LATE = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
_IDENTITY_AVAILABLE = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)


def _ts(y, m, d):
    return dt.datetime(y, m, d, tzinfo=dt.timezone.utc)


def _seed_contract_lake(tmp_path) -> str:
    root = str(tmp_path / "lake")
    create_all(root)

    tick_rows = [
        _tick(
            tick_version_id="tkv_a1", logical_tick_id="A", listing_id="lst_old",
            price=decimal.Decimal("100.0"), available_at_ts=_AVAILABLE_EARLY, revision_number=1,
        ),
        _tick(
            tick_version_id="tkv_a2", logical_tick_id="A", listing_id="lst_old",
            price=decimal.Decimal("101.0"), available_at_ts=_AVAILABLE_LATE, revision_number=2,
            is_correction=True, supersedes_tick_version_id="tkv_a1",
        ),
        _tick(
            tick_version_id="tkv_b1", logical_tick_id="B", listing_id="lst_new",
            price=decimal.Decimal("200.0"), available_at_ts=_AVAILABLE_EARLY, revision_number=1,
        ),
    ]
    tick_table = pa.Table.from_pylist(
        tick_rows, schema=SCHEMAS["silver.us_trade_tick_version"]
    )
    write_deltalake(f"{root}/silver/us_trade_tick_version", tick_table, mode="append")

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
            available_at_ts=_IDENTITY_AVAILABLE,
            observed_at_ts=_IDENTITY_AVAILABLE,
            system_from_ts=_IDENTITY_AVAILABLE,
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
            available_at_ts=_IDENTITY_AVAILABLE,
            observed_at_ts=_IDENTITY_AVAILABLE,
            system_from_ts=_IDENTITY_AVAILABLE,
        ),
    ]
    identifier_table = pa.Table.from_pylist(
        identifier_rows, schema=SCHEMAS["silver.identifier_assignment_version"]
    )
    write_deltalake(
        f"{root}/silver/identifier_assignment_version", identifier_table, mode="append"
    )

    return root


@pytest.fixture
def lake(tmp_path):
    root = _seed_contract_lake(tmp_path)
    con = duckdb.connect()
    yield con, root
    con.close()


def prices(df):
    return set(df["price"].astype(float).tolist())


def test_no_params_is_gold_mode(lake):
    con, root = lake
    result = query_ticks(
        con, root, start=dt.date(2021, 11, 1), end=dt.date(2021, 11, 10)
    )
    assert result.provenance["mode"] == MODE_GOLD
    # latest_ticks(): A corrected to 101.0, B at 200.0 - both included,
    # no symbol filter applied.
    assert prices(result.df) == {101.0, 200.0}


def test_as_of_only_is_pit(lake):
    con, root = lake
    result = query_ticks(
        con,
        root,
        start=dt.date(2021, 11, 1),
        end=dt.date(2021, 11, 10),
        as_of=dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc),
    )
    assert result.provenance["mode"] == MODE_PIT_KNOWLEDGE
    # as_of predates A's correction (available 2026-02-01) - original A
    # revision (100.0) and B (200.0) are what was knowable then.
    assert prices(result.df) == {100.0, 200.0}


def test_both_params_bitemporal_resolves_identity_at_effective(lake):
    con, root = lake
    as_of = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)  # sees A's correction

    result_old = query_ticks(
        con,
        root,
        symbols=["X"],
        start=dt.date(2021, 11, 1),
        end=dt.date(2021, 11, 10),
        as_of=as_of,
        effective=dt.date(2015, 6, 1),  # within lst_old's 2010-2017 window
    )
    assert result_old.provenance["mode"] == MODE_BITEMPORAL
    assert prices(result_old.df) == {101.0}  # lst_old's A, corrected data

    result_new = query_ticks(
        con,
        root,
        symbols=["X"],
        start=dt.date(2021, 11, 1),
        end=dt.date(2021, 11, 10),
        as_of=as_of,
        effective=dt.date(2021, 6, 1),  # within lst_new's 2020-> window
    )
    assert prices(result_new.df) == {200.0}  # lst_new's B


def test_effective_only_uses_latest_data_historical_identity(lake):
    con, root = lake
    result = query_ticks(
        con,
        root,
        symbols=["X"],
        start=dt.date(2021, 11, 1),
        end=dt.date(2021, 11, 10),
        effective=dt.date(2015, 6, 1),  # resolves "X" -> lst_old historically
    )
    assert result.provenance["mode"] == MODE_EFFECTIVE_ONLY
    # Identity is historical (lst_old), but data is the *latest* (corrected)
    # revision - 101.0, not the 100.0 that was contemporaneous with 2015.
    assert prices(result.df) == {101.0}


def test_future_as_of_raises(lake):
    con, root = lake
    far_future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3650)
    with pytest.raises(ValueError):
        query_ticks(
            con,
            root,
            start=dt.date(2021, 11, 1),
            end=dt.date(2021, 11, 10),
            as_of=far_future,
        )


def test_pre_capture_as_of_warns_not_errors(lake):
    con, root = lake
    result = query_ticks(
        con,
        root,
        start=dt.date(2021, 11, 1),
        end=dt.date(2021, 11, 10),
        as_of=dt.datetime(2019, 1, 1, tzinfo=dt.timezone.utc),
    )
    assert len(result.df) == 0
    assert "as_of predates earliest capture" in result.provenance["warnings"]


def test_provenance_pins_delta_versions(lake):
    con, root = lake
    result = query_ticks(
        con, root, start=dt.date(2021, 11, 1), end=dt.date(2021, 11, 10)
    )
    delta_versions = result.provenance["delta_versions"]
    # `create_all(root)` writes version 0 for every registered table.
    # `_seed_contract_lake` then does one further `write_deltalake(...,
    # mode="append")` each against `silver.us_trade_tick_version` and
    # `silver.identifier_assignment_version` (the tick rows and the "X"
    # identifier rows), bumping each of those two to version 1.
    # `silver.index_membership_version` is never written to in this
    # fixture, so it stays at version 0. All three are therefore only
    # guaranteed to be non-negative in general; the two appended-to
    # tables are additionally asserted to have advanced past the
    # `create_all` baseline (version >= 1).
    assert delta_versions["silver.us_trade_tick_version"] >= 0
    assert delta_versions["silver.index_membership_version"] >= 0
    assert delta_versions["silver.identifier_assignment_version"] >= 0
    assert delta_versions["silver.us_trade_tick_version"] >= 1
    assert delta_versions["silver.identifier_assignment_version"] >= 1
    assert result.provenance["sequence_policy_version"] == SEQUENCE_POLICY
    assert result.provenance["sl_decode_version"] == DECODE_VERSION
    assert result.provenance["parser_version"] == PARSER_VERSION


def test_historical_vendor_final_policy_uses_simulated_floor_not_local_capture(lake):
    # Fix-round finding 1: under HISTORICAL_VENDOR_FINAL_V1, availability is
    # SIMULATED as trade_date + 1 day @ 08:00 UTC, not the locally-stored
    # available_at_ts. Both tick rows here have trade_date = 2021-11-05, so
    # the simulated floor is 2021-11-06T08:00Z - well before either row's
    # real available_at_ts (2026-01-10 / 2026-02-01). An as_of just after
    # the simulated floor but long before local ingestion must therefore
    # see data (not be wrongly short-circuited to empty by the
    # local-capture floor).
    con, root = lake
    as_of = dt.datetime(2021, 11, 6, 9, 0, tzinfo=dt.timezone.utc)
    result = query_ticks(
        con,
        root,
        start=dt.date(2021, 11, 1),
        end=dt.date(2021, 11, 10),
        as_of=as_of,
        availability_policy=POLICY_HISTORICAL_VENDOR_FINAL,
    )
    assert len(result.df) > 0
    assert "as_of predates earliest capture" not in result.provenance["warnings"]
    assert "as_of predates simulated availability floor" not in result.provenance["warnings"]


def test_historical_vendor_final_policy_before_simulated_floor_warns_empty(lake):
    # Same policy, but as_of before the simulated floor (2021-11-06T08:00Z)
    # - must be empty with the simulated-floor warning, not the
    # local-capture one.
    con, root = lake
    as_of = dt.datetime(2021, 11, 6, 7, 0, tzinfo=dt.timezone.utc)
    result = query_ticks(
        con,
        root,
        start=dt.date(2021, 11, 1),
        end=dt.date(2021, 11, 10),
        as_of=as_of,
        availability_policy=POLICY_HISTORICAL_VENDOR_FINAL,
    )
    assert len(result.df) == 0
    assert "as_of predates simulated availability floor" in result.provenance["warnings"]


def test_empty_symbols_list_returns_empty_result_with_warning(lake):
    # Fix-round finding 2: symbols=[] is an explicit "nothing requested",
    # distinct from symbols=None ("no filter") - must not raise, must
    # return an empty result with a dedicated warning.
    con, root = lake
    result = query_ticks(
        con,
        root,
        symbols=[],
        start=dt.date(2021, 11, 1),
        end=dt.date(2021, 11, 10),
    )
    assert len(result.df) == 0
    assert "no symbols requested" in result.provenance["warnings"]


def test_gold_mode_with_nondefault_policy_warns_ignored(lake):
    # Fix-round finding 3: GOLD mode always reads latest_ticks() regardless
    # of availability_policy - passing a non-default policy alongside GOLD
    # mode (no as_of/effective) should warn, not silently do nothing.
    con, root = lake
    result = query_ticks(
        con,
        root,
        start=dt.date(2021, 11, 1),
        end=dt.date(2021, 11, 10),
        availability_policy=POLICY_HISTORICAL_VENDOR_FINAL,
    )
    assert result.provenance["mode"] == MODE_GOLD
    # M10 (final-review finding): the warning text now covers both GOLD
    # and EFFECTIVE_ONLY (see test_effective_only_mode_with_nondefault_
    # policy_warns_ignored below), since both modes ignore
    # availability_policy identically.
    assert (
        "availability_policy ignored in GOLD/EFFECTIVE_ONLY mode"
        in result.provenance["warnings"]
    )
    # Data is still latest_ticks() regardless of the (ignored) policy.
    assert prices(result.df) == {101.0, 200.0}


def test_effective_only_mode_with_nondefault_policy_warns_ignored(lake):
    # M10 (final-review finding): EFFECTIVE_ONLY mode also always reads
    # latest_ticks() regardless of availability_policy (mirroring GOLD) -
    # passing a non-default policy alongside EFFECTIVE_ONLY (effective
    # given, no as_of) should warn too, not silently do nothing.
    con, root = lake
    result = query_ticks(
        con,
        root,
        symbols=["X"],
        start=dt.date(2021, 11, 1),
        end=dt.date(2021, 11, 10),
        effective=dt.date(2015, 6, 1),
        availability_policy=POLICY_HISTORICAL_VENDOR_FINAL,
    )
    assert result.provenance["mode"] == MODE_EFFECTIVE_ONLY
    assert (
        "availability_policy ignored in GOLD/EFFECTIVE_ONLY mode"
        in result.provenance["warnings"]
    )
    # Data is still latest_ticks() regardless of the (ignored) policy.
    assert prices(result.df) == {101.0}

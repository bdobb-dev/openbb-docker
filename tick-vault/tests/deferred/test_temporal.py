"""Deferred tests for tick_vault.temporal (task-6 brief, Step 1).

Requires duckdb, deltalake, pyarrow, pytest - none of which are installable
in the authoring sandbox (network blocked). Placed under tests/deferred/ so
the sandbox mini-runner (tests/run_tests.py, which os.listdir()s tests/
non-recursively for test_*.py) does not pick these up. Run with:

    pytest tick-vault/tests

once the dependencies are available. UNVERIFIED until then.

Seeding (per task-6 brief Step 1 and the controller ruling): all seeded
ticks use `trade_date = 2021-11-05` so the `HISTORICAL_VENDOR_FINAL_V1`
simulated-availability test (as_of `2021-11-08`) sees data (simulated
availability = trade_date + 1 day @ 08:00 UTC = `2021-11-06 08:00:00+00`,
which is `<= 2021-11-08`).

  - tick A: rev 1, price 150.26, available 2026-01-10.
            rev 2 (correction), price 150.27, available 2026-02-01.
  - tick B: rev 1, price 10.0, available 2026-01-10.
            rev 2 (tombstone, is_cancelled), available 2026-02-01.
  - tick C: rev 1, price 99.0, available 2026-01-10.
"""
import datetime as dt
import decimal

import duckdb
import pyarrow as pa
import pytest
from deltalake import write_deltalake

from tick_vault.schemas import SCHEMAS, create_all
from tick_vault.temporal import attach, install_macros

_TRADE_DATE = dt.date(2021, 11, 5)
_TRADE_TS = dt.datetime(2021, 11, 5, 14, 30, 0, tzinfo=dt.timezone.utc)
_TRADE_TS_MS = int(_TRADE_TS.timestamp() * 1000)

_AVAILABLE_EARLY = dt.datetime(2026, 1, 10, 0, 0, 0, tzinfo=dt.timezone.utc)
_AVAILABLE_LATE = dt.datetime(2026, 2, 1, 0, 0, 0, tzinfo=dt.timezone.utc)


def _tick(**overrides) -> dict:
    """Build a full `silver.us_trade_tick_version` row dict, filling every
    non-nullable schema column with a reasonable default; callers override
    whatever's relevant to the scenario being tested."""
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
        "trade_date": _TRADE_DATE,
        "trade_ts_ms": _TRADE_TS_MS,
        "trade_ts": _TRADE_TS,
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
        "observed_at_ts": _AVAILABLE_EARLY,
        "committed_at_ts": _AVAILABLE_EARLY,
        "available_at_ts": _AVAILABLE_EARLY,
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


def _seed_lake(tmp_path) -> str:
    """Seed a lake at `tmp_path` with ticks A (rev1 + correction rev2), B
    (rev1 + cancelled tombstone rev2), and C (rev1), per the module
    docstring's scenario."""
    root = str(tmp_path / "lake")
    create_all(root)

    rows = [
        _tick(
            tick_version_id="tkv_a1", logical_tick_id="A", price=decimal.Decimal("150.26"),
            available_at_ts=_AVAILABLE_EARLY, revision_number=1,
        ),
        _tick(
            tick_version_id="tkv_a2", logical_tick_id="A", price=decimal.Decimal("150.27"),
            available_at_ts=_AVAILABLE_LATE, revision_number=2,
            is_correction=True, supersedes_tick_version_id="tkv_a1",
        ),
        _tick(
            tick_version_id="tkv_b1", logical_tick_id="B", price=decimal.Decimal("10.0"),
            available_at_ts=_AVAILABLE_EARLY, revision_number=1,
        ),
        _tick(
            tick_version_id="tkv_b2", logical_tick_id="B", price=decimal.Decimal("10.0"),
            available_at_ts=_AVAILABLE_LATE, revision_number=2,
            is_cancelled=True, supersedes_tick_version_id="tkv_b1",
        ),
        _tick(
            tick_version_id="tkv_c1", logical_tick_id="C", price=decimal.Decimal("99.0"),
            available_at_ts=_AVAILABLE_EARLY, revision_number=1,
        ),
    ]

    table = pa.Table.from_pylist(rows, schema=SCHEMAS["silver.us_trade_tick_version"])
    write_deltalake(
        f"{root}/silver/us_trade_tick_version", table, mode="append"
    )
    return root


@pytest.fixture
def lake(tmp_path):
    root = _seed_lake(tmp_path)
    con = duckdb.connect()
    attach(con, root)
    install_macros(con)
    yield con
    con.close()


def q(con, sql):
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def prices(rows):
    return {r["logical_tick_id"]: float(r["price"]) for r in rows}


def test_latest_serves_corrected_and_drops_cancelled(lake):
    rows = q(lake, "SELECT logical_tick_id, price FROM latest_ticks() ORDER BY 1")
    assert prices(rows) == {"A": 150.27, "C": 99.0}     # A corrected, B gone


def test_pit_before_correction_sees_original_and_uncancelled(lake):
    rows = q(lake, "SELECT * FROM pit_ticks(TIMESTAMP '2026-01-15 00:00:00+00')")
    assert prices(rows) == {"A": 150.26, "B": 10.0, "C": 99.0}


def test_pit_after_correction_matches_latest(lake):
    rows = q(lake, "SELECT * FROM pit_ticks(TIMESTAMP '2026-03-01 00:00:00+00')")
    assert prices(rows) == {"A": 150.27, "C": 99.0}


def test_pit_before_any_capture_is_empty(lake):
    assert q(lake, "SELECT * FROM pit_ticks(TIMESTAMP '2019-01-01 00:00:00+00')") == []


def test_simulated_policy_makes_history_visible(lake):
    rows = q(
        lake,
        "SELECT * FROM pit_ticks_policy("
        "TIMESTAMP '2021-11-08 00:00:00+00', 'HISTORICAL_VENDOR_FINAL_V1')",
    )
    assert len(rows) > 0   # trade_date 2021-11-05 + T+1 simulated availability


def test_tape_order_deterministic(tmp_path):
    """Two ticks that tie on every `tape_order` field except
    `tick_version_id` must sort by `tick_version_id` (stable tie-break),
    per baseline Sec 12.3's ORDER BY list ending in `tick_version_id`."""
    root = str(tmp_path / "lake")
    create_all(root)

    common = dict(
        logical_tick_id="D",
        trade_ts_ms=_TRADE_TS_MS,
        vendor_sequence_no=None,
        observed_at_ts=_AVAILABLE_EARLY,
        source_page_ordinal=1,
        source_row_ordinal=1,
        available_at_ts=_AVAILABLE_EARLY,
    )
    rows = [
        _tick(tick_version_id="tkv_bbb", price=decimal.Decimal("1.0"), **common),
        _tick(tick_version_id="tkv_aaa", price=decimal.Decimal("2.0"), **common),
    ]
    table = pa.Table.from_pylist(rows, schema=SCHEMAS["silver.us_trade_tick_version"])
    write_deltalake(f"{root}/silver/us_trade_tick_version", table, mode="append")

    con = duckdb.connect()
    attach(con, root)
    install_macros(con)

    ordered = q(con, "SELECT tick_version_id FROM tape_order()")
    ids_in_order = [r["tick_version_id"] for r in ordered]
    assert ids_in_order.index("tkv_aaa") < ids_in_order.index("tkv_bbb")

    # Re-run to confirm the ordering is stable across repeated calls.
    ordered_again = q(con, "SELECT tick_version_id FROM tape_order()")
    assert [r["tick_version_id"] for r in ordered_again] == ids_in_order
    con.close()


def test_tape_order_custom_venue_priorities_reorders_same_ms_ticks(tmp_path):
    """A custom `venue_priorities` dict passed to `install_macros` must
    change `tape_order()`'s ordering for ticks tied on every other field
    (trade_ts_ms, vendor_sequence_no, observed_at_ts, source_page_ordinal,
    source_row_ordinal) but differing by `venue_id` - lower venue_priority
    sorts first, per baseline Sec 12.3's ORDER BY list."""
    root = str(tmp_path / "lake")
    create_all(root)

    common = dict(
        logical_tick_id="E",
        trade_ts_ms=_TRADE_TS_MS,
        vendor_sequence_no=None,
        observed_at_ts=_AVAILABLE_EARLY,
        source_page_ordinal=1,
        source_row_ordinal=1,
        available_at_ts=_AVAILABLE_EARLY,
    )
    rows = [
        _tick(
            tick_version_id="tkv_zzz", price=decimal.Decimal("1.0"), venue_id="NASDAQ", **common
        ),
        _tick(
            tick_version_id="tkv_yyy", price=decimal.Decimal("2.0"), venue_id="NYSE", **common
        ),
    ]
    table = pa.Table.from_pylist(rows, schema=SCHEMAS["silver.us_trade_tick_version"])
    write_deltalake(f"{root}/silver/us_trade_tick_version", table, mode="append")

    con = duckdb.connect()
    attach(con, root)
    # Without a priority override, tkv_yyy (venue "NYSE") sorts before
    # tkv_zzz (venue "NASDAQ") because both default to priority 0 and
    # tick_version_id is the final tie-break ("tkv_y..." < "tkv_z...").
    install_macros(con)
    default_ordered = q(con, "SELECT tick_version_id FROM tape_order()")
    default_ids = [r["tick_version_id"] for r in default_ordered]
    assert default_ids.index("tkv_yyy") < default_ids.index("tkv_zzz")

    # Give NASDAQ a lower (better) priority than NYSE - it must now sort
    # first, overriding the tick_version_id tie-break.
    install_macros(con, venue_priorities={"NASDAQ": 0, "NYSE": 1})
    reordered = q(con, "SELECT tick_version_id FROM tape_order()")
    reordered_ids = [r["tick_version_id"] for r in reordered]
    assert reordered_ids.index("tkv_zzz") < reordered_ids.index("tkv_yyy")
    con.close()

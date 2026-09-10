"""Deferred tests for tick_vault.union_view (task-9 brief Step 1, and the
preflight controller ruling's deferred-test mechanics).

Requires duckdb, deltalake, pyarrow, pytest - none of which are installable
in the authoring sandbox (network blocked). Placed under tests/deferred/
(tests/run_tests.py os.listdir()s tests/ non-recursively for test_*.py, so
it never picks this file up). Run with `pytest tick-vault/tests` once the
dependencies are available. UNVERIFIED until then.

Scenario: legacy AAPL has two weeks of history in a `ticks_sip`-style
per-symbol Delta table (`<legacy_root>/AAPL`, columns `date`/`price`/
`size`/`mkt`/`sub_mkt`/`seq`/`sl`, partitioned by string `day`) - W1
(Monday 2021-11-01) and W2 (Monday 2021-11-08). AAPL has since been
re-downloaded for W2 into silver (one price deliberately corrected vs.
the legacy W2 print), watermark `AAPL -> 2021-11-08` (the W2 Monday) - so
W1 should still be served from legacy (`origin='LEGACY_UNVERSIONED'`), W2
from silver (`origin='CAPTURE'`, corrected price, no duplicates).
"""
import datetime as dt
import decimal

import duckdb
import pyarrow as pa
import pytest
from deltalake import write_deltalake

from tick_vault.schemas import SCHEMAS, create_all
from tick_vault.temporal import (
    attach,
    POLICY_AS_INGESTED_LOCAL,
    POLICY_HISTORICAL_VENDOR_FINAL,
)
from tick_vault.union_view import install_union_view
from tick_vault.contract import query_ticks

_W1_DATE = dt.date(2021, 11, 1)
_W2_DATE = dt.date(2021, 11, 8)

_LEGACY_W1_PRICE = 148.0
_LEGACY_W2_PRICE = 150.0
_SILVER_W2_PRICE = 151.5  # deliberately different from legacy's own W2 print

_SILVER_AVAILABLE_AT = dt.datetime(2021, 11, 8, 20, 0, 0, tzinfo=dt.timezone.utc)


def _write_legacy_table(legacy_root: str, symbol: str) -> None:
    rows = [
        {
            "date": dt.datetime(2021, 11, 1, 14, 30, 0),
            "price": _LEGACY_W1_PRICE,
            "size": 100,
            "mkt": "Q",
            "sub_mkt": "",
            "seq": 1001,
            "sl": "@   ",
            "day": "2021-11-01",
        },
        {
            "date": dt.datetime(2021, 11, 8, 14, 30, 0),
            "price": _LEGACY_W2_PRICE,
            "size": 100,
            "mkt": "Q",
            "sub_mkt": "",
            "seq": 2001,
            "sl": "@   ",
            "day": "2021-11-08",
        },
    ]
    table = pa.Table.from_pylist(
        rows,
        schema=pa.schema(
            [
                pa.field("date", pa.timestamp("us")),
                pa.field("price", pa.float64()),
                pa.field("size", pa.int64()),
                pa.field("mkt", pa.string()),
                pa.field("sub_mkt", pa.string()),
                pa.field("seq", pa.int64()),
                pa.field("sl", pa.string()),
                pa.field("day", pa.string()),
            ]
        ),
    )
    write_deltalake(
        f"{legacy_root}/{symbol}", table, mode="append", partition_by=["day"]
    )


def _silver_tick_row(**overrides) -> dict:
    row = {
        "tick_version_id": "tkv_w2",
        "logical_tick_id": "AAPL|2021-11-08|1",
        "source_system": "eodhd",
        "source_capture_id": "cap_w2",
        "source_page_ordinal": None,
        "source_row_ordinal": 0,
        "listing_id": "lst_aapl",
        "instrument_id": "ins_aapl",
        "vendor_request_symbol": "AAPL",
        "eodhd_exchange_code": None,
        "trade_date": _W2_DATE,
        "trade_ts_ms": int(
            dt.datetime(2021, 11, 8, 14, 30, tzinfo=dt.timezone.utc).timestamp() * 1000
        ),
        "trade_ts": dt.datetime(2021, 11, 8, 14, 30, tzinfo=dt.timezone.utc),
        "venue_code_raw": "Q",
        "sub_mkt_raw": None,
        "session_seq": 1,
        "venue_id": None,
        "mic": None,
        "price_venue_type": "consolidated",
        "price": decimal.Decimal(str(_SILVER_W2_PRICE)),
        "size": 100,
        "sale_condition_raw": "@   ",
        "sale_condition_flags": None,
        "origin": "CAPTURE",
        "vendor_trade_id": None,
        "vendor_sequence_no": None,
        "observed_at_ts": _SILVER_AVAILABLE_AT,
        "committed_at_ts": _SILVER_AVAILABLE_AT,
        "available_at_ts": _SILVER_AVAILABLE_AT,
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


_IDENTITY_AVAILABLE = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)


def _aapl_identifier_row(**overrides) -> dict:
    """A `silver.identifier_assignment_version` row resolving vendor
    symbol "AAPL" -> `listing_id="lst_aapl"` (the same listing_id
    `_silver_tick_row` writes), knowable from `_IDENTITY_AVAILABLE`
    (2020-01-01, i.e. always knowable at every as_of/decision_ts this
    file uses).

    Fix-round finding (final review): without this row, `pit_listing`
    resolves "AAPL" to nothing (the lake fixture seeded no identifier
    rows at all), so `tick_vault.contract.query_ticks`'s symbol
    resolution comes back empty and the silver-origin arm of the
    union-mode symbol filter (`listing_id IN (...)`) never matches -
    the W2 silver row becomes unreachable by symbol filtering even
    though it is present in `vault_trade_tick`."""
    row = {
        "assignment_version_id": "ia_aapl",
        "issuer_id": None,
        "registrant_id": None,
        "instrument_id": "ins_aapl",
        "listing_id": "lst_aapl",
        "id_namespace": "EODHD_SYMBOL",
        "id_value": "AAPL",
        "normalized_id_value": "AAPL",
        "effective_from_ts": dt.datetime(1980, 1, 1, tzinfo=dt.timezone.utc),
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


def _seed_lake(tmp_path):
    root = str(tmp_path / "lake")
    legacy_root = str(tmp_path / "ticks_sip")
    create_all(root)
    _write_legacy_table(legacy_root, "AAPL")

    table = pa.Table.from_pylist(
        [_silver_tick_row()], schema=SCHEMAS["silver.us_trade_tick_version"]
    )
    write_deltalake(f"{root}/silver/us_trade_tick_version", table, mode="append")

    identifier_table = pa.Table.from_pylist(
        [_aapl_identifier_row()],
        schema=SCHEMAS["silver.identifier_assignment_version"],
    )
    write_deltalake(
        f"{root}/silver/identifier_assignment_version", identifier_table, mode="append"
    )
    return root, legacy_root


@pytest.fixture
def lake(tmp_path):
    root, legacy_root = _seed_lake(tmp_path)
    con = duckdb.connect()
    attach(con, root)
    yield con, root, legacy_root
    con.close()


def test_w1_served_from_legacy_flagged(lake):
    con, root, legacy_root = lake
    handle = install_union_view(con, root, legacy_root, {"AAPL": _W2_DATE})
    assert handle.skipped_symbols == []
    rows = con.execute(
        "SELECT trade_date, price, origin FROM vault_trade_tick WHERE trade_date = ?",
        [_W1_DATE],
    ).fetchall()
    assert len(rows) == 1
    _, price, origin = rows[0]
    assert origin == "LEGACY_UNVERSIONED"
    assert price == _LEGACY_W1_PRICE


def test_w2_served_from_silver_corrected(lake):
    con, root, legacy_root = lake
    install_union_view(con, root, legacy_root, {"AAPL": _W2_DATE})
    rows = con.execute(
        "SELECT trade_date, price, origin FROM vault_trade_tick WHERE trade_date = ?",
        [_W2_DATE],
    ).fetchall()
    assert len(rows) == 1  # no W2 duplicates: legacy W2 excluded, only silver W2 present
    _, price, origin = rows[0]
    assert origin == "CAPTURE"
    assert price == _SILVER_W2_PRICE


def test_watermark_rollback_serves_legacy(lake):
    con, root, legacy_root = lake
    # Moving the watermark past W2 means W2 is no longer ">= watermark"
    # for silver, so it falls back to legacy.
    install_union_view(con, root, legacy_root, {"AAPL": dt.date(2021, 11, 15)})
    rows = con.execute(
        "SELECT trade_date, price, origin FROM vault_trade_tick WHERE trade_date = ?",
        [_W2_DATE],
    ).fetchall()
    assert len(rows) == 1
    _, price, origin = rows[0]
    assert origin == "LEGACY_UNVERSIONED"
    assert price == _LEGACY_W2_PRICE


def test_union_provenance_frontier(lake):
    con, root, legacy_root = lake
    handle = install_union_view(con, root, legacy_root, {"AAPL": _W2_DATE})
    prov = handle.union_provenance("AAPL")
    assert prov["symbol"] == "AAPL"
    assert prov["frontier"] == _W2_DATE
    assert prov["serving"] == {"silver_from": _W2_DATE, "legacy_before": _W2_DATE}


def test_pit_as_ingested_excludes_legacy(lake):
    con, root, legacy_root = lake
    result = query_ticks(
        con,
        root,
        symbols=["AAPL"],
        start=_W1_DATE,
        end=_W2_DATE,
        as_of=dt.datetime(2021, 11, 20, tzinfo=dt.timezone.utc),
        availability_policy=POLICY_AS_INGESTED_LOCAL,
        legacy_root=legacy_root,
        watermarks={"AAPL": _W2_DATE},
    )
    trade_dates = set(result.df["trade_date"].tolist())
    assert _W1_DATE not in trade_dates  # honest empty: legacy has no real capture time
    assert _W2_DATE in trade_dates


def test_pit_simulated_includes_legacy(lake):
    con, root, legacy_root = lake
    result = query_ticks(
        con,
        root,
        symbols=["AAPL"],
        start=_W1_DATE,
        end=_W2_DATE,
        as_of=dt.datetime(2021, 11, 10, tzinfo=dt.timezone.utc),
        availability_policy=POLICY_HISTORICAL_VENDOR_FINAL,
        legacy_root=legacy_root,
        watermarks={"AAPL": _W2_DATE},
    )
    trade_dates = set(result.df["trade_date"].tolist())
    # simulated floor for W1 (2021-11-01 + 1 day @ 08:00 = 2021-11-02 08:00)
    # is well before as_of (2021-11-10), so legacy W1 is visible.
    assert _W1_DATE in trade_dates



def _write_symbol_legacy_table(legacy_root: str, symbol: str, rows: list) -> None:
    table = pa.Table.from_pylist(
        rows,
        schema=pa.schema(
            [
                pa.field("date", pa.timestamp("us")),
                pa.field("price", pa.float64()),
                pa.field("size", pa.int64()),
                pa.field("mkt", pa.string()),
                pa.field("sub_mkt", pa.string()),
                pa.field("seq", pa.int64()),
                pa.field("sl", pa.string()),
                pa.field("day", pa.string()),
            ]
        ),
    )
    write_deltalake(
        f"{legacy_root}/{symbol}", table, mode="append", partition_by=["day"]
    )


def test_unwatermarked_symbol_served_from_legacy(lake):
    """Fix-round finding 1: a symbol with NO entry in `watermarks` at all
    (not even an empty-dict placeholder) must still be served entirely
    from legacy, discovered straight off disk under `legacy_root` -
    `install_union_view(con, root, legacy_root, {})` must not silently
    drop it."""
    con, root, legacy_root = lake
    handle = install_union_view(con, root, legacy_root, {})
    assert handle.watermarks == {}
    assert "AAPL" in handle.discovered_symbols
    rows = con.execute(
        "SELECT trade_date, price, origin FROM vault_trade_tick"
        " WHERE vendor_request_symbol = 'AAPL' ORDER BY trade_date"
    ).fetchall()
    # both legacy weeks served, unbounded (no watermark at all)
    assert len(rows) == 2
    for _, price, origin in rows:
        assert origin == "LEGACY_UNVERSIONED"
    prices = {price for _, price, _ in rows}
    assert prices == {_LEGACY_W1_PRICE, _LEGACY_W2_PRICE}

    prov = handle.union_provenance("AAPL")
    assert prov["frontier"] is None
    assert prov["serving"] == "legacy_only"


def test_legacy_boundary_print_gets_prior_ny_session_date(tmp_path):
    """Fix-round finding 3 (controller ruling): legacy `day` is a UTC
    CALENDAR day, not the NY session date. A print at `01:30:00` naive-UTC
    living in the `day='2021-11-06'` partition is really part of the
    `2021-11-05` NY session (04:00 UTC is NY midnight during EDT, so
    01:30 UTC is still the prior evening/night)."""
    root = str(tmp_path / "lake")
    legacy_root = str(tmp_path / "ticks_sip")
    create_all(root)
    _write_symbol_legacy_table(
        legacy_root,
        "MSFT",
        [
            {
                "date": dt.datetime(2021, 11, 6, 1, 30, 0),
                "price": 300.0,
                "size": 50,
                "mkt": "Q",
                "sub_mkt": "",
                "seq": 42,
                "sl": "@   ",
                "day": "2021-11-06",
            }
        ],
    )
    con = duckdb.connect()
    attach(con, root)
    handle = install_union_view(con, root, legacy_root, {})
    assert "MSFT" in handle.discovered_symbols
    rows = con.execute(
        "SELECT trade_date FROM vault_trade_tick WHERE vendor_request_symbol = 'MSFT'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == dt.date(2021, 11, 5)
    con.close()

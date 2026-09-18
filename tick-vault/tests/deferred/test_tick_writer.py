"""Deferred tests for tick_vault.tick_writer (task-2 brief, Step 1).

Requires deltalake, pyarrow, pytest - none of which are installable in the
authoring sandbox (network blocked). Placed under tests/deferred/ so the
sandbox mini-runner (tests/run_tests.py, which os.listdir()s tests/ non-
recursively for test_*.py) does not pick these up. Run with:

    pytest tick-vault/tests

once the dependencies are available. UNVERIFIED until then.

The pure completion/decimal-conversion logic (`complete_tick_frame`,
`_to_decimal`) is already covered by the runnable
`tests/test_tick_writer_core.py`; this file exercises only the
Delta-backed path (`write_tick_versions`/`write_correction_events` ->
`tick_vault.capture.append_rows` -> `tick_vault.schemas`), against a real
tmp_path Delta lake, per the task-2 brief's Step 1 key-case tests.
"""
import copy
import datetime as dt
import decimal
import json
from pathlib import Path

import pytest
from deltalake import DeltaTable

from tick_vault.diffing import diff_window
from tick_vault.schemas import create_all
from tick_vault.tick_parser import parse_tick_payload
from tick_vault.tick_writer import write_correction_events, write_tick_versions

PAYLOAD = json.loads(
    (Path(__file__).parent.parent / "fixtures/eodhd_ticks_sample.json").read_text()
)
OBS1 = dt.datetime(2021, 11, 6, 1, 0, tzinfo=dt.timezone.utc)
OBS2 = dt.datetime(2021, 11, 6, 3, 0, tzinfo=dt.timezone.utc)
COMMITTED1 = dt.datetime(2021, 11, 6, 1, 30, tzinfo=dt.timezone.utc)
COMMITTED2 = dt.datetime(2021, 11, 6, 3, 30, tzinfo=dt.timezone.utc)


def _read_delta(root, rel_path):
    dt_ = DeltaTable(str(Path(root) / rel_path))
    return dt_.to_pandas()


def _parsed(capture_id="cap_x"):
    return parse_tick_payload(
        PAYLOAD, capture_id=capture_id, listing_id="lst_a",
        instrument_id="ins_a", observed_at=OBS1,
    )


def test_write_fills_not_null_columns_and_row_count(tmp_path):
    create_all(str(tmp_path))
    n = write_tick_versions(
        str(tmp_path),
        _parsed(),
        vendor_request_symbol="AAPL.US",
        committed_at=COMMITTED1,
    )
    assert n == 3  # fixture dedups 4 rows -> 3 (see test_tick_parser)

    df = _read_delta(tmp_path, "silver/us_trade_tick_version")
    assert len(df) == 3
    for _, row in df.iterrows():
        assert row["source_system"] == "EODHD"
        assert row["vendor_request_symbol"] == "AAPL.US"
        assert row["price_venue_type"] == "CONSOLIDATED_US"
        assert row["committed_at_ts"] == COMMITTED1
        assert row["origin"] == "CAPTURE"


def test_price_round_trips_exactly_as_decimal(tmp_path):
    create_all(str(tmp_path))
    write_tick_versions(
        str(tmp_path), _parsed(), vendor_request_symbol="AAPL.US",
        committed_at=COMMITTED1,
    )
    df = _read_delta(tmp_path, "silver/us_trade_tick_version")
    row = df[df["logical_tick_id"].str.endswith("|1002")].iloc[0]
    assert row["price"] == decimal.Decimal("150.26")


def test_partition_dir_exists(tmp_path):
    create_all(str(tmp_path))
    write_tick_versions(
        str(tmp_path), _parsed(), vendor_request_symbol="AAPL.US",
        committed_at=COMMITTED1,
    )
    assert (tmp_path / "silver" / "us_trade_tick_version" / "trade_date=2021-11-05").exists()


def test_second_write_appends_not_replaces(tmp_path):
    create_all(str(tmp_path))
    write_tick_versions(
        str(tmp_path), _parsed("cap_x"), vendor_request_symbol="AAPL.US",
        committed_at=COMMITTED1,
    )
    write_tick_versions(
        str(tmp_path), _parsed("cap_y"), vendor_request_symbol="AAPL.US",
        committed_at=COMMITTED2,
    )
    df = _read_delta(tmp_path, "silver/us_trade_tick_version")
    assert len(df) == 6
    assert set(df["source_capture_id"]) == {"cap_x", "cap_y"}


def test_custom_source_and_price_venue_type(tmp_path):
    create_all(str(tmp_path))
    write_tick_versions(
        str(tmp_path), _parsed(), vendor_request_symbol="AAPL.US",
        source_system="OTHER_VENDOR", price_venue_type="SIP",
        committed_at=COMMITTED1,
    )
    df = _read_delta(tmp_path, "silver/us_trade_tick_version")
    assert set(df["source_system"]) == {"OTHER_VENDOR"}
    assert set(df["price_venue_type"]) == {"SIP"}


def test_correction_events_land_in_tick_correction_event(tmp_path):
    create_all(str(tmp_path))
    incoming_payload = copy.deepcopy(PAYLOAD)
    for record in incoming_payload:
        if record["seq"] == 1002:
            record["price"] = 150.27
    incoming_payload = [r for r in incoming_payload if r["seq"] != 1003]
    incoming_payload.append(
        {"ts": 1636119010000, "price": 151.00, "shares": 10, "seq": 1004,
         "sl": "@   ", "mkt": "Q", "sub_mkt": "", "ex": "US"}
    )

    existing = _parsed("cap_x")
    incoming = parse_tick_payload(
        incoming_payload, capture_id="cap_y", listing_id="lst_a",
        instrument_id="ins_a", observed_at=OBS1,
    )
    result = diff_window(
        existing, incoming, revealing_capture_id="cap_y", observed_at=OBS2
    )

    n_events = write_correction_events(str(tmp_path), result.events)
    assert n_events == 3

    df = _read_delta(tmp_path, "silver/tick_correction_event")
    assert len(df) == 3
    assert set(df["correction_kind"]) == {"REVISED", "CANCELLED", "LATE_ADD"}
    assert (df["observed_at_ts"] == OBS2).all()


def test_write_tick_versions_returns_row_count(tmp_path):
    create_all(str(tmp_path))
    n = write_tick_versions(
        str(tmp_path), _parsed(), vendor_request_symbol="AAPL.US",
        committed_at=COMMITTED1,
    )
    assert n == len(_read_delta(tmp_path, "silver/us_trade_tick_version"))

"""Runnable (pyarrow/deltalake/pytest-free) tests for the pure parts of
`tick_vault.tick_writer` (task-2 brief): `complete_tick_frame`'s column-
filling and its `price` -> `decimal.Decimal` conversion.

`tick_vault.tick_writer` itself imports bare (no pyarrow/deltalake at
module scope - those are lazy-imported inside `write_tick_versions`/
`write_correction_events`, neither of which this file exercises), so it's
importable and runnable here even though pyarrow/deltalake are not
installed in this sandbox. The Delta-writing path is verified separately
in `tests/deferred/test_tick_writer.py` (pytest, real tmp_path Delta
lake).
"""
import datetime as dt
import decimal
import json
from pathlib import Path

import pandas as pd

from tick_vault.tick_parser import parse_tick_payload
from tick_vault.tick_writer import complete_tick_frame, _to_decimal

PAYLOAD = json.loads(
    (Path(__file__).parent / "fixtures/eodhd_ticks_sample.json").read_text()
)
OBS = dt.datetime(2021, 11, 6, 1, 0, tzinfo=dt.timezone.utc)
COMMITTED = dt.datetime(2021, 11, 6, 2, 0, tzinfo=dt.timezone.utc)


def _parsed():
    return parse_tick_payload(
        PAYLOAD, capture_id="cap_x", listing_id="lst_a",
        instrument_id="ins_a", observed_at=OBS,
    )


def _completed():
    return complete_tick_frame(
        _parsed(),
        source_system="EODHD",
        vendor_request_symbol="AAPL.US",
        price_venue_type="CONSOLIDATED_US",
        committed_at=COMMITTED,
    )


def test_fills_the_four_not_null_columns():
    rows = _completed().to_dict("records")
    for r in rows:
        assert r["source_system"] == "EODHD"
        assert r["vendor_request_symbol"] == "AAPL.US"
        assert r["price_venue_type"] == "CONSOLIDATED_US"
        assert r["committed_at_ts"] == COMMITTED


def test_parser_left_committed_at_ts_none_before_completion():
    # sanity check the premise: parse_tick_payload really does leave it None
    rows = _parsed().to_dict("records")
    assert all(r["committed_at_ts"] is None for r in rows)


def test_price_becomes_decimal_and_round_trips_exactly():
    rows = _completed().to_dict("records")
    r1 = [r for r in rows if r["logical_tick_id"].endswith("|1002")][0]
    assert isinstance(r1["price"], decimal.Decimal)
    # 150.26 must round-trip exactly, not as a binary-float artifact like
    # Decimal('150.25999999999999090505...')
    assert r1["price"] == decimal.Decimal("150.26")
    assert str(r1["price"]) == "150.26"


def test_to_decimal_handles_none_and_nan():
    assert _to_decimal(None) is None
    assert _to_decimal(float("nan")) is None


def test_to_decimal_passthrough_existing_decimal():
    d = decimal.Decimal("1.5")
    assert _to_decimal(d) is d


def test_does_not_mutate_input_frame():
    original = _parsed()
    before = original.copy(deep=True)
    complete_tick_frame(
        original,
        source_system="EODHD",
        vendor_request_symbol="AAPL.US",
        price_venue_type="CONSOLIDATED_US",
        committed_at=COMMITTED,
    )
    assert "source_system" not in original.columns
    assert original["committed_at_ts"].isna().all()
    pd.testing.assert_frame_equal(original, before)


def test_row_count_unchanged():
    parsed = _parsed()
    completed = _completed()
    assert len(completed) == len(parsed)


def test_other_columns_untouched():
    parsed = _parsed()
    parsed_rows = parsed.to_dict("records")
    completed_rows = complete_tick_frame(
        parsed,
        source_system="EODHD",
        vendor_request_symbol="AAPL.US",
        price_venue_type="CONSOLIDATED_US",
        committed_at=COMMITTED,
    ).to_dict("records")
    for p, c in zip(parsed_rows, completed_rows):
        assert p["logical_tick_id"] == c["logical_tick_id"]
        assert p["tick_version_id"] == c["tick_version_id"]
        assert p["sale_condition_flags"] == c["sale_condition_flags"]

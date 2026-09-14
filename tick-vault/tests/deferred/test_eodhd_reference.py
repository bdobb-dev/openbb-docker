"""Deferred tests for tick_vault.eodhd_reference.ReferenceClient (task-3
brief, Step 1).

Requires deltalake, pyarrow, pytest - none of which are installable in the
authoring sandbox (network blocked). Placed under tests/deferred/ so the
sandbox mini-runner (tests/run_tests.py, which os.listdir()s tests/ non-
recursively for test_*.py) does not pick these up. Run with:

    pytest tick-vault/tests

once the dependencies are available. UNVERIFIED until then.

The pure parsers (`parse_symbols`, `parse_delisted`, `parse_changes`,
`parse_components`, `parse_fundamentals`) are already covered by the
runnable `tests/test_eodhd_reference_parsers.py`; this file exercises only
the `ReferenceClient` capture-wiring path: FakeTransport responses ->
`CaptureStore.fetch_and_capture` -> a real tmp_path Delta lake, per the
task-3 brief's Step 1 ("each getter captures to the right bronze table,
parses expected columns/values ... a fundamentals CUSIP surfaced") plus
the API-key-redaction requirement from the interfaces note.
"""
import datetime as dt
import json
from pathlib import Path

import pytest
from deltalake import DeltaTable

from tick_vault.capture import CaptureStore, FakeTransport
from tick_vault.eodhd_reference import ReferenceClient
from tick_vault.schemas import create_all

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "eodhd_ref"
OBS = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.timezone.utc)


def _load_bytes(name):
    return (_FIXTURE_DIR / name).read_bytes()


def _read_delta(root, rel_path):
    return DeltaTable(str(Path(root) / rel_path)).to_pandas()


def _client(tmp_path, responses):
    create_all(str(tmp_path))
    transport = FakeTransport(responses)
    store = CaptureStore(str(tmp_path))
    client = ReferenceClient(transport, store, api_key="SECRET_KEY_123", ingestion_run_id="run_x")
    return client, transport, store


def test_get_exchange_symbols_captures_to_right_table_and_parses(tmp_path):
    client, transport, _ = _client(
        tmp_path, [(200, _load_bytes("exchange_symbols.json"))]
    )
    df, record = client.get_exchange_symbols("US", observed_at=OBS)

    assert list(df.columns) == ["code", "name", "exchange", "type", "isin"]
    assert set(df["code"]) == {"AAPL", "MSFT"}

    bronze = _read_delta(tmp_path, "bronze/eodhd_exchange_symbols_capture")
    assert len(bronze) == 1
    row = bronze.iloc[0]
    assert row["capture_id"] == record.capture_id
    assert row["exchange_code"] == "US"
    assert row["endpoint"] == "exchange_symbol_list"
    assert row["http_status"] == 200


def test_get_delisted_captures_to_delisted_table(tmp_path):
    client, transport, _ = _client(
        tmp_path, [(200, _load_bytes("delisted_symbols.json"))]
    )
    df, record = client.get_delisted("US", observed_at=OBS)

    assert len(df) == 1
    assert df.iloc[0]["code"] == "TWTR"

    bronze = _read_delta(tmp_path, "bronze/eodhd_delisted_symbols_capture")
    assert len(bronze) == 1
    assert bronze.iloc[0]["exchange_code"] == "US"


def test_get_symbol_changes_captures_and_parses_date(tmp_path):
    client, transport, _ = _client(
        tmp_path, [(200, _load_bytes("symbol_changes.json"))]
    )
    df, record = client.get_symbol_changes(observed_at=OBS)

    row = df.iloc[0]
    assert row["old"] == "BK"
    assert row["new"] == "BNY"
    assert row["date"] == dt.date(2026, 7, 1)

    bronze = _read_delta(tmp_path, "bronze/eodhd_symbol_change_capture")
    assert len(bronze) == 1
    request_params = json.loads(bronze.iloc[0]["request_parameters_json"])
    assert "exchange" not in request_params


def test_get_index_components_merges_current_and_historical(tmp_path):
    client, transport, _ = _client(
        tmp_path, [(200, _load_bytes("components_gspc.json"))]
    )
    df, record = client.get_index_components("GSPC.INDX", observed_at=OBS)

    assert set(df["code"]) == {"AAPL", "TWTR"}
    twtr = df[df["code"] == "TWTR"].iloc[0]
    assert twtr["end_date"] == dt.date(2022, 10, 27)
    assert bool(twtr["is_active"]) is False

    bronze = _read_delta(tmp_path, "bronze/eodhd_index_components_capture")
    assert len(bronze) == 1
    assert bronze.iloc[0]["index_vendor_symbol"] == "GSPC.INDX"


def test_get_fundamentals_surfaces_cusip_and_captures(tmp_path):
    client, transport, _ = _client(
        tmp_path, [(200, _load_bytes("fundamentals.json"))]
    )
    df, record = client.get_fundamentals("AAPL.US", observed_at=OBS)

    row = df.iloc[0]
    assert row["code"] == "AAPL"
    assert row["cusip"] == "037833100"
    assert row["isin"] == "US0378331005"
    assert row["cik"] == "0000320193"

    bronze = _read_delta(tmp_path, "bronze/eodhd_fundamentals_capture")
    assert len(bronze) == 1
    assert bronze.iloc[0]["request_symbol"] == "AAPL.US"


def test_api_key_never_appears_in_captured_request_parameters_json(tmp_path):
    client, transport, _ = _client(
        tmp_path, [(200, _load_bytes("exchange_symbols.json"))]
    )
    client.get_exchange_symbols("US", observed_at=OBS)

    # The live request URL does carry the real key ...
    assert any("SECRET_KEY_123" in u for u in transport.requested_urls)

    # ... but the captured bronze row's request_parameters_json must not.
    bronze = _read_delta(tmp_path, "bronze/eodhd_exchange_symbols_capture")
    stored_params = bronze.iloc[0]["request_parameters_json"]
    assert "SECRET_KEY_123" not in stored_params
    parsed = json.loads(stored_params)
    assert parsed["token"] == "REDACTED"


def test_failed_fetch_still_captures_with_no_payload_uri_and_empty_frame(tmp_path):
    client, transport, _ = _client(tmp_path, [(500, b"server error")])
    df, record = client.get_exchange_symbols("US", observed_at=OBS)

    assert len(df) == 0
    assert list(df.columns) == ["code", "name", "exchange", "type", "isin"]

    bronze = _read_delta(tmp_path, "bronze/eodhd_exchange_symbols_capture")
    row = bronze.iloc[0]
    assert row["http_status"] == 500
    assert row["raw_payload_uri"] is None


def test_get_eod_captures_to_eod_table_and_parses(tmp_path):
    client, transport, _ = _client(tmp_path, [(200, _load_bytes("eod_slice.json"))])
    df, record = client.get_eod("AAPL.US", "2026-07-01", "2026-07-02", observed_at=OBS)

    assert list(df.columns) == ["date", "open", "high", "low", "close", "adjusted_close", "volume"]
    assert len(df) == 2
    assert df.iloc[0]["date"] == dt.date(2026, 7, 1)

    bronze = _read_delta(tmp_path, "bronze/eodhd_eod_capture")
    assert len(bronze) == 1
    row = bronze.iloc[0]
    assert row["capture_id"] == record.capture_id
    assert row["request_symbol"] == "AAPL.US"
    assert row["endpoint"] == "eod"
    assert row["http_status"] == 200
    assert row["request_from_sec"] == int(
        dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc).timestamp()
    )


def test_get_intraday_captures_to_intraday_table_and_parses(tmp_path):
    client, transport, _ = _client(tmp_path, [(200, _load_bytes("intraday_slice.json"))])
    df, record = client.get_intraday(
        "AAPL.US", "1m", 1751370600, 1751370660, observed_at=OBS
    )

    assert list(df.columns) == ["ts_ms", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df.iloc[0]["ts_ms"] == 1751370600 * 1000

    bronze = _read_delta(tmp_path, "bronze/eodhd_intraday_capture")
    assert len(bronze) == 1
    row = bronze.iloc[0]
    assert row["capture_id"] == record.capture_id
    assert row["request_symbol"] == "AAPL.US"
    assert row["interval"] == "1m"
    assert row["request_from_sec"] == 1751370600
    assert row["request_to_sec"] == 1751370660
    assert row["endpoint"] == "intraday"


def test_ingestion_run_id_shared_across_calls_on_same_client(tmp_path):
    client, transport, _ = _client(
        tmp_path,
        [
            (200, _load_bytes("exchange_symbols.json")),
            (200, _load_bytes("delisted_symbols.json")),
        ],
    )
    client.get_exchange_symbols("US", observed_at=OBS)
    client.get_delisted("US", observed_at=OBS)

    symbols = _read_delta(tmp_path, "bronze/eodhd_exchange_symbols_capture")
    delisted = _read_delta(tmp_path, "bronze/eodhd_delisted_symbols_capture")
    assert symbols.iloc[0]["ingestion_run_id"] == "run_x"
    assert delisted.iloc[0]["ingestion_run_id"] == "run_x"

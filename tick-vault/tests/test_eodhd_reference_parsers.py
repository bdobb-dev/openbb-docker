"""Runnable (pyarrow/deltalake/pytest-free) tests for the pure parsers in
`tick_vault.eodhd_reference` (task-3 brief): `parse_symbols`,
`parse_delisted`, `parse_changes`, `parse_components`,
`parse_fundamentals`.

`tick_vault.eodhd_reference` imports bare (no pyarrow/deltalake at module
scope - `ReferenceClient`'s Delta-backed capture wiring only ever touches
`tick_vault.capture`, itself bare-importable), so it's importable and
runnable here even though pyarrow/deltalake are not installed in this
sandbox. The `ReferenceClient` capture-wiring path (real Delta lake,
`FakeTransport`, token redaction) is verified separately in
`tests/deferred/test_eodhd_reference.py`.

Fixtures live under `tests/fixtures/eodhd_ref/*.json` and are loaded here
with the stdlib `json` module (no pytest fixtures).
"""
import datetime as dt
import json
from pathlib import Path

from tick_vault.eodhd_reference import (
    parse_changes,
    parse_components,
    parse_delisted,
    parse_eod,
    parse_fundamentals,
    parse_intraday,
    parse_symbols,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "eodhd_ref"


def _load(name):
    return json.loads((_FIXTURE_DIR / name).read_text())


# ---------------------------------------------------------------------------
# parse_symbols / parse_delisted
# ---------------------------------------------------------------------------

def test_parse_symbols_columns_and_values():
    df = parse_symbols(_load("exchange_symbols.json"))
    assert list(df.columns) == ["code", "name", "exchange", "type", "isin"]
    assert len(df) == 2
    row = df[df["code"] == "AAPL"].iloc[0]
    assert row["name"] == "Apple Inc"
    assert row["exchange"] == "US"
    assert row["type"] == "Common Stock"
    assert row["isin"] == "US0378331005"


def test_parse_symbols_is_case_insensitive_to_vendor_keys():
    payload = [{"code": "abc", "NAME": "ABC Co", "EXCHANGE": "US", "type": "X", "ISIN": None}]
    df = parse_symbols(payload)
    row = df.iloc[0]
    assert row["code"] == "abc"
    assert row["name"] == "ABC Co"
    assert row["exchange"] == "US"


def test_parse_symbols_empty_payload():
    df = parse_symbols([])
    assert list(df.columns) == ["code", "name", "exchange", "type", "isin"]
    assert len(df) == 0


def test_parse_delisted_shares_symbols_shape():
    df = parse_delisted(_load("delisted_symbols.json"))
    assert list(df.columns) == ["code", "name", "exchange", "type", "isin"]
    assert len(df) == 1
    assert df.iloc[0]["code"] == "TWTR"


# ---------------------------------------------------------------------------
# parse_changes
# ---------------------------------------------------------------------------

def test_parse_changes_columns_and_values():
    df = parse_changes(_load("symbol_changes.json"))
    assert list(df.columns) == ["old", "new", "date"]
    assert len(df) == 1
    row = df.iloc[0]
    assert row["old"] == "BK"
    assert row["new"] == "BNY"
    assert row["date"] == dt.date(2026, 7, 1)
    assert isinstance(row["date"], dt.date)


def test_parse_changes_alternate_key_names():
    payload = [{"OldSymbol": "FOO", "NewSymbol": "BAR", "ChangeDate": "2020-01-15"}]
    df = parse_changes(payload)
    row = df.iloc[0]
    assert row["old"] == "FOO"
    assert row["new"] == "BAR"
    assert row["date"] == dt.date(2020, 1, 15)


def test_parse_changes_empty_payload():
    df = parse_changes([])
    assert list(df.columns) == ["old", "new", "date"]
    assert len(df) == 0


# ---------------------------------------------------------------------------
# parse_components
# ---------------------------------------------------------------------------

def test_parse_components_columns():
    df = parse_components(_load("components_gspc.json"))
    assert list(df.columns) == ["code", "name", "start_date", "end_date", "is_active", "sector"]
    assert len(df) == 2


def test_parse_components_historical_wins_over_current_for_same_code():
    df = parse_components(_load("components_gspc.json"))
    aapl = df[df["code"] == "AAPL"].iloc[0]
    # AAPL appears in both Components (dateless) and HistoricalTickerComponents
    # (dated, open-ended) -> the dated historical row wins.
    assert aapl["start_date"] == dt.date(2015, 1, 1)
    assert aapl["end_date"] is None
    assert bool(aapl["is_active"]) is True
    assert aapl["sector"] == "Technology"


def test_parse_components_delisted_member_is_not_active():
    df = parse_components(_load("components_gspc.json"))
    twtr = df[df["code"] == "TWTR"].iloc[0]
    assert twtr["start_date"] == dt.date(2018, 6, 1)
    assert twtr["end_date"] == dt.date(2022, 10, 27)
    assert bool(twtr["is_active"]) is False
    assert twtr["sector"] == "Communication Services"


def test_parse_components_current_only_member_has_no_dates_but_is_active():
    payload = {
        "Components": {"0": {"Code": "XYZ", "Name": "XYZ Corp", "Sector": "Energy"}},
        "HistoricalTickerComponents": {},
    }
    df = parse_components(payload)
    row = df.iloc[0]
    assert row["code"] == "XYZ"
    assert row["start_date"] is None
    assert row["end_date"] is None
    assert bool(row["is_active"]) is True


def test_parse_components_missing_sections_yield_empty_frame():
    df = parse_components({})
    assert list(df.columns) == ["code", "name", "start_date", "end_date", "is_active", "sector"]
    assert len(df) == 0


# ---------------------------------------------------------------------------
# parse_fundamentals
# ---------------------------------------------------------------------------

def test_parse_fundamentals_columns_and_identifiers():
    df = parse_fundamentals(_load("fundamentals.json"))
    assert list(df.columns) == ["code", "cusip", "isin", "cik", "name", "type", "share_class"]
    assert len(df) == 1
    row = df.iloc[0]
    assert row["code"] == "AAPL"
    assert row["cusip"] == "037833100"
    assert row["isin"] == "US0378331005"
    assert row["cik"] == "0000320193"
    assert row["name"] == "Apple Inc"
    assert row["type"] == "Common Stock"


def test_parse_fundamentals_never_invents_missing_identifiers():
    # No CUSIP/ISIN/CIK/ShareClass in the payload at all -> all None, not
    # fabricated.
    payload = {"General": {"Code": "ZZZ", "Name": "ZZZ Inc", "Type": "Common Stock"}}
    df = parse_fundamentals(payload)
    row = df.iloc[0]
    assert row["cusip"] is None
    assert row["isin"] is None
    assert row["cik"] is None
    assert row["share_class"] is None


def test_parse_fundamentals_share_class_falls_back_to_shares_stats():
    payload = {
        "General": {"Code": "ZZZ"},
        "SharesStats": {"ShareClass": "A"},
    }
    df = parse_fundamentals(payload)
    assert df.iloc[0]["share_class"] == "A"


def test_parse_fundamentals_empty_payload():
    df = parse_fundamentals({})
    assert list(df.columns) == ["code", "cusip", "isin", "cik", "name", "type", "share_class"]
    row = df.iloc[0]
    assert row["code"] is None
    assert row["cusip"] is None


# ---------------------------------------------------------------------------
# parse_eod
# ---------------------------------------------------------------------------

def test_parse_eod_columns_and_values():
    df = parse_eod(_load("eod_slice.json"))
    assert list(df.columns) == ["date", "open", "high", "low", "close", "adjusted_close", "volume"]
    assert len(df) == 2
    row = df.iloc[0]
    assert row["date"] == dt.date(2026, 7, 1)
    assert row["open"] == 150.00
    assert row["high"] == 151.20
    assert row["low"] == 149.80
    assert row["close"] == 150.90
    assert row["adjusted_close"] == 150.90
    assert row["volume"] == 1000000


def test_parse_eod_is_case_insensitive_to_vendor_keys():
    payload = [{"date": "2026-01-02", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                "ADJUSTED_CLOSE": 1.5, "VOLUME": 42}]
    df = parse_eod(payload)
    row = df.iloc[0]
    assert row["date"] == dt.date(2026, 1, 2)
    assert row["adjusted_close"] == 1.5
    assert row["volume"] == 42


def test_parse_eod_empty_payload():
    df = parse_eod([])
    assert list(df.columns) == ["date", "open", "high", "low", "close", "adjusted_close", "volume"]
    assert len(df) == 0


# ---------------------------------------------------------------------------
# parse_intraday
# ---------------------------------------------------------------------------

def test_parse_intraday_columns_and_values():
    df = parse_intraday(_load("intraday_slice.json"))
    assert list(df.columns) == ["ts_ms", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    row = df.iloc[0]
    assert row["ts_ms"] == 1751370600 * 1000
    assert row["open"] == 150.00
    assert row["close"] == 150.05
    assert row["volume"] == 10000
    assert df.iloc[1]["ts_ms"] == 1751370660 * 1000


def test_parse_intraday_is_case_insensitive_to_vendor_keys():
    payload = [{"timestamp": 1700000000, "open": 1.0, "HIGH": 2.0, "Low": 0.5, "close": 1.5, "volume": 9}]
    df = parse_intraday(payload)
    row = df.iloc[0]
    assert row["ts_ms"] == 1700000000 * 1000
    assert row["high"] == 2.0
    assert row["low"] == 0.5


def test_parse_intraday_empty_payload():
    df = parse_intraday([])
    assert list(df.columns) == ["ts_ms", "open", "high", "low", "close", "volume"]
    assert len(df) == 0

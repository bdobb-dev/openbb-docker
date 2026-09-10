import json, datetime as dt
from pathlib import Path
from tick_vault.tick_parser import parse_tick_payload, session_date, PARSER_VERSION

PAYLOAD = json.loads((Path(__file__).parent / "fixtures/eodhd_ticks_sample.json").read_text())
OBS = dt.datetime(2021, 11, 6, 1, 0, tzinfo=dt.timezone.utc)


def _parse():
    return parse_tick_payload(PAYLOAD, capture_id="cap_x", listing_id="lst_a",
                               instrument_id="ins_a", observed_at=OBS)


def test_dedup_on_ts_seq():
    assert len(_parse()) == 3  # 4 rows, one boundary duplicate


def test_schema_and_provenance():
    t = _parse().to_dict("records")
    r = t[0]
    assert r["source_capture_id"] == "cap_x" and r["origin"] == "CAPTURE"
    assert r["revision_number"] == 1 and r["available_at_ts"] == OBS
    assert r["parser_version"] == PARSER_VERSION
    assert r["logical_tick_id"] == "lst_a|2021-11-05|1001"


def test_session_date_new_york():
    # 1636119000123 = 2021-11-05 13:30:00.123 UTC = 09:30 ET -> session 2021-11-05
    assert session_date(1636119000123) == dt.date(2021, 11, 5)


def test_sale_condition_flags_carried():
    rows = _parse().to_dict("records")
    assert "SOLD_OUT_OF_SEQUENCE" in rows[2]["sale_condition_flags"]


def test_same_ms_different_seq_not_collapsed():
    rows = _parse().to_dict("records")
    assert rows[0]["trade_ts_ms"] == rows[1]["trade_ts_ms"]  # both kept (§19.3)


def test_trade_ts_matches_trade_ts_ms_utc():
    rows = _parse().to_dict("records")
    for r in rows:
        expected = dt.datetime.fromtimestamp(r["trade_ts_ms"] / 1000, tz=dt.timezone.utc)
        assert r["trade_ts"] == expected

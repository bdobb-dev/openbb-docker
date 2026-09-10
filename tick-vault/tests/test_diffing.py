import copy
import json
import datetime as dt
from pathlib import Path

from tick_vault.diffing import diff_window
from tick_vault.tick_parser import parse_tick_payload

PAYLOAD = json.loads((Path(__file__).parent / "fixtures/eodhd_ticks_sample.json").read_text())
OBS1 = dt.datetime(2021, 11, 6, 1, 0, tzinfo=dt.timezone.utc)
OBS2 = dt.datetime(2021, 11, 6, 3, 0, tzinfo=dt.timezone.utc)

INCOMING_PAYLOAD = copy.deepcopy(PAYLOAD)
# seq 1002 price revised (150.26 -> 150.27)
for record in INCOMING_PAYLOAD:
    if record["seq"] == 1002:
        record["price"] = 150.27
# seq 1003 dropped (revealed as cancelled)
INCOMING_PAYLOAD = [r for r in INCOMING_PAYLOAD if r["seq"] != 1003]
# seq 1004 appended (late add), same trade_date as the rest of the fixture
INCOMING_PAYLOAD.append(
    {"ts": 1636119010000, "price": 151.00, "shares": 10, "seq": 1004, "sl": "@   ", "mkt": "Q", "sub_mkt": "", "ex": "US"}
)


# Built once so tick_version_ids are stable across a test's assertions
# (parse_tick_payload mints fresh ids on every call).
EXISTING = parse_tick_payload(PAYLOAD, capture_id="cap_x", listing_id="lst_a",
                               instrument_id="ins_a", observed_at=OBS1)
INCOMING = parse_tick_payload(INCOMING_PAYLOAD, capture_id="cap_y", listing_id="lst_a",
                               instrument_id="ins_a", observed_at=OBS1)


def _existing():
    return EXISTING


def _incoming():
    return INCOMING


def _diff():
    return diff_window(EXISTING, INCOMING, revealing_capture_id="cap_y", observed_at=OBS2)


def _existing_1002_version_id():
    rows = EXISTING.to_dict("records")
    return [r for r in rows if r["logical_tick_id"].endswith("|1002")][0]["tick_version_id"]


def test_revised_gets_capture_knowledge_time():
    res = _diff()
    rev = [r for r in res.new_versions.to_dict("records") if r["is_correction"]]
    assert len(rev) == 1
    rev = rev[0]
    assert rev["available_at_ts"] == OBS2  # revealing capture time, not the original observed_at
    assert rev["revision_number"] == 2
    assert rev["supersedes_tick_version_id"] == _existing_1002_version_id()


def test_cancel_is_tombstone_not_delete():
    res = _diff()
    tomb = [r for r in res.new_versions.to_dict("records") if r["is_cancelled"]]
    assert len(tomb) == 1
    tomb = tomb[0]
    assert tomb["logical_tick_id"].endswith("|1003")
    assert tomb["price"] is not None
    assert tomb["price"] == 150.10
    assert tomb["revision_number"] == 2


def test_unchanged_row_emits_nothing():
    ids = [r["logical_tick_id"] for r in _diff().new_versions.to_dict("records")]
    assert not any(i.endswith("|1001") for i in ids)


def test_event_kinds():
    kinds = sorted(r["correction_kind"] for r in _diff().events.to_dict("records"))
    assert kinds == ["CANCELLED", "LATE_ADD", "REVISED"]


def test_late_add_revision_1_flagged():
    res = _diff()
    late = [r for r in res.new_versions.to_dict("records") if r["is_late_add"]]
    assert len(late) == 1
    late = late[0]
    assert late["logical_tick_id"].endswith("|1004")
    assert late["revision_number"] == 1
    assert late["supersedes_tick_version_id"] is None
    assert late["available_at_ts"] == OBS2


def test_int_float_price_equality_not_a_change():
    existing_rows = _existing().to_dict("records")
    incoming_rows = _incoming().to_dict("records")
    # coerce types on seq 1001 to prove int vs float doesn't trigger a spurious REVISED
    for r in existing_rows:
        if r["logical_tick_id"].endswith("|1001"):
            r["price"] = 150.26
            r["size"] = 200
    for r in incoming_rows:
        if r["logical_tick_id"].endswith("|1001"):
            r["price"] = 150.26
            r["size"] = 200.0

    import pandas as pd
    from tick_vault.tick_parser import COLUMNS

    result = diff_window(
        pd.DataFrame(existing_rows, columns=COLUMNS),
        pd.DataFrame(incoming_rows, columns=COLUMNS),
        revealing_capture_id="cap_y",
        observed_at=OBS2,
    )
    ids = [r["logical_tick_id"] for r in result.new_versions.to_dict("records")]
    assert not any(i.endswith("|1001") for i in ids)


def test_events_carry_revealing_capture():
    res = _diff()
    for row in res.events.to_dict("records"):
        assert row["revealing_capture_id"] == "cap_y"
        assert row["observed_at_ts"] == OBS2


def test_source_capture_id_uniform_across_kinds():
    # incoming parsed from a different capture id than the revealing one;
    # every emitted new_versions row must be provenance-stamped to the
    # revealing capture, regardless of kind (REVISED, CANCELLED, LATE_ADD).
    incoming_other = parse_tick_payload(
        INCOMING_PAYLOAD, capture_id="cap_other", listing_id="lst_a",
        instrument_id="ins_a", observed_at=OBS1,
    )
    res = diff_window(EXISTING, incoming_other, revealing_capture_id="cap_y", observed_at=OBS2)
    rows = res.new_versions.to_dict("records")
    kinds_present = {
        "REVISED": [r for r in rows if r["is_correction"]],
        "CANCELLED": [r for r in rows if r["is_cancelled"]],
        "LATE_ADD": [r for r in rows if r["is_late_add"]],
    }
    for kind, kind_rows in kinds_present.items():
        assert len(kind_rows) == 1, kind
        assert kind_rows[0]["source_capture_id"] == "cap_y", kind

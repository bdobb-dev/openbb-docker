"""Validation and temporal resolution for security-master fixture contracts."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA = "security-master.temporal-fixture.v1"
_TOP_LEVEL_FIELDS = (
    "schema",
    "fixture_id",
    "capture_time_semantics",
    "exchange_mic",
    "exchange_name",
    "holiday_family",
    "events",
    "expected_states",
)
_EVENT_FIELDS = (
    "event_id",
    "published_on",
    "known_from",
    "source_kind",
    "authority",
    "source_url",
    "assertion",
)
_STATE_FIELDS = ("stage", "known_from", "evidence_event_ids")


def _instant(value: str | datetime) -> datetime:
    """Return an aware instant, rejecting timestamps without a UTC offset."""
    instant = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return instant


def _require_fields(record: dict[str, Any], fields: tuple[str, ...], label: str) -> None:
    for field in fields:
        if field not in record:
            raise ValueError(f"{label} missing required field: {field}")


def _validate_source_url(value: Any) -> None:
    parsed = urlsplit(str(value))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("event source_url must be a public HTTP(S) URL")


def load_temporal_fixture(path: str | Path) -> dict[str, Any]:
    """Load and validate a version-one temporal calendar fixture."""
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(fixture, dict):
        raise ValueError("fixture must be a JSON object")
    _require_fields(fixture, _TOP_LEVEL_FIELDS, "fixture")
    if fixture["schema"] != SCHEMA:
        raise ValueError(f"fixture schema must be {SCHEMA}")
    if fixture["capture_time_semantics"] != "synthetic_test_capture":
        raise ValueError("fixture capture_time_semantics must be synthetic_test_capture")

    events = fixture["events"]
    states = fixture["expected_states"]
    if not isinstance(events, list) or not events:
        raise ValueError("fixture events must be non-empty")
    if not isinstance(states, list) or not states:
        raise ValueError("fixture expected_states must be non-empty")

    event_ids: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        _require_fields(event, _EVENT_FIELDS, "event")
        date.fromisoformat(event["published_on"])
        _instant(event["known_from"])
        _validate_source_url(event["source_url"])
        event_ids.add(event["event_id"])

    previous: datetime | None = None
    for state in states:
        if not isinstance(state, dict):
            raise ValueError("state must be an object")
        _require_fields(state, _STATE_FIELDS, "state")
        known_from = _instant(state["known_from"])
        if previous is not None and known_from <= previous:
            raise ValueError("state known_from timestamps must be strictly increasing")
        previous = known_from
        for event_id in state["evidence_event_ids"]:
            if event_id not in event_ids:
                raise ValueError(f"state evidence references unknown event: {event_id}")

    return fixture


def state_at(
    fixture: dict[str, Any], known_at: str | datetime
) -> dict[str, Any] | None:
    """Return the latest expected state known at an inclusive cutoff instant."""
    cutoff = _instant(known_at)
    winner: dict[str, Any] | None = None
    for candidate in fixture["expected_states"]:
        if _instant(candidate["known_from"]) <= cutoff:
            winner = candidate
        else:
            break
    return winner

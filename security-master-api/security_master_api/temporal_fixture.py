# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Contract helpers for deterministic temporal calendar fixtures.

The fixtures distinguish a source's historical publication date from the
synthetic capture instant used by tests. Production ingestion will replace the
synthetic instant with the immutable bronze object's observed-at timestamp.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Temporal instant must include a UTC offset: {value}")
    return parsed


def load_temporal_fixture(path: str | Path) -> dict[str, Any]:
    """Load and validate a temporal scenario fixture."""

    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "fixture_id",
        "exchange_mic",
        "capture_time_semantics",
        "events",
        "expected_states",
    }
    missing = required.difference(fixture)
    if missing:
        raise ValueError(f"Fixture is missing required fields: {sorted(missing)}")
    if fixture["capture_time_semantics"] != "synthetic_test_capture":
        raise ValueError("Historical fixtures must identify synthetic test capture times")
    if not fixture["events"] or not fixture["expected_states"]:
        raise ValueError("Fixture must contain events and expected states")

    event_ids: set[str] = set()
    for event in fixture["events"]:
        event_ids.add(event["event_id"])
        _instant(event["known_from"])
        if not event["published_on"]:
            raise ValueError(f"Event {event['event_id']} is missing published_on")
        if not event["source_url"].startswith(("http://", "https://")):
            raise ValueError(f"Event {event['event_id']} has no public source URL")

    previous: datetime | None = None
    for state in fixture["expected_states"]:
        known_from = _instant(state["known_from"])
        if previous is not None and known_from <= previous:
            raise ValueError("Expected states must have strictly increasing known_from values")
        previous = known_from
        unknown_dependencies = set(state["evidence_event_ids"]).difference(event_ids)
        if unknown_dependencies:
            raise ValueError(
                f"State {state['stage']} references unknown events: {sorted(unknown_dependencies)}"
            )

    return fixture


def state_at(fixture: dict[str, Any], known_at: str | datetime) -> dict[str, Any] | None:
    """Return the latest state known at an inclusive bitemporal cutoff."""

    cutoff = _instant(known_at) if isinstance(known_at, str) else known_at
    if cutoff.tzinfo is None:
        raise ValueError("known_at must include a UTC offset")

    winner: dict[str, Any] | None = None
    for candidate in fixture["expected_states"]:
        if _instant(candidate["known_from"]) <= cutoff:
            winner = candidate
        else:
            break
    return winner


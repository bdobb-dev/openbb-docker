# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Tests for openbb_eodhd.economic_taxonomy."""

from openbb_eodhd.economic_taxonomy import EVENT_INDEX, TAXONOMY, classify


def test_event_index_is_unique():
    """Every event name in TAXONOMY appears exactly once across all releases."""
    seen = []
    for releases in TAXONOMY.values():
        for events in releases.values():
            seen.extend(events)
    assert len(seen) == len(set(seen))
    assert len(EVENT_INDEX) == len(seen)


def test_classify_known_event():
    assert classify("Core PPI") == ("BLS", "Producer Prices")


def test_classify_known_event_federal_reserve():
    assert classify("Philly Fed Prices Paid") == ("Federal Reserve", "Philly Fed")


def test_classify_is_case_insensitive():
    assert classify("core ppi") == ("BLS", "Producer Prices")


def test_classify_unknown_event():
    assert classify("Made Up") == (None, None)


def test_classify_none():
    assert classify(None) == (None, None)

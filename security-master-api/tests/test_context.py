# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime

import pytest

from security_master_api.errors import DomainError
from security_master_api.resolver.context import MODES, parse_context


def test_modes_are_the_spec_five():
    assert MODES == ("current_corrected", "known_at", "effective_on", "delta_snapshot", "captured_by")


def test_default_context_is_current_corrected_utc():
    ctx = parse_context(None)
    assert (ctx.mode, ctx.availability_policy, ctx.timezone) == ("current_corrected", "local_as_ingested", "UTC")
    assert ctx.effective_at is None and ctx.known_at is None


def test_known_at_requires_known_at_and_parses_z():
    ctx = parse_context({"mode": "known_at", "known_at": "2026-09-14T20:00:00Z",
                         "effective_at": "2026-09-14T00:00:00Z"})
    assert ctx.known_at == datetime(2026, 9, 14, 20, tzinfo=UTC)
    with pytest.raises(DomainError) as exc:
        parse_context({"mode": "known_at"})
    assert exc.value.code == "QUERY_REJECTED"


def test_effective_on_requires_effective_at():
    with pytest.raises(DomainError):
        parse_context({"mode": "effective_on"})


def test_unknown_mode_and_policy_are_unsupported():
    for raw in ({"mode": "latest"}, {"mode": "known_at", "known_at": "2026-01-01T00:00:00Z",
                                     "availability_policy": "archival"}):
        with pytest.raises(DomainError) as exc:
            parse_context(raw)
        assert exc.value.code == "TEMPORAL_MODE_UNSUPPORTED"


def test_naive_timestamp_is_rejected():
    with pytest.raises(DomainError):
        parse_context({"mode": "known_at", "known_at": "2026-01-01T00:00:00"})


def test_delta_snapshot_carries_versions():
    ctx = parse_context({"mode": "delta_snapshot", "versions": {"silver.listings": 3}})
    assert ctx.versions == {"silver.listings": 3}

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime

import pytest

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.store.schemas import INTERVAL_FIELDS, RELATIONS
from security_master_api.store.tables import (
    append, dataset, ensure, history, latest_version, open_table, relation_path,
)

_ISSUER_ROW = {
    "issuer_id": "iss_1", "name": "Example", "effective_from": "2020-01-01T00:00:00Z",
    "effective_to": None, "observed_at": "2020-01-02T00:00:00Z",
    "available_at": "2020-01-02T00:00:00Z", "system_from": "2020-01-02T00:00:00Z",
    "system_to": None, "capture_id": "cap_1", "assertion_id": "as_1",
    "assertion_status": "current", "supersedes_assertion_id": None,
}


@pytest.fixture
def settings(tmp_path):
    return Settings(root=str(tmp_path), preflight_secret="k")


def test_every_silver_relation_carries_the_interval_columns():
    names = {f.name for f in INTERVAL_FIELDS}
    assert names == {
        "effective_from", "effective_to", "observed_at", "available_at", "system_from",
        "system_to", "capture_id", "assertion_id", "assertion_status", "supersedes_assertion_id",
    }
    for key, schema in RELATIONS.items():
        if key.startswith("silver."):
            assert names <= set(schema.names), key


def test_relation_path_is_layer_double_underscore_name(settings):
    assert relation_path(settings, "silver.listings") == f"{settings.root}/silver__listings"


def test_ensure_then_append_versions_and_reads_back(settings):
    assert latest_version(settings, "silver.issuers") is None
    ensure(settings, "silver.issuers")
    assert latest_version(settings, "silver.issuers") == 0
    v = append(settings, "silver.issuers", [{
        "issuer_id": "iss_1", "name": "Example", "effective_from": "2020-01-01T00:00:00Z",
        "effective_to": None, "observed_at": "2020-01-02T00:00:00Z",
        "available_at": "2020-01-02T00:00:00Z", "system_from": "2020-01-02T00:00:00Z",
        "system_to": None, "capture_id": "cap_1", "assertion_id": "as_1",
        "assertion_status": "current", "supersedes_assertion_id": None,
    }])
    assert v == 1
    table = open_table(settings, "silver.issuers").to_pyarrow_table()
    assert table.num_rows == 1
    assert str(table.schema.field("effective_from").type) == "timestamp[us, tz=UTC]"
    assert open_table(settings, "silver.issuers", version=0).to_pyarrow_table().num_rows == 0
    assert [h["version"] for h in history(settings, "silver.issuers")] == [1, 0]
    assert dataset(settings, "silver.issuers", 1).count_rows() == 1


def test_missing_version_is_version_not_retained(settings):
    ensure(settings, "silver.issuers")
    with pytest.raises(DomainError) as exc:
        open_table(settings, "silver.issuers", version=7)
    assert exc.value.code == "VERSION_NOT_RETAINED"


def test_unknown_relation_is_rejected(settings):
    with pytest.raises(DomainError) as exc:
        ensure(settings, "silver.nope")
    assert exc.value.code == "QUERY_REJECTED"


def test_append_rejects_unknown_columns(settings):
    ensure(settings, "silver.issuers")
    with pytest.raises(ValueError, match="unknown column"):
        append(settings, "silver.issuers", [{"issuer_id": "x", "bogus": 1}])


def test_append_unknown_relation_is_rejected(settings):
    with pytest.raises(DomainError) as exc:
        append(settings, "silver.nope", [{"issuer_id": "x"}])
    assert exc.value.code == "QUERY_REJECTED"


def test_latest_version_unknown_relation_is_rejected(settings):
    with pytest.raises(DomainError) as exc:
        latest_version(settings, "silver.nope")
    assert exc.value.code == "QUERY_REJECTED"


def test_history_timestamp_is_utc_and_recent(settings):
    ensure(settings, "silver.issuers")
    append(settings, "silver.issuers", [_ISSUER_ROW])
    ts = history(settings, "silver.issuers")[0]["timestamp"]
    assert ts.endswith("Z")
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert abs((datetime.now(UTC) - parsed).total_seconds()) < 60

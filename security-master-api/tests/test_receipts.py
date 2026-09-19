# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.resolver.context import parse_context
from security_master_api.store.receipts import new_receipt, record_receipt
from security_master_api.store.seed import seed
from security_master_api.store.tables import open_table


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return s


def test_receipt_shape_and_ordering(settings):
    ctx = parse_context({"mode": "known_at", "known_at": "2026-09-14T20:00:00Z"})
    r = new_receipt(settings, "preview", ctx, {"silver.listings": 2, "bronze.source_captures": 1},
                    "req_1", "abc")
    assert r["execution_id"].startswith("qry_")
    assert r["dependencies"] == [{"relation": "bronze.source_captures", "delta_version": 1},
                                 {"relation": "silver.listings", "delta_version": 2}]
    assert r["temporal_mode"] == "known_at" and r["known_at"] == "2026-09-14T20:00:00Z"
    assert r["projection_version"] == "security-master-projection/1"
    assert r["availability_policy"] == "local_as_ingested"
    assert set(r) == {"execution_id", "request_id", "kind", "temporal_mode", "effective_at",
                      "known_at", "availability_policy", "projection_version", "dependencies",
                      "sql_fingerprint", "created_at"}


def test_record_appends_one_row(settings):
    ctx = parse_context(None)
    r = new_receipt(settings, "sql", ctx, {"silver.listings": 1}, "req_2")
    record_receipt(settings, r)
    rows = open_table(settings, "ops.receipts").to_pyarrow_table().to_pylist()
    assert [x["execution_id"] for x in rows] == [r["execution_id"]]

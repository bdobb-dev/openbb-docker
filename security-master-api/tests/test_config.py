# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import settings_from_env


def test_local_root_wins_and_defaults_apply(tmp_path):
    s = settings_from_env({"SECURITY_MASTER_ROOT": str(tmp_path), "SECURITY_MASTER_PREFLIGHT_SECRET": "k"})
    assert s.root == str(tmp_path)
    assert s.storage_options == {}
    assert s.delta_base is None
    assert (s.first_page, s.max_rows, s.timeout_ms) == (100, 10_000, 15_000)
    assert (s.per_principal_queries, s.scan_slots) == (8, 4)
    assert (s.sql_policy, s.acquisition_policy) == ("enabled", "review")
    assert s.openbb_url == "http://openbb-api:6900"


def test_s3_root_is_derived_from_delta_env():
    env = {
        "DELTA_S3_ENDPOINT": "minio.example", "DELTA_S3_BUCKET": "openbb",
        "DELTA_S3_ACCESS": "a", "DELTA_S3_SECRET": "b", "DELTA_S3_PORT": "9000",
        "DELTA_S3_SECURE": "false", "SECURITY_MASTER_PREFLIGHT_SECRET": "k",
    }
    s = settings_from_env(env)
    assert s.root == "s3://openbb/security_master"
    assert s.delta_base == "s3://openbb"
    assert s.storage_options["aws_endpoint"] == "http://minio.example:9000"
    assert s.storage_options["aws_conditional_put"] == "etag"


def test_missing_store_config_is_an_error():
    with pytest.raises(ValueError, match="SECURITY_MASTER_ROOT"):
        settings_from_env({"SECURITY_MASTER_PREFLIGHT_SECRET": "k"})


def test_policy_values_are_validated():
    with pytest.raises(ValueError, match="SECURITY_MASTER_SQL"):
        settings_from_env({"SECURITY_MASTER_ROOT": "/x", "SECURITY_MASTER_SQL": "maybe",
                           "SECURITY_MASTER_PREFLIGHT_SECRET": "k"})


def test_preflight_secret_is_required():
    with pytest.raises(ValueError, match="SECURITY_MASTER_PREFLIGHT_SECRET"):
        settings_from_env({"SECURITY_MASTER_ROOT": "/x"})

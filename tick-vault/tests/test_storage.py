"""Tier routing: `tick_vault.storage.table_uri` / `s3_options`."""

from __future__ import annotations

import pytest

from tick_vault.schemas import SCHEMAS
from tick_vault.storage import _TIERS, s3_options, table_uri, tier_for

TIERED_ENV = {
    "LAKE_BRONZE_URI": "s3://bronze",
    "LAKE_SILVER_URI": "s3://silver",
    "LAKE_SILVER_HOT_URI": "s3://silver-hot",
    "LAKE_GOLD_URI": "s3://gold",
    "LAKE_RECEIPTS_URI": "s3://receipts",
    "DELTA_S3_HOT_ENDPOINT": "http://minio:9000",
    "DELTA_S3_HOT_ACCESS_KEY": "hot-user",
    "DELTA_S3_HOT_SECRET_KEY": "hot-pass",
    "DELTA_S3_HOT_ALLOW_HTTP": "true",
    "DELTA_S3_COLD_ENDPOINT": "http://minio-hdd:9000",
    "DELTA_S3_COLD_ACCESS_KEY": "cold-user",
    "DELTA_S3_COLD_SECRET_KEY": "cold-pass",
    "DELTA_S3_COLD_ALLOW_HTTP": "true",
}


def test_every_registered_table_routes_to_a_known_tier():
    """A new table with a typo'd layer must fail here, not at 3am mid-walk."""
    for table in SCHEMAS:
        assert tier_for(table) in _TIERS, table


def test_no_lake_env_keeps_todays_local_layout():
    uri, options = table_uri("/data/vault", "silver.us_trade_tick_version", env={})
    assert uri == "/data/vault/silver/us_trade_tick_version"
    assert options == {}


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("bronze.eodhd_tick_capture", "s3://bronze/eodhd_tick_capture"),
        ("silver.us_trade_tick_version", "s3://silver/us_trade_tick_version"),
        ("silver.instrument", "s3://silver-hot/instrument"),
        ("gold.us_trade_tape", "s3://gold/us_trade_tape"),
        ("ops.backfill_manifest", "s3://receipts/backfill_manifest"),
    ],
)
def test_tiered_layout_puts_the_layer_in_the_bucket(table, expected):
    uri, _ = table_uri("/data/vault", table, env=TIERED_ENV)
    assert uri == expected


def test_the_bulk_silver_tables_are_cold_and_the_reference_ones_are_hot():
    """The split that keeps the walk off the HDD for per-item reads."""
    bulk, _ = table_uri("/r", "silver.us_trade_tick_version", env=TIERED_ENV)
    reference, _ = table_uri("/r", "silver.index_membership_version", env=TIERED_ENV)
    assert bulk.startswith("s3://silver/")
    assert reference.startswith("s3://silver-hot/")

    _, bulk_opts = table_uri("/r", "silver.us_trade_tick_version", env=TIERED_ENV)
    _, ref_opts = table_uri("/r", "silver.index_membership_version", env=TIERED_ENV)
    assert bulk_opts["aws_endpoint"] == "http://minio-hdd:9000"
    assert ref_opts["aws_endpoint"] == "http://minio:9000"


def test_credentials_follow_the_tier():
    assert s3_options("bronze", TIERED_ENV)["aws_access_key_id"] == "cold-user"
    assert s3_options("gold", TIERED_ENV)["aws_access_key_id"] == "hot-user"


def test_concurrent_appends_keep_the_lock_free_commit_setting():
    """The parallel walk's workers append concurrently; without conditional
    put on etag, MinIO commits race."""
    assert s3_options("silver", TIERED_ENV)["aws_conditional_put"] == "etag"


def test_plaintext_endpoint_is_allowed_explicitly():
    """delta-rs refuses http:// unless told; both tiers are http today."""
    assert s3_options("silver", TIERED_ENV)["aws_allow_http"] == "true"
    assert "aws_allow_http" not in s3_options(
        "gold", {**TIERED_ENV, "DELTA_S3_HOT_ENDPOINT": "https://minio:9000",
                 "DELTA_S3_HOT_ALLOW_HTTP": "false"}
    )


def test_a_tier_with_no_endpoint_has_no_options():
    assert s3_options("gold", {}) == {}


class _FakeCon:
    """Records SQL instead of running it."""

    def __init__(self):
        self.sql = []

    def execute(self, statement):
        self.sql.append(statement)


def test_duckdb_gets_one_scoped_secret_per_tier(monkeypatch):
    """delta_scan ignores delta-rs storage_options, so the two MinIO
    endpoints have to reach DuckDB as separate scoped SECRETs."""
    from tick_vault import temporal

    for key, value in TIERED_ENV.items():
        monkeypatch.setenv(key, value)
    con = _FakeCon()
    temporal._install_s3_secrets(con)

    joined = " ".join(con.sql)
    assert "LOAD httpfs" in joined
    secrets = [s for s in con.sql if "CREATE OR REPLACE SECRET" in s]
    assert len(secrets) == 5, [s[:60] for s in secrets]
    cold = next(s for s in secrets if "SCOPE 's3://bronze'" in s)
    hot = next(s for s in secrets if "SCOPE 's3://gold'" in s)
    assert "ENDPOINT 'minio-hdd:9000'" in cold and "USE_SSL false" in cold
    assert "ENDPOINT 'minio:9000'" in hot
    assert "KEY_ID 'cold-user'" in cold and "KEY_ID 'hot-user'" in hot


def test_no_duckdb_secrets_without_an_object_store():
    from tick_vault import temporal

    con = _FakeCon()
    temporal._install_s3_secrets(con)
    assert con.sql == []

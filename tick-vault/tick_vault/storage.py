"""Where each registered table physically lives, and how to reach it.

Every Delta read and write in tick-vault resolves its location through
`table_uri(root, table)` rather than building `f"{root}/{layer}/{name}"`
inline, so the local-disk vs tiered-object-store decision lives in exactly
one place.

Two layouts are supported:

* **Local** (the default, and what every test uses): no `LAKE_*_URI` in the
  environment, so a table resolves to `<root>/<layer>/<name>` with no storage
  options - byte-for-byte today's behaviour.
* **Tiered object store**: `LAKE_<TIER>_URI` names a bucket per tier and the
  table resolves to `<bucket>/<name>` - the bucket *is* the layer - with
  credentials for that tier's MinIO drawn from `DELTA_S3_HOT_*` /
  `DELTA_S3_COLD_*`.

`TIER_BY_TABLE` is the whole routing policy. Moving a table between the NVMe
(hot) and HDD (cold) MinIO is a one-line edit here, not a code change at the
~15 call sites.
"""

from __future__ import annotations

import os

# Tier -> (env var naming the bucket, which MinIO holds it).
_TIERS: dict[str, tuple[str, str]] = {
    "bronze": ("LAKE_BRONZE_URI", "COLD"),
    "silver": ("LAKE_SILVER_URI", "COLD"),
    "silver_hot": ("LAKE_SILVER_HOT_URI", "HOT"),
    "gold": ("LAKE_GOLD_URI", "HOT"),
    "receipts": ("LAKE_RECEIPTS_URI", "HOT"),
}

# Which tier each registered table belongs to. Unlisted tables fall back to
# their layer name, so a newly registered `bronze.*`/`gold.*` table lands on
# the right tier without an edit here; only the silver split needs stating.
#
# Silver is deliberately split. The three bulk tables carry the ~19.6 TB of
# tick history and are written once and read by the week -> HDD. The ten
# reference tables are small and read on EVERY item of the backfill walk
# (262,345 of them), so they sit on NVMe; putting them cold would buy a
# network round-trip per item for no space saving.
TIER_BY_TABLE: dict[str, str] = {
    # --- silver, cold: the bulk ---
    "silver.us_trade_tick_version": "silver",
    "silver.tick_correction_event": "silver",
    "silver.feed_coverage": "silver",
    # --- silver, hot: small, read per item ---
    "silver.instrument": "silver_hot",
    "silver.issuer": "silver_hot",
    "silver.registrant": "silver_hot",
    "silver.listing_version": "silver_hot",
    "silver.index_membership_version": "silver_hot",
    "silver.identifier_assignment_version": "silver_hot",
    "silver.figi_assignment_version": "silver_hot",
    "silver.instrument_event_version": "silver_hot",
    "silver.instrument_relationship_version": "silver_hot",
    "silver.venue_reference_version": "silver_hot",
    "silver.data_availability_policy": "silver_hot",
    # --- ops are operational receipts ---
    "ops.backfill_manifest": "receipts",
    "ops.backtest_run_manifest": "receipts",
    "ops.data_quality_issue": "receipts",
    "ops.ingestion_run": "receipts",
    "ops.openfigi_resolution_queue": "receipts",
}


def tier_for(table: str) -> str:
    """Tier owning `table` ("bronze.eodhd_tick_capture" -> "bronze")."""
    return TIER_BY_TABLE.get(table, table.split(".", 1)[0])


def s3_options(tier: str, env: "dict[str, str] | None" = None) -> dict:
    """delta-rs `storage_options` for `tier`'s MinIO, or `{}` when that tier
    has no endpoint configured (the local-disk case).

    Reads `DELTA_S3_HOT_*` / `DELTA_S3_COLD_*`. `aws_conditional_put="etag"`
    is what lets concurrent writers commit without a lock server on MinIO -
    the parallel walk depends on it, so it is not optional.
    """
    e = os.environ if env is None else env
    _, side = _TIERS[tier]
    endpoint = str(e.get(f"DELTA_S3_{side}_ENDPOINT", "")).strip()
    if not endpoint:
        return {}
    options = {
        "aws_endpoint": endpoint,
        "aws_access_key_id": str(e.get(f"DELTA_S3_{side}_ACCESS_KEY", "")).strip(),
        "aws_secret_access_key": str(e.get(f"DELTA_S3_{side}_SECRET_KEY", "")).strip(),
        "aws_region": str(e.get(f"DELTA_S3_{side}_REGION", "") or "us-east-1").strip(),
        "aws_virtual_hosted_style_request": "false",
        "aws_conditional_put": "etag",
    }
    allow_http = str(e.get(f"DELTA_S3_{side}_ALLOW_HTTP", "")).strip().lower()
    if allow_http == "true" or endpoint.startswith("http://"):
        # delta-rs refuses a plaintext endpoint unless told to allow it.
        options["aws_allow_http"] = "true"
    return options


def configured_tiers(env: "dict[str, str] | None" = None) -> list:
    """`(tier, bucket, storage_options)` for every tier backed by an object
    store. Empty on a plain local `VAULT_ROOT`.

    The DuckDB query layer needs this separately from `table_uri`: `delta_scan`
    authenticates through DuckDB SECRETs, not delta-rs storage options.
    """
    e = os.environ if env is None else env
    out = []
    for tier, (bucket_env, _side) in _TIERS.items():
        bucket = str(e.get(bucket_env, "")).strip()
        options = s3_options(tier, e) if bucket else {}
        if bucket and options:
            out.append((tier, bucket.rstrip("/"), options))
    return out


def table_uri(
    root: str, table: str, env: "dict[str, str] | None" = None
) -> "tuple[str, dict]":
    """Resolve `table` ("silver.us_trade_tick_version") to `(uri, storage_options)`.

    Falls back to `<root>/<layer>/<name>` with no options whenever the tier's
    bucket is unset, so a plain local `VAULT_ROOT` keeps working untouched.
    """
    layer, name = table.split(".", 1)
    e = os.environ if env is None else env
    tier = tier_for(table)
    bucket_env, _ = _TIERS.get(tier, ("", ""))
    bucket = str(e.get(bucket_env, "")).strip() if bucket_env else ""
    if not bucket:
        return f"{root}/{layer}/{name}", {}
    return f"{bucket.rstrip('/')}/{name}", s3_options(tier, e)

"""Delta schema registry for the tick-vault bronze/silver/gold/ops layers.

Transcribed from baseline design doc §7 (`docs/superpowers/specs/
2026-09-09-sp500-temporal-tick-database-baseline.md`), §6 (table inventory),
§12.4/§12.5 (gold replay tables), §13.3/§16/§17 (backtest selection,
data-quality, and ingestion-run contract prose used to derive the tables the
baseline doesn't give an explicit `CREATE TABLE` for), plus the tick-vault
episode spec's deltas to `silver.us_trade_tick_version` (`session_seq`,
`sub_mkt_raw`, `is_cancelled`, `is_late_add`, `origin`) and its added tables
(`bronze.eodhd_eod_capture`, `bronze.eodhd_intraday_capture`,
`ops.backfill_manifest` keyed `(listing_id, week)`, `silver.tick_correction_event`).

pyarrow and deltalake are not installable in the authoring sandbox (network
blocked); this module is written test-first against deferred tests
(`tick-vault/tests/deferred/test_schemas.py`) and is expected to import-fail
here. The only test runnable in this sandbox
(`tick-vault/tests/test_schema_registry_names.py`) checks this file's source
text via `ast`, without importing pyarrow.

No logic beyond declaration lives here by design.
"""
from __future__ import annotations

import pyarrow as pa

_TS = pa.timestamp("us", tz="UTC")
_DEC24_10 = pa.decimal128(24, 10)
_STR_LIST = pa.list_(pa.string())

# ---------------------------------------------------------------------------
# bronze
# ---------------------------------------------------------------------------

# Baseline §7.1, plus a `committed_date` date32 column (Delta cannot partition
# on an expression like `date(committed_at_ts)`, so the date is materialized
# as its own column and used as the partition column instead).
_BRONZE_EODHD_TICK_CAPTURE = pa.schema([
    pa.field("capture_id", pa.string(), nullable=False),
    pa.field("ingestion_run_id", pa.string(), nullable=False),
    pa.field("source_system", pa.string(), nullable=False),
    pa.field("endpoint", pa.string(), nullable=False),
    pa.field("parser_version", pa.string(), nullable=False),
    pa.field("request_symbol", pa.string(), nullable=False),
    pa.field("request_exchange_code", pa.string()),
    pa.field("request_from_sec", pa.int64(), nullable=False),
    pa.field("request_to_sec", pa.int64(), nullable=False),
    pa.field("request_limit", pa.int32()),
    pa.field("request_cursor", pa.string()),
    pa.field("request_parameters_json", pa.string(), nullable=False),
    pa.field("http_status", pa.int32(), nullable=False),
    pa.field("response_headers_json", pa.string()),
    # Baseline §10.2: failed/error captures are still recorded as a row
    # (no payload object exists for them), so this must be nullable;
    # raw_payload_sha256 stays NOT NULL because failures still hash the
    # error response body into it.
    pa.field("raw_payload_uri", pa.string()),
    pa.field("raw_payload_sha256", pa.string(), nullable=False),
    pa.field("raw_row_count", pa.int64()),
    pa.field("first_source_ts_ms", pa.int64()),
    pa.field("last_source_ts_ms", pa.int64()),
    pa.field("observed_at_ts", _TS, nullable=False),
    pa.field("committed_at_ts", _TS, nullable=False),
    pa.field("committed_date", pa.date32(), nullable=False),
    pa.field("created_at_ts", _TS, nullable=False),
])

# The baseline defines only `eodhd_tick_capture` in full (§7.1). The
# remaining bronze captures are full-refresh / snapshot-style vendor pulls
# (§6.1: "immutable metadata/index tables pointing to raw payload objects");
# they share a minimal generic capture envelope, with a request-scope field
# specific to each endpoint appended where the baseline's prose (§8.2, §9,
# §16.3) implies one is needed.


def _generic_capture_fields(*extra) -> list:
    base = [
        pa.field("capture_id", pa.string(), nullable=False),
        pa.field("ingestion_run_id", pa.string(), nullable=False),
        pa.field("source_system", pa.string(), nullable=False),
        pa.field("endpoint", pa.string(), nullable=False),
        pa.field("parser_version", pa.string(), nullable=False),
    ]
    base.extend(extra)
    base.extend([
        pa.field("request_parameters_json", pa.string(), nullable=False),
        pa.field("http_status", pa.int32(), nullable=False),
        # Baseline §10.2: failed/error captures are still recorded as a row
        # (no payload object exists for them), so this must be nullable;
        # raw_payload_sha256 stays NOT NULL because failures still hash the
        # error response body into it.
        pa.field("raw_payload_uri", pa.string()),
        pa.field("raw_payload_sha256", pa.string(), nullable=False),
        pa.field("raw_row_count", pa.int64()),
        pa.field("observed_at_ts", _TS, nullable=False),
        pa.field("committed_at_ts", _TS, nullable=False),
        pa.field("created_at_ts", _TS, nullable=False),
    ])
    return base


# §8.2 required input capture for index membership.
_BRONZE_EODHD_INDEX_COMPONENTS_CAPTURE = pa.schema(_generic_capture_fields(
    pa.field("index_vendor_symbol", pa.string(), nullable=False),
))

# §9 identifier/security-master inputs: full exchange symbol listing.
_BRONZE_EODHD_EXCHANGE_SYMBOLS_CAPTURE = pa.schema(_generic_capture_fields(
    pa.field("exchange_code", pa.string(), nullable=False),
))

_BRONZE_EODHD_DELISTED_SYMBOLS_CAPTURE = pa.schema(_generic_capture_fields(
    pa.field("exchange_code", pa.string(), nullable=False),
))

_BRONZE_EODHD_SYMBOL_CHANGE_CAPTURE = pa.schema(_generic_capture_fields(
    pa.field("exchange_code", pa.string(), nullable=False),
))

_BRONZE_EODHD_FUNDAMENTALS_CAPTURE = pa.schema(_generic_capture_fields(
    pa.field("request_symbol", pa.string(), nullable=False),
    pa.field("request_exchange_code", pa.string()),
))

# §9.3 corporate action handling input.
_BRONZE_EODHD_CORPORATE_ACTIONS_CAPTURE = pa.schema(_generic_capture_fields(
    pa.field("request_symbol", pa.string(), nullable=False),
    pa.field("request_exchange_code", pa.string()),
    pa.field("request_from_sec", pa.int64()),
    pa.field("request_to_sec", pa.int64()),
))

# §16.3 reconciliation checks aggregate ticks into daily OHLCV and compare
# against EODHD EOD/intraday data; these two captures are the raw evidence
# for that comparison (added by the episode spec, not in baseline §6.1/§7).
_BRONZE_EODHD_EOD_CAPTURE = pa.schema(_generic_capture_fields(
    pa.field("request_symbol", pa.string(), nullable=False),
    pa.field("request_exchange_code", pa.string()),
    pa.field("request_from_sec", pa.int64()),
    pa.field("request_to_sec", pa.int64()),
))

_BRONZE_EODHD_INTRADAY_CAPTURE = pa.schema(_generic_capture_fields(
    pa.field("request_symbol", pa.string(), nullable=False),
    pa.field("request_exchange_code", pa.string()),
    pa.field("request_from_sec", pa.int64()),
    pa.field("request_to_sec", pa.int64()),
    pa.field("interval", pa.string(), nullable=False),
))

# §9.2 CUSIP/ISIN/FIGI/CIK matching hierarchy input.
_BRONZE_OPENFIGI_MAPPING_CAPTURE = pa.schema(_generic_capture_fields(
    pa.field("request_id_type", pa.string(), nullable=False),
    pa.field("request_id_value", pa.string(), nullable=False),
    pa.field("request_exchange_code", pa.string()),
))

# ---------------------------------------------------------------------------
# silver
# ---------------------------------------------------------------------------

# Not given an explicit CREATE TABLE in baseline §7; derived from §3.2's
# separate-identity-scopes prose (issuer identity is distinct from
# registrant/CIK identity) and from `silver.instrument.issuer_id` (§7.2),
# mirroring the shape of `silver.instrument`'s own lifecycle columns.
_SILVER_ISSUER = pa.schema([
    pa.field("issuer_id", pa.string(), nullable=False),
    pa.field("issuer_name", pa.string()),
    pa.field("country_of_incorporation", pa.string()),
    pa.field("created_at_ts", _TS, nullable=False),
    pa.field("retired_at_ts", _TS),
    pa.field("status", pa.string(), nullable=False),
    pa.field("source_system", pa.string()),
])

# Not given an explicit CREATE TABLE in baseline §7; derived from §3.2 (CIK
# is "SEC filer / registrant identity", separate from issuer/security
# identity) and `silver.instrument.registrant_id` (§7.2).
_SILVER_REGISTRANT = pa.schema([
    pa.field("registrant_id", pa.string(), nullable=False),
    pa.field("cik", pa.string()),
    pa.field("registrant_name", pa.string()),
    pa.field("created_at_ts", _TS, nullable=False),
    pa.field("retired_at_ts", _TS),
    pa.field("status", pa.string(), nullable=False),
    pa.field("source_system", pa.string()),
])

# Baseline §7.2, verbatim.
_SILVER_INSTRUMENT = pa.schema([
    pa.field("instrument_id", pa.string(), nullable=False),
    pa.field("issuer_id", pa.string()),
    pa.field("registrant_id", pa.string()),
    pa.field("instrument_type", pa.string(), nullable=False),
    pa.field("share_class", pa.string()),
    pa.field("country_of_incorporation", pa.string()),
    pa.field("primary_currency", pa.string()),
    pa.field("created_at_ts", _TS, nullable=False),
    pa.field("retired_at_ts", _TS),
    pa.field("status", pa.string(), nullable=False),
    pa.field("source_system", pa.string()),
])

# Baseline §7.3, verbatim.
_SILVER_LISTING_VERSION = pa.schema([
    pa.field("listing_version_id", pa.string(), nullable=False),
    pa.field("listing_id", pa.string(), nullable=False),
    pa.field("instrument_id", pa.string(), nullable=False),
    pa.field("ticker", pa.string()),
    pa.field("eodhd_symbol", pa.string()),
    pa.field("eodhd_exchange_code", pa.string()),
    pa.field("venue_id", pa.string()),
    pa.field("mic", pa.string()),
    pa.field("operating_mic", pa.string()),
    pa.field("exchange_name", pa.string()),
    pa.field("listing_currency", pa.string()),
    pa.field("venue_figi", pa.string()),
    pa.field("composite_figi", pa.string()),
    pa.field("share_class_figi", pa.string()),
    pa.field("price_venue_type", pa.string(), nullable=False),
    pa.field("effective_from_ts", _TS),
    pa.field("effective_to_ts", _TS),
    pa.field("observed_at_ts", _TS, nullable=False),
    pa.field("available_at_ts", _TS, nullable=False),
    pa.field("system_from_ts", _TS, nullable=False),
    pa.field("system_to_ts", _TS),
    pa.field("source_system", pa.string(), nullable=False),
    pa.field("source_capture_id", pa.string(), nullable=False),
    pa.field("confidence", pa.decimal128(5, 4), nullable=False),
    pa.field("verification_status", pa.string(), nullable=False),
])

# Baseline §7.9, verbatim.
_SILVER_VENUE_REFERENCE_VERSION = pa.schema([
    pa.field("venue_reference_version_id", pa.string(), nullable=False),
    pa.field("venue_id", pa.string(), nullable=False),
    pa.field("vendor_venue_code", pa.string()),
    pa.field("mic", pa.string()),
    pa.field("operating_mic", pa.string()),
    pa.field("venue_name", pa.string()),
    pa.field("venue_type", pa.string()),
    pa.field("country_code", pa.string()),
    pa.field("timezone", pa.string()),
    pa.field("effective_from_ts", _TS),
    pa.field("effective_to_ts", _TS),
    pa.field("observed_at_ts", _TS, nullable=False),
    pa.field("available_at_ts", _TS, nullable=False),
    pa.field("system_from_ts", _TS, nullable=False),
    pa.field("system_to_ts", _TS),
    pa.field("source_system", pa.string(), nullable=False),
    pa.field("source_capture_id", pa.string(), nullable=False),
    pa.field("confidence", pa.decimal128(5, 4), nullable=False),
])

# Baseline §7.4, verbatim.
_SILVER_IDENTIFIER_ASSIGNMENT_VERSION = pa.schema([
    pa.field("assignment_version_id", pa.string(), nullable=False),
    pa.field("issuer_id", pa.string()),
    pa.field("registrant_id", pa.string()),
    pa.field("instrument_id", pa.string()),
    pa.field("listing_id", pa.string()),
    pa.field("id_namespace", pa.string(), nullable=False),
    pa.field("id_value", pa.string(), nullable=False),
    pa.field("normalized_id_value", pa.string(), nullable=False),
    pa.field("effective_from_ts", _TS),
    pa.field("effective_to_ts", _TS),
    pa.field("observed_at_ts", _TS, nullable=False),
    pa.field("available_at_ts", _TS, nullable=False),
    pa.field("system_from_ts", _TS, nullable=False),
    pa.field("system_to_ts", _TS),
    pa.field("source_system", pa.string(), nullable=False),
    pa.field("source_capture_id", pa.string(), nullable=False),
    pa.field("verification_status", pa.string(), nullable=False),
    pa.field("confidence", pa.decimal128(5, 4), nullable=False),
    pa.field("supersedes_assignment_version_id", pa.string()),
])

# Baseline §7.5, verbatim.
_SILVER_FIGI_ASSIGNMENT_VERSION = pa.schema([
    pa.field("figi_assignment_version_id", pa.string(), nullable=False),
    pa.field("instrument_id", pa.string()),
    pa.field("listing_id", pa.string()),
    pa.field("figi_level", pa.string(), nullable=False),
    pa.field("figi", pa.string(), nullable=False),
    pa.field("effective_from_ts", _TS),
    pa.field("effective_to_ts", _TS),
    pa.field("observed_at_ts", _TS, nullable=False),
    pa.field("available_at_ts", _TS, nullable=False),
    pa.field("system_from_ts", _TS, nullable=False),
    pa.field("system_to_ts", _TS),
    pa.field("source_system", pa.string(), nullable=False),
    pa.field("source_capture_id", pa.string(), nullable=False),
    pa.field("match_status", pa.string(), nullable=False),
    pa.field("confidence", pa.decimal128(5, 4), nullable=False),
])

# Baseline §7.6, verbatim.
_SILVER_INSTRUMENT_EVENT_VERSION = pa.schema([
    pa.field("event_id", pa.string(), nullable=False),
    pa.field("instrument_id", pa.string(), nullable=False),
    pa.field("related_instrument_id", pa.string()),
    pa.field("listing_id", pa.string()),
    pa.field("event_type", pa.string(), nullable=False),
    pa.field("announced_at_ts", _TS),
    pa.field("effective_at_ts", _TS),
    pa.field("settlement_at_ts", _TS),
    pa.field("ratio", pa.decimal128(30, 15)),
    pa.field("cash_consideration", pa.decimal128(30, 15)),
    pa.field("currency", pa.string()),
    pa.field("event_details_json", pa.string()),
    pa.field("observed_at_ts", _TS, nullable=False),
    pa.field("available_at_ts", _TS, nullable=False),
    pa.field("system_from_ts", _TS, nullable=False),
    pa.field("system_to_ts", _TS),
    pa.field("source_system", pa.string(), nullable=False),
    pa.field("source_capture_id", pa.string(), nullable=False),
    pa.field("confidence", pa.decimal128(5, 4), nullable=False),
])

# Baseline §7.7, verbatim.
_SILVER_INSTRUMENT_RELATIONSHIP_VERSION = pa.schema([
    pa.field("relationship_id", pa.string(), nullable=False),
    pa.field("from_instrument_id", pa.string(), nullable=False),
    pa.field("to_instrument_id", pa.string(), nullable=False),
    pa.field("relationship_type", pa.string(), nullable=False),
    pa.field("effective_at_ts", _TS),
    pa.field("exchange_ratio", pa.decimal128(30, 15)),
    pa.field("details_json", pa.string()),
    pa.field("observed_at_ts", _TS, nullable=False),
    pa.field("available_at_ts", _TS, nullable=False),
    pa.field("system_from_ts", _TS, nullable=False),
    pa.field("system_to_ts", _TS),
    pa.field("source_system", pa.string(), nullable=False),
    pa.field("source_capture_id", pa.string()),
    pa.field("confidence", pa.decimal128(5, 4), nullable=False),
    pa.field("review_status", pa.string(), nullable=False),
])

# Baseline §7.8, verbatim.
_SILVER_INDEX_MEMBERSHIP_VERSION = pa.schema([
    pa.field("membership_version_id", pa.string(), nullable=False),
    pa.field("index_id", pa.string(), nullable=False),
    pa.field("index_vendor_symbol", pa.string(), nullable=False),
    pa.field("listing_id", pa.string()),
    pa.field("instrument_id", pa.string()),
    pa.field("source_constituent_code", pa.string(), nullable=False),
    pa.field("source_exchange_code", pa.string()),
    pa.field("source_name", pa.string()),
    pa.field("membership_effective_from", pa.date32(), nullable=False),
    pa.field("membership_effective_to", pa.date32()),
    pa.field("inclusion_reason", pa.string()),
    pa.field("exclusion_reason", pa.string()),
    pa.field("index_weight", pa.decimal128(20, 12)),
    pa.field("observed_at_ts", _TS, nullable=False),
    pa.field("available_at_ts", _TS, nullable=False),
    pa.field("system_from_ts", _TS, nullable=False),
    pa.field("system_to_ts", _TS),
    pa.field("source_system", pa.string(), nullable=False),
    pa.field("source_capture_id", pa.string(), nullable=False),
    pa.field("resolution_status", pa.string(), nullable=False),
    pa.field("confidence", pa.decimal128(5, 4), nullable=False),
    pa.field("supersedes_membership_version_id", pa.string()),
])

# Baseline §7.10 verbatim, unioned with the episode spec's delta columns
# (`session_seq`, `sub_mkt_raw`, `is_cancelled`, `is_late_add`, `origin`) and
# every name in `tick_vault.tick_parser.COLUMNS` (checked 1:1 by
# `tests/test_schema_registry_names.py`). `capture_kind` is intentionally
# NOT present (dropped per spec grandfathering).
_SILVER_US_TRADE_TICK_VERSION = pa.schema([
    pa.field("tick_version_id", pa.string(), nullable=False),
    pa.field("logical_tick_id", pa.string(), nullable=False),
    pa.field("source_system", pa.string(), nullable=False),
    pa.field("source_capture_id", pa.string(), nullable=False),
    pa.field("source_page_ordinal", pa.int64()),
    pa.field("source_row_ordinal", pa.int64(), nullable=False),
    pa.field("listing_id", pa.string()),
    pa.field("instrument_id", pa.string()),
    pa.field("vendor_request_symbol", pa.string(), nullable=False),
    pa.field("eodhd_exchange_code", pa.string()),
    pa.field("trade_date", pa.date32(), nullable=False),
    pa.field("trade_ts_ms", pa.int64(), nullable=False),
    pa.field("trade_ts", _TS, nullable=False),
    pa.field("venue_code_raw", pa.string()),
    pa.field("sub_mkt_raw", pa.string()),
    pa.field("session_seq", pa.int64()),
    pa.field("venue_id", pa.string()),
    pa.field("mic", pa.string()),
    pa.field("price_venue_type", pa.string(), nullable=False),
    pa.field("price", _DEC24_10, nullable=False),
    pa.field("size", pa.int64()),
    pa.field("sale_condition_raw", pa.string()),
    pa.field("sale_condition_flags", _STR_LIST),
    pa.field("origin", pa.string()),
    pa.field("vendor_trade_id", pa.string()),
    pa.field("vendor_sequence_no", pa.int64()),
    pa.field("observed_at_ts", _TS, nullable=False),
    pa.field("committed_at_ts", _TS, nullable=False),
    pa.field("available_at_ts", _TS, nullable=False),
    pa.field("revision_number", pa.int32(), nullable=False),
    pa.field("is_correction", pa.bool_(), nullable=False),
    pa.field("is_cancelled", pa.bool_()),
    pa.field("is_late_add", pa.bool_()),
    pa.field("supersedes_tick_version_id", pa.string()),
    pa.field("payload_hash", pa.string(), nullable=False),
    pa.field("record_hash", pa.string(), nullable=False),
    pa.field("parser_version", pa.string(), nullable=False),
])

# Added by the episode spec (not in baseline §6.2/§7); column list matches
# `tick_vault.diffing.EVENT_COLUMNS` exactly, which is the existing
# in-repo interface this table backs.
_SILVER_TICK_CORRECTION_EVENT = pa.schema([
    pa.field("correction_event_id", pa.string(), nullable=False),
    pa.field("correction_kind", pa.string(), nullable=False),
    pa.field("logical_tick_id", pa.string(), nullable=False),
    pa.field("superseded_tick_version_id", pa.string()),
    pa.field("superseding_tick_version_id", pa.string(), nullable=False),
    pa.field("revealing_capture_id", pa.string(), nullable=False),
    pa.field("observed_at_ts", _TS, nullable=False),
])

# Baseline §7.11, verbatim.
_SILVER_FEED_COVERAGE = pa.schema([
    pa.field("feed_coverage_id", pa.string(), nullable=False),
    pa.field("source_system", pa.string(), nullable=False),
    pa.field("market_code", pa.string(), nullable=False),
    pa.field("trade_date", pa.date32(), nullable=False),
    pa.field("listing_id", pa.string()),
    pa.field("vendor_request_symbol", pa.string()),
    pa.field("expected_by_ts", _TS),
    pa.field("first_observed_at_ts", _TS),
    pa.field("complete_observed_at_ts", _TS),
    pa.field("committed_at_ts", _TS),
    pa.field("expected_tick_count", pa.int64()),
    pa.field("received_tick_count", pa.int64()),
    pa.field("first_tick_ts_ms", pa.int64()),
    pa.field("last_tick_ts_ms", pa.int64()),
    pa.field("feed_status", pa.string(), nullable=False),
    pa.field("source_capture_id", pa.string()),
    pa.field("ingestion_run_id", pa.string()),
    pa.field("details_json", pa.string()),
])

# Not given an explicit CREATE TABLE in baseline §7; derived from §4.2's
# availability-policy prose (each named mode -
# HISTORICAL_VENDOR_FINAL_V1/AS_INGESTED_LOCAL_V1/CURRENT_CORRECTED_V1/
# LIVE_PRODUCTION_V1 - is itself a versioned, declarable policy record that
# `ops.backtest_run_manifest.availability_policy_version` and
# `ops.ingestion_run` reference by version string).
_SILVER_DATA_AVAILABILITY_POLICY = pa.schema([
    pa.field("policy_version", pa.string(), nullable=False),
    pa.field("mode", pa.string(), nullable=False),
    pa.field("description", pa.string()),
    pa.field("created_at_ts", _TS, nullable=False),
    pa.field("effective_from_ts", _TS),
    pa.field("effective_to_ts", _TS),
    pa.field("is_active", pa.bool_(), nullable=False),
])

# ---------------------------------------------------------------------------
# gold
# ---------------------------------------------------------------------------

# Baseline §12.4, verbatim.
_GOLD_US_TRADE_TAPE = pa.schema([
    pa.field("trade_date", pa.date32(), nullable=False),
    pa.field("listing_id", pa.string(), nullable=False),
    pa.field("instrument_id", pa.string()),
    pa.field("synthetic_tape_seq", pa.int64(), nullable=False),
    pa.field("sequence_method", pa.string(), nullable=False),
    pa.field("sequence_policy_version", pa.string(), nullable=False),
    pa.field("trade_ts_ms", pa.int64(), nullable=False),
    pa.field("trade_ts", _TS, nullable=False),
    pa.field("venue_id", pa.string()),
    pa.field("mic", pa.string()),
    pa.field("venue_code_raw", pa.string()),
    pa.field("price", _DEC24_10, nullable=False),
    pa.field("size", pa.int64()),
    pa.field("sale_condition_raw", pa.string()),
    pa.field("sale_condition_flags", _STR_LIST),
    pa.field("logical_tick_id", pa.string(), nullable=False),
    pa.field("tick_version_id", pa.string(), nullable=False),
    pa.field("source_capture_id", pa.string(), nullable=False),
    pa.field("available_at_ts", _TS, nullable=False),
])

# Baseline §12.5, verbatim.
_GOLD_US_TRADE_TICK_BY_VENUE = pa.schema([
    pa.field("trade_date", pa.date32(), nullable=False),
    pa.field("venue_id", pa.string()),
    pa.field("mic", pa.string()),
    pa.field("listing_id", pa.string(), nullable=False),
    pa.field("instrument_id", pa.string()),
    pa.field("trade_ts_ms", pa.int64(), nullable=False),
    pa.field("trade_ts", _TS, nullable=False),
    pa.field("price", _DEC24_10, nullable=False),
    pa.field("size", pa.int64()),
    pa.field("sale_condition_raw", pa.string()),
    pa.field("logical_tick_id", pa.string(), nullable=False),
    pa.field("tick_version_id", pa.string(), nullable=False),
    pa.field("source_capture_id", pa.string(), nullable=False),
    pa.field("available_at_ts", _TS, nullable=False),
])

# Not given an explicit CREATE TABLE in baseline; derived from §13.2's
# "resolve index membership once per decision date or rebalance date" and
# the `pit_sp500_membership(...)` macro shape - this is that macro's
# materialized/frozen output, one row per (as_of, listing) universe member.
_GOLD_SP500_UNIVERSE_SNAPSHOT = pa.schema([
    pa.field("snapshot_id", pa.string(), nullable=False),
    pa.field("as_of_decision_date", pa.date32(), nullable=False),
    pa.field("as_of_knowledge_ts", _TS, nullable=False),
    pa.field("index_id", pa.string(), nullable=False),
    pa.field("listing_id", pa.string(), nullable=False),
    pa.field("instrument_id", pa.string()),
    pa.field("index_weight", pa.decimal128(20, 12)),
    pa.field("membership_version_id", pa.string(), nullable=False),
    pa.field("created_at_ts", _TS, nullable=False),
])

# Not given an explicit CREATE TABLE in baseline; derived from §13.3's
# `pit_tick_versions(as_of_ts)` macro ("materialize gold.selected_tick_version
# by availability snapshot or per run") - one row per selected
# (as_of/run, logical_tick_id) winner of that macro's row_number() pick.
_GOLD_SELECTED_TICK_VERSION = pa.schema([
    pa.field("selection_id", pa.string(), nullable=False),
    pa.field("as_of_ts", _TS, nullable=False),
    pa.field("run_id", pa.string()),
    pa.field("logical_tick_id", pa.string(), nullable=False),
    pa.field("tick_version_id", pa.string(), nullable=False),
    pa.field("listing_id", pa.string()),
    pa.field("instrument_id", pa.string()),
    pa.field("trade_date", pa.date32(), nullable=False),
    pa.field("created_at_ts", _TS, nullable=False),
])

# ---------------------------------------------------------------------------
# ops
# ---------------------------------------------------------------------------

# Baseline §7.12 (`ops.tick_download_manifest`), renamed `backfill_manifest`
# and re-keyed per the episode spec: `trade_date` (single-day key + partition
# column) is replaced with `week_monday` (the Monday of the ISO week being
# backfilled), so the natural key becomes `(listing_id, week_monday)` and
# `week_monday` is the partition column instead of `trade_date`.
_OPS_BACKFILL_MANIFEST = pa.schema([
    pa.field("work_id", pa.string(), nullable=False),
    pa.field("listing_id", pa.string(), nullable=False),
    pa.field("week_monday", pa.date32(), nullable=False),
    pa.field("instrument_id", pa.string()),
    pa.field("vendor_symbol_at_date", pa.string(), nullable=False),
    pa.field("eodhd_exchange_code", pa.string()),
    pa.field("request_from_sec", pa.int64(), nullable=False),
    pa.field("request_to_sec", pa.int64(), nullable=False),
    pa.field("membership_snapshot_id", pa.string(), nullable=False),
    pa.field("identity_snapshot_id", pa.string(), nullable=False),
    pa.field("availability_policy_version", pa.string(), nullable=False),
    pa.field("status", pa.string(), nullable=False),
    pa.field("attempts", pa.int32(), nullable=False),
    pa.field("priority", pa.int32(), nullable=False),
    pa.field("completed_at_ts", _TS),
    pa.field("last_error", pa.string()),
    pa.field("created_at_ts", _TS, nullable=False),
    pa.field("updated_at_ts", _TS, nullable=False),
])

# Not given an explicit CREATE TABLE in baseline; derived field-for-field
# from §17's `ops.ingestion_run` field list ("source endpoint/version" split
# into two fields; "input manifest range" / "output Delta table versions" /
# "quality-check summary" / "error summary" given `_json` suffixes to match
# the `*_json` convention used elsewhere in §7 for structured detail
# columns, e.g. `event_details_json`, `details_json`).
_OPS_INGESTION_RUN = pa.schema([
    pa.field("run_id", pa.string(), nullable=False),
    pa.field("run_type", pa.string(), nullable=False),
    pa.field("code_git_commit", pa.string(), nullable=False),
    pa.field("config_hash", pa.string(), nullable=False),
    pa.field("started_at_ts", _TS, nullable=False),
    pa.field("completed_at_ts", _TS),
    pa.field("status", pa.string(), nullable=False),
    pa.field("source_endpoint", pa.string()),
    pa.field("source_version", pa.string()),
    pa.field("parser_version", pa.string()),
    pa.field("availability_policy_version", pa.string()),
    pa.field("input_manifest_range_json", pa.string()),
    pa.field("output_table_versions_json", pa.string()),
    pa.field("quality_check_summary_json", pa.string()),
    pa.field("error_summary", pa.string()),
])

# Not given an explicit CREATE TABLE in baseline; derived from §9.2/§9.4
# (CUSIP/ISIN/FIGI/CIK matching hierarchy) - a work queue of unresolved or
# in-progress OpenFIGI lookups, mirroring `ops.tick_download_manifest`'s
# work-item shape (§7.12) and pointing at the
# `silver.figi_assignment_version` row it produces once resolved.
_OPS_OPENFIGI_RESOLUTION_QUEUE = pa.schema([
    pa.field("queue_id", pa.string(), nullable=False),
    pa.field("instrument_id", pa.string()),
    pa.field("listing_id", pa.string()),
    pa.field("id_namespace", pa.string(), nullable=False),
    pa.field("id_value", pa.string(), nullable=False),
    pa.field("exchange_code", pa.string()),
    pa.field("status", pa.string(), nullable=False),
    pa.field("attempts", pa.int32(), nullable=False),
    pa.field("priority", pa.int32(), nullable=False),
    pa.field("last_error", pa.string()),
    pa.field("created_at_ts", _TS, nullable=False),
    pa.field("updated_at_ts", _TS, nullable=False),
    pa.field("resolved_figi_assignment_version_id", pa.string()),
])

# Not given an explicit CREATE TABLE in baseline; derived from §16's data
# quality control checklists (§16.1-§16.4) - one row per detected issue
# instance from any of those checks, generic enough to cover all four
# categories (universe/coverage/reconciliation/corporate-action).
_OPS_DATA_QUALITY_ISSUE = pa.schema([
    pa.field("issue_id", pa.string(), nullable=False),
    pa.field("check_name", pa.string(), nullable=False),
    pa.field("severity", pa.string(), nullable=False),
    pa.field("trade_date", pa.date32()),
    pa.field("listing_id", pa.string()),
    pa.field("instrument_id", pa.string()),
    pa.field("source_capture_id", pa.string()),
    pa.field("details_json", pa.string()),
    pa.field("status", pa.string(), nullable=False),
    pa.field("detected_at_ts", _TS, nullable=False),
    pa.field("resolved_at_ts", _TS),
    pa.field("ingestion_run_id", pa.string()),
])

# Baseline §7.13, verbatim.
_OPS_BACKTEST_RUN_MANIFEST = pa.schema([
    pa.field("run_id", pa.string(), nullable=False),
    pa.field("run_name", pa.string()),
    pa.field("strategy_git_commit", pa.string(), nullable=False),
    pa.field("strategy_config_json", pa.string(), nullable=False),
    pa.field("strategy_config_hash", pa.string(), nullable=False),
    pa.field("research_mode", pa.string(), nullable=False),
    pa.field("availability_policy_version", pa.string(), nullable=False),
    pa.field("decision_schedule_version", pa.string(), nullable=False),
    pa.field("execution_policy_version", pa.string(), nullable=False),
    pa.field("universe_policy_version", pa.string(), nullable=False),
    pa.field("start_date", pa.date32(), nullable=False),
    pa.field("end_date", pa.date32(), nullable=False),
    pa.field("knowledge_cutoff_ts", _TS),
    pa.field("input_delta_versions_json", pa.string(), nullable=False),
    pa.field("input_snapshot_ids_json", pa.string()),
    pa.field("query_hash", pa.string()),
    pa.field("output_hash", pa.string()),
    pa.field("created_at_ts", _TS, nullable=False),
    pa.field("completed_at_ts", _TS),
    pa.field("status", pa.string(), nullable=False),
])

# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

SCHEMAS: dict = {
    "bronze.eodhd_tick_capture": _BRONZE_EODHD_TICK_CAPTURE,
    "bronze.eodhd_index_components_capture": _BRONZE_EODHD_INDEX_COMPONENTS_CAPTURE,
    "bronze.eodhd_exchange_symbols_capture": _BRONZE_EODHD_EXCHANGE_SYMBOLS_CAPTURE,
    "bronze.eodhd_delisted_symbols_capture": _BRONZE_EODHD_DELISTED_SYMBOLS_CAPTURE,
    "bronze.eodhd_symbol_change_capture": _BRONZE_EODHD_SYMBOL_CHANGE_CAPTURE,
    "bronze.eodhd_fundamentals_capture": _BRONZE_EODHD_FUNDAMENTALS_CAPTURE,
    "bronze.eodhd_corporate_actions_capture": _BRONZE_EODHD_CORPORATE_ACTIONS_CAPTURE,
    "bronze.eodhd_eod_capture": _BRONZE_EODHD_EOD_CAPTURE,
    "bronze.eodhd_intraday_capture": _BRONZE_EODHD_INTRADAY_CAPTURE,
    "bronze.openfigi_mapping_capture": _BRONZE_OPENFIGI_MAPPING_CAPTURE,
    "silver.issuer": _SILVER_ISSUER,
    "silver.registrant": _SILVER_REGISTRANT,
    "silver.instrument": _SILVER_INSTRUMENT,
    "silver.listing_version": _SILVER_LISTING_VERSION,
    "silver.venue_reference_version": _SILVER_VENUE_REFERENCE_VERSION,
    "silver.identifier_assignment_version": _SILVER_IDENTIFIER_ASSIGNMENT_VERSION,
    "silver.figi_assignment_version": _SILVER_FIGI_ASSIGNMENT_VERSION,
    "silver.instrument_event_version": _SILVER_INSTRUMENT_EVENT_VERSION,
    "silver.instrument_relationship_version": _SILVER_INSTRUMENT_RELATIONSHIP_VERSION,
    "silver.index_membership_version": _SILVER_INDEX_MEMBERSHIP_VERSION,
    "silver.us_trade_tick_version": _SILVER_US_TRADE_TICK_VERSION,
    "silver.tick_correction_event": _SILVER_TICK_CORRECTION_EVENT,
    "silver.feed_coverage": _SILVER_FEED_COVERAGE,
    "silver.data_availability_policy": _SILVER_DATA_AVAILABILITY_POLICY,
    "gold.us_trade_tape": _GOLD_US_TRADE_TAPE,
    "gold.us_trade_tick_by_venue": _GOLD_US_TRADE_TICK_BY_VENUE,
    "gold.sp500_universe_snapshot": _GOLD_SP500_UNIVERSE_SNAPSHOT,
    "gold.selected_tick_version": _GOLD_SELECTED_TICK_VERSION,
    "ops.backfill_manifest": _OPS_BACKFILL_MANIFEST,
    "ops.ingestion_run": _OPS_INGESTION_RUN,
    "ops.openfigi_resolution_queue": _OPS_OPENFIGI_RESOLUTION_QUEUE,
    "ops.data_quality_issue": _OPS_DATA_QUALITY_ISSUE,
    "ops.backtest_run_manifest": _OPS_BACKTEST_RUN_MANIFEST,
}

# Partitioning per baseline §14.1 and the episode spec's deltas: partitioned
# tables use `trade_date` except `eodhd_tick_capture` (`committed_date`,
# since Delta can't partition on the `date(committed_at_ts)` expression) and
# `ops.backfill_manifest` (`week_monday`, per its re-keying to weekly work
# items).
PARTITIONING: dict = {
    "bronze.eodhd_tick_capture": ["committed_date"],
    "silver.us_trade_tick_version": ["trade_date"],
    "silver.feed_coverage": ["trade_date"],
    "gold.us_trade_tape": ["trade_date"],
    "gold.us_trade_tick_by_venue": ["trade_date"],
    "ops.backfill_manifest": ["week_monday"],
}


def create_all(root: str) -> None:
    """Create every registered table as an empty Delta table under `root`.

    Each table is written to `<root>/<schema>/<table>` (e.g.
    `<root>/silver/us_trade_tick_version`), partitioned per `PARTITIONING`
    when declared there.
    """
    import deltalake

    for name, schema in SCHEMAS.items():
        layer, table = name.split(".", 1)
        path = f"{root}/{layer}/{table}"
        empty_table = pa.Table.from_pylist([], schema=schema)
        deltalake.write_deltalake(
            path, empty_table, partition_by=PARTITIONING.get(name)
        )

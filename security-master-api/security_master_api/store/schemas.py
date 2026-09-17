# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""One pyarrow schema per logical relation. The keys are the API's relation names."""

from __future__ import annotations

import pyarrow as pa

TS = pa.timestamp("us", tz="UTC")

INTERVAL_FIELDS = [
    pa.field("effective_from", TS),
    pa.field("effective_to", TS),
    pa.field("observed_at", TS),
    pa.field("available_at", TS),
    pa.field("system_from", TS),
    pa.field("system_to", TS),
    pa.field("capture_id", pa.string()),
    pa.field("assertion_id", pa.string()),
    pa.field("assertion_status", pa.string()),
    pa.field("supersedes_assertion_id", pa.string()),
]


def _silver(*fields: pa.Field) -> pa.Schema:
    return pa.schema([*fields, *INTERVAL_FIELDS])


S = pa.string
RELATIONS: dict[str, pa.Schema] = {
    "bronze.source_captures": pa.schema([
        pa.field("capture_id", S()), pa.field("provider", S()), pa.field("endpoint", S()),
        pa.field("request_fingerprint", S()), pa.field("captured_at", TS),
        pa.field("content_hash", S()), pa.field("status", pa.int32()),
        pa.field("payload", S()), pa.field("job_id", S()),
    ]),
    "bronze.request_log": pa.schema([
        pa.field("request_id", S()), pa.field("job_id", S()), pa.field("capture_id", S()),
        pa.field("requested_at", TS), pa.field("response_status", pa.int32()),
        pa.field("attempt", pa.int32()), pa.field("retry_after_s", pa.int32()),
        pa.field("error", S()),
    ]),
    "bronze.payload_rows": pa.schema([
        pa.field("capture_id", S()), pa.field("ordinal", pa.int32()),
        pa.field("source_key", S()), pa.field("payload", S()),
    ]),
    "bronze.ingestion_runs": pa.schema([
        pa.field("run_id", S()), pa.field("job_id", S()), pa.field("code_version", S()),
        pa.field("started_at", TS), pa.field("ended_at", TS), pa.field("outcome", S()),
    ]),
    "silver.issuers": _silver(pa.field("issuer_id", S()), pa.field("name", S())),
    "silver.instruments": _silver(
        pa.field("instrument_id", S()), pa.field("issuer_id", S()), pa.field("name", S()),
        pa.field("instrument_type", S()),
    ),
    "silver.securities": _silver(
        pa.field("security_id", S()), pa.field("instrument_id", S()), pa.field("issuer_id", S()),
        pa.field("cusip", S()), pa.field("predecessor_security_id", S()),
        pa.field("successor_security_id", S()), pa.field("reorganization_kind", S()),
    ),
    "silver.listings": _silver(
        pa.field("listing_id", S()), pa.field("instrument_id", S()), pa.field("issuer_id", S()),
        pa.field("security_id", S()), pa.field("exchange_id", S()), pa.field("mic", S()),
        pa.field("symbol", S()), pa.field("provider_symbol", S()), pa.field("currency", S()),
        pa.field("status", S()), pa.field("name", S()), pa.field("instrument_type", S()),
    ),
    "silver.identifiers": _silver(
        pa.field("identifier_type", S()), pa.field("identifier", S()),
        pa.field("listing_id", S()), pa.field("instrument_id", S()),
        pa.field("security_id", S()), pa.field("issuer_id", S()),
    ),
    "silver.fundamental_facts": _silver(
        pa.field("issuer_id", S()), pa.field("fact", S()), pa.field("period_end", pa.date32()),
        pa.field("value", pa.float64()), pa.field("unit", S()), pa.field("filing_kind", S()),
    ),
    "silver.prices_normalized": _silver(
        pa.field("listing_id", S()), pa.field("market_date", pa.date32()),
        pa.field("open", pa.float64()), pa.field("high", pa.float64()),
        pa.field("low", pa.float64()), pa.field("close", pa.float64()),
        pa.field("volume", pa.float64()), pa.field("price_basis", S()),
    ),
    "silver.corporate_actions": _silver(
        pa.field("listing_id", S()), pa.field("action_type", S()),
        pa.field("ex_date", pa.date32()), pa.field("ratio", pa.float64()),
        pa.field("amount", pa.float64()), pa.field("currency", S()),
    ),
    "silver.universe_membership": _silver(
        pa.field("universe_id", S()), pa.field("listing_id", S()),
    ),
    "silver.exchanges": _silver(
        pa.field("exchange_id", S()), pa.field("calendar_id", S()), pa.field("mic", S()),
        pa.field("name", S()), pa.field("timezone", S()), pa.field("calendar_alias", S()),
        pa.field("eodhd_code", S()),
    ),
    "silver.calendar_authorities": _silver(
        pa.field("authority_id", S()), pa.field("calendar_id", S()),
        pa.field("authority_type", S()), pa.field("authority_name", S()),
        pa.field("jurisdiction", S()), pa.field("holiday_family", S()),
    ),
    "silver.calendar_rules": _silver(
        pa.field("rule_id", S()), pa.field("calendar_id", S()), pa.field("calendar_alias", S()),
        pa.field("adapter", S()), pa.field("adapter_version", S()), pa.field("rule_kind", S()),
    ),
    "silver.calendar_exceptions": _silver(
        pa.field("calendar_id", S()), pa.field("session_date", pa.date32()),
        pa.field("assertion_domain", S()), pa.field("holiday_family", S()),
        pa.field("calendar_system", S()), pa.field("evidence_status", S()),
        pa.field("authority_type", S()), pa.field("authority_name", S()),
        pa.field("authority_verified_at", TS), pa.field("source_kind", S()),
        pa.field("source_version", S()), pa.field("rule_id", S()),
        pa.field("market_effect", S()), pa.field("holiday_name", S()),
        pa.field("special_open", pa.bool_()), pa.field("special_close", pa.bool_()),
        pa.field("market_open", TS), pa.field("market_close", TS),
        pa.field("candidate_branches", S()), pa.field("selected_branch", S()),
        pa.field("expiry_date", pa.date32()), pa.field("settlement_status", S()),
    ),
    "silver.market_sessions": _silver(
        pa.field("calendar_id", S()), pa.field("session_date", pa.date32()),
        pa.field("trade_date", pa.date32()), pa.field("market_open", TS),
        pa.field("market_close", TS), pa.field("break_start", TS), pa.field("break_end", TS),
        pa.field("rule_id", S()), pa.field("source_version", S()),
    ),
    "silver.session_interruptions": _silver(
        pa.field("calendar_id", S()), pa.field("session_date", pa.date32()),
        pa.field("interruption_start", TS), pa.field("interruption_end", TS),
    ),
    "ops.jobs": pa.schema([
        pa.field("job_id", S()), pa.field("kind", S()), pa.field("fingerprint", S()),
        pa.field("state", S()), pa.field("created_at", TS), pa.field("updated_at", TS),
        pa.field("request", S()), pa.field("summary", S()), pa.field("preflight_token_hash", S()),
        pa.field("seq", pa.int32()),
    ]),
    "ops.job_events": pa.schema([
        pa.field("event_id", S()), pa.field("job_id", S()), pa.field("seq", pa.int32()),
        pa.field("stage", S()), pa.field("at", TS), pa.field("detail", S()),
        pa.field("worker_id", S()),
    ]),
    "ops.receipts": pa.schema([
        pa.field("execution_id", S()), pa.field("request_id", S()), pa.field("kind", S()),
        pa.field("temporal_mode", S()), pa.field("effective_at", TS), pa.field("known_at", TS),
        pa.field("availability_policy", S()), pa.field("projection_version", S()),
        pa.field("dependencies", S()), pa.field("sql_fingerprint", S()),
        pa.field("created_at", TS),
    ]),
}

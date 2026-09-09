# S&P 500 Temporal Tick Database

## Development Design for EODHD + Delta Lake + DuckDB

**Status:** Design baseline  
**Primary use case:** Ten years of US trade-level tick data for the point-in-time S&P 500 universe, with reproducible historical research and backtests.  
**Data source:** EODHD US Tick Data API, Fundamentals/Index Components APIs, Exchange APIs, corporate-action data, and symbol-change data.  
**Storage/query stack:** Delta Lake on Parquet; DuckDB for local analytical queries, research, and backtest preparation.

---

## 1. Objectives

Build a database that can answer all of the following accurately and reproducibly:

1. Which securities were members of the S&P 500 on a given historical market date?
2. Which EODHD symbol, ticker, CUSIP, CIK, FIGI, and venue mapped to that security at that time?
3. Which individual trades were available for a listing, venue, and session?
4. In what deterministic order should ticks be replayed across venues?
5. What information would have been available under a defined historical-feed or live-ingestion availability policy?
6. Can an old backtest be rerun with the exact same source captures, data versions, identity mappings, and configuration?

The system must prevent:

- Survivorship bias from using current S&P 500 members for historical dates.
- Look-ahead bias from future ticker mappings, later corporate-action corrections, or late vendor updates.
- Ticker-reuse errors.
- Incorrect merging of different share classes, ADRs, cross-listings, mergers, or spin-offs.
- Nondeterministic ordering of same-millisecond trades from multiple venues.
- Loss of raw vendor evidence after normalization or correction handling.

---

## 2. Non-Goals and Constraints

### 2.1 Non-goals

This initial design does not attempt to reconstruct a complete official SIP/CTA/UTP tape, NBBO, order-book queue state, or quote/trade causality.

EODHD tick data can support trade-level historical research and deterministic replay, but do not represent generated sequence values as official consolidated-tape sequence numbers unless EODHD explicitly supplies such a field.

### 2.2 Source limitations

EODHD’s US Tick Data API is described as providing trade-level data with millisecond timestamps, venue, and sale-condition fields. Treat the exact delivered schema as authoritative and preserve every source field even if it is not initially modeled.

The source may not provide:

- Official SIP/CTA/UTP sequence numbers.
- Nanosecond timestamps.
- Quote/NBBO events.
- Full correction/cancel semantics equivalent to a direct-feed or SIP replay source.

Therefore, use an explicitly named synthetic deterministic ordering policy for same-millisecond ties.

### 2.3 Data scale

Ten years of trade-level data for all historical S&P 500 constituents is large. Design for:

- Many historical constituents, not only 500 current names.
- Repeated constituent tenures.
- Symbol changes, delistings, acquisitions, migrations, and ticker reuse.
- Venue-specific analysis and consolidated per-listing replay.
- Efficient date- and listing-filtered scans in DuckDB.

---

## 3. Design Principles

### 3.1 Internal identifiers are canonical

Never use ticker, vendor symbol, CUSIP, ISIN, FIGI, or CIK as the database primary key.

Use internal immutable IDs:

```text
issuer_id       Economic/legal issuer group
registrant_id   SEC reporting entity
instrument_id   Specific economic security or share class
listing_id      Tradable listing / venue representation
venue_id        Trading venue reference entity
index_id        Index definition, e.g. S&P 500
```

### 3.2 Separate identity scopes

```text
CIK                 SEC filer / registrant identity
CUSIP / ISIN        Security issue identity
Share-class FIGI    Global economic share-class identity
Composite FIGI      Country/market aggregation identity
Venue FIGI          Specific listing/venue representation
Ticker              Time-bounded listing attribute
EODHD symbol        Time-bounded vendor retrieval identifier
MIC                 Specific market/venue identifier
```

### 3.3 Every fact has market time and knowledge time

The system is bitemporal.

```text
effective time / event time:
  When the fact was true in the market.

knowledge time / availability time:
  When the fact became available to the simulated strategy or to this database.
```

### 3.4 Preserve raw source evidence

Every EODHD request and response is immutable evidence. Corrections, retries, backfills, and re-downloads create new captures. They never overwrite the original raw capture.

### 3.5 Silver is correct; gold is fast

```text
Bronze: Immutable raw captures and request metadata.
Silver: Normalized, append-only, temporal truth and provenance.
Gold: Denormalized, optimized tables for research and replay.
```

Do not run a backtest against raw API payloads or broad silver-table window functions when a materialized gold input is appropriate.

---

## 4. Time Model

### 4.1 Required timestamps

Use UTC timestamps for all stored timestamps. Keep `trade_date` as the exchange session date in `America/New_York` for US equities.

| Field | Meaning |
|---|---|
| `trade_ts_ms` | Exact vendor event timestamp in UTC epoch milliseconds |
| `trade_ts` | Timestamp representation derived from `trade_ts_ms` |
| `trade_date` | US exchange-session date, not UTC calendar date |
| `effective_from_ts` | When a mapping or event became true in the market |
| `effective_to_ts` | When a mapping or event ceased to be true in the market |
| `vendor_published_at_ts` | Vendor publication/update timestamp, if source supplies it |
| `observed_at_ts` | When the pipeline received the source response |
| `committed_at_ts` | When the validated Delta write committed |
| `available_at_ts` | Earliest strategy-permitted use time under a policy |
| `system_from_ts` | When a curated record version became visible |
| `system_to_ts` | When that curated version ceased to be the latest system understanding |

### 4.2 Availability policies

Backtests must declare an `availability_policy_version`.

Support at least these modes:

| Mode | Meaning |
|---|---|
| `HISTORICAL_VENDOR_FINAL_V1` | Uses a documented or conservative historical publication/finalization policy for archival backfill research |
| `AS_INGESTED_LOCAL_V1` | Uses actual local capture and validation times; historical backfilled data is not treated as available before it was downloaded |
| `CURRENT_CORRECTED_V1` | Uses the latest known data; not valid for live-like historical information-set testing |
| `LIVE_PRODUCTION_V1` | Uses actual forward ingestion timestamps and validated feed-completeness status |

Never infer a historical availability time from `trade_date` alone.

### 4.3 Core eligibility predicate

A fact can be used at `decision_ts` only when:

```sql
available_at_ts <= decision_ts
```

For mappings and events, also enforce their effective interval:

```sql
effective_from_ts <= decision_ts
AND (effective_to_ts IS NULL OR effective_to_ts > decision_ts)
AND available_at_ts <= decision_ts
AND (system_to_ts IS NULL OR system_to_ts > decision_ts)
```

---

## 5. Logical Data Model

### 5.1 Entity relationship overview

```text
index_id
  └── index_membership_version
        └── listing_id
              └── listing_version
                    ├── instrument_id
                    │     ├── issuer_id
                    │     ├── registrant_id / SEC CIK mappings
                    │     ├── CUSIP / ISIN / share-class FIGI mappings
                    │     └── corporate-action relationships
                    ├── venue_id / MIC
                    ├── venue FIGI / composite FIGI mappings
                    └── ticker and EODHD-symbol history

listing_id
  └── normalized trade ticks
        ├── venue_id
        ├── deterministic tape sequence
        ├── source capture provenance
        └── price availability/version history
```

### 5.2 ID allocation rules

| ID | Allocate when | Must change? |
|---|---|---|
| `issuer_id` | A distinct economic/legal issuer group is identified | No |
| `registrant_id` | A distinct SEC reporting filer is identified | No |
| `instrument_id` | A distinct security/share class/security issue is identified | No |
| `listing_id` | A distinct tradable listing/venue representation is identified | No |
| `venue_id` | A distinct market/venue/reference venue is identified | No |
| `index_id` | A distinct index definition is modeled | No |

Examples:

```text
Ticker rename:
  Same instrument_id and listing_id.

Simple issuer name change:
  Same instrument_id and listing_id.

Stock split:
  Same instrument_id and listing_id; add corporate-action event.

Cash merger:
  Target and acquirer retain separate instrument_id values; target expires.

Stock merger:
  Target and successor/acquirer remain distinct instruments; add exchange relationship and ratio.

Spin-off:
  Parent and child have distinct instrument_id values; add spun_off_from relationship.

Multiple share classes:
  Separate instrument_id values, often same issuer_id/CIK.

Venue migration:
  Same instrument_id where validated; old and new listing_id values are distinct.

Cross-listing:
  Same instrument_id/share class where validated; separate listing_id values.
```

---

## 6. Delta Table Inventory

### 6.1 Bronze tables

Bronze tables are immutable metadata/index tables pointing to raw payload objects. Store raw EODHD payloads as immutable compressed objects/files; do not store one giant raw JSON string per tick in a Delta row.

```text
bronze.eodhd_tick_capture
bronze.eodhd_fundamentals_capture
bronze.eodhd_exchange_symbols_capture
bronze.eodhd_delisted_symbols_capture
bronze.eodhd_symbol_change_capture
bronze.eodhd_index_components_capture
bronze.eodhd_exchange_details_capture
bronze.eodhd_corporate_actions_capture
bronze.openfigi_mapping_capture
bronze.sec_submissions_capture
```

### 6.2 Silver tables

```text
silver.issuer
silver.registrant
silver.instrument
silver.listing_version
silver.venue_reference_version
silver.identifier_assignment_version
silver.figi_assignment_version
silver.instrument_event_version
silver.instrument_relationship_version
silver.index_membership_version
silver.us_trade_tick_version
silver.feed_coverage
silver.data_availability_policy
```

### 6.3 Gold tables

```text
gold.us_trade_tick_by_venue
gold.us_trade_tape
gold.eod_bar_current
gold.sp500_universe_snapshot
gold.selected_tick_version
gold.selected_bar_version
gold.backtest_input_bar
gold.backtest_input_tick
gold.instrument_attributes_pit
```

### 6.4 Operations and audit tables

```text
ops.tick_download_manifest
ops.ingestion_run
ops.openfigi_resolution_queue
ops.data_quality_issue
ops.backtest_run_manifest
```

---

## 7. Table Schemas

The following schemas are logical baselines. Adapt types to the Delta writer in use, but preserve field meaning and append-only semantics.

### 7.1 `bronze.eodhd_tick_capture`

One row per EODHD request/page/capture.

```sql
CREATE TABLE bronze.eodhd_tick_capture (
  capture_id STRING NOT NULL,
  ingestion_run_id STRING NOT NULL,

  source_system STRING NOT NULL,          -- EODHD
  endpoint STRING NOT NULL,
  parser_version STRING NOT NULL,

  request_symbol STRING NOT NULL,
  request_exchange_code STRING,
  request_from_sec BIGINT NOT NULL,
  request_to_sec BIGINT NOT NULL,
  request_limit INTEGER,
  request_cursor STRING,
  request_parameters_json STRING NOT NULL,

  http_status INTEGER NOT NULL,
  response_headers_json STRING,
  raw_payload_uri STRING NOT NULL,
  raw_payload_sha256 STRING NOT NULL,
  raw_row_count BIGINT,
  first_source_ts_ms BIGINT,
  last_source_ts_ms BIGINT,

  observed_at_ts TIMESTAMP NOT NULL,
  committed_at_ts TIMESTAMP NOT NULL,
  created_at_ts TIMESTAMP NOT NULL
)
USING DELTA
PARTITIONED BY (date(committed_at_ts));
```

### 7.2 `silver.instrument`

One durable internal record per economic security/share class.

```sql
CREATE TABLE silver.instrument (
  instrument_id STRING NOT NULL,
  issuer_id STRING,
  registrant_id STRING,

  instrument_type STRING NOT NULL,
  -- COMMON_STOCK, ETF, ADR, PREFERRED, RIGHT, WARRANT, SPINOFF, etc.

  share_class STRING,
  country_of_incorporation STRING,
  primary_currency STRING,

  created_at_ts TIMESTAMP NOT NULL,
  retired_at_ts TIMESTAMP,
  status STRING NOT NULL,
  source_system STRING
)
USING DELTA;
```

### 7.3 `silver.listing_version`

One bitemporal listing/venue representation version.

```sql
CREATE TABLE silver.listing_version (
  listing_version_id STRING NOT NULL,
  listing_id STRING NOT NULL,
  instrument_id STRING NOT NULL,

  ticker STRING,
  eodhd_symbol STRING,
  eodhd_exchange_code STRING,

  venue_id STRING,
  mic STRING,
  operating_mic STRING,
  exchange_name STRING,
  listing_currency STRING,

  venue_figi STRING,
  composite_figi STRING,
  share_class_figi STRING,

  price_venue_type STRING NOT NULL,
  -- EXCHANGE_SPECIFIC, CONSOLIDATED_US, OTC, COMPOSITE, UNKNOWN

  effective_from_ts TIMESTAMP,
  effective_to_ts TIMESTAMP,

  observed_at_ts TIMESTAMP NOT NULL,
  available_at_ts TIMESTAMP NOT NULL,
  system_from_ts TIMESTAMP NOT NULL,
  system_to_ts TIMESTAMP,

  source_system STRING NOT NULL,
  source_capture_id STRING NOT NULL,
  confidence DECIMAL(5,4) NOT NULL,
  verification_status STRING NOT NULL
)
USING DELTA;
```

### 7.4 `silver.identifier_assignment_version`

All external identifiers are retained as time-bounded assignments.

```sql
CREATE TABLE silver.identifier_assignment_version (
  assignment_version_id STRING NOT NULL,

  issuer_id STRING,
  registrant_id STRING,
  instrument_id STRING,
  listing_id STRING,

  id_namespace STRING NOT NULL,
  -- EODHD_SYMBOL, TICKER, SEC_CIK, CUSIP, ISIN,
  -- FIGI, COMPOSITE_FIGI, SHARE_CLASS_FIGI, MIC, LEI

  id_value STRING NOT NULL,
  normalized_id_value STRING NOT NULL,

  effective_from_ts TIMESTAMP,
  effective_to_ts TIMESTAMP,

  observed_at_ts TIMESTAMP NOT NULL,
  available_at_ts TIMESTAMP NOT NULL,
  system_from_ts TIMESTAMP NOT NULL,
  system_to_ts TIMESTAMP,

  source_system STRING NOT NULL,
  source_capture_id STRING NOT NULL,
  verification_status STRING NOT NULL,
  confidence DECIMAL(5,4) NOT NULL,
  supersedes_assignment_version_id STRING
)
USING DELTA;
```

### 7.5 `silver.figi_assignment_version`

Use a dedicated table if FIGI resolution becomes material enough to warrant an explicit model. It may alternatively be represented exclusively in `identifier_assignment_version`; do not duplicate without a defined derivation relationship.

```sql
CREATE TABLE silver.figi_assignment_version (
  figi_assignment_version_id STRING NOT NULL,
  instrument_id STRING,
  listing_id STRING,

  figi_level STRING NOT NULL,
  -- VENUE, COMPOSITE, SHARE_CLASS

  figi STRING NOT NULL,

  effective_from_ts TIMESTAMP,
  effective_to_ts TIMESTAMP,

  observed_at_ts TIMESTAMP NOT NULL,
  available_at_ts TIMESTAMP NOT NULL,
  system_from_ts TIMESTAMP NOT NULL,
  system_to_ts TIMESTAMP,

  source_system STRING NOT NULL,          -- OPENFIGI
  source_capture_id STRING NOT NULL,
  match_status STRING NOT NULL,
  confidence DECIMAL(5,4) NOT NULL
)
USING DELTA;
```

### 7.6 `silver.instrument_event_version`

Corporate actions and identity-relevant events.

```sql
CREATE TABLE silver.instrument_event_version (
  event_id STRING NOT NULL,
  instrument_id STRING NOT NULL,
  related_instrument_id STRING,
  listing_id STRING,

  event_type STRING NOT NULL,
  -- NAME_CHANGE, TICKER_CHANGE, SPLIT, REVERSE_SPLIT,
  -- CASH_ACQUISITION, STOCK_ACQUISITION, SPIN_OFF,
  -- RIGHTS_ISSUE, DELISTING, EXCHANGE_MIGRATION

  announced_at_ts TIMESTAMP,
  effective_at_ts TIMESTAMP,
  settlement_at_ts TIMESTAMP,

  ratio DECIMAL(30,15),
  cash_consideration DECIMAL(30,15),
  currency STRING,
  event_details_json STRING,

  observed_at_ts TIMESTAMP NOT NULL,
  available_at_ts TIMESTAMP NOT NULL,
  system_from_ts TIMESTAMP NOT NULL,
  system_to_ts TIMESTAMP,

  source_system STRING NOT NULL,
  source_capture_id STRING NOT NULL,
  confidence DECIMAL(5,4) NOT NULL
)
USING DELTA;
```

### 7.7 `silver.instrument_relationship_version`

Do not encode merger/spin-off/cross-listing relationships as identifier replacements.

```sql
CREATE TABLE silver.instrument_relationship_version (
  relationship_id STRING NOT NULL,
  from_instrument_id STRING NOT NULL,
  to_instrument_id STRING NOT NULL,

  relationship_type STRING NOT NULL,
  -- TICKER_RENAME_SAME_INSTRUMENT, ACQUIRED_FOR_CASH,
  -- ACQUIRED_FOR_STOCK, EXCHANGED_FOR, SPUN_OFF_FROM,
  -- ADR_OF, SHARE_CLASS_OF_SAME_ISSUER, CROSS_LISTED_AS,
  -- SUCCESSOR_AFTER_REORGANIZATION

  effective_at_ts TIMESTAMP,
  exchange_ratio DECIMAL(30,15),
  details_json STRING,

  observed_at_ts TIMESTAMP NOT NULL,
  available_at_ts TIMESTAMP NOT NULL,
  system_from_ts TIMESTAMP NOT NULL,
  system_to_ts TIMESTAMP,

  source_system STRING NOT NULL,
  source_capture_id STRING,
  confidence DECIMAL(5,4) NOT NULL,
  review_status STRING NOT NULL
)
USING DELTA;
```

### 7.8 `silver.index_membership_version`

This is the core point-in-time S&P 500 universe table.

```sql
CREATE TABLE silver.index_membership_version (
  membership_version_id STRING NOT NULL,

  index_id STRING NOT NULL,               -- idx_sp500
  index_vendor_symbol STRING NOT NULL,     -- GSPC.INDX

  listing_id STRING,
  instrument_id STRING,

  source_constituent_code STRING NOT NULL,
  source_exchange_code STRING,
  source_name STRING,

  membership_effective_from DATE NOT NULL,
  membership_effective_to DATE,

  inclusion_reason STRING,
  exclusion_reason STRING,
  index_weight DECIMAL(20,12),

  observed_at_ts TIMESTAMP NOT NULL,
  available_at_ts TIMESTAMP NOT NULL,
  system_from_ts TIMESTAMP NOT NULL,
  system_to_ts TIMESTAMP,

  source_system STRING NOT NULL,           -- EODHD
  source_capture_id STRING NOT NULL,
  resolution_status STRING NOT NULL,
  -- RESOLVED, AMBIGUOUS, UNRESOLVED

  confidence DECIMAL(5,4) NOT NULL,
  supersedes_membership_version_id STRING
)
USING DELTA;
```

### 7.9 `silver.venue_reference_version`

Normalize raw venue values only through an explicit versioned reference mapping.

```sql
CREATE TABLE silver.venue_reference_version (
  venue_reference_version_id STRING NOT NULL,
  venue_id STRING NOT NULL,

  vendor_venue_code STRING,
  mic STRING,
  operating_mic STRING,
  venue_name STRING,
  venue_type STRING,
  -- EXCHANGE, ATS, TRF, OTC, CONSOLIDATED, UNKNOWN

  country_code STRING,
  timezone STRING,

  effective_from_ts TIMESTAMP,
  effective_to_ts TIMESTAMP,

  observed_at_ts TIMESTAMP NOT NULL,
  available_at_ts TIMESTAMP NOT NULL,
  system_from_ts TIMESTAMP NOT NULL,
  system_to_ts TIMESTAMP,

  source_system STRING NOT NULL,
  source_capture_id STRING NOT NULL,
  confidence DECIMAL(5,4) NOT NULL
)
USING DELTA;
```

### 7.10 `silver.us_trade_tick_version`

This is the canonical normalized trade-level table. Keep source values, identity resolution, timing, sequence provenance, and revision lineage.

```sql
CREATE TABLE silver.us_trade_tick_version (
  tick_version_id STRING NOT NULL,
  logical_tick_id STRING NOT NULL,

  source_system STRING NOT NULL,           -- EODHD
  source_capture_id STRING NOT NULL,
  source_page_ordinal BIGINT,
  source_row_ordinal BIGINT NOT NULL,

  listing_id STRING,
  instrument_id STRING,

  vendor_request_symbol STRING NOT NULL,
  eodhd_exchange_code STRING,

  trade_date DATE NOT NULL,
  trade_ts_ms BIGINT NOT NULL,
  trade_ts TIMESTAMP NOT NULL,

  venue_code_raw STRING,
  venue_id STRING,
  mic STRING,
  price_venue_type STRING NOT NULL,
  -- EXCHANGE_SPECIFIC, CONSOLIDATED_US, OTC, UNKNOWN

  price DECIMAL(24,10) NOT NULL,
  size BIGINT,
  sale_condition_raw STRING,
  sale_condition_flags ARRAY<STRING>,

  vendor_trade_id STRING,
  vendor_sequence_no BIGINT,

  observed_at_ts TIMESTAMP NOT NULL,
  committed_at_ts TIMESTAMP NOT NULL,
  available_at_ts TIMESTAMP NOT NULL,

  revision_number INTEGER NOT NULL,
  is_correction BOOLEAN NOT NULL,
  supersedes_tick_version_id STRING,

  payload_hash STRING NOT NULL,
  record_hash STRING NOT NULL,
  parser_version STRING NOT NULL
)
USING DELTA
PARTITIONED BY (trade_date);
```

### 7.11 `silver.feed_coverage`

Track expected and received feed state separately from the tick facts.

```sql
CREATE TABLE silver.feed_coverage (
  feed_coverage_id STRING NOT NULL,
  source_system STRING NOT NULL,

  market_code STRING NOT NULL,
  trade_date DATE NOT NULL,
  listing_id STRING,
  vendor_request_symbol STRING,

  expected_by_ts TIMESTAMP,
  first_observed_at_ts TIMESTAMP,
  complete_observed_at_ts TIMESTAMP,
  committed_at_ts TIMESTAMP,

  expected_tick_count BIGINT,
  received_tick_count BIGINT,
  first_tick_ts_ms BIGINT,
  last_tick_ts_ms BIGINT,

  feed_status STRING NOT NULL,
  -- PENDING, PARTIAL, COMPLETE, LATE_COMPLETE, FAILED, HOLIDAY, UNKNOWN

  source_capture_id STRING,
  ingestion_run_id STRING,
  details_json STRING
)
USING DELTA
PARTITIONED BY (trade_date);
```

### 7.12 `ops.tick_download_manifest`

This drives the ten-year historical backfill and makes missing work visible.

```sql
CREATE TABLE ops.tick_download_manifest (
  work_id STRING NOT NULL,
  trade_date DATE NOT NULL,

  listing_id STRING NOT NULL,
  instrument_id STRING,
  vendor_symbol_at_date STRING NOT NULL,
  eodhd_exchange_code STRING,

  request_from_sec BIGINT NOT NULL,
  request_to_sec BIGINT NOT NULL,

  membership_snapshot_id STRING NOT NULL,
  identity_snapshot_id STRING NOT NULL,
  availability_policy_version STRING NOT NULL,

  status STRING NOT NULL,
  -- PENDING, IN_PROGRESS, COMPLETE, PARTIAL, RETRY, FAILED, SKIPPED, NOT_AVAILABLE

  attempts INTEGER NOT NULL,
  priority INTEGER NOT NULL,
  completed_at_ts TIMESTAMP,
  last_error STRING,
  created_at_ts TIMESTAMP NOT NULL,
  updated_at_ts TIMESTAMP NOT NULL
)
USING DELTA
PARTITIONED BY (trade_date);
```

### 7.13 `ops.backtest_run_manifest`

Every research run must be reproducible.

```sql
CREATE TABLE ops.backtest_run_manifest (
  run_id STRING NOT NULL,
  run_name STRING,

  strategy_git_commit STRING NOT NULL,
  strategy_config_json STRING NOT NULL,
  strategy_config_hash STRING NOT NULL,

  research_mode STRING NOT NULL,
  availability_policy_version STRING NOT NULL,
  decision_schedule_version STRING NOT NULL,
  execution_policy_version STRING NOT NULL,
  universe_policy_version STRING NOT NULL,

  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  knowledge_cutoff_ts TIMESTAMP,

  input_delta_versions_json STRING NOT NULL,
  input_snapshot_ids_json STRING,
  query_hash STRING,
  output_hash STRING,

  created_at_ts TIMESTAMP NOT NULL,
  completed_at_ts TIMESTAMP,
  status STRING NOT NULL
)
USING DELTA;
```

---

## 8. S&P 500 Membership Pipeline

### 8.1 Objective

Produce a resolved, bitemporal membership history for `idx_sp500` from EODHD historical constituent data.

### 8.2 Required input captures

Capture and retain:

```text
GSPC.INDX historical component response
Current component response
Historical snapshot/change-date responses, if available
EODHD constituent metadata/name fields
EODHD symbol lists, including delisted symbols
EODHD fundamentals for all unique historical constituents
EODHD US symbol-change history
```

### 8.3 Membership ingestion rules

1. Ingest raw membership payloads into `bronze.eodhd_index_components_capture`.
2. Parse each vendor membership interval or snapshot.
3. Resolve the vendor constituent code to a dated `listing_id` using the temporal identifier map.
4. Write `silver.index_membership_version` records.
5. Preserve unresolved and ambiguous mappings. Never choose a ticker match silently.
6. Validate no overlapping resolved membership intervals exist for the same `(index_id, listing_id)` unless a source-specific exception is documented.
7. Validate intervals with snapshot/change-date data where available.
8. Retain source membership text and raw symbol even after resolution.

### 8.4 Point-in-time membership query

Conceptual DuckDB macro:

```sql
CREATE OR REPLACE MACRO pit_sp500_membership(
  market_date,
  decision_ts
) AS TABLE
SELECT DISTINCT
  m.listing_id,
  m.instrument_id
FROM silver.index_membership_version AS m
WHERE m.index_id = 'idx_sp500'
  AND m.membership_effective_from <= market_date
  AND (
    m.membership_effective_to IS NULL
    OR m.membership_effective_to > market_date
  )
  AND m.available_at_ts <= decision_ts
  AND (
    m.system_to_ts IS NULL
    OR m.system_to_ts > decision_ts
  )
  AND m.resolution_status = 'RESOLVED';
```

### 8.5 Membership boundary policy

Document an explicit policy for whether an EODHD join/leave date means:

```text
- effective at prior session close,
- effective at next regular session open,
- effective at session close,
- or another vendor-defined index-rebalance boundary.
```

Do not leave this implicit. The policy should be versioned and included in every backtest manifest.

---

## 9. Identifier and Corporate-Action Rules

### 9.1 Ticker and name changes

For a pure name or ticker change affecting the same security:

```text
Same instrument_id
Same listing_id
Same FIGI where source confirms it
Old and new ticker/EODHD symbols are separate dated mappings
Historical raw ticks retain original source symbol
```

### 9.2 CUSIP, ISIN, FIGI, and CIK

Use each for the scope it actually identifies:

```text
SEC CIK:
  Attach to registrant_id; strong EDGAR/issuer bridge; not a security key.

CUSIP / ISIN:
  Attach to instrument_id as issue-level evidence; retain time intervals.

Share-class FIGI:
  Attach to instrument_id where resolved.

Composite FIGI:
  Attach to market/country aggregate context for an instrument/listing.

Venue FIGI:
  Attach to listing_id; strong cross-vendor listing identity evidence.
```

### 9.3 Corporate action handling

| Event | Identity treatment | Price-series treatment |
|---|---|---|
| Name change | Same instrument/listing | No stitching required |
| Ticker change | Same instrument/listing | No stitching required; preserve old source symbol |
| Split/reverse split | Same instrument/listing | Preserve raw ticks; version adjustment factors separately |
| Cash acquisition | Target ends; acquirer remains distinct | Do not concatenate target and acquirer prices |
| Stock acquisition | Target and acquirer/successor distinct | Record conversion ratio; no automatic concatenation |
| Spin-off | Parent and child distinct | Link by event; model child separately |
| New share class | New instrument | Separate history and identifiers |
| Cross-listing | Separate listing(s), possibly same instrument | Keep price series separate |
| Venue migration | New listing; same instrument only if validated | Preserve venue-specific raw series |

### 9.4 Matching hierarchy

Automatic resolution should proceed only through strong evidence:

```text
1. Exact historical internal mapping for EODHD symbol.
2. Exact CUSIP/ISIN plus compatible instrument type and effective dates.
3. Exact FIGI mapping with compatible listing/venue context.
4. Documented EODHD symbol-change history.
5. CIK plus compatible share-class/security attributes.
6. Name/ticker/exchange candidate generation only.
```

Steps 5–6 should normally require manual review before merging records.

---

## 10. Tick Ingestion and Normalization

### 10.1 Backfill workflow

1. Build historical S&P 500 membership intervals.
2. Build or enrich the temporal security master for every unique historical member.
3. Generate `ops.tick_download_manifest` rows for each eligible `(trade_date, listing_id)`.
4. Resolve the correct vendor symbol **as of the trade date**, not today’s symbol.
5. Download EODHD tick data in bounded windows/pages.
6. Persist every raw response and request metadata in bronze.
7. Parse and normalize tick records into silver.
8. Deduplicate overlapping pagination results deterministically.
9. Resolve raw venue code through `silver.venue_reference_version`.
10. Append normalized tick versions; never overwrite a previously stored version.
11. Update feed coverage and validation status.
12. Materialize gold venue and consolidated tape layouts.

### 10.2 Capture rules

For each HTTP/API request:

- Store request parameters exactly.
- Store request start/end in source units and normalized UTC timestamps.
- Store full response headers when feasible.
- Store raw payload checksum.
- Store row count and first/last observed source timestamp.
- Store parser version.
- Store response status and error body/checksum for failures.
- Make retries new captures, even if request parameters are identical.

### 10.3 Timestamp handling

- Preserve vendor timestamp as `trade_ts_ms BIGINT`.
- Derive `trade_ts TIMESTAMP` from it.
- Derive `trade_date` using the US market session calendar and `America/New_York`.
- Never convert a tick timestamp to local time and discard the original UTC representation.
- Treat pre-market/after-hours prints as separate session classifications if source timestamps permit.

### 10.4 Pagination and inclusive boundaries

The downloader must be designed for potentially inclusive API window boundaries.

Recommended approach:

```text
- Use intentionally overlapping request windows when exact millisecond cursoring is unavailable.
- Preserve every raw page.
- Normalize all records.
- Deduplicate only in silver through a defined logical-tick identity rule.
- Retain capture/page/row lineage even when a duplicate is suppressed from gold.
```

### 10.5 Tick identity and deduplication

Preferred logical tick key:

```text
source-provided trade ID or source-provided sequence number, if available
```

Fallback key:

```text
hash(
  source_system,
  vendor_request_symbol,
  trade_ts_ms,
  venue_code_raw,
  price,
  size,
  sale_condition_raw,
  source-specific stable fields
)
```

Do not use only timestamp + price + size: multiple legitimate executions can share those values.

When two source records have the same fallback key but differ in non-key fields:

```text
- preserve both raw captures,
- create a data-quality issue,
- avoid silently collapsing the records,
- use an explicit correction/conflict rule.
```

### 10.6 Tick corrections

If the source provides corrections or a re-pull yields a changed logical tick:

```text
- insert revision_number = prior revision + 1,
- set is_correction = true,
- populate supersedes_tick_version_id,
- preserve prior tick version,
- let current views select latest revision,
- let PIT views select the version available at decision time.
```

---

## 11. Venue Model and Price Mapping

### 11.1 Rule

A price/trade belongs to a **listing + venue context**, not merely an issuer or ticker.

```text
Tick
  → vendor request symbol
  → listing_id
  → raw venue code
  → venue_id / MIC, when resolvable
  → instrument_id
```

### 11.2 Raw and normalized venue values

Always preserve:

```text
venue_code_raw
```

Only add:

```text
venue_id
mic
operating_mic
```

when a source-supported, versioned mapping exists. Do not invent a NASDAQ MIC just because a security’s primary listing is NASDAQ.

### 11.3 Price venue types

Use a constrained vocabulary:

```text
EXCHANGE_SPECIFIC
CONSOLIDATED_US
OTC
TRF
ATS
COMPOSITE
UNKNOWN
```

For every EODHD dataset, document whether its price represents a specific venue, consolidated US activity, or another vendor-defined aggregation.

---

## 12. Sequencing and Consolidated Replay

### 12.1 Sequence definitions

| Field | Meaning | Authority |
|---|---|---|
| `vendor_sequence_no` | Sequence supplied by EODHD/source | Source-authoritative if present |
| `source_page_ordinal` | Page/order in one API response/request chain | Pipeline provenance |
| `source_row_ordinal` | Original row position in source page | Pipeline provenance |
| `ingest_sequence_no` | Monotonic local ingestion order, if needed | Audit only |
| `synthetic_tape_seq` | Deterministic total ordering built by this system | Internal replay only |
| Official SIP/CTA/UTP sequence | Consolidated market-data dissemination order | Only valid if explicitly supplied by source |

### 12.2 Required rule

Never label `synthetic_tape_seq` as an official consolidated tape sequence number.

### 12.3 Synthetic ordering policy

Define and version a total ordering policy. Initial policy:

```text
EODHD_TS_MS_THEN_SOURCE_ORDER_V1

ORDER BY
  trade_ts_ms,
  vendor_sequence_no present before null,
  vendor_sequence_no,
  venue_priority,
  source_capture_observed_at_ts,
  source_page_ordinal,
  source_row_ordinal,
  tick_version_id
```

`venue_priority` must be an explicitly versioned lookup. It exists only as a deterministic tie-breaker, not as a claim of real market ordering across venues.

### 12.4 `gold.us_trade_tape`

Materialize a consolidated replay table after the full listing/session tick set has been assembled.

```sql
CREATE TABLE gold.us_trade_tape (
  trade_date DATE NOT NULL,
  listing_id STRING NOT NULL,
  instrument_id STRING,

  synthetic_tape_seq BIGINT NOT NULL,
  sequence_method STRING NOT NULL,
  sequence_policy_version STRING NOT NULL,

  trade_ts_ms BIGINT NOT NULL,
  trade_ts TIMESTAMP NOT NULL,
  venue_id STRING,
  mic STRING,
  venue_code_raw STRING,

  price DECIMAL(24,10) NOT NULL,
  size BIGINT,
  sale_condition_raw STRING,
  sale_condition_flags ARRAY<STRING>,

  logical_tick_id STRING NOT NULL,
  tick_version_id STRING NOT NULL,
  source_capture_id STRING NOT NULL,
  available_at_ts TIMESTAMP NOT NULL
)
USING DELTA
PARTITIONED BY (trade_date);
```

### 12.5 `gold.us_trade_tick_by_venue`

Materialize a venue-oriented layout from the same selected silver tick versions.

```sql
CREATE TABLE gold.us_trade_tick_by_venue (
  trade_date DATE NOT NULL,
  venue_id STRING,
  mic STRING,
  listing_id STRING NOT NULL,
  instrument_id STRING,

  trade_ts_ms BIGINT NOT NULL,
  trade_ts TIMESTAMP NOT NULL,
  price DECIMAL(24,10) NOT NULL,
  size BIGINT,
  sale_condition_raw STRING,

  logical_tick_id STRING NOT NULL,
  tick_version_id STRING NOT NULL,
  source_capture_id STRING NOT NULL,
  available_at_ts TIMESTAMP NOT NULL
)
USING DELTA
PARTITIONED BY (trade_date);
```

---

## 13. Point-in-Time Backtesting

### 13.1 Research modes

A backtest must specify whether it is evaluating:

```text
A. Historical archival market replay
B. As-ingested local-system replay
C. Current corrected historical research
D. Forward/live production simulation
```

Do not mix these modes in one result without explicit labels.

### 13.2 Point-in-time universe selection

Resolve index membership once per decision date or rebalance date:

```sql
CREATE TEMP TABLE universe AS
SELECT listing_id, instrument_id
FROM pit_sp500_membership(
  DATE '2021-11-05',
  TIMESTAMP '2021-11-05 13:30:00+00:00'
);
```

Do not join bitemporal membership logic to every tick row across billions of events.

### 13.3 Point-in-time tick selection

Conceptual selection logic:

```sql
CREATE OR REPLACE MACRO pit_tick_versions(as_of_ts) AS TABLE
SELECT *
FROM (
  SELECT
    t.*,
    row_number() OVER (
      PARTITION BY t.logical_tick_id
      ORDER BY
        t.available_at_ts DESC,
        t.committed_at_ts DESC,
        t.revision_number DESC
    ) AS rn
  FROM silver.us_trade_tick_version AS t
  WHERE t.available_at_ts <= as_of_ts
) AS x
WHERE rn = 1;
```

For repeated simulations, do not execute this broad window repeatedly. Materialize `gold.selected_tick_version` by availability snapshot or per run.

### 13.4 Trade eligibility

A strategy must independently enforce:

```text
- Tick/bar was available by decision timestamp.
- Relevant membership was effective and known by decision timestamp.
- Identifier/listing mapping was effective and known by decision timestamp.
- Strategy uses a documented execution rule.
- Execution price occurs after the signal was available.
- Feed completeness policy allows the trade.
```

### 13.5 Backtest input snapshot

For a material strategy run, materialize a frozen input product:

```text
gold.backtest_input_tick
  selected tick version IDs
  selected membership mappings
  selected identifier/listing mappings
  selected corporate-action events
  availability policy version
  sequence policy version
  universe policy version
```

Record all source Delta versions and output hashes in `ops.backtest_run_manifest`.

---

## 14. Physical Layout and Performance

### 14.1 Partitioning

For tick facts:

```text
Partition by trade_date.
```

For a large implementation, retain year/month/day directory partitioning if your Delta writer and query tooling benefit from it, but retain `trade_date` in the table schema.

Do **not** partition by:

```text
ticker
instrument_id
listing_id
CUSIP
FIGI
venue_id
```

These are high-cardinality dimensions and can create excessive small files/partitions.

### 14.2 Clustering/sorting

Recommended physical ordering:

```text
silver.us_trade_tick_version:
  listing_id, trade_ts_ms, venue_id

gold.us_trade_tape:
  listing_id, trade_ts_ms, synthetic_tape_seq

gold.us_trade_tick_by_venue:
  venue_id, listing_id, trade_ts_ms
```

Apply the available Delta optimization mechanism compatible with the selected writer/reader stack. Validate that DuckDB reads the generated Delta features correctly before depending on a particular optimization feature.

### 14.3 File hygiene

Do not create one Parquet/Delta data file per API request or per symbol/day.

Recommended write pattern:

```text
Raw payload: one immutable compressed file per request/page is acceptable.
Silver/gold Delta facts: batch many symbols/day into reasonably large writes.
Compaction: run after a day/backfill range is stable.
```

Start with a benchmarked target file-size range rather than a fixed assumption. Measure with representative local NVMe and DuckDB workloads.

### 14.4 Gold-table denormalization

Include commonly filtered/joined dimensions directly in gold facts:

```text
listing_id
instrument_id
trade_date
trade_ts_ms
venue_id
mic
vendor symbol
price venue type
availability timestamp
source capture ID
tick/bar version ID
```

The silver security master remains authoritative. Gold values are an explicitly selected/materialized convenience projection.

### 14.5 DuckDB query rules

- Filter on `trade_date` first.
- Filter on `listing_id`/`instrument_id` early.
- Select only needed columns.
- Resolve the PIT universe once into a temporary relation.
- Attach or pin Delta versions once per backtest, not once per simulated decision date.
- Materialize PIT selections outside the strategy’s inner loop.
- Use `EXPLAIN ANALYZE` against representative date ranges and universes before changing physical layout.

---

## 15. Delta Time Travel and Retention

### 15.1 Two reproducibility safeguards

Use both:

```text
Logical reproducibility:
  available_at_ts / system-time predicates select the data permitted at a decision time.

Physical reproducibility:
  Each backtest manifest pins exact Delta table versions.
```

### 15.2 Retention policy

Delta time travel only works while older Delta log entries and referenced files are retained.

For audit/research-critical tables:

```text
Bronze raw captures:
  Retain indefinitely.

Silver temporal facts:
  Long log and data-file retention.

Gold backtest inputs:
  Retain for material research results or regenerate from pinned silver versions.

VACUUM:
  Never run with aggressive retention settings on research-critical tables.
```

Before any destructive retention operation, verify that required historical table versions remain reproducible or have been snapshotted/exported according to policy.

### 15.3 Delta version manifest

Record exact versions for at least:

```text
silver.us_trade_tick_version
silver.index_membership_version
silver.listing_version
silver.identifier_assignment_version
silver.instrument_event_version
silver.feed_coverage
silver.data_availability_policy
gold selection/input tables, if used
```

---

## 16. Data Quality Controls

### 16.1 Daily universe checks

```text
- Historical member count by trade date.
- Number of resolved vs ambiguous vs unresolved membership records.
- Overlapping membership intervals.
- Membership records with no effective listing mapping.
- Ticker reuse collisions.
```

### 16.2 Tick coverage checks

```text
- Expected jobs vs completed jobs from tick_download_manifest.
- First/last tick timestamp per listing/date.
- Tick count and volume by listing/date/venue.
- Missing regular-session dates.
- Partial/failed feed coverage.
- Out-of-session trades.
- Timestamp monotonicity within a source capture/page.
- Same-millisecond collision rates.
- Duplicate logical_tick_id counts.
- Conflicting duplicate fallback-key records.
- Unknown raw venue-code counts.
```

### 16.3 Reconciliation checks

Aggregate ticks into daily OHLCV and compare with EODHD EOD/intraday data using a documented inclusion policy:

```text
- regular session only vs all sessions,
- eligible sale conditions,
- price correction handling,
- odd-lot handling,
- venue/consolidated scope.
```

A mismatch is a diagnostic, not automatically an error.

### 16.4 Corporate-action checks

```text
- Large discontinuities flagged around split/reverse-split dates.
- Symbol transition has valid dated mappings.
- Delisted constituent retains historical mapping.
- No target/acquirer price-history merge without an explicit policy.
- Share-class identity remains distinct.
```

---

## 17. Ingestion Run Contract

Every pipeline run creates an immutable `ops.ingestion_run` record:

```text
run_id
run_type
code_git_commit
config_hash
started_at_ts
completed_at_ts
status
source endpoint/version
parser version
availability policy version
input manifest range
output Delta table versions
quality-check summary
error summary
```

All bronze captures and silver/gold writes must carry `ingestion_run_id` or a traceable parent reference.

---

## 18. Implementation Phases

### Phase 0: Establish source contracts

- Confirm the exact EODHD Tick API response schema from real licensed responses.
- Confirm pagination/cursor semantics, inclusive boundaries, rate limits, retention, and correction behavior.
- Confirm whether EODHD supplies a source trade ID or sequence number.
- Confirm the semantics of venue and sale-condition fields.
- Confirm S&P 500 historical component endpoint fields and effective-date semantics.
- Confirm entitlement/licensing restrictions for price, CUSIP, ISIN, FIGI, and redistribution.

### Phase 1: Security master and membership

- Implement internal ID allocation.
- Implement bronze capture framework.
- Ingest S&P 500 historical components.
- Build `index_membership_version`.
- Ingest EODHD exchange symbol lists, delisted lists, fundamentals, and symbol-change history.
- Implement temporal EODHD symbol-to-listing resolution.
- Add CIK/CUSIP/ISIN/FIGI mapping capture and review workflow.
- Build unresolved/ambiguous review queues.

### Phase 2: Tick backfill engine

- Generate historical `tick_download_manifest` from membership + symbol history.
- Implement idempotent downloader with raw capture persistence.
- Implement retry, rate-limit, and failure handling.
- Normalize timestamp, venue, conditions, and source row ordinal.
- Implement logical tick identity and overlap deduplication.
- Populate `feed_coverage`.

### Phase 3: Gold projections and replay

- Materialize venue-oriented tick layout.
- Materialize consolidated deterministic tape layout.
- Version synthetic ordering policy.
- Build daily OHLCV aggregation/reconciliation.
- Benchmark DuckDB scans and tune physical layout.

### Phase 4: Point-in-time backtest framework

- Implement PIT membership resolution.
- Implement PIT identifier/listing resolution.
- Implement PIT tick/bar selection or snapshot materialization.
- Implement backtest run manifest.
- Pin Delta versions.
- Test identical reruns.

### Phase 5: Forward/live operations

- Begin actual scheduled captures.
- Use actual observed/validated/available timestamps.
- Detect delayed/partial feed conditions.
- Enforce production availability policies.
- Maintain long-term retention and periodic restore/replay tests.

---

## 19. Acceptance Tests

The implementation is not complete until these cases pass.

### 19.1 Membership tests

- A current constituent absent from the S&P 500 in 2017 is not present in a 2017 universe query.
- A former constituent acquired/delisted after membership end remains present for dates within its tenure.
- A ticker reused by a later issuer resolves to distinct historical `listing_id` values.
- A membership source record with unresolved ticker identity is visible as unresolved and excluded or handled according to explicit policy.

### 19.2 Identifier tests

- A pure ticker rename resolves old and new vendor symbols to the same listing/instrument where validated.
- Two share classes with one CIK resolve to separate `instrument_id` values.
- A cash acquisition does not concatenate target and acquirer price history.
- A venue migration retains venue-specific history and does not overwrite old venue data.

### 19.3 Tick tests

- Re-downloading an overlapping window does not create duplicate gold ticks.
- Every gold tick can trace to a bronze capture and source row ordinal.
- Two valid same-millisecond, same-price, same-size trades are not collapsed solely due to matching fields.
- Consolidated replay order is identical across repeated materializations with the same input Delta versions and policy.
- Synthetic ordering is clearly labeled and never exposed as official SIP/CTA sequencing.

### 19.4 Point-in-time tests

- A correction captured after `decision_ts` is absent from a PIT query at `decision_ts` and present in a current query.
- A delayed feed is unavailable before its `available_at_ts`.
- A future identifier mapping cannot resolve a past simulated trade when using PIT logic.
- A historical rerun with pinned Delta versions and the same configuration produces the same input selection and output hash.

### 19.5 Performance tests

- A one-day, one-listing tick replay prunes to the expected date partition and completes within the defined benchmark.
- A month of S&P 500 venue-level analysis completes within the defined benchmark.
- A full-universe daily membership resolution is performed once per decision date, not per tick.
- Gold-table scan plans avoid scanning bronze payload tables and avoid broad silver version-window computation in the strategy inner loop.

---

## 20. Recommended Initial Defaults

```text
Index:
  idx_sp500
  EODHD vendor symbol: GSPC.INDX

Canonical IDs:
  UUID or UUIDv7-style strings for issuer_id, registrant_id, instrument_id, listing_id, venue_id

Stored time zone:
  UTC

US session date:
  America/New_York exchange calendar

Tick timestamp:
  trade_ts_ms BIGINT is authoritative

Raw source retention:
  Indefinite

Silver version policy:
  Append-only; corrections create new versions

Gold tape sequence policy:
  EODHD_TS_MS_THEN_SOURCE_ORDER_V1

Default price venue policy:
  Preserve raw venue code; classify only when reference mapping is sourced and versioned

Backtest default:
  Materialized PIT snapshot + pinned Delta versions + explicit availability policy

Partitioning:
  Tick facts by trade_date

Clustering/sorting:
  tape: listing_id, trade_ts_ms, synthetic_tape_seq
  venue: venue_id, listing_id, trade_ts_ms
```

---

## 21. Development Checklist

### Security master

- [ ] Internal ID generator and allocation registry
- [ ] EODHD symbol/history capture
- [ ] Historical delisted-symbol capture
- [ ] Fundamentals capture and parser
- [ ] SEC CIK enrichment/capture
- [ ] FIGI mapping queue and cache
- [ ] Temporal identifier map
- [ ] Manual-review workflow for ambiguous matches
- [ ] Corporate-action and relationship tables

### Index membership

- [ ] Historical S&P 500 component capture
- [ ] Membership interval parser
- [ ] Change-date snapshot validation
- [ ] Membership effective-boundary policy
- [ ] PIT membership macro/materialization

### Tick lake

- [ ] Download manifest generator
- [ ] Rate-limit-safe downloader
- [ ] Raw immutable payload writer
- [ ] Source request/response audit metadata
- [ ] Timestamp and session-date normalization
- [ ] Venue normalization reference map
- [ ] Pagination-overlap deduplication
- [ ] Tick correction/version detection
- [ ] Feed completeness ledger

### Gold and backtesting

- [ ] Venue-optimized gold projection
- [ ] Consolidated replay gold projection
- [ ] Versioned synthetic ordering policy
- [ ] PIT selected-version materialization
- [ ] Delta-version-pinned DuckDB query setup
- [ ] Backtest run manifest
- [ ] Deterministic rerun tests
- [ ] Daily OHLCV reconciliation

### Operations

- [ ] Data-quality dashboards/reports
- [ ] Failed/backfill job retry process
- [ ] Delta retention/VACUUM policy
- [ ] Restore/time-travel test procedure
- [ ] Source licensing and redistribution review

---

## 22. Final Architectural Decisions

1. **Membership is a bitemporal fact**, not a current list of 500 tickers.
2. **Ticker is a dated listing attribute**, not a security key.
3. **Prices are tied to a listing and venue context**, not merely an issuer or ticker.
4. **Ticks retain vendor timestamps, raw venue fields, and capture provenance.**
5. **Venue storage and consolidated replay are separate optimized projections of the same normalized tick history.**
6. **Synthetic ordering is deterministic and auditable, but not an official market-data sequence.**
7. **Delayed feeds and corrections become new versions, never overwrites.**
8. **Backtest correctness uses both logical availability predicates and physically pinned Delta versions.**
9. **Silver preserves truth and lineage; gold preserves performance.**
10. **Every important result has a run manifest containing source versions, policies, strategy code version, and configuration hashes.**

This design is intentionally conservative: it makes uncertainty, delayed data, source limitations, and ambiguous security mappings visible rather than silently converting them into false historical precision.

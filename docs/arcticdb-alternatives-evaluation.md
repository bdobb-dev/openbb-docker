<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# ArcticDB Alternatives Evaluation (Ep. 11)

*2026-09-01 — prompted by ArcticDB licensing concerns.*

## The licensing problem

ArcticDB is BSL 1.1 with **no additional-use grant**: free for non-production
use only; any production deployment requires a paid ArcticDB Pro license from
Man Group ([Licensing FAQ](https://docs.arcticdb.io/5.1.2/licensing/)). Each
release converts to Apache 2.0 two years after it ships (conversion table in
the [ArcticDB README](https://github.com/man-group/ArcticDB)) — as of
September 2026 only ~mid-2024 releases are freely production-usable. For an
episode teaching viewers a caching stack, that is a stack they cannot legally
run in production without a license.

Side pain point that also goes away: ArcticDB ships no aarch64 Linux wheels,
which is why `docker-compose.yml` forces `platform: linux/amd64` (emulation on
Apple Silicon). Every candidate below ships native arm64 wheels.

## What Ep. 11 actually uses

Audited `openbb-arcticdb` (store.py, accessor.py, models/historical.py) and
`tick-lab/tick_lab/store.py`:

- symbol-keyed DataFrame **write** with metadata, and **append**
- **read** with `date_range` and `columns` projection
- `list_symbols` / `has_symbol` / `delete` / `read_metadata`
- MinIO **S3 backend**, LMDB local fallback (`resolve_config`)
- `as_of` is exposed in the `ArcticStore.read()` signature but **never
  exercised** anywhere — time-travel is a nice-to-have, not a requirement
- no QueryBuilder, no snapshots, no `update`

`ArcticStore` already isolates the backend: a swap touches `utils.py`, the
store internals, and `tick_lab/store.py`, not the public API.

## Candidates

| Candidate | License | Verdict |
|---|---|---|
| **deltalake (delta-rs)** | Apache 2.0 | **Recommended.** Parquet + transaction log, no server/JVM. `write_deltalake(mode="append")` replaces `lib.append()`; commit metadata replaces symbol metadata; time travel restores `as_of` for free. Native S3/MinIO; reads to pandas/Arrow; DuckDB's delta extension queries the same tables. Closest semantic drop-in for what `ArcticStore` wraps. |
| **Plain DuckDB + Parquet** | MIT | Simplest if versioning is dropped from the API: one Parquet dataset per symbol plus a small metadata table; range/column pushdown free. Hand-rolled append (part-files per write), no `as_of`. |
| **DuckLake** | MIT | DuckDB's own lakehouse — snapshots, time travel, aimed at single-node setups. Young (2025) and adds a catalog database as a moving part. Episode mention, not backbone. |
| Pin Apache-converted ArcticDB (≤ ~v4.x/2024) | Apache 2.0 | Legal but perpetually two years stale; confusing to teach. |
| QuestDB / ClickHouse | Apache 2.0 | Server processes; kdb+ already covers the tick read-through story from Ep. 10. |
| TimescaleDB | TSL / Apache | Best features are under the restricted Timescale License — same problem being escaped. |
| PyStore | Apache 2.0 | Unmaintained. |

## Migration notes

- The MinIO container stays exactly as-is — delta-rs and DuckDB both speak S3.
- The `load-firstratedata-tick-zip` skill writes to ArcticDB and needs its
  writer updated.
- `platform: linux/amd64` pins in `docker-compose.yml` can be dropped once
  ArcticDB is out of the image.

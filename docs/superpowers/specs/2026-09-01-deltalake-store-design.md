<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Ep. 11 — Delta Lake replaces ArcticDB as the shared store

Design spec for Adventures in OpenBB, Chapter 11. Supersedes the storage
choice in `2026-08-05-arcticdb-minio-design.md`; the MinIO/tailnet design in
that spec stands unchanged.

## Context

ArcticDB is BSL 1.1 with no additional-use grant: production use requires a
paid license, and each release only converts to Apache 2.0 two years after it
ships. The episode would be teaching viewers a stack they cannot legally run
in production. Full evaluation of alternatives:
`docs/arcticdb-alternatives-evaluation.md`.

The replacement is [Delta Lake](https://delta.io) via the
[`deltalake`](https://delta-io.github.io/delta-rs/) Python package (delta-rs,
Apache 2.0): Parquet files plus a transaction log, no server, no JVM, native
S3/MinIO support, and version time travel. A side benefit: `deltalake` and
`pyarrow` ship aarch64 Linux wheels, so the `platform: linux/amd64` pins
(ArcticDB's missing arm64 wheels) come out and Apple Silicon runs native.

Decisions already made with the operator:

- **Clean replacement.** ArcticDB is removed entirely; git history keeps the
  old stack. The episode teaches one store.
- **Named for the technology.** `provider="deltalake"`, package
  `openbb-deltalake`, env vars `DELTA_*`.
- **Time travel is kept and featured.** ArcticDB's `as_of` was exposed but
  unused; Delta makes it a headline segment.
- **One Delta table per symbol**, mirroring ArcticDB's per-symbol semantics.

## Goals

1. `provider="deltalake"` serves stored OHLCV/tick data through the standard
   OpenBB interface, byte-for-byte matching today's resample behaviour.
2. The generic `store` API and OBBject accessor keep their public surface —
   `write` / `append` / `read(start_date, end_date, columns, as_of)` /
   `list_symbols` / `has` / `delete` / `read_metadata`.
3. tick-lab still demonstrates "any Python process, no Platform install":
   its store wrapper talks to the `deltalake` package directly.
4. Everything in the image is Apache 2.0 / MIT / BSD — reproducible in
   production with no paid license.
5. Time travel works end to end: overwrite a symbol, read it back
   `as_of` an earlier version or timestamp.

## Non-goals

- No lakehouse catalog (Unity, Glue, DuckLake). Tables are addressed by
  path; `list_symbols` lists prefixes.
- No cross-symbol SQL layer. DuckDB can query these tables later; not this
  chapter.
- No change to MinIO, the tailnet design, kdb+ (Ep. 10), or the FirstRate
  dataset and golden-parity methodology.
- No migration tooling for existing ArcticDB data. Stores are rebuilt by
  re-running the loaders.

## Layout & configuration

One Delta table per symbol:

    s3://<bucket>/<library>/<symbol>/          # MinIO
    ~/.openbb_platform/deltalake/<library>/<symbol>/   # local fallback

The local fallback replaces LMDB — Delta works on plain filesystem paths, no
special local engine.

`minio.env` keys rename `ARCTICDB_S3_*` → `DELTA_S3_*` (same shape:
ENDPOINT, BUCKET, ACCESS, SECRET, PORT, SECURE). `resolve_config` precedence
is unchanged: explicit arg > OpenBB credential > `DELTA_URI` > `DELTA_S3_*`
parts > local default.

Internally the S3 parts become delta-rs `storage_options` (endpoint URL,
access/secret keys, `allow_http` when `DELTA_S3_SECURE=false`) rather than an
ArcticDB-style query-string URI. Credentials therefore never appear in a URI,
which retires `redact_uri` and the log-leak concern it existed for.

## Store mapping

Public API preserved; `ArcticStore` becomes `DeltaStore`.

| Today (ArcticDB)                        | Becomes (deltalake)                                                        |
| --------------------------------------- | -------------------------------------------------------------------------- |
| `lib.write(sym, df, metadata=...)`      | `write_deltalake(path, mode="overwrite", schema_mode="overwrite")`; metadata as commit metadata |
| `lib.append(sym, df)`                   | `write_deltalake(path, mode="append")`                                     |
| `lib.read(sym, date_range, columns, as_of)` | `DeltaTable(path, version=...)` or timestamp load → pyarrow dataset scan with date filter + column projection → pandas |
| `list_symbols()`                        | list `<library>/` prefixes via `pyarrow.fs` (works locally and on MinIO)   |
| `has_symbol(sym)`                       | `DeltaTable.is_deltatable(path)`                                           |
| `read_metadata(sym)`                    | latest commit's custom metadata                                            |
| `delete(sym)`                           | delete the table prefix                                                    |

Index round-trip: Parquet has no index, so the `DatetimeIndex` travels as the
existing `date` column convention — `normalize_index` keeps producing it,
writes store it as a column, reads restore it as the sorted index.
`to_bounds` / `parse_temporal` carry over unchanged, including whole-day
widening of date-shaped `end` bounds.

`as_of` accepts an integer (Delta version) or a date/datetime/string
(timestamp-based travel).

Error classes: tick-lab's `LibraryNotFoundError` and `StoreWriteError` keep
their names and contracts; `StoreWriteError` wraps delta-rs's `DeltaError`
instead of `ArcticException`.

## Provider & accessor

The five OHLCV fetchers (equity/etf/crypto/currency/index), `_resample_spec`,
and `_pandas_ohlcv` move over essentially verbatim — they touch the store
only through read. Renames: `provider="deltalake"`, accessor `.deltalake`,
`DeltaLake*Fetcher` classes, query-param fields `library`/`uri` keep their
meaning (`uri` now accepts a path or `s3://` URL).

## tick-lab

Keeps its own thin wrapper (`TickStore`) over the `deltalake` package
directly — no dependency on `openbb-deltalake`, preserving the chapter's
point that a plain Python process reads the shared store with no Platform
install. Same `write / read / list_symbols / has` surface.

## Docker & CI

- `platform: linux/amd64` pins removed from `docker-compose.yml`.
- MinIO container and tailnet wiring untouched; `minio.env.example` renames
  the keys.
- `extension-constraints.txt` drops arcticdb; adds deltalake's floor.
- `key-maint/app/registry.py` and any `ARCTICDB_*` references in compose
  comments update to `DELTA_*`.
- `load-firstratedata-tick-zip` (the operator's skill, outside this repo)
  needs its writer updated — tracked as follow-up, not part of this repo's
  change.

## Testing

- Existing suites port with fixture changes only: LMDB tmp fixtures become
  tmp-path Delta tables. Golden-parity tests (FirstRate vs independent
  source) are backend-agnostic and must pass unchanged.
- New: time-travel round-trip (write, overwrite, read `as_of` both the
  version number and a timestamp).
- New: concurrent-append test against MinIO. delta-rs uses S3 conditional
  puts for commit atomicity and MinIO supports them, but this is verified,
  not assumed. If it fails, the fallback decision (single-writer discipline
  vs a lock) comes back to the operator before implementation proceeds.
- Integration `test_provider_parity.py` renames its provider and must pass.

## Episode beats affected

1. The licensing story becomes part of the chapter: why the store changed.
2. Time-travel demo segment (new).
3. Apple Silicon runs native — the "emulation is slower" caveat disappears.

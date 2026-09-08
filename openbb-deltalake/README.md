<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# openbb-deltalake

[Delta Lake](https://delta.io) integration for the OpenBB Platform, via
[delta-rs](https://github.com/delta-io/delta-rs) (Apache 2.0) — **both
directions**:

- **Write** — persist any OBBject result to a Delta Lake library:
  ```python
  res = obb.equity.price.historical("AAPL", provider="yfinance")
  res.deltalake.write("AAPL")                       # -> default library
  res.deltalake.write("AAPL", library="prices", metadata={"src": "yfinance"})
  res.deltalake.append("AAPL")                      # append new rows
  res.deltalake.list_symbols(library="prices")
  ```
- **Read (OHLCV)** — serve stored bars back through the normal OpenBB interface
  for **equity, ETF, crypto, currency, and index** historical:
  ```python
  obb.equity.price.historical("AAPL", provider="deltalake")
  obb.crypto.price.historical("BTCUSD", provider="deltalake", start_date="2026-01-01")
  ```
- **Tick → OHLCV on read** — pass `interval` to resample a stored tick symbol
  (a `price` column + optional `size`/`volume`) into OHLCV bars. The date
  filter is pushed down to Parquet row-group stats; the resample itself is
  done client-side with pandas.
  ```python
  obb.equity.price.historical("XYZ", provider="deltalake", library="ticks",
                              interval="1m", start_date="2026-06-01", end_date="2026-06-02")
  ```
  Supported intervals (lowercase `m` = minute, per OpenBB):
  `1s`, `1m`/`5m`/`15m`/`30m`, `1h`/`4h`, `1d`, `1w`/`2w`, `1mo`/`3mo` (also `1M`/`3M`).
  Both `start_date` and `end_date` accept **date or datetime** (a date `end` is
  inclusive of the whole day; a datetime is exact).
  **No `interval` → defaults to `1d`** (this also covers `interval="1d"`, which
  OpenBB silently strips because `1d` is its global default). For raw, non-OHLCV
  rows use `store.read("XYZ", library="ticks")`.
- **`pandas_anchor`** (bool, default `False`) — bucket anchoring. `False`
  (default) uses an epoch origin; `True` uses the pandas default anchor
  (`origin='start_day'`). Mainly visible on intraday intervals that don't evenly
  divide a day (e.g. `5h`: start-of-day edges vs epoch edges).
- **Generic read/write (any data)** — for non-OHLCV data (economy series,
  fundamentals, screeners, arbitrary DataFrames), use the `store` API:
  ```python
  from openbb_deltalake import store
  s = store(library="research")            # uri/library default to env/local directory
  s.write("gdp", obb.economy.gdp.real(provider="oecd"))   # OBBject, DataFrame, or records
  s.write("notes", my_dataframe, metadata={"src": "manual"})
  obj = s.read("gdp")                       # OBBject (default): .to_df(), charting, ...
  df  = s.read("gdp", output="dataframe", start_date="2026-01-01", columns=["value"])
  s.list_symbols(); s.has("gdp"); s.read_metadata("gdp"); s.delete("gdp"); s.append("gdp", more)
  ```
- **Time travel** — `store.read()` takes `as_of`: an **int Delta version**, or
  a **date/datetime/ISO-string timestamp** for time-based travel:
  ```python
  s.read("gdp", as_of=3)                    # version 3
  s.read("gdp", as_of="2026-08-15")         # state as of that day
  ```
  (`read_metadata()` always reflects the *latest* commit, even when `as_of`
  points at an older version.) The OHLCV `provider="deltalake"` read path does
  not expose `as_of` — it always reads the current version; use the `store`
  API above for time travel.

The round-trip lets you pull once from a live provider, store it, then re-read
offline with no API calls or rate limits — versioned and time-travel capable.
Writing moves a DatetimeIndex into a `date` column for Parquet; reading sets
it back as the index when present, so a value that went in with a date index
comes back with one.

The `.deltalake` accessor (on any OBBject) mirrors the write side:
`res.deltalake.write/append/list_symbols/read_metadata/delete(...)`.

Timezone convention: tick data is stored tz-aware (source timestamps, e.g.
US-Eastern), daily bars as naive midnight timestamps — range-filter bounds
adapt to the stored column's tz automatically, but keeping the convention
avoids off-by-hours query bounds.

## Configuration

No OpenBB credentials are required. The connection is resolved with this
precedence: **per-call query param > OpenBB credential > `DELTA_URI` >
`DELTA_S3_*` (assembled) > local directory default**.

- `DELTA_URI`     — e.g. `/root/.openbb_platform/deltalake` (local directory
  store, default) or an S3 URI, e.g. `s3://openbb`.
- `DELTA_LIBRARY` — defaults to `openbb`

If `DELTA_URI` isn't set, the extension will assemble an S3 URI from these
parts instead — handy for pointing both the container and a laptop-side CLI
at the same MinIO from one credentials file:

- `DELTA_S3_ENDPOINT` — host (no scheme), e.g. `minio.example.ts.net`
- `DELTA_S3_BUCKET`   — bucket name
- `DELTA_S3_ACCESS`   — access key
- `DELTA_S3_SECRET`   — secret key
- `DELTA_S3_PORT`     — optional, defaults to `9000`
- `DELTA_S3_SECURE`   — optional, `true` (default, → HTTPS) or `false`
  (→ plain HTTP)

All four required parts (`DELTA_S3_ENDPOINT`/`BUCKET`/`ACCESS`/`SECRET`) must
be set or the assembly is skipped entirely (falls through to the local
directory default) rather than producing a connection that would fail deep
inside delta-rs.

The default local store lives under `OPENBB_HOME`
(`~/.openbb_platform/deltalake/<library>/<symbol>/` if unset), so in the
container it sits on the persistent `openbb-data` volume automatically. It is
a plain directory of Parquet files plus a `_delta_log` — no LMDB, no embedded
database.

## Supported

- Provider read path: `EquityHistorical`, `EtfHistorical`, `CryptoHistorical`,
  `CurrencyHistorical`, `IndexHistorical` (OHLCV standard models), with an
  `interval` param that resamples stored tick/fine data into OHLCV on read
- Generic `store` API: `write`, `append`, `read` (OBBject or DataFrame, with
  `start_date`/`end_date`/`columns`/`as_of`), `list_symbols`, `has`, `delete`,
  `read_metadata` — for any data shape
- `.deltalake` OBBject accessor: `write`, `append`, `list_symbols`,
  `read_metadata`, `delete`

## Note

The package registers the name `deltalake` as both a provider and an OBBject
accessor (intentional). OpenBB emits a harmless one-line warning
("Skipping 'deltalake', name already in user.") when credentials are first
loaded; it does not affect functionality.

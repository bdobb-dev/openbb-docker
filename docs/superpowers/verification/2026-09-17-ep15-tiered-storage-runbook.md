# Episode 15 — Tiered Storage Bring-Up & Re-Benchmark Runbook

**Who runs this:** Art, at the desktop (or via SSH to the NAS: `<user>@<nas-host>`).

**Why this exists:** the tiering seam (`91920b3`, `4cc4974`, `41dae5f`) is code-complete and unit-
tested, but **has never written a byte to MinIO**. `LAKE_*_URI` appears only in the shipped-empty
template, in fake-env unit tests, and in `storage.py` itself. The 410 passing tests all exercise the
*local-fallback* path. Everything below is the verification that has not happened yet.

**Needs:** EODHD API key; the hot MinIO's existing tailnet endpoint (the one `openbb-api` already
uses); a directory on the HDD array for the cold store; a directory on the big array for
`_payloads`; `pip install -e "tick-vault[dev]"`.

Every step is idempotent and safe to re-run. **Do not start the walk from this runbook** — it ends
at a GO/NO-GO.

---

## Step 0 — environment

```sh
cd ~/Developer/openbb-docker            # branch ep15-tick-vault
python3 -m venv .venv-ep15 && source .venv-ep15/bin/activate
pip install -e "tick-vault[dev]"
pytest tick-vault/tests -q              # expect 410 green BEFORE changing anything
```

Fill the Ep-15 block in `minio.env` (see `minio.env.example` for every key). Four traps, each of
which has already cost time once:

1. **No inline `# comment` after a value.** The whole rest of the line becomes the value.
2. **`DELTA_S3_HOT_ENDPOINT` is the existing tailnet `https://` URL**, not `http://minio:9000`. The
   hot store is a Tailscale node terminating TLS with a cert issued for its own MagicDNS name;
   plaintext fails to connect and the container alias fails hostname verification.
3. **`COLD_DATA_DIR`** must be a path on the HDD array. Unset, `minio-hdd` silently falls back to a
   named volume on the ~99 GB Docker pool.
4. **`VAULT_DATA_DIR`** must be a path on the big array. Tiering does **not** move the raw payload
   store — `capture.write_payload` is plain POSIX, so `<VAULT_ROOT>/_payloads/**.json.gz` lands on
   whatever is mounted, and unset means the same 99 GB pool.

Leaving every `LAKE_*_URI` empty is a valid state: the vault stays entirely on local disk. That is
the rollback for every step below.

## Step 1 — bring up the cold tier and create the buckets

```sh
docker compose --profile tick-vault up -d minio-hdd
docker compose --profile tick-vault up minio-hdd-init      # one-shot: creates bronze, silver
docker compose up minio-init                               # one-shot: adds silver-hot, gold, receipts
```

Verify both stores independently before trusting either — the cold console is on host port 9003,
the API on 9002:

```sh
mc alias set cold  http://localhost:9002 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc alias set hot   "<the tailnet https endpoint>" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc ls cold    # expect: bronze/ silver/
mc ls hot     # expect: the existing bucket, plus silver-hot/ gold/ receipts/
```

**If `minio-hdd` will not start:** it runs the STOCK `quay.io/minio/minio` image, not
`openbb-minio:11.0.0`. The latter is a Tailscale node whose entrypoint exits without `TS_AUTHKEY`.
Do not "fix" this by switching images.

## Step 2 — smoke-test the seam (the actual unknowns; a handful of API calls)

```sh
export VAULT_ROOT=/path/to/scratch/vault      # a THROWAWAY root; not the real one
```

**2a. Empty tables land in the right buckets.**

```python
from tick_vault.schemas import create_all
from tick_vault.storage import table_uri
create_all(os.environ["VAULT_ROOT"])
print(table_uri(os.environ["VAULT_ROOT"], "silver.us_trade_tick_version"))  # -> s3://silver/...
print(table_uri(os.environ["VAULT_ROOT"], "silver.instrument"))             # -> s3://silver-hot/...
```

Then `mc ls cold/bronze cold/silver hot/silver-hot hot/gold hot/receipts`. Every registered table
must appear on exactly one store. A table in the wrong bucket is a one-line fix in
`storage.py::TIER_BY_TABLE` — that is what the dict is for.

**2b. A real fetch splits across both stores.** One liquid symbol, one week:

```sh
vault reference --sync
vault manifest --generate <monday> <monday>
vault backfill --loop --workers 1        # let ONE item complete, then stop
```

Confirm: silver rows in `cold/silver`, the bronze capture row in `cold/bronze`, the manifest row in
`hot/receipts`, and **the payload gzip on local disk** under `$VAULT_ROOT/_payloads/` — that last
one is the proof that `VAULT_DATA_DIR` matters.

**2c. Concurrent commits actually work on MinIO.** This is the one that can quietly corrupt a walk:
delta-rs commits lock-free via `aws_conditional_put=etag`, and the parallel walk depends on it.
Repeat the Phase-0 concurrency check against S3 rather than local disk — 6 processes × 10 appends ×
5,000 rows into `silver.us_trade_tick_version`, then assert 300,000 rows land and the Delta version
count matches. **A silent row loss here is a NO-GO**, not a tuning problem.

**2d. DuckDB reads across both endpoints.** `temporal.attach` uses `delta_scan`, which ignores
delta-rs `storage_options` and authenticates through its own scoped SECRETs:

```python
import duckdb
from tick_vault.temporal import attach
con = duckdb.connect(); attach(con, os.environ["VAULT_ROOT"])
con.execute("SELECT count(*) FROM silver_us_trade_tick_version").fetchall()   # cold
con.execute("SELECT count(*) FROM silver_instrument").fetchall()             # hot
```

Both must return without an auth error. One working and one failing means a SCOPE/endpoint mismatch
in `_install_s3_secrets`.

## Step 3 — re-benchmark (the numbers that gate Step 4)

§13a's **9.4 s/item and ~28.5 days were measured on local disk** and do not survive the move:
`ops.backfill_manifest` takes a targeted `DeltaTable.update` **per item** (0.08–0.12 s locally),
which is now a network round-trip in the critical path.

Run the same shape as the §13a benchmark — **6 items serial, then 18 items through 6 workers.**
Do NOT run 6-on-6: six items across six workers is a single wave with nothing to refill, which is
exactly the blind benchmark that produced a meaningless 11.4 s/item.

Baselines to beat, all local-disk:

| | per item | source |
|---|---|---|
| serial | 26.2 s | §13a |
| batch, 6 workers | 10.3 s | §13 |
| streaming, 6 workers | 9.4 s | §13a |

Record the tiered per-item time and the implied days over 262,345 items.

## Step 4 — GO / NO-GO

**NO-GO on any of:**

- Step 2c loses rows, or Delta versions do not match appends.
- Any table lands in the wrong bucket after `TIER_BY_TABLE` is corrected.
- Step 2d fails on either tier.
- `_payloads` is on the Docker pool rather than the big array.
- The tiered per-item time projects past the wall-clock budget and no mitigation is identified.

**If throughput is the only failure**, the first thing to attack is the parent-side per-item writes
(manifest / gate / DQ), not the dispatcher — the barrier was already removed and was worth only 9%.
Batching manifest updates across several items, or keeping the manifest local and syncing
periodically, are the obvious candidates and neither has been tried.

**On GO:** the walk is still a separate, supervised decision. Nothing in this runbook starts it.

---

## Still open, independent of this runbook

- **NAS IP + login in PR #40 history** (`b6174bc`) — purge needs a force-push; Art's call.
- **Rotate the EODHD API key** that EODHD's 404 page echoed into a session transcript.
- **amd64 image build** blocked by a Docker Desktop registry timeout — restart Docker Desktop.
- **Tiering `eod-dump`/`tick-lab`** is deliberately NOT in scope here. `ticks_live` accumulates
  daily and is a real candidate, but its natural axis is AGE (the key already carries the date), not
  layer, so `TIER_BY_TABLE` does not port — and every reader (`stores-explorer`, `tick-lab`, the
  provider) would need to resolve which store holds a given key. Measure how much `ticks_live`
  actually holds before designing it.

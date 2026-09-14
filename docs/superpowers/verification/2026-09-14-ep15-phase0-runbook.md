# Episode 15 — Phase-0 / Calibration Desktop Runbook (Plan 2, Task 12)

**Who runs this:** Art, at the desktop (or via SSH to the NAS: `<user>@<nas-host>`).
**Needs:** EODHD API key, the NAS Delta root (VAULT_ROOT), the legacy store path (LEGACY_ROOT →
`.../ticks_sip`), python 3.12 with `pip install -e "tick-vault[dev]"` (deps install fine outside the
authoring sandbox). Every step below is idempotent and safe to re-run.
**Commit this file's results:** findings go to `docs/superpowers/verification/` per the plan.

## Step 0 — one-time environment

```sh
cd ~/Developer/openbb-docker           # on branch ep15-tick-vault (or wherever it's checked out)
python3 -m venv .venv-ep15 && source .venv-ep15/bin/activate
pip install -e "tick-vault[dev]"
export EOD_API_KEY=...                 # from your key store; never committed
export VAULT_ROOT=/path/to/new/vault   # NAS share mount or local NVMe for the calibration trial
export LEGACY_ROOT=/path/to/ticks_sip  # the existing store (read-only for this runbook)
pytest tick-vault/tests -q             # full suite incl. deferred: expect all green before starting
```

## Step 1 — Phase-0 contract verification (spec §Phase 0; ~5 API calls)

1. **One live tick request** (SPY, one hour of a recent regular session) through the real path:
   `python3 -c` snippet in `tick-vault/tests/live/` is not yet written — simplest is a 5-line REPL:
   `from tick_vault.capture import CaptureStore, UrlLibTransport; from tick_vault.engine import build_tick_url` →
   fetch, then inspect the JSON records. **Verify against the parser's assumptions:** fields ts/price/
   shares/seq/sl/mkt/sub_mkt/ex present; ts in ms UTC; seq unique within the session.
2. **Record every distinct `sl` code** seen (`sorted({r["sl"] for r in payload})`) and check each against
   `tick_vault/sl_conditions.py`'s `_TABLE`. Unknown codes → decide eligibility, add to the table, bump
   `DECODE_VERSION` to `SL_DECODE_V2`, update the decode tests. THIS MATTERS: bar eligibility (and thus
   the reconciliation gate) keys off these flags.
3. **One GSPC.INDX components pull** via `ReferenceClient.get_index_components` — verify the
   `Components` / `HistoricalTickerComponents` key names and date semantics against
   `eodhd_reference.parse_components`; fix `_ENDPOINTS` if any URL 404s (they are one dict, documented
   as Phase-0-verifiable; symbol-change endpoint is global by design).
4. **Membership boundary evidence:** for one known recent index change, compare EODHD's StartDate/EndDate
   against the S&P press-release effective date; write down what the vendor dates mean and confirm or
   amend `MEMBERSHIP_BOUNDARY_V1`'s definition in `tick_vault/membership.py`.
5. Write findings to `docs/superpowers/verification/<date>-ep15-phase0.md` and commit.

## Step 2 — reference sync + manifest (cheap, ~10 calls + 1 per unique constituent for fundamentals)

```sh
vault reference --sync          # symbols, delisted, changes, GSPC components, fundamentals for members
vault figi --drain              # optional now; keyless is slow (25/min) — fine to defer
vault manifest --generate 2016-09-12 2026-09-07   # or your chosen 10y span; BLOCKED_IDENTITY rows are
                                                  # normal for thin names — review them, don't panic
vault status                    # counts by status; sanity-check universe sizes per week
```

## Step 3 — calibration week (the gate; ~505 calls + references)

```sh
vault calibrate --week 2026-08-31 --legacy-root "$LEGACY_ROOT"
```

What it does: runs that week end-to-end through the real engine (bronze captures → silver → gold-less
verify → reconciliation vs EOD/1m refs), measures per-call wall time, rows, and on-disk bytes (filesystem
delta of VAULT_ROOT), reads the legacy `_progress/*.json` wall_minutes for comparison, and writes the
calibration report with the frozen projection (calls / budget-days / wall-clock weeks at 6 workers / TB).

**Review the projection formula before trusting it** (`tick_vault/calibrate.py::calibration_projection`,
documented in its docstring): per-symbol rates × 505 symbols × 522 weeks × 1.15 span-halving overhead on
calls and wall time. It was implementer-designed to be conservative and transparent — the constants are
arguments, so rerun with your own if you disagree. The reconciliation gate must PASS (or every divergence
understood/waived) for this week before proceeding.

## Step 4 — GO/NO-GO (yours alone)

The projection of record from planning: **~300–325k calls, ~3–6 EODHD budget-days, ~2–6 weeks wall-clock,
storage ≈ measured-bytes × 522**. If the calibration report's numbers are in family and acceptable:

```sh
docker buildx build -f tick-vault/Dockerfile -t openbb-tick-vault:15.0.0 .
docker save openbb-tick-vault:15.0.0 | ssh <user>@<nas-host> '<docker> load'
# on the NAS: docker compose --profile tick-vault up -d tick-vault-status
# then start the walk (the CLI refuses without the calibration report — that's the gate working):
docker compose --profile tick-vault run -d tick-vault vault backfill --loop --until 2016-09-12
```

Single-writer rule: exactly ONE `vault` process per VAULT_ROOT (documented in cli.py; optimistic
concurrency is Plan 3). The old sip-backfill loop keeps running untouched until you decide to stop it —
tick-vault writes only to its own VAULT_ROOT.

## Step 5 — ongoing

`vault status` (or the tick-vault-status widget at :8valueless-port/status via the compose service) shows
manifest progress, frontier, open DQ issues. Early tranches run under the reconciliation gate (first 2,000
hold on divergence); expect a few holds while the sl decode table hardens — fix or `Gate.waive` with a
reason, both land in ops.data_quality_issue.

## Known open items this runbook may hit (from the SDD ledgers, all deliberate)
- `_ENDPOINTS` URLs are best-effort until Step 1 verifies them (one dict to fix).
- `/status` budget fields are null placeholders (engine.Budget grows counters in Plan 3).
- calibration projection formula needs your sign-off (Step 3).
- deferred CI suites are the correctness verdict for all Delta/DuckDB paths — keep CI green before Step 3.

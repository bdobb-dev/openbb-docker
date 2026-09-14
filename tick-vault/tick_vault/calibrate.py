"""Calibration mode: measure one representative backfill week end-to-end,
project the full S&P 500 / full-history run's cost from that measurement,
and write a frozen markdown report an operator reads before committing to
the ~300k-call historical walk (task-10 brief; the go/no-go gate that
`tick_vault.cli`'s `backfill --loop` refuses to bypass without).

Pure by design (task brief: "pure math"/"pure given a dir of JSONs") - no
pyarrow/deltalake import anywhere in this module, so it runs bare in the
authoring sandbox and is exercised by `tests/test_calibrate_core.py`
without pytest. `vault calibrate` (in `tick_vault.cli`) is the only
caller that actually runs a real calibration week end-to-end (through
`tick_vault.engine.fetch_week`) and hands this module's pure functions
the resulting `CalibrationMeasurements`.

Projection formula (binding choice - the task brief's own worked example,
"measured 300 calls/40 wall_min for 505 symbols", is illustrative prose,
not a literal formula; this module picks and documents one concrete,
self-consistent rate-based formula rather than leaving it ambiguous)
-------------------------------------------------------------------------
The calibration week's measurement (`CalibrationMeasurements`) is assumed
to cover `measured.symbols` distinct listings in that one week - ordinarily
the full universe (`measured.symbols == universe_size`), since the whole
point of a representative calibration week is to backfill every current
S&P 500 member for it. From that, this module derives a per-symbol,
per-week RATE for each measured quantity (calls, wall-minutes, rows,
bytes) and scales that rate up by `universe_size * weeks_total`
(`weeks_total` defaults to 522, ~10 years of ISO weeks) to get the full
historical-walk projection, then applies `halving_overhead` (default
1.15, a flat +15% fudge factor for the extra HTTP calls/wall-time that
`tick_vault.engine.SpanMemory`'s span-halving adds on top of the
happy-path one-call-per-symbol-per-week case - NOT applied to
rows/bytes, since span-halving changes how many requests fetch a given
week's data, not how much data exists in it):

    rate(x) = measured.x / measured.symbols
    projected_calls        = round(rate(calls)        * universe_size * weeks_total * halving_overhead)
    projected_wall_minutes = rate(wall_minutes) * universe_size * weeks_total * halving_overhead
    projected_rows         = round(rate(rows)          * universe_size * weeks_total)
    projected_bytes        = round(rate(bytes_written) * universe_size * weeks_total)

`projected_wall_clock_days` divides `projected_wall_minutes` by `60 * 24`
and then by the worker-parallelism constant `CALIBRATION_WORKERS` (6,
sip_backfill's own historical concurrency, transcribed here as the
number of parallel worker processes the historical walk is expected to
run under) to get real wall-clock days rather than serial CPU-minutes.
`projected_storage_tb` divides `projected_bytes` by `1e12`.

Worked example (transcribed into `tests/test_calibrate_core.py` as the
binding expected numbers): `measured=CalibrationMeasurements(calls=300,
wall_minutes=40.0, rows=50_000, bytes_written=2_000_000, symbols=505,
week=...)`, `universe_size=505`, `weeks_total=522`, `halving_overhead=1.15`
-> `rate(calls) = 300/505`, so `projected_calls = round(300/505 * 505 *
522 * 1.15) = round(300 * 522 * 1.15) = 180090` (the `measured.symbols ==
universe_size` case cancels exactly, matching the brief's own
`522*505*1.15`-shaped example once `505` is recognized as both the
measured symbol count AND the universe size).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import statistics
from dataclasses import dataclass

import pandas as pd

DEFAULT_UNIVERSE_SIZE = 505
DEFAULT_WEEKS_TOTAL = 522
DEFAULT_HALVING_OVERHEAD = 1.15
CALIBRATION_WORKERS = 6

_LEGACY_COLUMNS = ["week", "wall_minutes", "finished", "settled"]


# ---------------------------------------------------------------------------
# dir_bytes
# ---------------------------------------------------------------------------

def dir_bytes(path: "str | None") -> int:
    """Sum the on-disk byte size of every regular file found by a
    recursive `os.walk` under `path`. Pure filesystem measurement, no
    Delta/pyarrow involved - `tick_vault.cli`'s `handle_calibrate` calls
    this once before and once after running a calibration week's real
    fetches (over `VAULT_ROOT`) and uses the delta as the calibration's
    `bytes_written` measurement (previously hardcoded to 0, which made
    every calibration report's projected storage-TB figure always read
    zero - see task-10 fix-round brief).

    Missing/non-directory `path` returns 0 rather than raising (mirrors
    `read_legacy_wall_minutes`'s best-effort-on-bad-input convention);
    a file that disappears between `os.walk` listing it and `os.path.
    getsize` (benign race, e.g. a concurrent writer) is skipped rather
    than raising."""
    if not path or not os.path.isdir(path):
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            file_path = os.path.join(dirpath, name)
            try:
                total += os.path.getsize(file_path)
            except OSError:
                continue
    return total


# ---------------------------------------------------------------------------
# read_legacy_wall_minutes
# ---------------------------------------------------------------------------

def read_legacy_wall_minutes(progress_dir: "str | None") -> pd.DataFrame:
    """Parse `ticks_sip/_progress/<monday>.json` legacy progress files
    (sip_backfill's own per-week state dict: `wall_minutes` a float,
    `finished`/`settled` ISO-8601 timestamp strings or `None`, plus
    `done`/`failed`/`empty` per-symbol maps this function doesn't need)
    into a `(week, wall_minutes, finished, settled)` frame, one row per
    `<monday>.json` file found directly under `progress_dir`.

    Pure given a directory of JSON files - callers (and this module's own
    tests) write temp JSONs and point this at that temp dir; nothing here
    touches a real Delta lake. A missing `progress_dir`, a file that isn't
    valid JSON, or a filename that isn't an ISO date is skipped rather
    than raising - legacy progress data is best-effort context for a
    calibration report, never a hard dependency.
    """
    if not progress_dir or not os.path.isdir(progress_dir):
        return pd.DataFrame(columns=_LEGACY_COLUMNS)

    rows = []
    for name in sorted(os.listdir(progress_dir)):
        if not name.endswith(".json"):
            continue
        stem = name[: -len(".json")]
        try:
            week = dt.date.fromisoformat(stem)
        except ValueError:
            continue

        path = os.path.join(progress_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue

        rows.append({
            "week": week,
            "wall_minutes": data.get("wall_minutes"),
            "finished": data.get("finished"),
            "settled": data.get("settled"),
        })

    return pd.DataFrame(rows, columns=_LEGACY_COLUMNS)


# ---------------------------------------------------------------------------
# calibration_projection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CalibrationMeasurements:
    calls: int
    wall_minutes: float
    rows: int
    bytes_written: int
    symbols: int
    week: "dt.date | str"


def calibration_projection(
    measured: CalibrationMeasurements,
    universe_size: int = DEFAULT_UNIVERSE_SIZE,
    weeks_total: int = DEFAULT_WEEKS_TOTAL,
    halving_overhead: float = DEFAULT_HALVING_OVERHEAD,
    *,
    legacy_df: "pd.DataFrame | None" = None,
) -> dict:
    """Pure math: project the full historical-walk cost from one
    measured calibration week. See module docstring for the exact
    per-quantity formula.

    When `legacy_df` (a `read_legacy_wall_minutes`-shaped frame) is
    supplied and non-empty, `legacy_wall_minutes_median` is included -
    the median of its non-null `wall_minutes` column - as an independent
    cross-check against `projected_wall_minutes_per_week`; `None` if
    `legacy_df` is `None`/empty/has no non-null `wall_minutes` values.
    """
    if measured.symbols <= 0:
        raise ValueError("measured.symbols must be > 0")

    rate_calls = measured.calls / measured.symbols
    rate_wall_minutes = measured.wall_minutes / measured.symbols
    rate_rows = measured.rows / measured.symbols
    rate_bytes = measured.bytes_written / measured.symbols

    projected_calls = round(rate_calls * universe_size * weeks_total * halving_overhead)
    projected_wall_minutes = rate_wall_minutes * universe_size * weeks_total * halving_overhead
    projected_rows = round(rate_rows * universe_size * weeks_total)
    projected_bytes = round(rate_bytes * universe_size * weeks_total)

    projected_wall_clock_days = projected_wall_minutes / (60.0 * 24.0) / CALIBRATION_WORKERS
    projected_storage_tb = projected_bytes / 1e12

    projected_wall_minutes_per_week = rate_wall_minutes * universe_size * halving_overhead

    legacy_wall_minutes_median = None
    if legacy_df is not None and not legacy_df.empty and "wall_minutes" in legacy_df.columns:
        values = [v for v in legacy_df["wall_minutes"].tolist() if v is not None and not pd.isna(v)]
        if values:
            legacy_wall_minutes_median = statistics.median(values)

    return {
        "universe_size": universe_size,
        "weeks_total": weeks_total,
        "halving_overhead": halving_overhead,
        "workers": CALIBRATION_WORKERS,
        "measured_symbols": measured.symbols,
        "projected_calls": projected_calls,
        "projected_wall_minutes": projected_wall_minutes,
        "projected_wall_minutes_per_week": projected_wall_minutes_per_week,
        "projected_wall_clock_days": projected_wall_clock_days,
        "projected_rows": projected_rows,
        "projected_bytes": projected_bytes,
        "projected_storage_tb": projected_storage_tb,
        "legacy_wall_minutes_median": legacy_wall_minutes_median,
    }


# ---------------------------------------------------------------------------
# write_calibration_report
# ---------------------------------------------------------------------------

def write_calibration_report(
    path: str,
    projection: dict,
    measurements: CalibrationMeasurements,
    legacy_df: "pd.DataFrame | None" = None,
) -> None:
    """Write a frozen markdown calibration report to `path` (creating
    parent directories as needed), naming convention per
    `docs/superpowers/verification/<date>-ep15-calibration.md` (the
    caller, `tick_vault.cli`'s `calibrate` handler, picks the actual
    filename/date; this function just writes whatever `path` it's given).

    The report is deliberately simple markdown (no templating engine):
    the measured inputs, the projected go/no-go figures, and (when
    available) the legacy sip_backfill wall-clock median as a sanity
    cross-check. `tests/test_calibrate_core.py` asserts the key figures'
    string forms appear in the written file - this function's exact
    prose/layout is not itself a frozen contract, only that those
    figures are present and legible.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    week_str = measurements.week.isoformat() if hasattr(measurements.week, "isoformat") else str(measurements.week)

    lines = [
        "# EP15 Calibration Report",
        "",
        f"Measured calibration week: **{week_str}**",
        "",
        "## Measured",
        "",
        f"- calls: {measurements.calls}",
        f"- wall_minutes: {measurements.wall_minutes}",
        f"- rows: {measurements.rows}",
        f"- bytes_written: {measurements.bytes_written}",
        f"- symbols: {measurements.symbols}",
        "",
        "## Projection",
        "",
        f"- universe_size: {projection['universe_size']}",
        f"- weeks_total: {projection['weeks_total']}",
        f"- halving_overhead: {projection['halving_overhead']}",
        f"- workers: {projection['workers']}",
        f"- projected_calls: {projection['projected_calls']}",
        f"- projected_wall_minutes: {projection['projected_wall_minutes']:.2f}",
        f"- projected_wall_clock_days: {projection['projected_wall_clock_days']:.2f}",
        f"- projected_rows: {projection['projected_rows']}",
        f"- projected_bytes: {projection['projected_bytes']}",
        f"- projected_storage_tb: {projection['projected_storage_tb']:.4f}",
        "",
    ]

    if projection.get("legacy_wall_minutes_median") is not None:
        lines.extend([
            "## Legacy cross-check (sip_backfill)",
            "",
            f"- legacy_wall_minutes_median: {projection['legacy_wall_minutes_median']:.2f}",
            "",
        ])
    elif legacy_df is not None and not legacy_df.empty:
        lines.extend([
            "## Legacy cross-check (sip_backfill)",
            "",
            "- legacy progress data found but no usable wall_minutes values",
            "",
        ])

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

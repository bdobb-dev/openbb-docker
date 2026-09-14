"""End-of-day flush: the kdb RDB surrenders the day's ticks to the Delta HDB.

The classic kdb+ day cycle (tickerplant -> RDB -> `.u.end` -> `.Q.hdpf` ->
HDB) mapped onto this stack:

    role         classic              here
    tickerplant  tick.q               the kdb service (kdb-ws/startup.q: upd+pub,
                                      plus persist.q's tick log + -11! replay)
    RDB          in-memory, g# syms   the SAME q -- trades is RAM-resident
    EOD flush    .u.end -> .Q.hdpf    this script: one UTC day -> Delta Lake,
                                      one Delta table per (ticker, day)
    HDB          date-partitioned     the Ep. 11 Delta/MinIO store, read by
                 splayed tables       tick-lab, DuckDB, Polars, anything
    RAM purge    .Q.hdpf deletes      NOT copied: live-grid's rolling window
                 tables, .Q.gc[]      prunes the cache on its own cadence --
                                      the chart's seam needs the trailing day

Idempotent per (symbol, day): the Delta key is `<SYM>_<YYYY_MM_DD>`, and a
re-run overwrites that table (a new Delta version) instead of duplicating.
Runs in the openbb-local image (Ep. 11+), which carries pykx, deltalake, and
the same DELTA_S3_* env every other consumer of the store reads. The IPC read
runs in a pykx-only subprocess: pykx and pyarrow's S3 client segfault when a
big IPC read and the object store share one process.

    python eod_dump.py --date 2026-08-28    # flush one UTC day, then exit
    python eod_dump.py --dry-run            # read kdb, print, write nothing
    python eod_dump.py --loop               # daily at 00:05 UTC, prior day
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("eod-dump")

LIBRARY = os.getenv("EOD_LIBRARY", "ticks_live")
FLUSH_UTC_MINUTES = 5  # daily at 00:05 UTC, flushing the day that just ended


def _read_day_ipc(day: date):
    """The day's ticks over q IPC -- for a flush running ON the docker host,
    where the bridge (or the loopback publish) reaches q. Runs pykx in a
    child process (see module docstring) and hands the frame over as
    parquet; the read is chunked because a single >1M-row IPC message also
    segfaults pykx."""
    import subprocess
    import sys
    import tempfile

    import pandas as pd

    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as fh:
        stage = fh.name
    try:
        subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--stage-day", str(day), stage],
            check=True,
        )
        return pd.read_parquet(stage)
    finally:
        try:
            os.unlink(stage)
        except OSError:
            pass


def _stage_day(day: date, out_path: str) -> None:
    """Child-process body: pykx only, chunked read, parquet out."""
    import pandas as pd
    import pykx as kx

    host = os.getenv("KDB_HOST", "kdb")
    port = int(os.getenv("KDB_PORT", "5000"))
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)
    chunk = 250_000
    q_count = "{[s;e] exec count i from trades where time within (s;e-1)}"
    # ponytail: the within-filter recomputes per chunk; fine at cache scale
    q_slice = (
        "{[s;e;o;n] n sublist o _ select time, sym, price, size from trades "
        "where time within (s;e-1)}"
    )
    with kx.SyncQConnection(host=host, port=port) as conn:
        s = kx.toq(start, kx.TimestampAtom)
        e = kx.toq(end, kx.TimestampAtom)
        n = int(conn(q_count, s, e).py())
        parts = [conn(q_slice, s, e, lo, chunk).pd() for lo in range(0, max(n, 1), chunk)]
    pd.concat(parts, ignore_index=True).to_parquet(out_path, index=False)


def _read_day_http(day: date):
    """The day's ticks over the read-only .z.ph endpoints, per symbol -- for
    a flush running OFF the docker host (the NAS), reached through tailscale
    Serve, which proxies only HTTP and so never exposes raw q IPC."""
    import json
    from urllib.request import urlopen

    import pandas as pd

    base = os.environ["KDB_HTTP_URL"].rstrip("/")
    syms = json.load(urlopen(f"{base}/syms", timeout=30))
    frames = []
    for sym in syms:
        rows = json.load(urlopen(f"{base}/day?date={day}&sym={sym}", timeout=120))
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df["sym"] = sym
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["time", "sym", "price", "size"])
    out = pd.concat(frames, ignore_index=True)
    out["time"] = pd.to_datetime(out["time"])
    return out


def read_day(day: date):
    """The day's ticks out of the RDB, as a pandas frame."""
    if os.getenv("KDB_HTTP_URL"):
        return _read_day_http(day)
    return _read_day_ipc(day)


def dump_day(day: date, dry_run: bool = False) -> int:
    """Flush one UTC day to the HDB. Returns rows written."""
    frame = read_day(day)
    if frame.empty:
        log.info("%s: RDB holds no ticks for this day; nothing to flush", day)
        return 0

    total = 0
    groups = {str(sym): g for sym, g in frame.groupby("sym", observed=True) if len(g)}
    if dry_run:
        for sym, g in sorted(groups.items()):
            log.info("%s %-8s %6d ticks (dry run)", day, sym, len(g))
        return int(sum(len(g) for g in groups.values()))

    import re

    from openbb_deltalake import store as delta_store

    s = delta_store(library=LIBRARY)
    day_suffix = day.isoformat().replace("-", "_")
    for sym, g in sorted(groups.items()):
        df = g.drop(columns=["sym"]).rename(columns={"time": "date"})
        # one Delta table per (sym, day): a re-run overwrites the whole
        # table -- the idempotency .Q.hdpf gets from rewriting the partition
        key = re.sub(r"[^0-9A-Za-z_]", "_", sym) + "_" + day_suffix
        info = s.write(
            key, df, metadata={"source": "eod-dump", "sym": sym, "day": str(day)}
        )
        total += len(df)
        log.info(
            "%s %-8s %6d ticks -> %s/%s (delta v%s)",
            day, sym, len(df), LIBRARY, key, info.get("version"),
        )
    log.info("%s: EOD complete, %d rows across %d symbols", day, total, len(groups))
    return total


def _sleep_until_next_flush() -> date:
    """Sleep to the next 00:05 UTC; return the day that will have just ended."""
    now = datetime.now(timezone.utc)
    nxt = now.replace(hour=0, minute=FLUSH_UTC_MINUTES, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    log.info("next flush at %s (in %ds)", nxt, int((nxt - now).total_seconds()))
    time.sleep((nxt - now).total_seconds())
    return (nxt - timedelta(days=1)).date()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="UTC day to flush (YYYY-MM-DD); default yesterday")
    parser.add_argument("--dry-run", action="store_true", help="read the RDB, write nothing")
    parser.add_argument("--loop", action="store_true", help="flush daily at 00:05 UTC")
    parser.add_argument("--stage-day", nargs=2, metavar=("DAY", "OUT"),
                        help=argparse.SUPPRESS)  # internal: pykx-only child process
    args = parser.parse_args()

    if args.stage_day:
        _stage_day(date.fromisoformat(args.stage_day[0]), args.stage_day[1])
        return

    if args.loop:
        # Catch-up before sleeping: a loop that starts after 00:05 (container
        # recreated, host rebooted) must not skip the day that already ended
        # while nobody was scheduled -- exactly how the first NAS flush was
        # missed. Idempotent, so re-flushing a covered day is harmless.
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        try:
            dump_day(yesterday)
        except Exception:  # noqa: BLE001 - catch-up is best-effort
            log.exception("catch-up flush failed for %s", yesterday)
        while True:
            day = _sleep_until_next_flush()
            # the RDB holds a rolling day, so a failed flush (the docker host
            # asleep at 00:05) still has its data for hours -- retry hourly
            # rather than surrendering the day to the prune
            for attempt in range(20):
                try:
                    dump_day(day)
                    break
                except Exception:  # noqa: BLE001 - one bad day must not kill the cycle
                    log.exception(
                        "EOD flush failed for %s (attempt %d); retrying in 1h",
                        day, attempt + 1,
                    )
                    time.sleep(3600)
        return

    day = (
        date.fromisoformat(args.date)
        if args.date
        else (datetime.now(timezone.utc) - timedelta(days=1)).date()
    )
    dump_day(day, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

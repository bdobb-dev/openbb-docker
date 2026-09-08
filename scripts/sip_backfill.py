"""Walk the S&P 500's consolidated tick history into the Delta tick vault, one week at a time.

Layout: one Delta table per symbol, `ticks_sip/<SYM>`, partitioned by `day`
(TickStore.replace_days). A week load replaces exactly its own partitions, so
any week can be re-run. Columns: date (naive UTC, index), price, size, mkt,
sub_mkt, seq, sl -- the Tick Data API's fields with `shares` renamed to `size`.

Per symbol: one API call for the whole week (7-day max; the heaviest names time
out and fall back to five day calls, deduplicated on (ts, seq)), save to a local
parquet, write to Delta, read the days back and check row count and seq
uniqueness, delete the parquet.

Universe: the index's HistoricalTickerComponents, filtered to members whose
tenure overlaps the week at all -- an add on Wednesday or a delete on Tuesday
gets the full Monday..Friday. Ordered by current market cap, descending.

Progress lives beside the data, one JSON per week at
`ticks_sip/_progress/<monday>.json` (done / failed / empty per symbol), so a
restart resumes mid-week and a NAS reboot loses nothing.

  sip_backfill.py --week 2026-08-31            one week, then exit
  sip_backfill.py --loop [--until 2008-01-01]  newest complete week first, then
                                               backwards; new weeks are picked
                                               up as they complete (T+1: a week
                                               counts as complete from Saturday
                                               08:00 UTC); sleeps when caught up

Env: EODHD_API_KEY, DELTA_S3_* (tick-lab's S3Config), SIP_WORKERS (6),
SIP_WORK (/tmp/sip), SIP_LIBRARY (ticks_sip).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tick-lab"))
from tick_lab.config import from_env  # noqa: E402
from tick_lab.store import TickStore  # noqa: E402

log = logging.getLogger("sip-backfill")
TICKS = "https://eodhd.com/api/mp/unicornbay/tickdata/ticks"
FLOOR = date(2008, 1, 1)  # ticks exist back to at least 2008-03 (probed 2026-09-07)


def _get_json(url: str, timeout: int = 1800, tries: int = 3):
    """GET with retry on 5xx/429/timeouts. 429 (budget) waits 30 minutes."""
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()[:120]
            if e.code == 429:
                log.warning("429 from EODHD; sleeping 30 min")
                time.sleep(1800)
                continue
            if e.code in (500, 502, 503, 504) and attempt < tries:
                time.sleep(5 * attempt)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body!r}") from e
        except (TimeoutError, OSError) as e:
            if attempt < tries:
                time.sleep(5 * attempt)
                continue
            raise RuntimeError(f"{type(e).__name__}: {e}") from e


# -- universe -----------------------------------------------------------------
class Universe:
    """Index membership by week, ordered by current market cap."""

    def __init__(self, key: str):
        self.key = key
        comps = _get_json(f"https://eodhd.com/api/fundamentals/GSPC.INDX?filter=HistoricalTickerComponents&fmt=json&api_token={key}")
        self.members = []
        for c in comps.values():
            start = date.fromisoformat(c["StartDate"]) if c.get("StartDate") else FLOOR
            end = date.fromisoformat(c["EndDate"]) if c.get("EndDate") else None
            self.members.append((c["Code"], start, end))
        self.caps: dict[str, float] = {}
        for offset in range(0, 1000, 100):  # the screener's offset stops at 999
            q = urllib.parse.urlencode({"filters": json.dumps([["exchange", "=", "us"]]),
                                        "sort": "market_capitalization.desc", "limit": 100,
                                        "offset": offset, "api_token": key})
            for row in _get_json("https://eodhd.com/api/screener?" + q)["data"]:
                self.caps.setdefault(row["code"], row.get("market_capitalization") or 0)

    def for_week(self, monday: date) -> list[str]:
        friday = monday + timedelta(days=4)
        codes = {code for code, start, end in self.members
                 if start <= friday and (end is None or end >= monday)}
        # Ranked members first (cap desc), the rest alphabetical.
        return sorted(codes, key=lambda c: (c not in self.caps, -self.caps.get(c, 0), c))


# -- progress, kept in the bucket next to the data -------------------------------
class Progress:
    def __init__(self, store: TickStore, library: str, monday: date):
        self.fsys, root = store._fs_and_root()  # noqa: SLF001
        self.path = f"{root}/{library}/_progress/{monday}.json"
        self.lock = threading.Lock()
        self.state = {"week": str(monday), "done": {}, "failed": {}, "empty": [], "started": None, "finished": None}
        try:
            with self.fsys.open_input_stream(self.path) as f:
                self.state.update(json.loads(f.read()))
        except (FileNotFoundError, OSError):
            pass

    def save(self) -> None:
        with self.lock:
            with self.fsys.open_output_stream(self.path) as f:
                f.write(json.dumps(self.state, indent=1).encode())

    def record(self, sym: str, result: dict | None, error: str | None) -> None:
        with self.lock:
            self.state["failed"].pop(sym, None)
            if error:
                self.state["failed"][sym] = error[:300]
            elif result and result.get("rows", 0) == 0:
                if sym not in self.state["empty"]:
                    self.state["empty"].append(sym)
            else:
                self.state["done"][sym] = result
        self.save()

    def pending(self, universe: list[str]) -> list[str]:
        settled = set(self.state["done"]) | set(self.state["empty"])
        return [s for s in universe if s not in settled]


# -- per-symbol pipeline --------------------------------------------------------
def load_symbol(sym: str, key: str, store: TickStore, library: str, monday: date, work: Path) -> dict:
    out: dict = {}
    t0 = time.time()
    frm = int(datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc).timestamp())
    to = frm + 5 * 86400  # Mon 00:00 .. Sat 00:00 UTC
    q = urllib.parse.urlencode({"s": sym, "from": frm, "to": to, "api_token": key})
    try:
        frames = [pd.DataFrame(_get_json(f"{TICKS}?{q}"))]
    except RuntimeError as e:
        # Mega-caps (NVDA: 2.5M ticks/day) 500 on a whole-week call -- a
        # server-side timeout. Fall back to one call per day.
        out["note"] = f"week call failed ({str(e)[:40]}); fetched per day"
        frames = []
        for d in range(5):
            qd = urllib.parse.urlencode({"s": sym, "from": frm + d * 86400, "to": frm + (d + 1) * 86400, "api_token": key})
            raw = _get_json(f"{TICKS}?{qd}")
            if raw and "ts" in raw:
                frames.append(pd.DataFrame(raw))
    out["fetch_s"] = round(time.time() - t0, 1)
    frames = [f for f in frames if len(f) and "ts" in f.columns]
    if not frames:
        out["rows"] = 0
        return out
    # Day calls share their boundary second (from*1000 <= ts <= to*1000), so a
    # print stamped exactly on the boundary arrives twice. seq is unique per
    # DAY only (values recur across days), so dedup on (ts, seq), never seq alone.
    df = pd.concat(frames, ignore_index=True).drop_duplicates(["ts", "seq"]).rename(columns={"shares": "size"})
    df["date"] = pd.to_datetime(df.pop("ts"), unit="ms", utc=True).dt.tz_localize(None)
    df = df.set_index("date").sort_index()
    out["rows"] = int(len(df))

    local = work / f"{sym}_{monday}.parquet"
    df.to_parquet(local)
    out["local_bytes"] = local.stat().st_size

    t1 = time.time()
    # `fetched` makes Delta history answer "what did the vendor serve when":
    # every re-fetch is a new table version, readable by time travel.
    days = store.replace_days(library, sym, df, metadata={
        "source": "eodhd-unicornbay-tickdata", "week": str(monday),
        "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    out["write_s"] = round(time.time() - t1, 1)

    t2 = time.time()
    from deltalake import DeltaTable
    dt = DeltaTable(store._path(library, sym), storage_options=store._opts)  # noqa: SLF001
    back = dt.to_pyarrow_dataset(partitions=[("day", "in", days)]).to_table().to_pandas()
    problems = []
    if len(back) != len(df):
        problems.append(f"{len(back)} rows read back, {len(df)} written")
    per_day = back.groupby("day")["seq"].nunique()
    sizes = back.groupby("day").size()
    for d in days:
        if per_day.get(d, 0) != sizes.get(d, 0):
            problems.append(f"{d}: seq not unique")
    out["verify_s"] = round(time.time() - t2, 1)
    out["days"] = {d: int(sizes.get(d, 0)) for d in days}
    if problems:
        raise RuntimeError("; ".join(problems))
    local.unlink()
    out["total_s"] = round(time.time() - t0, 1)
    return out


def load_week(monday: date, uni: Universe, key: str, store: TickStore, library: str, work: Path, workers: int, only: set[str] | None = None, force: bool = False) -> Progress:
    prog = Progress(store, library, monday)
    symbols = uni.for_week(monday)
    if only:
        symbols = [s for s in symbols if s in only]
    todo = symbols if (only and force) else prog.pending(symbols)
    if prog.state["started"] is None:
        prog.state["started"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prog.state["universe"] = len(symbols)
    prog.save()
    log.info("week %s: %d members, %d done, %d pending", monday, len(symbols), len(prog.state["done"]), len(todo))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(load_symbol, s, key, store, library, monday, work): s for s in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            s = futs[fut]
            try:
                r = fut.result()
                prog.record(s, r, None)
                log.info("[%s %3d/%d] %-6s rows=%9s fetch=%6.1fs write=%5.1fs verify=%5.1fs elapsed=%5.1fm",
                         monday, i, len(todo), s, f"{r.get('rows', 0):,}", r.get("fetch_s", 0),
                         r.get("write_s", 0), r.get("verify_s", 0), (time.time() - t0) / 60)
            except Exception as e:  # noqa: BLE001 -- one bad symbol must not stop the week
                prog.record(s, None, str(e))
                log.warning("[%s %3d/%d] %-6s FAILED %s", monday, i, len(todo), s, str(e)[:120])
                (work / f"{s}_{monday}.parquet").unlink(missing_ok=True)
    prog.state["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prog.state["wall_minutes"] = round((prog.state.get("wall_minutes") or 0) + (time.time() - t0) / 60, 1)
    prog.save()
    st = prog.state
    log.info("week %s complete: %d loaded, %d empty, %d failed, %s ticks, %.1f min this pass",
             monday, len(st["done"]), len(st["empty"]), len(st["failed"]),
             f"{sum(r.get('rows', 0) for r in st['done'].values()):,}", (time.time() - t0) / 60)
    return prog


def latest_complete_monday(now: datetime | None = None) -> date:
    """The Monday of the newest week whose Friday ticks are already served.
    The API loads a day between ~00:00 and 02:00 ET the next morning, so a
    week is complete from Saturday 08:00 UTC."""
    now = now or datetime.now(timezone.utc)
    monday = (now.date() - timedelta(days=now.weekday()))
    if now.weekday() < 5 or (now.weekday() == 5 and now.hour < 8):
        monday -= timedelta(days=7)
    return monday


def is_complete(store: TickStore, library: str, monday: date) -> bool:
    return Progress(store, library, monday).state["finished"] is not None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", help="Monday, YYYY-MM-DD: load this one week and exit")
    ap.add_argument("--loop", action="store_true", help="walk backwards from the newest complete week, forever")
    ap.add_argument("--until", default=str(FLOOR), help="oldest Monday to load in --loop mode")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("SIP_WORKERS", "6")))
    ap.add_argument("--only", help="comma-separated symbols: restrict the universe")
    ap.add_argument("--force", action="store_true", help="with --only: re-fetch even if already done")
    a = ap.parse_args()
    if not a.week and not a.loop:
        ap.error("--week or --loop")
    key = os.environ["EODHD_API_KEY"]
    library = os.environ.get("SIP_LIBRARY", "ticks_sip")
    work = Path(os.environ.get("SIP_WORK", "/tmp/sip"))
    work.mkdir(parents=True, exist_ok=True)
    store = TickStore(from_env())
    uni = Universe(key)
    log.info("universe: %d historical members, %d ranked by cap", len(uni.members), len(uni.caps))

    if a.week:
        load_week(date.fromisoformat(a.week), uni, key, store, library, work, a.workers, set(a.only.split(",")) if a.only else None, a.force)
        return 0

    floor = date.fromisoformat(a.until)
    while True:
        monday = latest_complete_monday()
        target = None
        while monday >= floor:
            if not is_complete(store, library, monday):
                target = monday
                break
            monday -= timedelta(days=7)
        if target is None:
            log.info("caught up back to %s; sleeping 6h", floor)
            time.sleep(6 * 3600)
            continue
        load_week(target, uni, key, store, library, work, a.workers)


if __name__ == "__main__":
    sys.exit(main())

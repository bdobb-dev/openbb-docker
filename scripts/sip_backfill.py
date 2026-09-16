"""Walk the S&P 500's consolidated tick history into the Delta tick vault, one week at a time.

Layout: one Delta table per symbol, `ticks_sip/<SYM>`, partitioned by `day`
(TickStore.replace_days). A week load replaces exactly its own partitions, so
any week can be re-run. Columns: date (naive UTC, index), price, size, mkt,
sub_mkt, seq, sl -- the Tick Data API's fields with `shares` renamed to `size`.

Per symbol: one API call for the whole week (7-day max). A span the server
times out on is halved rather than retried, and the span that worked is
remembered per symbol, so a mega-cap costs 2-3 requests, not 3 wasted
retries plus 5 day calls. Chunks are deduplicated on (ts, seq). Save to a local
parquet, write to Delta, read the days back and check row count and seq
uniqueness, delete the parquet.

Universe: the index's HistoricalTickerComponents, filtered to members whose
tenure overlaps the week at all -- an add on Wednesday or a delete on Tuesday
gets the full Monday..Friday. Ordered by current market cap, descending.

Progress lives beside the data, one JSON per week at
`ticks_sip/_progress/<monday>.json` (done / failed / empty per symbol), so a
restart resumes mid-week and a NAS reboot loses nothing.

  sip_backfill.py --week 2026-08-31            one week, then exit
  sip_backfill.py --loop [--until 2008-01-01]  each cycle, in priority order:
                                               0. the daily pass, once a day after
                                                  02:05 New York: the trailing 7
                                                  days for every member, so
                                                  yesterday lands and the six days
                                                  before it pick up late prints
                                               1. the newest complete week not yet
                                                  loaded (T+1: complete from
                                                  Saturday 08:00 UTC)
                                               2. a week due for its settle pass
                                               3. a finished week with failed
                                                  symbols not retried in 24 h
                                               4. the next week backwards
                                               sleeps when nothing is due
  sip_backfill.py --daily [2026-09-08]         one daily pass now
  sip_backfill.py --settle 2026-08-31          settle one week now (re-fetch all)
  sip_backfill.py --retry 2026-08-31           retry that week's failures now

Settle pass: the vendor's T+1 file is append-only and keeps receiving late
prints for about a week, so a week first fetched young is re-fetched once
SIP_SETTLE_LAG_DAYS after its Friday. Each symbol's prior state stays as a
Delta version; the pass records rows added per symbol. Weeks first fetched
after the lag (the backfill) are already settled and are never re-fetched.

Retry sweep: failed symbols of finished weeks are retried at most once a day,
and given up after SIP_RETRY_MAX attempts. A symbol unservable in SIP_BLACKLIST_WEEKS (5)
different weeks inside its membership tenure (a 5xx at a one-day span on a day
the vendor serves) is skipped for weeks
at or before its newest failure and, while that failure is recent, in the daily
pass (_progress/_blacklist.json). Scoped in time because a ticker rename makes
a symbol unservable before the change only. A 5xx on a longer span only halves it.

Budget: one request per symbol per window is the floor. On the first-party
endpoint a request is 1 API call of the 100k/day (it was 10 on the marketplace
product); the daily pass runs only on mornings after a session (Tue-Sat ET).

Env: EODHD_API_KEY, DELTA_S3_* (tick-lab's S3Config), SIP_WORKERS (6),
SIP_WORK (/tmp/sip), SIP_LIBRARY (ticks_sip), SIP_SETTLE_LAG_DAYS (7),
SIP_RETRY_MAX (3), SIP_VACUUM_DAYS (30: on Sundays the daily pass drops table
versions older than this, since every daily rewrite of a partition keeps
the previous file; 0 disables).
"""
from __future__ import annotations

import argparse
import http.client
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
# The All-in-One package's own tick endpoint (EODHD support, 2026-09-14). Same
# schema as the marketplace UnicornBay product it replaced: ts (ms), price,
# shares, mkt, sub_mkt, seq, sl; from/to inclusive in seconds. Differences:
# 1 API call per request (not 10), ~7x faster, class shares with a dot
# (BRK.B, not BRK-B), 404 for an unknown symbol.
TICKS = "https://eodhd.com/api/ticks/"


def api_symbol(sym: str) -> str:
    """Vendor spelling: the components list says BRK-B, the tick API wants BRK.B."""
    return sym.replace("-", ".")
FLOOR = date(2008, 1, 1)  # ticks exist back to at least 2008-03 (probed 2026-09-07)


class TooBig(RuntimeError):
    """A 5xx that arrived after a long wait: the server gave up on the span."""


class Unservable(RuntimeError):
    """A 5xx even on a one-day span: the vendor does not serve this symbol
    (BRK-B, BF-B) or this day. Only these count toward the blacklist."""


class Budget(Exception):
    """429 for hours, or 403 for everyone: the API is not serving us right now
    (budget spent, entitlement, or a block). Not a RuntimeError on purpose --
    nothing may treat it as a failure of the symbol or the span."""


class NotFound(RuntimeError):
    """HTTP 404: the endpoint does not know the symbol (or serves no ticks for
    it). A verdict on the symbol -- no halving, no SPY control."""


class Forbidden(RuntimeError):
    """HTTP 403 on one request. Per-symbol (BRK.B) or global (2026-09-13: the
    tick endpoint answered 403 to every symbol for hours, and the old code
    turned that into 338 "empty" symbol-weeks). The caller asks SPY to tell."""


def _get_json(url: str, timeout: int = 1800, tries: int = 3):
    """GET with retry on quick 5xx/timeouts. 429 (budget) waits 30 minutes.
    A 5xx that took over a minute is a server-side timeout on the span --
    retrying the same call costs another request and another wait, so it
    raises TooBig immediately for the caller to split the span."""
    waits = 0
    attempt = 0
    while attempt < tries:
        attempt += 1
        t0 = time.time()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()[:120]
            if e.code == 429:
                # The budget, not the request: wait it out without spending
                # an attempt, and give up as Budget after ~6 h so the caller
                # pauses instead of recording symbols as failed or empty.
                waits += 1
                if waits > 12:
                    raise Budget("429 for 6 hours") from e
                log.warning("429 from EODHD; sleeping 30 min (%d)", waits)
                time.sleep(1800)
                attempt -= 1
                continue
            if e.code in (500, 502, 503, 504):
                if time.time() - t0 > 60:
                    raise TooBig(f"HTTP {e.code} after {time.time() - t0:.0f}s") from e
                if attempt < tries:
                    time.sleep(5 * attempt)
                    continue
            if e.code == 403:
                raise Forbidden(f"HTTP 403: {body!r}") from e
            if e.code == 404:
                raise NotFound(f"HTTP 404: unknown symbol or no coverage") from e
            raise RuntimeError(f"HTTP {e.code}: {body!r}") from e
        except (TimeoutError, OSError, http.client.HTTPException) as e:
            # IncompleteRead is an HTTPException, not an OSError: a big payload
            # truncated mid-stream (ORCL 72 MB, PLTR 18 MB on 2026-09-16).
            # Retry it like a timeout, and on the last attempt raise
            # RuntimeError so fetch_span halves the span instead of failing.
            if attempt < tries:
                time.sleep(5 * attempt)
                continue
            raise RuntimeError(f"{type(e).__name__}: {e}") from e
    raise RuntimeError("no attempts left")


def _read_json(fsys, path: str) -> dict | None:
    try:
        with fsys.open_input_stream(path) as f:
            return json.loads(f.read())
    except (FileNotFoundError, OSError):
        return None


def _write_json(fsys, path: str, obj: dict) -> None:
    with fsys.open_output_stream(path) as f:
        f.write(json.dumps(obj, indent=1).encode())


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
        return self.for_range(monday, monday + timedelta(days=4))

    def in_tenure(self, sym: str, monday: date) -> bool:
        """Whether the whole week sits inside the symbol's membership: the
        vendor's EndDate lags the last trade, so the final (partial) week and
        anything after it is expected to be empty. A symbol that ended before
        the week's Friday is out of tenure for that week."""
        friday = monday + timedelta(days=4)
        for code, start, end in self.members:
            if code == sym:
                return start <= monday and (end is None or end - timedelta(days=14) >= friday)
        return True

    def for_range(self, first: date, last: date) -> list[str]:
        """Members whose tenure overlaps [first, last] at all."""
        codes = {code for code, start, end in self.members
                 if start <= last and (end is None or end >= first)}
        # Ranked members first (cap desc), the rest alphabetical.
        return sorted(codes, key=lambda c: (c not in self.caps, -self.caps.get(c, 0), c))


# -- progress, kept in the bucket next to the data -------------------------------
class Blacklist:
    """Symbols the vendor does not serve -- scoped in TIME, because a ticker
    rename (BK -> BNY, mid-2026) makes a symbol unservable before the change
    and fine after it. Delistings are the mirror image (unservable AFTER the
    end) and must never block older weeks: failures outside the symbol's
    membership tenure are ignored (11.4.7; before that MRO, CTLT, JNPR and
    DAY were blocked for every older week). A symbol unservable in 3 different weeks is skipped
    for every week at or before its newest failure, and in the daily pass
    only while that newest failure is recent. Persisted at
    _progress/_blacklist.json. (Fetching pre-rename weeks under the old
    ticker is a separate, unbuilt step.)"""

    def __init__(self, store: TickStore, library: str):
        self.fsys, root = store._fs_and_root()  # noqa: SLF001
        self.path = f"{root}/{library}/_progress/_blacklist.json"
        self.state = _read_json(self.fsys, self.path) or {}
        self.state.setdefault("unservable", {})  # sym -> [mondays]

    THRESHOLD = int(os.environ.get("SIP_BLACKLIST_WEEKS", "5"))

    def _newest(self, sym: str) -> date | None:
        weeks = self.state["unservable"].get(sym, [])
        return max(date.fromisoformat(w) for w in weeks) if len(weeks) >= self.THRESHOLD else None

    def blocked_for(self, monday: date) -> set[str]:
        return {s for s in self.state["unservable"] if (n := self._newest(s)) and monday <= n}

    def blocked_daily(self, today: date) -> set[str]:
        return {s for s in self.state["unservable"] if (n := self._newest(s)) and n >= today - timedelta(days=21)}

    def note_week(self, monday: date, failed: dict, uni: "Universe | None" = None) -> None:
        for sym, err in failed.items():
            if not str(err).startswith("unservable"):
                continue  # a transient failure or a too-big span never blacklists
            if uni is not None and not uni.in_tenure(sym, monday):
                continue  # a delisting-week artefact (no data exists): never a verdict on older weeks
            weeks = self.state["unservable"].setdefault(sym, [])
            if str(monday) not in weeks:
                weeks.append(str(monday))
            if len(weeks) == self.THRESHOLD:
                log.warning("%s unservable in %d weeks; skipping it for weeks up to %s", sym, self.THRESHOLD, max(weeks))
        _write_json(self.fsys, self.path, self.state)


class Progress:
    def __init__(self, store: TickStore, library: str, monday: date):
        self.fsys, root = store._fs_and_root()  # noqa: SLF001
        self.path = f"{root}/{library}/_progress/{monday}.json"
        self.lock = threading.Lock()
        self.state = {"week": str(monday), "done": {}, "failed": {}, "empty": [], "started": None, "finished": None,
                      "retries": {}, "retried_at": None, "settled": None, "settle_added": {}}
        try:
            with self.fsys.open_input_stream(self.path) as f:
                self.state.update(json.loads(f.read()))
        except (FileNotFoundError, OSError):
            pass
        for k, v in (("retries", {}), ("settle_added", {})):
            self.state.setdefault(k, v)

    def save(self) -> None:
        with self.lock:
            with self.fsys.open_output_stream(self.path) as f:
                f.write(json.dumps(self.state, indent=1).encode())

    def record(self, sym: str, result: dict | None, error: str | None) -> None:
        with self.lock:
            self.state["failed"].pop(sym, None)
            if error:
                self.state["failed"][sym] = error[:300]
                self.state["retries"][sym] = self.state["retries"].get(sym, 0) + 1
            elif result and "added" in result:
                self.state["settle_added"][sym] = result["added"]
            elif result and result.get("rows", 0) == 0:
                if sym not in self.state["empty"]:
                    self.state["empty"].append(sym)
            else:
                self.state["done"][sym] = result
        self.save()

    def pending(self, universe: list[str]) -> list[str]:
        settled = set(self.state["done"]) | set(self.state["empty"])
        return [s for s in universe if s not in settled]

    def retryable(self, retry_max: int) -> list[str]:
        return [s for s in self.state["failed"] if self.state["retries"].get(s, 0) < retry_max]

    def due_for_retry(self, retry_max: int, now: datetime) -> bool:
        if not self.state["finished"] or not self.retryable(retry_max):
            return False
        last = self.state.get("retried_at")
        return last is None or datetime.fromisoformat(last) <= now - timedelta(hours=24)

    def due_for_settle(self, lag_days: int, now: datetime) -> bool:
        """First pass finished, not yet settled, first fetched before the
        settle point (young data), and the settle point has passed."""
        st = self.state
        if not st["finished"] or st["settled"] or not st["started"]:
            return False
        settle_at = datetime.combine(date.fromisoformat(st["week"]) + timedelta(days=4), datetime.min.time(),
                                     tzinfo=timezone.utc) + timedelta(days=lag_days)
        return datetime.fromisoformat(st["started"]) < settle_at <= now


# -- per-symbol pipeline --------------------------------------------------------
_spans: dict[str, int] = {}  # symbol -> largest span (days) known to succeed
_spans_lock = threading.Lock()


def _api_alive(key: str) -> bool:
    """Whether the tick endpoint serves anything at all: SPY, limit=1, on a
    day it has always served. A 403/429/5xx here means the outage is ours,
    not the symbol's."""
    frm = int(datetime(2026, 9, 4, tzinfo=timezone.utc).timestamp())
    q = urllib.parse.urlencode({"s": "SPY", "from": frm, "to": frm + 86400 - 1, "limit": 1, "api_token": key, "fmt": "json"})
    try:
        raw = _get_json(f"{TICKS}?{q}", tries=1)
    except (RuntimeError, Budget):
        return False
    return bool(raw) and "ts" in raw and len(raw["ts"]) > 0


def _day_served(key: str, frm: int) -> bool:
    """Whether the vendor serves this UTC day at all (SPY, limit=1)."""
    q = urllib.parse.urlencode({"s": "SPY", "from": frm, "to": frm + 86400 - 1, "limit": 1, "api_token": key, "fmt": "json"})
    try:
        raw = _get_json(f"{TICKS}?{q}", tries=1)
    except Forbidden:
        raise Budget("403 on the SPY control: the API is not serving us")
    except RuntimeError:  # Budget is not a RuntimeError and propagates
        if not _api_alive(key):
            raise Budget("the SPY control fails too: outage or quota, not this day")
        return False
    if not raw or "ts" not in raw or not raw["ts"]:
        # An empty control on a multi-day-capable endpoint: holiday, or the
        # vendor serving empties while over quota. Tell them apart.
        if not _api_alive(key):
            raise Budget("empty SPY control and the API is not alive")
        return False
    return True


def fetch_span(sym: str, key: str, frm: int, ndays: int) -> list:
    """The API's frames for [frm, frm+ndays days), as few requests as the
    server will serve. A span the server times out on is halved, not retried
    and not dropped straight to single days; the span that worked is
    remembered so the next window for that symbol starts there."""
    with _spans_lock:
        start_span = min(ndays, _spans.get(sym, ndays))
    frames, pos = [], 0
    while pos < ndays:
        span = min(start_span, ndays - pos)
        while True:
            # `to` is inclusive at the second (from*1000 <= ts <= to*1000), so
            # end one second early: otherwise a print stamped exactly at
            # midnight lands in the next day's partition (2-row Saturday
            # partitions were seen for many symbols) and chunks overlap.
            q = urllib.parse.urlencode({"s": api_symbol(sym), "from": frm + pos * 86400, "to": frm + (pos + span) * 86400 - 1, "api_token": key, "fmt": "json"})
            try:
                raw = _get_json(f"{TICKS}?{q}")
                break
            except NotFound as e:
                raise Unservable(str(e)) from e
            except Forbidden as e:
                if not _api_alive(key):
                    raise Budget(f"{sym}: 403 and the SPY control fails: the API is not serving us") from e
                raise Unservable(str(e)) from e
            except (TooBig, RuntimeError) as e:
                # A 5xx on a multi-day span -- slow (server timeout) or quick
                # (the server refuses the span outright, which is how NVDA
                # and MU weeks fail) -- means: halve it. A 5xx on a single
                # day is a verdict on the symbol, UNLESS the vendor has that
                # day for nobody: a holiday, or a session not loaded yet,
                # also answers 500 (probed 2026-09-09). One cheap control
                # request on SPY tells the two apart.
                if span == 1:
                    if _day_served(key, frm + pos * 86400):
                        raise Unservable(str(e)) from e
                    log.info("%s: %s not served by the vendor for anyone; skipping the day", sym, datetime.fromtimestamp(frm + pos * 86400, timezone.utc).date())
                    raw = None
                    break
                span = (span + 1) // 2
                # Remember the shrink (a tail chunk shorter than start_span is
                # not a shrink and must not be remembered as one).
                start_span = span
                with _spans_lock:
                    _spans[sym] = span
                log.info("%s: span too big, trying %d-day chunks", sym, span)
        if raw and "ts" in raw:
            frames.append(pd.DataFrame(raw))
        elif span > 1 and not _api_alive(key):
            # A whole multi-day span empty for a listed symbol while SPY
            # also fails: the vendor is answering empties over quota.
            raise Budget(f"{sym}: empty {span}-day span and the API is not alive")
        pos += span
    return frames


def load_symbol(sym: str, key: str, store: TickStore, library: str, start: date, work: Path, diff: bool = False, ndays: int = 5) -> dict:
    """Fetch, save, write, verify, clean. With diff=True (settle pass) the
    rows already stored for the week are read first and the result carries
    `added` / `removed` against them plus the Delta version they live in."""
    out: dict = {}
    t0 = time.time()
    frm = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    frames = fetch_span(sym, key, frm, ndays)
    out["fetch_s"] = round(time.time() - t0, 1)
    out["requests"] = len(frames) if frames else 1
    if len(frames) > 1:
        out["note"] = f"fetched in {len(frames)} chunks"
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

    local = work / f"{sym}_{start}.parquet"
    df.to_parquet(local)
    out["local_bytes"] = local.stat().st_size

    from deltalake import DeltaTable
    if diff and DeltaTable.is_deltatable(store._path(library, sym), storage_options=store._opts):  # noqa: SLF001
        prev = DeltaTable(store._path(library, sym), storage_options=store._opts)  # noqa: SLF001
        span_days = [str(start + timedelta(days=i)) for i in range(ndays)]
        before = prev.to_pyarrow_dataset(partitions=[("day", "in", span_days)]).to_table(columns=["day", "seq"]).to_pandas()
        old_keys = set(zip(before["day"], before["seq"]))
        new_keys = set(zip(df.index.strftime("%Y-%m-%d"), df["seq"]))
        out["version_before"] = prev.version()
        out["added"] = len(new_keys - old_keys)
        out["removed"] = len(old_keys - new_keys)

    t1 = time.time()
    # `fetched` makes Delta history answer "what did the vendor serve when":
    # every re-fetch is a new table version, readable by time travel.
    days = store.replace_days(library, sym, df, metadata={
        "source": "eodhd-unicornbay-tickdata", "span": f"{start}/{ndays}d",
        "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    out["write_s"] = round(time.time() - t1, 1)

    t2 = time.time()
    dt = DeltaTable(store._path(library, sym), storage_options=store._opts)  # noqa: SLF001
    back = dt.to_pyarrow_dataset(partitions=[("day", "in", days)]).to_table().to_pandas()
    problems = []
    if len(back) != len(df):
        problems.append(f"{len(back)} rows read back, {len(df)} written")
    # No seq-uniqueness check: a UTC-day partition holds the tail of one US
    # session (00:00-04:00 UTC) and most of the next, and the vendor's seq
    # restarts per session, so a few collisions per busy day are legitimate
    # (AVGO 2026-03-05: 5 in 973k rows). That check mislabelled 86
    # symbol-weeks as failed and the retries of them spent a day's budget.
    sizes = back.groupby("day").size()
    out["verify_s"] = round(time.time() - t2, 1)
    out["days"] = {d: int(sizes.get(d, 0)) for d in days}
    if problems:
        raise RuntimeError("; ".join(problems))
    local.unlink()
    out["total_s"] = round(time.time() - t0, 1)
    return out


def load_week(monday: date, uni: Universe, key: str, store: TickStore, library: str, work: Path, workers: int,
              only: set[str] | None = None, force: bool = False, mode: str = "load", retry_max: int = 3) -> Progress:
    """mode: 'load' = pending symbols; 'settle' = every done symbol again plus
    retryable failures, diffing against the stored week; 'retry' = retryable
    failures only."""
    prog = Progress(store, library, monday)
    blacklist = Blacklist(store, library)
    symbols = [s for s in uni.for_week(monday) if s not in blacklist.blocked_for(monday)]
    if only:
        symbols = [s for s in symbols if s in only]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if mode == "settle":
        todo = [s for s in symbols if s in prog.state["done"] or s in prog.retryable(retry_max)]
    elif mode == "retry":
        todo = [s for s in symbols if s in prog.retryable(retry_max)]
        prog.state["retried_at"] = now
    else:
        todo = symbols if (only and force) else prog.pending(symbols)
    if prog.state["started"] is None:
        prog.state["started"] = now
    prog.state["universe"] = len(symbols)
    prog.save()
    log.info("week %s [%s]: %d members, %d done, %d to fetch", monday, mode, len(symbols), len(prog.state["done"]), len(todo))
    t0 = time.time()
    budget_hit = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(load_symbol, s, key, store, library, monday, work, mode == "settle"): s for s in todo}  # ndays=5
        for i, fut in enumerate(as_completed(futs), 1):
            s = futs[fut]
            try:
                r = fut.result()
                prog.record(s, r, None)
                log.info("[%s %3d/%d] %-6s rows=%9s fetch=%6.1fs write=%5.1fs verify=%5.1fs%s elapsed=%5.1fm",
                         monday, i, len(todo), s, f"{r.get('rows', 0):,}", r.get("fetch_s", 0),
                         r.get("write_s", 0), r.get("verify_s", 0),
                         f" added={r['added']:,}" if "added" in r else "", (time.time() - t0) / 60)
            except Budget:
                log.warning("API budget spent; abandoning this pass of week %s (nothing recorded for %s)", monday, s)
                pool.shutdown(wait=False, cancel_futures=True)
                budget_hit = True
                break
            except Exception as e:  # noqa: BLE001 -- one bad symbol must not stop the week
                prog.record(s, None, ("unservable: " if isinstance(e, Unservable) else "") + str(e))
                log.warning("[%s %3d/%d] %-6s FAILED %s", monday, i, len(todo), s, str(e)[:120])
                (work / f"{s}_{monday}.parquet").unlink(missing_ok=True)
    if budget_hit:
        prog.save()
        raise Budget(f"week {monday} [{mode}] interrupted")
    if mode == "settle":
        prog.state["settled"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    elif mode == "load":
        prog.state["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        blacklist.note_week(monday, prog.state["failed"], uni)
    prog.state["wall_minutes"] = round((prog.state.get("wall_minutes") or 0) + (time.time() - t0) / 60, 1)
    prog.save()
    st = prog.state
    log.info("week %s [%s] complete: %d loaded, %d empty, %d failed%s, %s ticks, %.1f min this pass",
             monday, mode, len(st["done"]), len(st["empty"]), len(st["failed"]),
             f", {sum(st['settle_added'].values()):,} rows added by settle" if mode == "settle" else "",
             f"{sum(r.get('rows', 0) for r in st['done'].values()):,}", (time.time() - t0) / 60)
    return prog


# -- daily rolling refresh -----------------------------------------------------------
ET = "America/New_York"


def daily_due(store: TickStore, library: str, now: datetime, key: str | None = None) -> date | None:
    """The New York date to run a daily pass for, or None. Due once per day
    after 02:05 New York time AND once the vendor serves the previous session:
    the first-party endpoint loads later than the marketplace product did
    (2026-09-15: SPY for Monday was still 404 at 02:25 ET), so the pass
    defers until SPY for yesterday answers with data or an empty holiday."""
    from zoneinfo import ZoneInfo
    local = now.astimezone(ZoneInfo(ET))
    if (local.hour, local.minute) < (2, 5):
        return None
    if local.weekday() in (6, 0):  # Sunday, Monday: yesterday had no session; save the ~5k calls
        return None
    # The day is the NEW YORK date, not the UTC one: the UTC date rolls over at
    # 20:00 ET, and keying on it ran the pass at 21:25 ET on 2026-09-08 --
    # before the vendor had loaded that session -- and then blocked the real
    # 02:05 run because "today's" file already existed.
    today = local.date()
    fsys, root = store._fs_and_root()  # noqa: SLF001
    st = _read_json(fsys, f"{root}/{library}/_progress/daily/{today}.json")
    if st and st.get("finished"):
        return None
    if key is not None and not _session_loaded(key, today - timedelta(days=1)):
        log.info("daily %s deferred: the vendor has not loaded %s yet", today, today - timedelta(days=1))
        return None
    return today


def _session_loaded(key: str, day: date) -> bool:
    """Whether the vendor serves `day` yet: SPY, limit=1. 404 = not loaded;
    200 with rows or an empty holiday answer = loaded."""
    frm = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    q = urllib.parse.urlencode({"s": "SPY", "from": frm, "to": frm + 86400 - 1, "limit": 1, "api_token": key, "fmt": "json"})
    try:
        _get_json(f"{TICKS}?{q}", tries=1)
        return True
    except NotFound:
        return False
    except (RuntimeError, Budget):
        return False


def daily_pass(today: date, uni: Universe, key: str, store: TickStore, library: str, work: Path,
               workers: int, vacuum_days: int) -> None:
    """Fetch the trailing 7 days [today-7, today) for every member, replacing
    those partitions: yesterday arrives, the six days before it pick up late
    prints. Records to _progress/daily/<today>.json (resumable), then
    finalises the weekly progress files those days belong to, and vacuums
    versions older than vacuum_days on Sundays."""
    fsys, root = store._fs_and_root()  # noqa: SLF001
    path = f"{root}/{library}/_progress/daily/{today}.json"
    first = today - timedelta(days=7)
    st = _read_json(fsys, path) or {"date": str(today), "window": [str(first), str(today - timedelta(days=1))],
                                   "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                   "finished": None, "done": {}, "failed": {}}
    lock = threading.Lock()
    blocked = Blacklist(store, library).blocked_daily(today)
    symbols = [s for s in uni.for_range(first, today - timedelta(days=1)) if s not in blocked]
    todo = [s for s in symbols if s not in st["done"]]
    log.info("daily %s [%s..%s]: %d members, %d done, %d to fetch", today, first, today - timedelta(days=1), len(symbols), len(st["done"]), len(todo))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(load_symbol, s, key, store, library, first, work, True, 7): s for s in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            s = futs[fut]
            try:
                r = fut.result()
                with lock:
                    st["failed"].pop(s, None)
                    st["done"][s] = {k: r[k] for k in ("rows", "days", "added", "removed", "version_before", "fetch_s", "note") if k in r}
                    _write_json(fsys, path, st)
                log.info("[daily %s %3d/%d] %-6s rows=%9s added=%7s fetch=%6.1fs elapsed=%5.1fm", today, i, len(todo), s,
                         f"{r.get('rows', 0):,}", f"{r.get('added', 0):,}", r.get("fetch_s", 0), (time.time() - t0) / 60)
            except Budget:
                log.warning("API budget spent; abandoning today's daily pass (resumes next cycle)")
                pool.shutdown(wait=False, cancel_futures=True)
                _write_json(fsys, path, st)
                raise
            except Exception as e:  # noqa: BLE001 -- one bad symbol must not stop the pass
                with lock:
                    st["failed"][s] = (("unservable: " if isinstance(e, Unservable) else "") + str(e))[:300]
                    _write_json(fsys, path, st)
                log.warning("[daily %s %3d/%d] %-6s FAILED %s", today, i, len(todo), s, str(e)[:120])
                (work / f"{s}_{first}.parquet").unlink(missing_ok=True)
    st["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    st["wall_minutes"] = round((time.time() - t0) / 60, 1)
    _write_json(fsys, path, st)
    log.info("daily %s complete: %d loaded, %d failed, %s ticks, %s rows added, %.1f min",
             today, len(st["done"]), len(st["failed"]), f"{sum(r['rows'] for r in st['done'].values()):,}",
             f"{sum(r.get('added', 0) for r in st['done'].values()):,}", (time.time() - t0) / 60)
    finalize_weeks(today, uni, store, library)
    if today.weekday() == 6 and vacuum_days > 0:
        from deltalake import DeltaTable
        for s in st["done"]:
            try:
                DeltaTable(store._path(library, s), storage_options=store._opts).vacuum(  # noqa: SLF001
                    retention_hours=vacuum_days * 24, dry_run=False, enforce_retention_duration=vacuum_days >= 7)
            except Exception as e:  # noqa: BLE001
                log.warning("vacuum %s failed: %s", s, str(e)[:120])
        log.info("vacuumed versions older than %d days for %d symbols", vacuum_days, len(st["done"]))


def finalize_weeks(today: date, uni: Universe, store: TickStore, library: str) -> None:
    """From the daily records, write/merge the weekly progress files of the
    weeks the trailing window has touched, so the weekly loader never redoes
    them. A week is `finished` once its Saturday has passed (every day
    attempted) and `settled` once its Friday is 7 days old (every day
    refreshed by the daily passes since). A symbol missing a trading day is
    listed as failed, so the retry sweep refetches its whole week."""
    fsys, root = store._fs_and_root()  # noqa: SLF001
    # per (symbol, day) -> rows, from every daily file of the last 3 weeks (latest wins)
    rows: dict[tuple[str, str], int] = {}
    for d in range(21, -1, -1):
        rec = _read_json(fsys, f"{root}/{library}/_progress/daily/{today - timedelta(days=d)}.json")
        if not rec:
            continue
        for sym, r in rec["done"].items():
            for day, n in r.get("days", {}).items():
                rows[(sym, day)] = n
    monday = today - timedelta(days=today.weekday())
    for back in (0, 7, 14):
        m = monday - timedelta(days=back)
        if m + timedelta(days=5) > today:  # Saturday not reached: still being loaded
            continue
        week_days = [str(m + timedelta(days=i)) for i in range(5)]
        trading = [d for d in week_days if any(rows.get((s, d), 0) > 0 for s in uni.for_week(m))]
        if not trading:
            continue
        prog = Progress(store, library, m)
        st = prog.state
        for sym in uni.for_week(m):
            have = {d: rows[(sym, d)] for d in trading if (sym, d) in rows}
            if len(have) == len(trading):
                st["done"].setdefault(sym, {"rows": sum(have.values()), "days": have, "source": "daily"})
                st["failed"].pop(sym, None)
            elif sym not in st["done"] and sym not in st["empty"]:
                st["failed"].setdefault(sym, f"daily: missing {sorted(set(trading) - set(have))}")
        st["universe"] = len(uni.for_week(m))
        st.setdefault("started", None)
        st["started"] = st["started"] or datetime.now(timezone.utc).isoformat(timespec="seconds")
        st["finished"] = st["finished"] or datetime.now(timezone.utc).isoformat(timespec="seconds")
        if m + timedelta(days=11) <= today:  # Friday + 7
            st["settled"] = st["settled"] or datetime.now(timezone.utc).isoformat(timespec="seconds")
        prog.save()
        log.info("week %s finalised from daily passes: %d done, %d failed, settled=%s", m, len(st["done"]), len(st["failed"]), bool(st["settled"]))


def latest_complete_monday(now: datetime | None = None) -> date:
    """The Monday of the newest week whose Friday ticks are already served.
    The API loads a day between ~00:00 and 02:00 ET the next morning, so a
    week is complete from Saturday 08:00 UTC."""
    now = now or datetime.now(timezone.utc)
    monday = (now.date() - timedelta(days=now.weekday()))
    if now.weekday() < 5 or (now.weekday() == 5 and now.hour < 8):
        monday -= timedelta(days=7)
    return monday


def _known_weeks(store: TickStore, library: str) -> list[date]:
    """Mondays that have a progress file, newest first."""
    from pyarrow import fs as pafs
    fsys, root = store._fs_and_root()  # noqa: SLF001
    sel = pafs.FileSelector(f"{root}/{library}/_progress", allow_not_found=True)
    out = []
    for info in fsys.get_file_info(sel):
        try:
            out.append(date.fromisoformat(info.base_name.removesuffix(".json")))
        except ValueError:
            continue
    return sorted(out, reverse=True)


def is_complete(store: TickStore, library: str, monday: date) -> bool:
    return Progress(store, library, monday).state["finished"] is not None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", help="Monday, YYYY-MM-DD: load this one week and exit")
    ap.add_argument("--daily", nargs="?", const="today", help="run one daily pass (trailing 7 days) for a UTC date (default today) and exit")
    ap.add_argument("--settle", help="Monday: settle-pass this one week now and exit")
    ap.add_argument("--retry", help="Monday: retry this one week's failures now and exit")
    ap.add_argument("--loop", action="store_true", help="walk backwards from the newest complete week, forever")
    ap.add_argument("--until", default=str(FLOOR), help="oldest Monday to load in --loop mode")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("SIP_WORKERS", "6")))
    ap.add_argument("--only", help="comma-separated symbols: restrict the universe")
    ap.add_argument("--force", action="store_true", help="with --only: re-fetch even if already done")
    a = ap.parse_args()
    if not (a.week or a.settle or a.retry or a.loop or a.daily):
        ap.error("--week, --daily, --settle, --retry or --loop")
    key = os.environ["EODHD_API_KEY"]
    library = os.environ.get("SIP_LIBRARY", "ticks_sip")
    work = Path(os.environ.get("SIP_WORK", "/tmp/sip"))
    work.mkdir(parents=True, exist_ok=True)
    store = TickStore(from_env())
    uni = Universe(key)
    log.info("universe: %d historical members, %d ranked by cap", len(uni.members), len(uni.caps))
    settle_lag = int(os.environ.get("SIP_SETTLE_LAG_DAYS", "7"))
    retry_max = int(os.environ.get("SIP_RETRY_MAX", "3"))
    vacuum_days = int(os.environ.get("SIP_VACUUM_DAYS", "30"))
    only = set(a.only.split(",")) if a.only else None

    if a.daily:
        from zoneinfo import ZoneInfo
        d = datetime.now(ZoneInfo(ET)).date() if a.daily == "today" else date.fromisoformat(a.daily)
        if only:
            uni.members = [m for m in uni.members if m[0] in only]
        daily_pass(d, uni, key, store, library, work, a.workers, vacuum_days)
        return 0

    if a.week:
        load_week(date.fromisoformat(a.week), uni, key, store, library, work, a.workers, only, a.force)
        return 0
    if a.settle:
        load_week(date.fromisoformat(a.settle), uni, key, store, library, work, a.workers, only, mode="settle", retry_max=retry_max)
        return 0
    if a.retry:
        load_week(date.fromisoformat(a.retry), uni, key, store, library, work, a.workers, only, mode="retry", retry_max=retry_max)
        return 0

    floor = date.fromisoformat(a.until)
    while True:
        try:
            _cycle(a, uni, key, store, library, work, floor, settle_lag, retry_max, vacuum_days)
        except Budget as e:
            log.warning("%s; sleeping 1 h before the next cycle", e)
            time.sleep(3600)


def _cycle(a, uni, key, store, library, work, floor, settle_lag, retry_max, vacuum_days) -> None:
    """One scheduling decision and the work it picks (see the module doc)."""
    if True:
        now = datetime.now(timezone.utc)
        # 0. the daily rolling refresh: yesterday plus late prints for the six days before
        d = daily_due(store, library, now, key)
        if d:
            daily_pass(d, uni, key, store, library, work, a.workers, vacuum_days)
            return
        newest = latest_complete_monday(now)
        # 1. the newest week, if it has never been loaded
        if not is_complete(store, library, newest):
            load_week(newest, uni, key, store, library, work, a.workers)
            return
        # 2/3. finished weeks due for a settle pass or a retry, newest first
        weeks = [Progress(store, library, m) for m in _known_weeks(store, library)]
        due = next((p for p in weeks if p.due_for_settle(settle_lag, now)), None)
        if due:
            load_week(date.fromisoformat(due.state["week"]), uni, key, store, library, work, a.workers, mode="settle", retry_max=retry_max)
            return
        due = next((p for p in weeks if p.due_for_retry(retry_max, now)), None)
        if due:
            load_week(date.fromisoformat(due.state["week"]), uni, key, store, library, work, a.workers, mode="retry", retry_max=retry_max)
            return
        # 4. backfill: the next week backwards that has never been loaded
        monday, target = newest, None
        while monday >= floor:
            if not is_complete(store, library, monday):
                target = monday
                break
            monday -= timedelta(days=7)
        if target is None:
            log.info("caught up back to %s and nothing due; sleeping 15 min", floor)
            time.sleep(900)
            return
        load_week(target, uni, key, store, library, work, a.workers)


if __name__ == "__main__":
    sys.exit(main())

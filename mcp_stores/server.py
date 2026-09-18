# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Read-only MCP server exposing Delta Lake and kdb+ discovery/query tools.

Runs beside openbb-api inside the NAS shared network namespace
(network_mode: service:tailscale), serving streamable-http MCP on
127.0.0.1:6902 (path /mcp), published by Tailscale Serve on
https://openbb.<your-tailnet>.ts.net:8444/mcp.

Why it exists: the REST deltalake/kdb providers only answer known-symbol OHLCV
queries and return 204 against this store's raw quote/trade tick data. These
tools give an agent discovery (libraries/symbols/tables/schemas), metadata
answered from the transaction log, time travel, plus capped raw reads.

Config (already present in the NAS env files):
  DELTA_S3_*         ENDPOINT/BUCKET/ACCESS/SECRET/PORT/SECURE for the
                     MinIO-backed store, or DELTA_URI for a path or s3:// URL.
                     The endpoint is a NAME, not a raw tailnet IP. MagicDNS
                     does not resolve inside these containers, but the
                     entrypoint's HOSTS_ALIAS writes the mapping into
                     /etc/hosts at start, and the name is what the MinIO
                     node's certificate is issued for -- a raw IP cannot
                     match it.
                     NOTE: credentials reach delta-rs as storage_options and
                     never enter a URI, which retired ArcticDB's
                     credential-bearing URI entirely. They can still surface
                     in a raw backend exception -- see _scrub / _bounded.
  KX_HOST / KX_PORT  kdb IPC target; KX_PORT may be the combined
                     "127.0.0.1:5000" form used by deps.env — parsed here.
  STORES_HOST/PORT   bind address, default 127.0.0.1:6902.
  STORES_TIMEOUT_S   hard wall-clock timeout (seconds) applied to every
                     Delta/kdb+ backend call, default 15. Bounds how long
                     a dead S3 endpoint or unreachable kdb+ can hang a tool
                     call (and, transitively, an anyio worker thread).

  DELTA_LIBRARY is intentionally NOT read here. It configures the REST
  deltalake provider/other services sharing these env files; this server
  always requires an explicit `library` argument (discovered via
  delta_list_libraries) because cross-library reads are the point of these
  tools, not a default to paper over.

Safety: all tools are read-only. kdb access NEVER interpolates user input into
q source: the table name is validated against tables[], symbols/timestamps are
regex-gated and passed as typed ARGUMENTS to fixed q lambdas (_Q_META,
_Q_SELECT). Strings cross IPC as char vectors (bytes) and are cast server-side
(`$s / "P"$st), so a hostile string can never become q code. _Q_META and
_Q_SELECT are the only two strings ever sent as q source, always verbatim.
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeoutError

from fastmcp import FastMCP

import daykeys

MAX_ROWS = 10_000
STORES_TIMEOUT_S = float(os.environ.get("STORES_TIMEOUT_S", "15"))
_IDENT_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
_TIME_RE = re.compile(r"\A[0-9T:. \-]{1,35}\Z")  # ISO-ish date/timestamp text

# Fixed q sources. Only ever sent verbatim with arguments — never edited.
_Q_META = '{[t] 0!meta get `$t}'
_Q_SELECT = (
    '{[t;sc;tc;s;st;et;n] '
    'tbl:get `$t; '
    'm:(count tbl)#1b; '
    'if[(count s)>0; m:m and (tbl `$sc)=`$s]; '
    'if[(count st)>0; m:m and (tbl `$tc)>="P"$st]; '
    'if[(count et)>0; m:m and (tbl `$tc)<="P"$et]; '
    'select[n] from tbl where m}'  # [n] bounds rows AT the select, not after
)
_KDB_TIME_TYPES = tuple("pmdznuvt")  # q temporal type chars

# mask_error_details is a FastMCP behavior that could change upstream; it's
# belt-and-braces on top of the source-level scrub in _bounded (finding 1).
mcp = FastMCP("stores", mask_error_details=True)


# ---------- credential scrubbing / backend call bounding ----------

# Covers both shapes, wherever they show up in exception text: the legacy URI query form (access=/secret=) and
# delta-rs storage_options, which surface in exception text as
# `access_key_id: ...` / `secret_access_key=...`. The credential no longer
# rides in a URI, but it can still reach an error string.
_CRED_PARAM_RE = re.compile(
    r"(?i)\b((?:aws_)?(?:access|secret)(?:_key_id|_access_key)?)\s*[=:]\s*[^&,\s\"'}]*"
)
_S3_URI_RE = re.compile(r"s3s?://\S+")


def _scrub(text: str) -> str:
    """Redact S3 credentials from arbitrary error text before it can reach
    the caller. Idempotent and harmless on text with nothing to redact."""
    text = _S3_URI_RE.sub("s3://<redacted>", text)
    text = _CRED_PARAM_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    return text


def _bounded(fn, *args, **kwargs):
    """Run a raw Delta/kdb backend call with a hard wall-clock timeout, and
    scrub any embedded S3 credentials out of its exception text.

    Only wraps calls that touch deltalake directly (never our own validation
    raises, which don't contain secrets and should keep their exception
    type). delta-rs/pykx give no cooperative cancellation, so on timeout the
    worker thread is abandoned rather than joined -- this call always
    returns or raises within STORES_TIMEOUT_S regardless of what the backend
    is doing.
    """
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(fn, *args, **kwargs)
        try:
            return fut.result(timeout=STORES_TIMEOUT_S)
        except _FutureTimeoutError:
            raise TimeoutError(
                f"backend call timed out after {STORES_TIMEOUT_S}s"
            ) from None
        except Exception as e:
            scrubbed = _scrub(str(e))
            try:
                raise type(e)(scrubbed) from None
            except TypeError:  # exception type needs >1 positional arg
                raise RuntimeError(scrubbed) from None
    finally:
        ex.shutdown(wait=False)


# ---------- shared helpers ----------

def _delta(library: str):
    """A DeltaStore for `library`. Credentials never enter a URI here --
    delta-rs takes them as storage_options, which is what retired the
    combined ARCTICDB_URI form and its leak risk."""
    from openbb_deltalake.store import DeltaStore  # lazy: tests use tmp paths

    return _bounded(DeltaStore, None, library)


def _kdb_conn():
    import pykx  # lazy: unit tests mock the module

    host = os.environ.get("KX_HOST", "127.0.0.1")
    port = os.environ.get("KX_PORT", "5000")
    if ":" in port:  # deps.env combined form: KX_PORT=127.0.0.1:5000
        host, port = port.rsplit(":", 1)
    return pykx.SyncQConnection(
        host=host,
        port=int(port),
        connection_timeout=STORES_TIMEOUT_S,
        timeout=STORES_TIMEOUT_S,
    )


def _decode_df(df):
    """Decode q symbol/char cells that arrive as bytes."""

    def dec(v):
        return v.decode() if isinstance(v, (bytes, bytearray)) else v

    func = getattr(df, "map", None) or df.applymap  # pandas >=2.1 / older
    return func(dec)


def _records(df):
    """DataFrame -> JSON-safe row dicts (ISO-8601 timestamps)."""
    return json.loads(df.to_json(orient="records", date_format="iso"))


def _check_ident(kind, value):
    if not _IDENT_RE.match(value or ""):
        raise ValueError(f"invalid {kind} {value!r}: must match {_IDENT_RE.pattern}")


# ---------- Delta Lake tools ----------

def delta_list_libraries() -> list[str]:
    """List Delta libraries in the shared store (e.g. 'ticks')."""
    from openbb_deltalake import describe as D  # lazy: tests use tmp paths

    store = _delta("_")
    return sorted(_bounded(D.list_libraries, store.base, store.storage_options))


def _raw_keys(library: str) -> list[str]:
    """Every Delta table in `library`, by its raw key, sorted."""
    _check_ident("library", library)
    if library not in delta_list_libraries():
        raise ValueError(
            f"unknown library {library!r}; call delta_list_libraries first"
        )
    return sorted(_bounded(_delta(library).list_symbols))


def delta_list_symbols(library: str) -> list[str]:
    """List symbols stored in a library.

    A symbol whose tables are keyed by day (`AAPL_2026_09_11`, the EOD dump's
    layout) is listed ONCE, as `AAPL`; the read tools expand it to the days a
    window needs. See daykeys.
    """
    return daykeys.bases(_raw_keys(library))


def _require_symbol(library: str, symbol: str):
    """(store, raw keys) for library, once symbol is known to be in it.

    Every tool validates through here so the "unknown X; call Y first" error
    contract is one implementation, not one per entry point. `symbol` may be
    a base (`AAPL`) or a raw day key (`AAPL_2026_09_11`): the second keeps an
    older client, or an agent quoting a key it was shown, working.
    """
    _check_ident("symbol", symbol)
    raw = _raw_keys(library)
    if symbol not in raw and symbol not in daykeys.bases(raw):
        raise ValueError(
            f"unknown symbol {symbol!r} in {library!r}; call delta_list_symbols first"
        )
    return _delta(library), raw


def delta_describe(library: str, symbol: str) -> dict:
    """Row count, stored date range and column dtypes for a symbol.

    Answered from the transaction log: this reads no rows, however large the
    symbol is. A day-keyed symbol is the sum of its day tables -- rows added,
    the range's outer bounds taken, columns from the newest day -- plus
    `days`, so a strip can say how many tables stand behind the number.
    That is one log walk per day; a library of forty symbols over a few
    weeks is a few dozen small reads, measured against MinIO before merge.
    """
    from openbb_deltalake import describe as D

    store, raw = _require_symbol(library, symbol)
    keys = daykeys.day_keys(raw, symbol)
    parts = [_bounded(D.describe, store, key) for key in keys]
    ranges = [p["date_range"] for p in parts if p["date_range"]]
    # ponytail: min/max over the log's own string form. Every table in a
    # library is written by one process with one precision, so the strings
    # sort as the instants do; parse them if a library ever mixes writers.
    return {
        "library": library,
        "symbol": symbol,
        "row_count": sum(p["row_count"] for p in parts),
        "date_range": [min(r[0] for r in ranges), max(r[1] for r in ranges)] if ranges else None,
        "columns": parts[-1]["columns"],
        "days": len(keys),
    }


def _iso_ms(timestamp: str) -> str:
    """Epoch-millisecond text (what delta-rs's history carries) as ISO UTC.

    Millisecond precision on purpose: the string is what a client sends back
    as `as_of`, and a commit's own instant must resolve to that commit. Text
    that is not all digits is left alone -- a test fake, or a future delta-rs
    that formats for us.
    """
    if not timestamp.isdigit():
        return timestamp
    from datetime import datetime, timezone

    ms = int(timestamp)
    dt = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def delta_history(library: str, symbol: str) -> list[dict]:
    """Delta versions for a symbol, newest first -- the time-travel choices.

    A day-keyed symbol answers the union of its day tables' commits. Version
    numbers are per table and do not line up across days, so a client that
    spans tables travels by timestamp; the number is kept for single tables.
    """
    from openbb_deltalake import describe as D

    store, raw = _require_symbol(library, symbol)
    entries = []
    for key in daykeys.day_keys(raw, symbol):
        entries.extend(
            {"version": e["version"], "timestamp": _iso_ms(e["timestamp"])}
            for e in _bounded(D.history, store, key)
        )
    return sorted(entries, key=lambda e: (e["timestamp"], e["version"]), reverse=True)


def _committed_by(store, key: str, as_of) -> bool:
    """Whether `key` existed at a timestamp `as_of`.

    delta-rs (1.6.3) answers an as_of earlier than a table's first commit by
    loading version 0, not by raising -- so a day table written AFTER the
    chosen instant would silently contribute rows that did not exist then.
    One history walk per key is the price of a truthful time travel; an int
    version or no as_of skips the walk.
    """
    if as_of is None or isinstance(as_of, int):
        return True
    from openbb_deltalake import describe as D
    from pandas import Timestamp

    first_ms = min(int(e["timestamp"]) for e in _bounded(D.history, store, key))
    ts = Timestamp(as_of)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return Timestamp(first_ms, unit="ms", tz="UTC") <= ts


def _read_one(store, key: str, start, end, tail_rows: int, as_of):
    """(rows in range, frame) for one table -- the v11.0.0 read, per key.

    The READ is bounded, not just the response: with no start/end this reads
    only the trailing files the transaction log says hold those rows, so an
    unfiltered call never materializes the whole table -- the guarantee
    ArcticDB gave via Library.tail, which delta-rs has no equivalent for.
    """
    from openbb_deltalake import describe as D

    if start or end:
        df = _bounded(
            store.read, key, start_date=start, end_date=end,
            as_of=as_of, output="dataframe",
        )
        return len(df), df
    total = _bounded(D.describe, store, key)["row_count"]
    return total, _bounded(store.read_trailing, key, tail_rows, as_of)


def delta_read(
    library: str,
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    tail_rows: int = 1000,
    as_of: str | int | None = None,
) -> dict:
    """Read a symbol from a Delta library.

    start/end are ISO dates/timestamps filtering the stored index. as_of is an
    int Delta version or an ISO timestamp for time travel. Returns at most
    tail_rows rows (the most recent in range, hard cap MAX_ROWS) as JSON
    records with ISO timestamps. For a symbol read across several day tables,
    `as_of` must be a timestamp; an int version is rejected, since versions
    are per table.

    A symbol stored as one table per day (see daykeys) is read across the day
    tables the window covers, oldest first, and tailed as one frame. With no
    window it reads its newest day only, so it stays as bounded as a single
    table. With no window and a timestamp `as_of`, the day read is the newest
    one already committed at `as_of`. A window that covers no day table
    answers zero rows, not an error: the symbol exists, that stretch of it
    does not. `_bounded`'s deadline applies per table, so a symbol spanning N
    days may take up to N times STORES_TIMEOUT_S -- bounded and linear, never
    the whole symbol.
    """
    import pandas as pd

    for name, val in (("start", start), ("end", end)):
        if val is not None and val != "" and not _TIME_RE.match(val):
            raise ValueError(f"invalid {name} {val!r}: must be an ISO date or naive timestamp")

    tail_rows = max(1, min(int(tail_rows), MAX_ROWS))
    store, raw = _require_symbol(library, symbol)
    if isinstance(as_of, str) and as_of.isdigit():
        as_of = int(as_of)

    if not (start or end) and isinstance(as_of, str):
        # No window plus a time travel: "the newest day" means the newest day
        # that existed THEN, or an As-of older than the newest table would
        # read as nothing. Newest-first, first hit, still one table (D4).
        keys = [
            k for k in reversed(daykeys.day_keys(raw, symbol)) if _committed_by(store, k, as_of)
        ][:1]
    else:
        keys = daykeys.in_window(raw, symbol, start, end)

    if isinstance(as_of, int) and len(keys) > 1:
        # Version numbers are per table and do not line up across days: v3 of
        # Monday is not v3 of Tuesday, and a day lacking the number would be a
        # raw delta-rs error. A timestamp applies uniformly; delta_history
        # answers in that form.
        raise ValueError(
            "as_of must be a timestamp for a symbol that spans days; call delta_history first"
        )

    parts = [
        _read_one(store, key, start, end, tail_rows, as_of)
        for key in keys
        if _committed_by(store, key, as_of)
    ]
    total = sum(n for n, _ in parts)
    frames = [df for _, df in parts if len(df)]
    df = pd.concat(frames).tail(tail_rows).reset_index() if frames else pd.DataFrame()

    return {
        "library": library,
        "symbol": symbol,
        "total_rows_in_range": total,
        "returned_rows": len(df),
        "rows": _records(df),
    }


# ---------- kdb+ tools ----------

def _tables(conn) -> list[str]:
    raw = conn("tables[]").py()
    return sorted(
        t.decode() if isinstance(t, (bytes, bytearray)) else str(t) for t in raw
    )


def _meta_df(conn, table):
    return _decode_df(conn(_Q_META, table.encode()).pd())


def _cols_by_type(meta) -> tuple[str, str]:
    """(symbol_column, time_column) from a q meta frame; '' when absent."""
    sym_col = time_col = ""
    for _, row in meta.iterrows():
        if not sym_col and row["t"] == "s":
            sym_col = row["c"]
        if not time_col and row["t"] in _KDB_TIME_TYPES:
            time_col = row["c"]
    return sym_col, time_col


def kdb_tables() -> list[str]:
    """List tables defined in the kdb+ server."""
    with _kdb_conn() as conn:
        return _tables(conn)


def kdb_table_schema(table: str) -> list[dict]:
    """Column names/types of a kdb+ table (q meta: c=column, t=type char)."""
    _check_ident("table", table)
    with _kdb_conn() as conn:
        if table not in _tables(conn):
            raise ValueError(f"unknown table {table!r}; call kdb_tables first")
        return _records(_meta_df(conn, table))


def kdb_select(
    table: str,
    symbol: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int = 1000,
) -> dict:
    """Filtered read of a kdb+ table (parameterized; never raw q).

    symbol filters the table's symbol-typed column (empty/whitespace means
    "no filter", same as omitting it); start_time/end_time are ISO
    timestamps filtering the first temporal column. Returns at most `limit`
    rows (hard cap 10000) -- bounded at the q select itself (`select[n]`),
    not by truncating a fully materialized result.
    """
    _check_ident("table", table)
    if symbol is not None:
        symbol = symbol.strip() or None
    if symbol is not None:
        _check_ident("symbol", symbol)
    for name, val in (("start_time", start_time), ("end_time", end_time)):
        if val is not None and not _TIME_RE.match(val):
            raise ValueError(f"invalid {name} {val!r}")
    limit = max(1, min(int(limit), MAX_ROWS))

    with _kdb_conn() as conn:
        if table not in _tables(conn):
            raise ValueError(f"unknown table {table!r}; call kdb_tables first")
        sym_col, time_col = _cols_by_type(_meta_df(conn, table))
        if symbol and not sym_col:
            raise ValueError(f"table {table!r} has no symbol-typed column")
        if (start_time or end_time) and not time_col:
            raise ValueError(f"table {table!r} has no temporal column")
        res = conn(
            _Q_SELECT,
            table.encode(),
            sym_col.encode(),
            time_col.encode(),
            (symbol or "").encode(),
            (start_time or "").encode(),
            (end_time or "").encode(),
            limit,
        )
        df = _decode_df(res.pd())
        return {"table": table, "returned_rows": len(df), "rows": _records(df)}


# Registered here (not via decorator) so tests import the plain functions.
for _fn in (
    delta_list_libraries,
    delta_list_symbols,
    delta_describe,
    delta_history,
    delta_read,
    kdb_tables,
    kdb_table_schema,
    kdb_select,
):
    mcp.tool(_fn)


if __name__ == "__main__":
    # CORS is required by browser-based MCP clients (the BDOBB desktop app's
    # webview discovers tools with window.fetch, which preflights). Without
    # it, OPTIONS /mcp returned 405 with no Access-Control headers and WebKit
    # reported "TypeError: Load failed" — while curl, Rita (server-side), and
    # node test clients all worked, since none of them preflight. Mirrors the
    # openbb-mcp-server (:8443) CORS posture; mcp-session-id must be exposed
    # or streamable-http clients cannot read their session handle.
    import uvicorn
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware

    app = mcp.http_app(
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
                expose_headers=["mcp-session-id", "mcp-protocol-version"],
            )
        ]
    )
    uvicorn.run(
        app,
        host=os.environ.get("STORES_HOST", "127.0.0.1"),
        port=int(os.environ.get("STORES_PORT", "6902")),
    )

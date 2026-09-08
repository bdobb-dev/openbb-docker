# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The type boundary between pandas and q, on a window that reaches the present.

The fakes elsewhere in this suite hand back Python-native values and merge bar
frames with `pd.concat`, which coerces anything into anything. Real q does not:
`upsert` requires the incoming column's q type to MATCH the stored column's
type and raises `'type` otherwise. pandas, meanwhile, infers a column's dtype
from the values of whichever batch it is handed -- so a provider column that is
float64 in a 251-row year is `object` in a 2-row tail batch where every value
happens to be None.

That mismatch cannot occur on a window ending in the past: its second request is
a full cache hit and writes nothing. It is CERTAIN on any window ending today,
because the forming bar is never cached and the tail gap therefore writes a
second, small batch into the same table on every single request.

`QTypedConn` below is a connection fake that models exactly that one q rule --
column types, and an upsert that refuses to coerce them. Everything else about
it is a convenience. What these tests prove is that `KdbStore.write_bars`
conforms a batch to the stored table's types; what they do NOT prove is that
PyKX maps every pandas dtype to the q type assumed here, or anything else about
real q. `scripts/live_check.py` covers that against a licensed q.
"""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from openbb_kdb.cache import ReadThroughCache
from kdb_store.config import KdbConfig
from kdb_store.store import KdbStore

D = lambda s: datetime.fromisoformat(s)  # noqa: E731


class QTypeError(RuntimeError):
    """Stands in for PyKX's QError('type')."""


def q_type(dtype) -> str:
    """The q type character PyKX produces for a pandas dtype.

    Approximate but faithful where it matters: an all-null object column
    becomes a GENERIC list (type 0h, meta shows blank), which is a different q
    type from the float column the same provider field produces when it has
    values -- and that is the whole bug.
    """
    if dtype.kind == "O":
        return " "
    if dtype.kind == "M":
        return "p"
    if dtype.kind == "f":
        return "f"
    if dtype.kind == "b":
        return "b"
    if dtype.kind in "iu":
        return {1: "x", 2: "h", 4: "i"}.get(dtype.itemsize, "j")
    return "s"


class _Wrapped:
    def __init__(self, value):
        self._value = value

    def py(self):
        return self._value

    def pd(self):
        return self._value


class QTypedConn:
    """A connection fake that enforces q's upsert typing rule.

    Only the statements `KdbStore` actually issues are understood; anything
    else answers with a null, exactly as an unrecognised query would look to
    the store's `.py()`/`.pd()` call sites.
    """

    def __init__(self):
        self.tables: dict[str, pd.DataFrame] = {}
        self.coverage: list[tuple] = []
        self.data: dict = {}
        self.queries: list[str] = []
        self.heap = 1_000

    def __setitem__(self, key, value):
        self.data[key] = value

    def __call__(self, query, *args):
        self.queries.append(query)
        return _Wrapped(self._dispatch(query, args))

    def _dispatch(self, query, args):
        if "`cov in key" in query:  # _INIT_SCHEMA
            return None
        if ".Q.w[]" in query:
            return {"used": self.heap, "heap": self.heap, "wmax": 4_000_000_000}
        if query.startswith("0#"):
            return self.tables[query[2:]].iloc[0:0]
        if query.endswith("in key `.") and query.startswith("`"):
            return query[1:].split(" ")[0] in self.tables
        if "upsert incoming_bars" in query:
            return self._upsert(query.split(":")[0].strip())
        if query.startswith("delete incoming_bars"):
            self.data.pop("incoming_bars", None)
            return None
        if query.startswith("{[qwlo;qwhi]"):
            return self._select_bars(query, args)
        if "select s, e from .cache.cov" in query:
            sym, iv = args[0].py(), args[1].py()
            return [(s, e) for k, i, s, e in self.coverage if (k, i) == (sym, iv)]
        if ".cache.cov upsert" in query:
            self.coverage.append(tuple(a.py() for a in args))
            return None
        if "select sym, iv, atime from .cache.lru" in query:
            return []
        if "delete from .cache.cov where" in query:
            sym, iv = args[0].py(), args[1].py()
            self.coverage = [r for r in self.coverage if (r[0], r[1]) != (sym, iv)]
            return None
        if query.startswith("if[`") and " from `.]" in query:  # drop a bar table
            self.tables.pop(query.split("delete ")[1].split(" ")[0], None)
            return None
        return None

    def _select_bars(self, query, args):
        name = query.split("from ")[1].split(" ")[0]
        frame = self.tables.get(name)
        if frame is None:
            return pd.DataFrame()
        lo, hi = args[0].py(), args[1].py()
        return frame[(frame["t"] >= lo) & (frame["t"] <= hi)].reset_index(drop=True)

    def _upsert(self, name):
        incoming = self.data["incoming_bars"]
        existing = self.tables.get(name)
        if existing is not None:
            for column in incoming.columns:
                if column not in existing.columns:
                    continue
                if q_type(incoming[column].dtype) != q_type(existing[column].dtype):
                    raise QTypeError("type")
            merged = pd.concat([existing, incoming])
        else:
            merged = incoming.copy()
        merged = merged.sort_values("t").drop_duplicates(subset="t", keep="last")
        # An upsert never changes the stored column types, so neither does this.
        if existing is not None:
            merged = merged.astype({c: existing[c].dtype for c in existing.columns})
        self.tables[name] = merged.reset_index(drop=True)
        return None


class OwnerThreadSession:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn

    def run(self, fn):
        return fn()


def cfg(**kw) -> KdbConfig:
    base = dict(host="127.0.0.1", port=5000, bind_host="127.0.0.1", may_spawn=True,
                memory_mb=1024, watermark=0.75, upstream="yfinance", qhome="/opt/kx",
                qlic="/opt/kx", local_qhome="/opt/kx")
    base.update(kw)
    return KdbConfig(**base)


def test_upsert_fake_rejects_a_drifting_column_type():
    """Guard on the guard: without conforming, this fake must reproduce 'type.

    If it did not, every test below would pass vacuously.
    """
    conn = QTypedConn()
    conn.tables["bars_X_1d"] = pd.DataFrame({
        "t": pd.to_datetime(["2026-08-03"]), "dividend": [0.0],
    })
    conn["incoming_bars"] = pd.DataFrame({
        "t": pd.to_datetime(["2026-08-04"]), "dividend": [None],
    })
    with pytest.raises(QTypeError):
        conn("bars_X_1d: `t xasc 0!(`t xkey bars_X_1d) upsert incoming_bars")


def test_write_bars_conforms_an_all_null_column_to_the_stored_type():
    """The observed failure: a 251-row batch types `dividend` float64, the
    2-row tail batch has None for every dividend and types it object, and q
    refuses the upsert. Same provider, same column, different batch."""
    conn = QTypedConn()
    store = KdbStore(OwnerThreadSession(conn))

    store.write_bars("NVDA", "1d", pd.DataFrame({
        "t": pd.to_datetime(["2026-08-03", "2026-08-04"]),
        "close": [206.6, 211.9], "volume": [128406900, 133610200],
        "dividend": [0.0, 0.0], "vwap": [None, None],
    }))
    store.write_bars("NVDA", "1d", pd.DataFrame({
        "t": pd.to_datetime(["2026-08-04", "2026-08-05"]),
        "close": [211.9, 213.4], "volume": [133610200.0, np.nan],
        "dividend": [None, None], "vwap": [None, None],
    }))

    stored = conn.tables["bars_NVDA_1d"]
    assert len(stored) == 3
    assert q_type(stored["dividend"].dtype) == "f"
    assert q_type(stored["volume"].dtype) == "j"
    # A NaN volume must survive as q's integer null, not blow up the cast.
    assert stored["volume"].iloc[-1] == np.iinfo(np.int64).min


def test_write_bars_conforms_a_typed_column_to_a_stored_generic_one():
    """The mirror case: the FIRST batch is the all-null one, so the stored
    column is an untyped generic list and a later batch arrives typed."""
    conn = QTypedConn()
    store = KdbStore(OwnerThreadSession(conn))

    store.write_bars("NVDA", "1d", pd.DataFrame({
        "t": pd.to_datetime(["2026-08-03"]), "vwap": [None],
    }))
    store.write_bars("NVDA", "1d", pd.DataFrame({
        "t": pd.to_datetime(["2026-08-04"]), "vwap": [211.5],
    }))

    stored = conn.tables["bars_NVDA_1d"]
    assert len(stored) == 2
    assert q_type(stored["vwap"].dtype) == " "
    assert stored["vwap"].iloc[-1] == 211.5


def test_write_bars_leaves_an_unconvertible_column_alone():
    """A column that cannot be cast must be handed through untouched, so the
    upsert fails exactly as it does today (bypass) rather than silently
    writing a value the provider never sent."""
    conn = QTypedConn()
    store = KdbStore(OwnerThreadSession(conn))
    store.write_bars("NVDA", "1d", pd.DataFrame({
        "t": pd.to_datetime(["2026-08-03"]), "volume": [1],
    }))
    with pytest.raises(QTypeError):
        store.write_bars("NVDA", "1d", pd.DataFrame({
            "t": pd.to_datetime(["2026-08-04"]), "volume": ["not a number"],
        }))


def test_write_bars_never_truncates_a_fractional_value_into_a_stored_int_column():
    """`astype` silently truncates float->int (133610200.7 -> 133610200)
    without raising, so a naive cast would corrupt data with no error
    anywhere. A column that cannot be cast exactly must fall through to the
    same bypass path as any other unconvertible column instead."""
    conn = QTypedConn()
    store = KdbStore(OwnerThreadSession(conn))
    store.write_bars("NVDA", "1d", pd.DataFrame({
        "t": pd.to_datetime(["2026-08-03"]), "volume": [128406900],
    }))
    with pytest.raises(QTypeError):
        store.write_bars("NVDA", "1d", pd.DataFrame({
            "t": pd.to_datetime(["2026-08-04"]), "volume": [133610200.7],
        }))
    # The bypass means nothing was ever appended; the stored column is
    # untouched and definitely never holds a truncated value.
    stored = conn.tables["bars_NVDA_1d"]
    assert list(stored["volume"]) == [128406900]


def _yfinance_shaped(days, dividends_as_none, forming=None):
    """Rows shaped like a provider response, with per-batch dtype drift.

    `dividend` is 0.0 in a batch that spans a dividend record date and None in
    a short batch that does not -- which is what makes pandas type the same
    column float64 once and object the next time. Prices are a function of the
    DATE, so a bar re-fetched in the tail overlap matches the cached one and
    the corporate-action backstop stays quiet.
    """
    rows = []
    for day in days:
        seed = datetime.fromisoformat(day).toordinal() % 50
        rows.append({
            "date": day,
            "open": 200.0 + seed, "high": 205.0 + seed,
            "low": 199.0 + seed, "close": 202.0 + seed,
            "volume": 100_000_000 + seed,
            "vwap": None, "split_ratio": None,
            "dividend": None if dividends_as_none else 0.0,
        })
    if forming is not None:
        # The still-forming bar: no volume yet, which is what turns an int64
        # column into a float64 one.
        rows.append({
            "date": forming, "open": 210.0, "high": 211.0, "low": 209.0,
            "close": 210.5, "volume": None, "vwap": None,
            "split_ratio": None, "dividend": None,
        })
    return rows


async def test_a_window_ending_now_hits_the_cache_on_the_second_request():
    """The end-to-end regression: two identical requests for a window that
    reaches today, through the REAL KdbStore, against a q-typed connection.

    Before the fix the second request raised `'type` inside `write_bars` and
    the whole read-through degraded to `cache="bypass"`.
    """
    conn = QTypedConn()
    store = KdbStore(OwnerThreadSession(conn))
    cache = ReadThroughCache(store, cfg())

    now = datetime(2026, 8, 5, 15, 30)
    start = datetime(2025, 8, 5)
    end = datetime(2026, 8, 5)
    history = [
        (start + timedelta(days=i)).date().isoformat()
        for i in range((end - start).days)
    ]

    fetched = []

    async def fake_fetch(provider, model, params, credentials):
        fetched.append((params["start_date"], params["end_date"]))
        if len(fetched) == 1:  # the cold year: dividends present, so float64
            return _yfinance_shaped(history, dividends_as_none=False)
        # the tail: three overlap bars plus today's forming bar, all-None
        # dividends (object) and a null volume (float64)
        return _yfinance_shaped(history[-3:], dividends_as_none=True,
                                forming=end.date().isoformat())

    cache._fetch_gap = fake_fetch
    args = ("NVDA", "1d", start, end, "EquityHistorical", {"symbol": "NVDA"}, None)

    _, first = await cache.get(*args, now=now)
    assert first["cache"] == "miss"

    rows, second = await cache.get(*args, now=now)
    assert second["cache"] == "partial", second
    # Exactly one gap: the forming bar. Nothing else was refetched.
    assert second["gaps_fetched"] == 1
    assert second["rows_from_cache"] > 0
    assert len(fetched) == 2
    # The forming bar is served, and the year behind it came out of the cache.
    assert any(pd.Timestamp(r["date"]) == pd.Timestamp(end) for r in rows)
    assert len(rows) == len(history) + 1


async def test_a_window_ending_now_never_bypasses_over_many_requests():
    """The demo chart polls. A tail write happens on every single request, so
    the type boundary is crossed every time, not once."""
    conn = QTypedConn()
    store = KdbStore(OwnerThreadSession(conn))
    cache = ReadThroughCache(store, cfg())

    now = datetime(2026, 8, 5, 15, 30)
    start, end = datetime(2026, 6, 5), datetime(2026, 8, 5)
    history = [
        (start + timedelta(days=i)).date().isoformat()
        for i in range((end - start).days)
    ]
    calls = {"n": 0}

    async def fake_fetch(provider, model, params, credentials):
        calls["n"] += 1
        if calls["n"] == 1:
            return _yfinance_shaped(history, dividends_as_none=False)
        return _yfinance_shaped(history[-3:], dividends_as_none=True,
                                forming=end.date().isoformat())

    cache._fetch_gap = fake_fetch
    args = ("NVDA", "1d", start, end, "EquityHistorical", {"symbol": "NVDA"}, None)

    statuses = []
    for _ in range(5):
        _, meta = await cache.get(*args, now=now)
        statuses.append(meta["cache"])
    assert statuses == ["miss", "partial", "partial", "partial", "partial"], statuses

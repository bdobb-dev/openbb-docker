"""EODHD reference-data clients and parsers (task-3 brief).

Covers the five vendor "reference"/full-refresh endpoints that feed the
identifier and index-membership tables (baseline design doc §8.2, §9):
exchange symbol lists, delisted symbol lists, symbol-change history, index
component membership, and per-symbol fundamentals. Each is a full-refresh
snapshot pull (not a time-ranged tick fetch), so there is no
request_from_sec/request_to_sec bookkeeping - just the one request-scope
column each bronze `*_capture` table carries for the endpoints that are
actually exchange/index/symbol-scoped on the vendor side
(`tick_vault.schemas._generic_capture_fields` callers: `exchange_code`,
`index_vendor_symbol`, `request_symbol`/`request_exchange_code`).
`symbol-change-history` is the one exception: EODHD's endpoint is global
(no `{exchange}` path/query parameter), so `eodhd_symbol_change_capture`
carries no such column and `get_symbol_changes()` takes no `exchange`
argument - any exchange scoping of symbol-change rows happens downstream,
at resolution time, against the `old`/`new` codes already in the parsed
rows, never by filtering the vendor request.

URL verification caveat
------------------------
The exact EODHD paths/query-parameter names below (`_ENDPOINTS`) are
best-effort from EODHD's public docs, NOT yet verified against a live
response. They are deliberately collected in one dict, keyed by the same
names `ReferenceClient` methods use internally, so that fixing a wrong URL
during the Phase-0 live smoke test (mentioned in the task-3 brief) is a
one-line change here, not a hunt through five methods.

Split for testability (no pyarrow/deltalake/pytest in the authoring
sandbox; network blocked)
--------------------------
- The parsers (`parse_symbols`, `parse_changes`, `parse_components`,
  `parse_fundamentals`) are pure functions: `payload (dict/list) -> pandas
  DataFrame`. They touch nothing but the stdlib + pandas, so they're
  exercised directly by the sandbox mini-runner
  (`tests/test_eodhd_reference_parsers.py`, plain asserts, no pytest
  import) against JSON fixtures under `tests/fixtures/eodhd_ref/`.
- `ReferenceClient` wires those parsers to `CaptureStore.fetch_and_capture`
  (itself lazy-importing pyarrow/deltalake/schemas), so it needs a real
  Delta lake to test end-to-end. That's `tests/deferred/
  test_eodhd_reference.py` (pytest, `FakeTransport` + real tmp_path lake).
  This module itself has no pyarrow/deltalake import at module scope -
  only `tick_vault.capture` (imported bare, matching its own no-pyarrow-
  at-module-scope contract) and pandas.

Index-components merge rule
----------------------------
EODHD's fundamentals-shaped index payload (`/api/fundamentals/<INDEX>`)
carries index membership in up to two sections: `Components` (the
*current* membership, keyed by vendor symbol, with no date range) and
`HistoricalTickerComponents` (dated stints, each with a `StartDate` and
either an `EndDate` or `null` for "still a member"). `parse_components`
merges them keyed by `code`: an entry from `HistoricalTickerComponents`
always overwrites an entry for the same `code` sourced from `Components`
(the dated entry is strictly more informative - a dateless `Components`
row is really just "currently active, exact start date unknown"). A code
present in only one section passes through unchanged. `is_active` is
derived, not vendor-supplied: `end_date is None`.

Fundamentals identifiers
-------------------------
`parse_fundamentals` reads `code`/`name`/`type`/`cusip`/`isin`/`cik` from
the `General` section and `share_class` from `General` (falling back to
`SharesStats` only if `General` doesn't have it - EODHD has moved this
field between sections across API versions). Any key absent from the
payload stays `None` - this parser never invents or guesses an
identifier.

EOD / intraday getters (task-9 preflight ruling)
--------------------------------------------------
`get_eod`/`get_intraday` follow the exact same parse-and-capture pattern
as the five original getters above, feeding
`bronze.eodhd_eod_capture`/`bronze.eodhd_intraday_capture`
(`tick_vault.schemas`) so the reconciliation gate
(`tick_vault.reconcile`) has vendor OHLCV to compare against. `parse_eod`
reads EODHD's `/api/eod/<SYM>` array shape (`date`/`open`/`high`/`low`/
`close`/`adjusted_close`/`volume`, case-insensitive keys, `date` parsed to
`datetime.date`); `parse_intraday` reads `/api/intraday/<SYM>`'s array
shape (`timestamp` - vendor unix seconds - plus `open`/`high`/`low`/
`close`/`volume`), producing `ts_ms`/OHLCV. Both endpoint URL templates
carry the same Phase-0 "best-effort, unverified against a live response"
caveat as the rest of `_ENDPOINTS` above.

API-key hygiene
----------------
`ReferenceClient` builds the live request URL with the real `api_key`
(EODHD requires it as a query parameter, e.g. `api_token=...`), but the
`request_params` dict handed to `CaptureStore.fetch_and_capture` -
serialized verbatim into `request_parameters_json` on the captured bronze
row - always carries the literal string `"REDACTED"` in place of the key,
never the real value. The key is never written to the payload store or
logged either: `fetch_and_capture`/`CaptureStore.record` only ever see the
response body, not the request URL.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pandas as pd

from tick_vault.capture import CaptureStore, Transport
from tick_vault.ids import new_id

PARSER_VERSION = "EODHD_REFERENCE_V1"

# Best-effort EODHD URL templates - see "URL verification caveat" above.
# `{token}` is always the raw api key (substituted at call time, never
# stored); `{exchange}`/`{symbol}` are substituted per call.
_ENDPOINTS = {
    "exchange_symbols": "https://eodhd.com/api/exchange-symbol-list/{exchange}?api_token={token}&fmt=json",
    "delisted_symbols": "https://eodhd.com/api/exchange-symbol-list/{exchange}?delisted=1&api_token={token}&fmt=json",
    "symbol_changes": "https://eodhd.com/api/symbol-change-history?api_token={token}&fmt=json",
    "index_components": "https://eodhd.com/api/fundamentals/{symbol}?api_token={token}",
    "fundamentals": "https://eodhd.com/api/fundamentals/{symbol}?api_token={token}",
    "eod": "https://eodhd.com/api/eod/{symbol}?from={from_date}&to={to_date}&api_token={token}&fmt=json",
    "intraday": "https://eodhd.com/api/intraday/{symbol}?interval={interval}&from={from_ts}&to={to_ts}&api_token={token}&fmt=json",
}

_REDACTED = "REDACTED"


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------

def _ci_get(d: dict, *keys: str, default: Any = None) -> Any:
    """Case-insensitive lookup of the first of `keys` present in `d`."""
    if not d:
        return default
    lower = {str(k).lower(): v for k, v in d.items()}
    for key in keys:
        if key.lower() in lower:
            return lower[key.lower()]
    return default


def _parse_date(value: Any) -> dt.date | None:
    """Parse an EODHD `"YYYY-MM-DD"` string into a `datetime.date`.

    `None`, empty string, and the literal `"0000-00-00"` (an EODHD
    placeholder for "no date") all map to `None` rather than raising.
    """
    if not value:
        return None
    text = str(value)[:10]
    if text == "0000-00-00":
        return None
    return dt.date.fromisoformat(text)


def _fmt_date(value: "dt.date | str") -> str:
    """Format a `datetime.date` (or an already-ISO-ish string) as
    `"YYYY-MM-DD"` for the `eod` endpoint's `from`/`to` query params."""
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)[:10]


def _to_epoch_seconds(value: "dt.date | str") -> int:
    """Convert a `from_date`/`to_date` (date or `"YYYY-MM-DD"` string) into
    UTC-midnight unix seconds, for the `request_from_sec`/`request_to_sec`
    bronze bookkeeping columns (shared with the other date-ranged EODHD
    capture tables)."""
    if isinstance(value, str):
        value = dt.date.fromisoformat(value[:10])
    return int(dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# pure parsers (payload -> DataFrame)
# ---------------------------------------------------------------------------

_SYMBOL_COLUMNS = ["code", "name", "exchange", "type", "isin"]


def parse_symbols(payload: list) -> pd.DataFrame:
    """Parse an exchange-symbol-list (or delisted-symbol-list) payload: a
    JSON array of objects with (case-insensitive) `Code`/`Name`/`Exchange`/
    `Type`/`Isin` keys -> a `code, name, exchange, type, isin` DataFrame.
    """
    rows = []
    for item in payload or []:
        rows.append({
            "code": _ci_get(item, "Code"),
            "name": _ci_get(item, "Name"),
            "exchange": _ci_get(item, "Exchange"),
            "type": _ci_get(item, "Type"),
            "isin": _ci_get(item, "Isin"),
        })
    return pd.DataFrame(rows, columns=_SYMBOL_COLUMNS)


# Delisted symbols share the exact same shape as the active list.
parse_delisted = parse_symbols


_CHANGE_COLUMNS = ["old", "new", "date"]


def parse_changes(payload: list) -> pd.DataFrame:
    """Parse a symbol-change-history payload (JSON array) -> an
    `old, new, date` DataFrame, `date` as `datetime.date`.
    """
    rows = []
    for item in payload or []:
        rows.append({
            "old": _ci_get(item, "OldCode", "OldSymbol", "Old"),
            "new": _ci_get(item, "NewCode", "NewSymbol", "New"),
            "date": _parse_date(_ci_get(item, "Date", "ChangeDate")),
        })
    return pd.DataFrame(rows, columns=_CHANGE_COLUMNS)


_COMPONENT_COLUMNS = ["code", "name", "start_date", "end_date", "is_active", "sector"]


def parse_components(payload: dict) -> pd.DataFrame:
    """Parse an index fundamentals-style payload's `Components` +
    `HistoricalTickerComponents` sections -> a `code, name, start_date,
    end_date, is_active, sector` DataFrame.

    Merge rule (see module docstring): entries are keyed by `code`;
    `HistoricalTickerComponents` entries always win over a `Components`
    entry for the same code. `is_active` is derived as `end_date is None`.
    """
    payload = payload or {}
    merged: dict[str, dict] = {}

    for item in (payload.get("Components") or {}).values():
        code = _ci_get(item, "Code")
        if not code:
            continue
        merged[code] = {
            "code": code,
            "name": _ci_get(item, "Name"),
            "start_date": None,
            "end_date": None,
            "sector": _ci_get(item, "Sector"),
        }

    for item in (payload.get("HistoricalTickerComponents") or {}).values():
        code = _ci_get(item, "Code")
        if not code:
            continue
        end_date = _parse_date(_ci_get(item, "EndDate"))
        merged[code] = {
            "code": code,
            "name": _ci_get(item, "Name"),
            "start_date": _parse_date(_ci_get(item, "StartDate")),
            "end_date": end_date,
            "sector": _ci_get(item, "Sector"),
        }

    rows = []
    for code in sorted(merged):
        row = dict(merged[code])
        row["is_active"] = row["end_date"] is None
        rows.append(row)
    return pd.DataFrame(rows, columns=_COMPONENT_COLUMNS)


_FUNDAMENTALS_COLUMNS = ["code", "cusip", "isin", "cik", "name", "type", "share_class"]


def parse_fundamentals(payload: dict) -> pd.DataFrame:
    """Parse a per-symbol fundamentals payload's `General` (and, for
    `share_class` only, `SharesStats`) sections -> a single-row `code,
    cusip, isin, cik, name, type, share_class` DataFrame.

    Never invents an identifier: any key absent from the payload stays
    `None`.
    """
    payload = payload or {}
    general = payload.get("General") or {}
    shares = payload.get("SharesStats") or {}
    row = {
        "code": _ci_get(general, "Code"),
        "cusip": _ci_get(general, "CUSIP"),
        "isin": _ci_get(general, "ISIN"),
        "cik": _ci_get(general, "CIK"),
        "name": _ci_get(general, "Name"),
        "type": _ci_get(general, "Type"),
        "share_class": _ci_get(general, "ShareClass", default=_ci_get(shares, "ShareClass")),
    }
    return pd.DataFrame([row], columns=_FUNDAMENTALS_COLUMNS)


_EOD_COLUMNS = ["date", "open", "high", "low", "close", "adjusted_close", "volume"]


def parse_eod(payload: list) -> pd.DataFrame:
    """Parse an EODHD `/api/eod/<SYM>` payload (a JSON array of daily bars,
    case-insensitive `Date`/`Open`/`High`/`Low`/`Close`/`Adjusted_close`/
    `Volume` keys) -> a `date, open, high, low, close, adjusted_close,
    volume` DataFrame, `date` as `datetime.date`.
    """
    rows = []
    for item in payload or []:
        rows.append({
            "date": _parse_date(_ci_get(item, "Date")),
            "open": _ci_get(item, "Open"),
            "high": _ci_get(item, "High"),
            "low": _ci_get(item, "Low"),
            "close": _ci_get(item, "Close"),
            "adjusted_close": _ci_get(item, "Adjusted_close", "AdjustedClose"),
            "volume": _ci_get(item, "Volume"),
        })
    return pd.DataFrame(rows, columns=_EOD_COLUMNS)


_INTRADAY_COLUMNS = ["ts_ms", "open", "high", "low", "close", "volume"]


def parse_intraday(payload: list) -> pd.DataFrame:
    """Parse an EODHD `/api/intraday/<SYM>` payload (a JSON array of
    intraday bars, case-insensitive `Timestamp` - vendor unix seconds -
    plus `Open`/`High`/`Low`/`Close`/`Volume` keys) -> a `ts_ms, open,
    high, low, close, volume` DataFrame, `ts_ms` as vendor-timestamp
    milliseconds (`Timestamp * 1000`).
    """
    rows = []
    for item in payload or []:
        ts = _ci_get(item, "Timestamp")
        rows.append({
            "ts_ms": int(ts) * 1000 if ts is not None else None,
            "open": _ci_get(item, "Open"),
            "high": _ci_get(item, "High"),
            "low": _ci_get(item, "Low"),
            "close": _ci_get(item, "Close"),
            "volume": _ci_get(item, "Volume"),
        })
    return pd.DataFrame(rows, columns=_INTRADAY_COLUMNS)


# ---------------------------------------------------------------------------
# ReferenceClient (Delta-backed capture wiring)
# ---------------------------------------------------------------------------

class ReferenceClient:
    """Fetches EODHD reference endpoints via a `Transport`, captures every
    response to its bronze table through `capture_store`, and returns
    `(parsed_df, CaptureRecord)`.

    `ingestion_run_id` is minted once per client (a `ReferenceClient`
    instance = one ingestion run) unless one is supplied explicitly - e.g.
    by an orchestrator that wants several clients/tasks to share one run
    id.
    """

    def __init__(
        self,
        transport: Transport,
        capture_store: CaptureStore,
        api_key: str,
        *,
        ingestion_run_id: str | None = None,
        source_system: str = "eodhd",
    ):
        self.transport = transport
        self.capture_store = capture_store
        self.api_key = api_key
        self.ingestion_run_id = ingestion_run_id or new_id("run")
        self.source_system = source_system

    def _fetch(
        self,
        *,
        url: str,
        table: str,
        endpoint: str,
        request_params: dict,
        extra_cols: dict | None,
        observed_at: dt.datetime | None,
    ) -> tuple[bytes | None, Any]:
        observed_at = observed_at or dt.datetime.now(dt.timezone.utc)
        return self.capture_store.fetch_and_capture(
            self.transport,
            url=url,
            table=table,
            endpoint=endpoint,
            request_params=request_params,
            observed_at=observed_at,
            ingestion_run_id=self.ingestion_run_id,
            parser_version=PARSER_VERSION,
            extra_cols=extra_cols,
            source_system=self.source_system,
        )

    @staticmethod
    def _load_json(payload_bytes: bytes | None, default: Any) -> Any:
        if not payload_bytes:
            return default
        return json.loads(payload_bytes)

    def get_exchange_symbols(
        self, exchange: str = "US", *, observed_at: dt.datetime | None = None
    ) -> tuple[pd.DataFrame, Any]:
        url = _ENDPOINTS["exchange_symbols"].format(exchange=exchange, token=self.api_key)
        payload_bytes, record = self._fetch(
            url=url,
            table="bronze.eodhd_exchange_symbols_capture",
            endpoint="exchange_symbol_list",
            request_params={"exchange": exchange, "token": _REDACTED},
            extra_cols={"exchange_code": exchange},
            observed_at=observed_at,
        )
        payload = self._load_json(payload_bytes, [])
        return parse_symbols(payload), record

    def get_delisted(
        self, exchange: str = "US", *, observed_at: dt.datetime | None = None
    ) -> tuple[pd.DataFrame, Any]:
        url = _ENDPOINTS["delisted_symbols"].format(exchange=exchange, token=self.api_key)
        payload_bytes, record = self._fetch(
            url=url,
            table="bronze.eodhd_delisted_symbols_capture",
            endpoint="exchange_symbol_list_delisted",
            request_params={"exchange": exchange, "delisted": True, "token": _REDACTED},
            extra_cols={"exchange_code": exchange},
            observed_at=observed_at,
        )
        payload = self._load_json(payload_bytes, [])
        return parse_delisted(payload), record

    def get_symbol_changes(
        self, *, observed_at: dt.datetime | None = None
    ) -> tuple[pd.DataFrame, Any]:
        """Fetch the full symbol-change-history feed.

        This EODHD endpoint has no `{exchange}` path/query parameter - it
        is a single global feed covering all exchanges, unlike
        `get_exchange_symbols`/`get_delisted`. Any exchange-scoping of the
        returned rows happens downstream, at resolution time, against the
        `old`/`new` codes already carried in the parsed rows - never by
        filtering the vendor request. `request_params`/the captured bronze
        row therefore carry no `exchange` key.
        """
        url = _ENDPOINTS["symbol_changes"].format(token=self.api_key)
        payload_bytes, record = self._fetch(
            url=url,
            table="bronze.eodhd_symbol_change_capture",
            endpoint="symbol_change_history",
            request_params={"token": _REDACTED, "fmt": "json"},
            extra_cols=None,
            observed_at=observed_at,
        )
        payload = self._load_json(payload_bytes, [])
        return parse_changes(payload), record

    def get_index_components(
        self, index_symbol: str = "GSPC.INDX", *, observed_at: dt.datetime | None = None
    ) -> tuple[pd.DataFrame, Any]:
        url = _ENDPOINTS["index_components"].format(symbol=index_symbol, token=self.api_key)
        payload_bytes, record = self._fetch(
            url=url,
            table="bronze.eodhd_index_components_capture",
            endpoint="index_components",
            request_params={"symbol": index_symbol, "token": _REDACTED},
            extra_cols={"index_vendor_symbol": index_symbol},
            observed_at=observed_at,
        )
        payload = self._load_json(payload_bytes, {})
        return parse_components(payload), record

    def get_fundamentals(
        self,
        symbol: str,
        *,
        exchange: str | None = None,
        observed_at: dt.datetime | None = None,
    ) -> tuple[pd.DataFrame, Any]:
        url = _ENDPOINTS["fundamentals"].format(symbol=symbol, token=self.api_key)
        request_params = {"symbol": symbol, "token": _REDACTED}
        if exchange is not None:
            request_params["exchange"] = exchange
        payload_bytes, record = self._fetch(
            url=url,
            table="bronze.eodhd_fundamentals_capture",
            endpoint="fundamentals",
            request_params=request_params,
            extra_cols={"request_symbol": symbol, "request_exchange_code": exchange},
            observed_at=observed_at,
        )
        payload = self._load_json(payload_bytes, {})
        return parse_fundamentals(payload), record

    def get_eod(
        self,
        symbol: str,
        from_date: "dt.date | str",
        to_date: "dt.date | str",
        *,
        exchange: str | None = None,
        observed_at: dt.datetime | None = None,
    ) -> tuple[pd.DataFrame, Any]:
        """Fetch a date-ranged daily-bar (EOD) slice for `symbol`.

        Mirrors `get_fundamentals`'s `exchange` convention: purely
        informational bookkeeping (`request_exchange_code`), never used to
        change the vendor URL - `symbol` is assumed to already carry
        whatever exchange suffix EODHD needs (e.g. `"AAPL.US"`).
        """
        from_str = _fmt_date(from_date)
        to_str = _fmt_date(to_date)
        url = _ENDPOINTS["eod"].format(
            symbol=symbol, from_date=from_str, to_date=to_str, token=self.api_key
        )
        request_params = {"symbol": symbol, "from": from_str, "to": to_str, "token": _REDACTED}
        if exchange is not None:
            request_params["exchange"] = exchange
        payload_bytes, record = self._fetch(
            url=url,
            table="bronze.eodhd_eod_capture",
            endpoint="eod",
            request_params=request_params,
            extra_cols={
                "request_symbol": symbol,
                "request_exchange_code": exchange,
                "request_from_sec": _to_epoch_seconds(from_date),
                "request_to_sec": _to_epoch_seconds(to_date),
            },
            observed_at=observed_at,
        )
        payload = self._load_json(payload_bytes, [])
        return parse_eod(payload), record

    def get_intraday(
        self,
        symbol: str,
        interval: str,
        from_ts: int,
        to_ts: int,
        *,
        exchange: str | None = None,
        observed_at: dt.datetime | None = None,
    ) -> tuple[pd.DataFrame, Any]:
        """Fetch an intraday-bar slice for `symbol` at `interval`
        (EODHD's own interval strings, e.g. `"1m"`/`"5m"`) between
        `from_ts`/`to_ts` (unix seconds, per EODHD's intraday endpoint
        convention - unlike `get_eod`'s date-string `from`/`to`).
        """
        url = _ENDPOINTS["intraday"].format(
            symbol=symbol, interval=interval, from_ts=from_ts, to_ts=to_ts, token=self.api_key
        )
        request_params = {
            "symbol": symbol, "interval": interval, "from": from_ts, "to": to_ts,
            "token": _REDACTED,
        }
        if exchange is not None:
            request_params["exchange"] = exchange
        payload_bytes, record = self._fetch(
            url=url,
            table="bronze.eodhd_intraday_capture",
            endpoint="intraday",
            request_params=request_params,
            extra_cols={
                "request_symbol": symbol,
                "request_exchange_code": exchange,
                "request_from_sec": from_ts,
                "request_to_sec": to_ts,
                "interval": interval,
            },
            observed_at=observed_at,
        )
        payload = self._load_json(payload_bytes, [])
        return parse_intraday(payload), record

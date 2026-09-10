"""EODHD tick payload parser (baseline §19; tick-vault temporal design §4, §7).

Parses raw EODHD tick-by-tick payloads into rows shaped like
`silver.us_trade_tick_version`. The core returns a pandas DataFrame; a
`pa.Table` facade is intentionally deferred (pyarrow not installable in this
environment yet) — all normalization logic lives here so that facade can be a
thin wrapper later.

M8 (final-review finding): `COLUMNS`/`parse_tick_payload` do not populate
every NOT NULL column of `silver.us_trade_tick_version` - `source_system`,
`vendor_request_symbol`, and `price_venue_type` are absent from `COLUMNS`
entirely, and `committed_at_ts` is explicitly set to `None` below. Plan 2's
Delta writer (whatever turns this module's DataFrame into an actual
`write_deltalake` call) MUST supply real values for all four before
writing, or the write will fail against the schema (or worse, silently
succeed against a permissive writer and leave nulls in NOT NULL columns).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from zoneinfo import ZoneInfo

import pandas as pd

from tick_vault.ids import new_id
from tick_vault.sl_conditions import decode_sl

PARSER_VERSION = "TICK_PARSER_V1"

_NY_TZ = ZoneInfo("America/New_York")

# Column order for the silver.us_trade_tick_version-shaped DataFrame.
COLUMNS = [
    "tick_version_id",
    "logical_tick_id",
    "source_capture_id",
    "source_page_ordinal",
    "source_row_ordinal",
    "listing_id",
    "instrument_id",
    "trade_date",
    "trade_ts_ms",
    "trade_ts",
    "venue_code_raw",
    "sub_mkt_raw",
    "session_seq",
    "price",
    "size",
    "sale_condition_raw",
    "sale_condition_flags",
    "origin",
    "revision_number",
    "is_correction",
    "is_cancelled",
    "is_late_add",
    "supersedes_tick_version_id",
    "available_at_ts",
    "observed_at_ts",
    "committed_at_ts",
    "parser_version",
    "payload_hash",
    "record_hash",
]


def session_date(ts_ms: int) -> dt.date:
    """The America/New_York calendar date of a UTC-millisecond print timestamp."""
    utc_dt = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc)
    return utc_dt.astimezone(_NY_TZ).date()


def _canonical_json(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def _payload_hash(record: dict) -> str:
    return hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()


def _record_hash(logical_tick_id: str, price, size, sale_condition_raw, venue_code_raw) -> str:
    key = {
        "logical_tick_id": logical_tick_id,
        "price": price,
        "size": size,
        "sale_condition_raw": sale_condition_raw,
        "venue_code_raw": venue_code_raw,
    }
    return hashlib.sha256(_canonical_json(key).encode("utf-8")).hexdigest()


def parse_tick_payload(
    payload: list[dict],
    *,
    capture_id: str,
    listing_id: str,
    instrument_id: str,
    observed_at: dt.datetime,
    page_ordinal: int = 0,
) -> pd.DataFrame:
    """Normalize a raw EODHD tick payload into a silver.us_trade_tick_version-shaped DataFrame.

    Dedup is applied within the payload on (ts, seq), keeping the first
    occurrence (boundary-second duplicates per sip_backfill's convention).
    `source_row_ordinal` is the index of the kept row in the ORIGINAL
    (pre-dedup) payload.
    """
    seen: set[tuple[int, int]] = set()
    rows = []

    for row_ordinal, record in enumerate(payload):
        ts_ms = record["ts"]
        seq = record["seq"]
        dedup_key = (ts_ms, seq)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        trade_date = session_date(ts_ms)
        trade_ts = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc)
        venue_code_raw = record.get("mkt")
        sub_mkt_raw = record.get("sub_mkt")
        price = record.get("price")
        size = record.get("shares")
        sale_condition_raw = record.get("sl")
        sale_conditions = decode_sl(sale_condition_raw or "")

        logical_tick_id = f"{listing_id}|{trade_date.isoformat()}|{seq}"

        rows.append(
            {
                "tick_version_id": new_id("tkv"),
                "logical_tick_id": logical_tick_id,
                "source_capture_id": capture_id,
                "source_page_ordinal": page_ordinal,
                "source_row_ordinal": row_ordinal,
                "listing_id": listing_id,
                "instrument_id": instrument_id,
                "trade_date": trade_date,
                "trade_ts_ms": ts_ms,
                "trade_ts": trade_ts,
                "venue_code_raw": venue_code_raw,
                "sub_mkt_raw": sub_mkt_raw,
                "session_seq": seq,
                "price": price,
                "size": size,
                "sale_condition_raw": sale_condition_raw,
                "sale_condition_flags": list(sale_conditions.flags),
                "origin": "CAPTURE",
                "revision_number": 1,
                "is_correction": False,
                "is_cancelled": False,
                "is_late_add": False,
                "supersedes_tick_version_id": None,
                "available_at_ts": observed_at,
                "observed_at_ts": observed_at,
                "committed_at_ts": None,
                "parser_version": PARSER_VERSION,
                "payload_hash": _payload_hash(record),
                "record_hash": _record_hash(
                    logical_tick_id, price, size, sale_condition_raw, venue_code_raw
                ),
            }
        )

    return pd.DataFrame(rows, columns=COLUMNS)

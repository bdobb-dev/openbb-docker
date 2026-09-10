"""Correction diff engine (tick-vault temporal design §7).

Compares a `latest, non-cancelled` window of existing tick-version rows
against a freshly-parsed incoming payload for the same logical scope, and
produces the new tick-version rows (REVISED corrections, CANCELLED
tombstones, LATE_ADD inserts) plus the `silver.tick_correction_event` rows
that describe why each new version exists.

Both `existing` and `incoming` are pandas DataFrames shaped like
`tick_vault.tick_parser.parse_tick_payload`'s output (pyarrow is not
installable in this environment, so the pa.Table facade described in the
original plan is deferred - see tick_parser.py's module docstring).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from tick_vault.ids import new_id
from tick_vault.tick_parser import COLUMNS

# Fields compared to decide whether a logical tick's content changed.
_COMPARISON_FIELDS = [
    "price",
    "size",
    "sale_condition_raw",
    "sub_mkt_raw",
    "venue_code_raw",
]

EVENT_COLUMNS = [
    "correction_event_id",
    "correction_kind",
    "logical_tick_id",
    "superseded_tick_version_id",
    "superseding_tick_version_id",
    "revealing_capture_id",
    "observed_at_ts",
]


@dataclass
class DiffResult:
    new_versions: pd.DataFrame
    events: pd.DataFrame


def _norm_price(value):
    return None if value is None or pd.isna(value) else float(value)


def _norm_size(value):
    return None if value is None or pd.isna(value) else int(value)


def _norm_key(row: dict) -> tuple:
    return (
        _norm_price(row.get("price")),
        _norm_size(row.get("size")),
        row.get("sale_condition_raw"),
        row.get("sub_mkt_raw"),
        row.get("venue_code_raw"),
    )


def _make_event(kind: str, logical_tick_id: str, superseded, superseding,
                 revealing_capture_id: str, observed_at: dt.datetime) -> dict:
    return {
        "correction_event_id": new_id("evt"),
        "correction_kind": kind,
        "logical_tick_id": logical_tick_id,
        "superseded_tick_version_id": superseded,
        "superseding_tick_version_id": superseding,
        "revealing_capture_id": revealing_capture_id,
        "observed_at_ts": observed_at,
    }


def diff_window(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
    *,
    revealing_capture_id: str,
    observed_at: dt.datetime,
) -> DiffResult:
    """Diff `incoming` against `existing` (pre-filtered to latest, non-cancelled).

    `existing` and `incoming` are matched on `logical_tick_id`. Three
    branches: REVISED (changed comparison fields), CANCELLED (present in
    `existing` but missing from `incoming`), LATE_ADD (present in `incoming`
    but missing from `existing`).
    """
    new_version_rows: list[dict] = []
    event_rows: list[dict] = []

    existing_by_key = {
        row["logical_tick_id"]: row for row in existing.to_dict("records")
    }
    incoming_by_key = {
        row["logical_tick_id"]: row for row in incoming.to_dict("records")
    }

    all_keys = set(existing_by_key) | set(incoming_by_key)

    for key in all_keys:
        existing_row = existing_by_key.get(key)
        incoming_row = incoming_by_key.get(key)

        if existing_row is not None and incoming_row is not None:
            if _norm_key(existing_row) == _norm_key(incoming_row):
                continue  # unchanged: emit nothing

            new_row = dict(incoming_row)
            new_row["tick_version_id"] = new_id("tkv")
            new_row["source_capture_id"] = revealing_capture_id
            new_row["revision_number"] = existing_row["revision_number"] + 1
            new_row["is_correction"] = True
            new_row["is_cancelled"] = False
            new_row["is_late_add"] = False
            new_row["supersedes_tick_version_id"] = existing_row["tick_version_id"]
            new_row["available_at_ts"] = observed_at
            new_row["observed_at_ts"] = observed_at
            new_version_rows.append(new_row)
            event_rows.append(
                _make_event(
                    "REVISED",
                    key,
                    existing_row["tick_version_id"],
                    new_row["tick_version_id"],
                    revealing_capture_id,
                    observed_at,
                )
            )

        elif existing_row is not None:  # in existing but not incoming: tombstone
            tomb_row = dict(existing_row)
            tomb_row["tick_version_id"] = new_id("tkv")
            tomb_row["source_capture_id"] = revealing_capture_id
            tomb_row["revision_number"] = existing_row["revision_number"] + 1
            tomb_row["is_correction"] = False
            tomb_row["is_cancelled"] = True
            tomb_row["is_late_add"] = False
            tomb_row["supersedes_tick_version_id"] = existing_row["tick_version_id"]
            tomb_row["available_at_ts"] = observed_at
            tomb_row["observed_at_ts"] = observed_at
            new_version_rows.append(tomb_row)
            event_rows.append(
                _make_event(
                    "CANCELLED",
                    key,
                    existing_row["tick_version_id"],
                    tomb_row["tick_version_id"],
                    revealing_capture_id,
                    observed_at,
                )
            )

        else:  # in incoming but not existing: late add
            late_row = dict(incoming_row)
            late_row["revision_number"] = 1
            late_row["is_correction"] = False
            late_row["is_cancelled"] = False
            late_row["is_late_add"] = True
            late_row["supersedes_tick_version_id"] = None
            late_row["available_at_ts"] = observed_at
            late_row["observed_at_ts"] = observed_at
            new_version_rows.append(late_row)
            event_rows.append(
                _make_event(
                    "LATE_ADD",
                    key,
                    None,
                    late_row["tick_version_id"],
                    revealing_capture_id,
                    observed_at,
                )
            )

    new_versions = pd.DataFrame(new_version_rows, columns=COLUMNS)
    events = pd.DataFrame(event_rows, columns=EVENT_COLUMNS)
    return DiffResult(new_versions=new_versions, events=events)

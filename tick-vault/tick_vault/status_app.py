# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Status endpoint for tick-vault (Task 11).

Unlike stores-explorer's FastAPI app.main.py -- this module's RULING
divergence -- tick-vault's status server is stdlib `http.server`
(ThreadingHTTPServer), not FastAPI. tick-vault's pyproject.toml (pandas,
pyarrow, deltalake, duckdb) has no web-framework dependency and Task 11
does not add one; the response SHAPES below still mirror stores-explorer's
pattern (a static `/widgets.json` descriptor plus a live data endpoint), just
served over stdlib instead of uvicorn/FastAPI.

Three pieces:
  - `build_status_payload` -- pure function, `/status`'s JSON body.
  - `make_widgets_manifest` -- pure function, `/widgets.json`'s JSON body
    (one widget descriptor, same field names as
    stores-explorer/widgets.json's entries: name, description, category,
    type, endpoint, dataKey, gridData{w,h}, params, source).
  - `StatusServer` -- the actual `ThreadingHTTPServer`, wired to injectable
    `readers` so tests (and this module's own docstring example) never touch
    a real Delta Lake. Real deltalake wiring (manifest_reader, dq_issue_reader,
    frontier/budget/calibration lookups against `VAULT_ROOT`) is intentionally
    NOT built here -- that is lazy, left to whatever constructs the real
    `StatusServer` at deploy time (mirrors cli.py's ctx_factory laziness:
    `EOD_API_KEY`/`VAULT_ROOT`/`LEGACY_ROOT` are read at call time, never at
    import time).
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


def build_status_payload(
    manifest_counts: dict,
    frontier: dict,
    dq_open: int,
    budget: dict,
    calibration_present: bool,
) -> dict:
    """Pure formatter for GET /status's JSON body.

    `manifest_counts` is `{status: row_count}` -- the same shape
    `cli.handle_status` derives from `manifest_df["status"].value_counts()`
    (see cli.py's PENDING/IN_PROGRESS/COMPLETE/PARTIAL/FAILED/
    BLOCKED_IDENTITY statuses; this function does not normalize or
    require any particular key set, it passes counts through as given).
    `frontier` is e.g. `{"min_complete_week": ..., "max_complete_week": ...}`
    (or a dict with None values when there is no frontier yet). `budget` is
    e.g. `{"calls_today": int, "last_429": str | None}` -- passed through
    unchanged, same "caller owns the shape" contract `format_status` uses
    for its own `budget_state` argument in cli.py.
    """
    return {
        "manifest": dict(manifest_counts),
        "frontier": dict(frontier),
        "quality_issues_open": dq_open,
        "budget": dict(budget),
        "calibration": "present" if calibration_present else "absent",
    }


def make_widgets_manifest(base_url: str) -> dict:
    """The /widgets.json descriptor for tick-vault's one status widget.

    Field names mirror stores-explorer/widgets.json's entries (name,
    description, category, type, endpoint, dataKey, gridData.w/.h, params,
    source) -- `endpoint` here is an absolute URL (`base_url` + "/status")
    rather than stores-explorer's relative path, since this server has no
    surrounding OpenBB Workspace proxy to resolve a relative one against.
    """
    base = base_url.rstrip("/")
    return {
        "tick_vault_status": {
            "name": "Tick Vault Status",
            "description": (
                "Backfill manifest counts, PENDING-week frontier, open "
                "data-quality issues, EODHD call budget, and calibration "
                "report presence for the tick-vault engine."
            ),
            "category": "Data",
            "type": "table",
            "endpoint": f"{base}/status",
            "dataKey": "manifest",
            "gridData": {"w": 20, "h": 10},
            "params": [],
            "source": ["tick-vault"],
        }
    }


class StatusServer(ThreadingHTTPServer):
    """`ThreadingHTTPServer` serving GET /status and GET /widgets.json.

    `readers` is a dict of zero-arg callables, injected so tests supply
    fakes and the real deltalake/duckdb wiring stays out of this module:
      - "manifest_counts" -> dict
      - "frontier" -> dict
      - "dq_open" -> int
      - "budget" -> dict
      - "calibration_present" -> bool

    `root` is carried through (unused by this class directly) so a caller
    building the real server can close over `VAULT_ROOT` in its reader
    callables without this class needing to know about paths at all.
    """

    def __init__(
        self,
        root: str,
        readers: "dict[str, Callable[[], object]]",
        port: int,
        host: str = "127.0.0.1",
    ):
        self.vault_root = root
        self.readers = readers
        super().__init__((host, port), _StatusHandler)

    @property
    def base_url(self) -> str:
        host = self.server_address[0]
        # 0.0.0.0 is a bind address, not a reachable one -- report loopback
        # for anything that binds "all interfaces" (matches how a client
        # would actually reach a same-container caller).
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"http://{host}:{self.server_address[1]}"


class _StatusHandler(BaseHTTPRequestHandler):
    server: StatusServer

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        # Quiet by default; tick-vault's own logging (ops.ingestion_run) is
        # the record of truth, not this server's access log.
        pass

    def _write_json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib method name
        readers = self.server.readers
        if self.path == "/status":
            payload = build_status_payload(
                manifest_counts=readers["manifest_counts"](),
                frontier=readers["frontier"](),
                dq_open=readers["dq_open"](),
                budget=readers["budget"](),
                calibration_present=readers["calibration_present"](),
            )
            self._write_json(payload)
        elif self.path == "/widgets.json":
            self._write_json(make_widgets_manifest(self.server.base_url))
        else:
            self.send_response(404)
            self.end_headers()


def _default_manifest_counts(root: str) -> dict:
    """Real reader: `ops.backfill_manifest`'s status value-counts, same
    shape `cli.handle_status` derives (lazy deltalake import, matching
    `cli.py`'s own convention)."""
    from tick_vault.cli import _default_manifest_reader

    df = _default_manifest_reader(root)
    if df is None or df.empty:
        return {}
    return {str(k): int(v) for k, v in df["status"].value_counts().items()}


def _isoformat_or_none(value) -> "str | None":
    """`value` may be a `datetime.date`/`datetime.datetime`, a pandas
    `Timestamp`/`NaT`, `None`, or plain `nan` - normalize all of those to
    an ISO-8601 string (or `None`) so `json.dumps` never chokes on a raw
    `date`/`Timestamp` object (`TypeError: Object of type date is not
    JSON serializable`)."""
    import pandas as pd

    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _default_frontier(root: str) -> dict:
    """Real reader: the PENDING-week frontier, `{min_complete_week,
    max_complete_week}` (naming matches the brief's `/status` contract;
    values are `None` when there is no PENDING work left). Values are
    normalized to ISO-8601 strings (`_isoformat_or_none`) - a raw
    `datetime.date`/pandas `Timestamp` is not JSON-serializable, and
    `build_status_payload`'s dict is always run through `json.dumps` by
    `StatusServer`."""
    from tick_vault.cli import _default_manifest_reader

    df = _default_manifest_reader(root)
    if df is None or df.empty:
        return {"min_complete_week": None, "max_complete_week": None}
    pending = df[df["status"] == "PENDING"]
    if pending.empty:
        return {"min_complete_week": None, "max_complete_week": None}
    return {
        "min_complete_week": _isoformat_or_none(pending["week_monday"].min()),
        "max_complete_week": _isoformat_or_none(pending["week_monday"].max()),
    }


def _default_dq_open(root: str) -> int:
    """Real reader: count of `ops.data_quality_issue` rows with
    `status == "OPEN"` (same query `cli.handle_status` runs)."""
    from deltalake import DeltaTable

    from tick_vault.storage import table_uri

    try:
        path, opts = table_uri(root, "ops.data_quality_issue")
        df = DeltaTable(path, storage_options=opts).to_pandas()
    except Exception:
        return 0
    if df is None or df.empty:
        return 0
    return int((df["status"] == "OPEN").sum())


def _default_budget() -> dict:
    """Real reader placeholder: `tick_vault.engine.Budget` tracks
    `consecutive_waits`/`max_consecutive_waits` (429-backoff state), not
    the `calls_today`/`last_429` counters the brief's `/status` contract
    asks for -- those would need a persisted call-log this codebase does
    not have yet (Plan-3 candidate). Returns a `None`-valued placeholder
    of the right shape rather than fabricating numbers."""
    return {"calls_today": None, "last_429": None}


def _default_calibration_present() -> bool:
    """Real reader: does `calibration_report_path()` exist on disk (same
    check `cli.handle_backfill`'s gate runs before `--loop`)."""
    import os

    from tick_vault.cli import calibration_report_path

    return os.path.exists(calibration_report_path())


def make_default_readers(root: str) -> "dict[str, Callable[[], object]]":
    """The real (lazy, Delta-backed) reader set `main()` wires into
    `StatusServer` -- kept out of `StatusServer.__init__` itself so tests
    construct the server with pure fakes and never import `deltalake`."""
    return {
        "manifest_counts": lambda: _default_manifest_counts(root),
        "frontier": lambda: _default_frontier(root),
        "dq_open": lambda: _default_dq_open(root),
        "budget": _default_budget,
        "calibration_present": _default_calibration_present,
    }


def main() -> int:
    """`python -m tick_vault.status_app` entry point: serves `/status` and
    `/widgets.json` on `127.0.0.1:PORT` (`PORT` env var, default 8600),
    reading `VAULT_ROOT` (default `./vault_data`) fresh at each request via
    `make_default_readers` -- never cached at import time, matching
    `cli.build_ctx_from_env`'s convention."""
    import os

    root = os.environ.get("VAULT_ROOT", "./vault_data")
    port = int(os.environ.get("PORT", "8600"))
    # HOST defaults to loopback (matches stores-explorer/key-maint's posture:
    # no app-level auth, so the network boundary IS the auth). Set
    # HOST=0.0.0.0 only inside a container on an already-isolated network
    # (see docker-compose.yml's tick-vault-status service).
    host = os.environ.get("HOST", "127.0.0.1")
    server = StatusServer(root, make_default_readers(root), port, host=host)
    print(f"tick-vault status server listening on {server.base_url}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

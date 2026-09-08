# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""FastAPI app for stores-explorer: read-only browsing of the shared
Delta Lake and kdb+ stores, for a bdobb widget (the widget's door -- stores-mcp
is the analyst's). Loopback-only; Tailscale Serve is the ingress (see the
repo compose file). No app-level auth -- matches live-grid/stores-mcp's
posture for read-only discovery of data already behind the tailnet.

Every Delta/kdb+ call is a thin wrapper around mcp_stores's existing,
already-scrubbed, already-timeout-bounded functions -- this file adds no
backend client code at all. (Under ArcticDB it had one exception: /arctic/summary
reached into the client directly because mcp_stores had no metadata-only tool.
delta_describe is that tool, so the exception is gone.)

Every backend call is injectable via create_app's keyword arguments, defaulting
to the real mcp_stores imports, so the test suite never touches a real store
(mirrors live-grid's seed_client/client_factory injection pattern).
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from server import (
    delta_describe,
    delta_history,
    delta_list_libraries,
    delta_list_symbols,
    delta_read,
    kdb_select,
    kdb_table_schema,
    kdb_tables,
)

WIDGETS_PATH = Path(__file__).resolve().parent.parent / "widgets.json"
APPS_PATH = Path(__file__).resolve().parent.parent / "apps.json"


def create_app(
    *,
    delta_libraries_fn=delta_list_libraries,
    delta_symbols_fn=delta_list_symbols,
    delta_describe_fn=delta_describe,
    delta_history_fn=delta_history,
    delta_read_fn=delta_read,
    kdb_tables_fn=kdb_tables,
    kdb_schema_fn=kdb_table_schema,
    kdb_select_fn=kdb_select,
) -> FastAPI:
    app = FastAPI()

    # No credential to protect once topology is the gate -- matches
    # live-grid's posture, not key-maint's narrow allowlist (design spec D1/D2).
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    def _404_on_value_error(fn, *args, **kwargs):
        """Every backend ValueError is a "call Y first" contract error.

        One helper rather than a try/except per route: the mapping is
        identical everywhere, and a route that grew its own would be the one
        that quietly stopped preserving the already-scrubbed message.
        """
        try:
            return fn(*args, **kwargs)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.get("/widgets.json")
    def widgets() -> JSONResponse:
        return JSONResponse(json.loads(WIDGETS_PATH.read_text()))

    @app.get("/apps.json")
    def apps() -> JSONResponse:
        """The Ep. 11 example dashboard.

        bdobb fetches this per backend during discovery and resolves each card
        by WIDGET id, not by the backend's per-install UUID -- which is what
        lets a shipped dashboard land on whichever backend serves these
        widgets, with no import step and no bdobb-side code.
        """
        return JSONResponse(json.loads(APPS_PATH.read_text()))

    @app.get("/delta/libraries")
    def delta_libraries() -> list[str]:
        return delta_libraries_fn()

    @app.get("/delta/symbols")
    def delta_symbols(library: str) -> list[str]:
        return _404_on_value_error(delta_symbols_fn, library)

    @app.get("/delta/describe")
    def delta_describe_route(library: str, symbol: str) -> dict:
        return _404_on_value_error(delta_describe_fn, library, symbol)

    @app.get("/delta/history")
    def delta_history_route(library: str, symbol: str) -> list[dict]:
        return _404_on_value_error(delta_history_fn, library, symbol)

    @app.get("/delta/series")
    def delta_series(
        library: str, symbol: str,
        start: str | None = None, end: str | None = None,
        tail_rows: int = 1000, as_of: str | None = None,
    ) -> dict:
        return _404_on_value_error(
            delta_read_fn, library=library, symbol=symbol,
            start=start, end=end, tail_rows=tail_rows, as_of=as_of,
        )

    @app.get("/kdb/tables")
    def kdb_tables_route() -> list[str]:
        return kdb_tables_fn()

    @app.get("/kdb/schema")
    def kdb_schema(table: str) -> list[dict]:
        return _404_on_value_error(kdb_schema_fn, table)

    @app.get("/kdb/select")
    def kdb_select_route(
        table: str, symbol: str | None = None,
        start_time: str | None = None, end_time: str | None = None, limit: int = 1000,
    ) -> dict:
        return _404_on_value_error(
            kdb_select_fn, table, symbol=symbol,
            start_time=start_time, end_time=end_time, limit=limit,
        )

    return app


app = create_app()

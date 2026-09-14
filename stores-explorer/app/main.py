"""FastAPI app for stores-explorer: read-only browsing of the shared
ArcticDB and kdb+ store, for a bdobb widget (the widget's door -- stores-mcp
is the analyst's). Loopback-only; Tailscale Serve is the ingress (see the
repo compose file). No app-level auth -- matches live-grid/stores-mcp's
posture for read-only discovery of data already behind the tailnet.

Every ArcticDB/kdb+ call is a thin wrapper around mcp_stores's existing,
already-scrubbed, already-timeout-bounded functions -- this file adds no new
backend client code except /arctic/summary, which reuses mcp_stores's own
_arctic()/_bounded() helpers directly (deliberately imported despite the
leading underscore -- see the design spec's D3/D6) rather than duplicating
them. Every backend call is injectable via create_app's keyword arguments,
defaulting to the real mcp_stores imports, so the test suite never touches a
real ArcticDB or kdb+ (mirrors live-grid's seed_client/client_factory
injection pattern).
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from server import (
    _arctic,
    _bounded,
    arctic_list_libraries,
    arctic_list_symbols,
    arctic_read,
    kdb_select,
    kdb_table_schema,
    kdb_tables,
)

WIDGETS_PATH = Path(__file__).resolve().parent.parent / "widgets.json"


def create_app(
    *,
    arctic_libraries_fn=arctic_list_libraries,
    arctic_symbols_fn=arctic_list_symbols,
    arctic_read_fn=arctic_read,
    arctic_client_factory=_arctic,
    bounded_fn=_bounded,
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

    @app.get("/widgets.json")
    def widgets() -> JSONResponse:
        return JSONResponse(json.loads(WIDGETS_PATH.read_text()))

    @app.get("/arctic/libraries")
    def arctic_libraries() -> list[str]:
        return arctic_libraries_fn()

    @app.get("/arctic/symbols")
    def arctic_symbols(library: str) -> list[str]:
        try:
            return arctic_symbols_fn(library)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.get("/arctic/summary")
    def arctic_summary(library: str, symbol: str) -> dict:
        try:
            symbols = arctic_symbols_fn(library)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        if symbol not in symbols:
            raise HTTPException(
                status_code=404,
                detail=f"unknown symbol {symbol!r} in {library!r}; call /arctic/symbols first",
            )
        ac = arctic_client_factory()
        lib = bounded_fn(ac.__getitem__, library)
        desc = bounded_fn(lib.get_description, symbol)
        return {
            "library": library,
            "symbol": symbol,
            "row_count": desc.row_count,
            "date_range": [str(desc.date_range[0]), str(desc.date_range[1])],
            "columns": [{"name": c.name, "dtype": str(c.dtype)} for c in desc.columns],
        }

    @app.get("/arctic/series")
    def arctic_series(
        library: str, symbol: str,
        start: str | None = None, end: str | None = None, tail_rows: int = 1000,
    ) -> dict:
        try:
            return arctic_read_fn(library, symbol, start=start, end=end, tail_rows=tail_rows)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.get("/kdb/tables")
    def kdb_tables_route() -> list[str]:
        return kdb_tables_fn()

    @app.get("/kdb/schema")
    def kdb_schema(table: str) -> list[dict]:
        try:
            return kdb_schema_fn(table)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.get("/kdb/select")
    def kdb_select_route(
        table: str, symbol: str | None = None,
        start_time: str | None = None, end_time: str | None = None, limit: int = 1000,
    ) -> dict:
        try:
            return kdb_select_fn(
                table, symbol=symbol, start_time=start_time, end_time=end_time, limit=limit
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    return app


app = create_app()

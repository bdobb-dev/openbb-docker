# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""FastAPI app tying the pieces together: widgets.json, REST seeding, the
websocket stream, and health. One service, loopback-only; Tailscale Serve is
the ingress (see the repo compose file)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware import Middleware

from app.auth import BasicAuthMiddleware
from app.classify import split_by_feed
from app.feeds import FeedManager
from app.figure import build_figure
from app.openbb_client import fetch_series
from app.quotes import QuoteTable

log = logging.getLogger("live-grid")

WIDGETS_PATH = Path(__file__).resolve().parent.parent / "widgets.json"
FLUSH_INTERVAL = 0.25  # seconds between coalesced row flushes per connection


def _api_key() -> str | None:
    return os.environ.get("EODHD_API_KEY") or None


def _rest_client(api_key: str):
    """Official SDK REST client, one per app. Import deferred so unit tests
    that inject their own seed client never import the SDK at all."""
    from eodhd import APIClient  # pylint: disable=import-outside-toplevel

    return APIClient(api_key)


def _parse_symbols(raw: str) -> list[str]:
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


async def build_series(symbol, interval, start, end, recorder, window, provider="kdb"):
    """History joined to tick-derived bars at the first fully-covered bar."""
    from app.series import seam_boundary, stitch, tick_capable, window_end

    history, meta = await fetch_series(symbol, interval, start, end, provider)
    meta = dict(meta)
    meta["rows_from_ticks"] = 0
    meta["seam"] = None

    if recorder is None or not tick_capable(interval, window):
        return history, meta

    try:
        from kdb_store.aggregate import aggregate_ticks

        # tick_span is a blocking IPC round-trip; off the loop like the
        # aggregation below it, or a chart request stalls the grid's
        # websocket flush.
        span = await asyncio.to_thread(recorder.store.tick_span, symbol)
        if span is None:
            return history, meta
        boundary = seam_boundary(span[0], interval)
        # Ticks are only ever as recent as *now*, so a window that ended in
        # the past must not have them tacked on. Clip to whichever of the two
        # ends first; if that leaves nothing at or after the seam, the request
        # is purely historical.
        hi = min(span[1], window_end(end))
        if hi < boundary:
            return history, meta
        ticks = await asyncio.to_thread(
            aggregate_ticks, recorder.store, symbol, interval, boundary, hi
        )
    except Exception as exc:  # noqa: BLE001 - the chart still works without ticks
        log.warning("tick aggregation unavailable for %s: %s", symbol, exc)
        return history, meta

    if not ticks:
        return history, meta
    meta["rows_from_ticks"] = len(ticks)
    meta["seam"] = boundary.isoformat()
    return stitch(history, ticks, boundary), meta


def create_app(*, api_key: str | None = None, seed_client=None, client_factory=None) -> FastAPI:
    """Build the app. Test seams: `seed_client` replaces the SDK REST client,
    `client_factory` is passed through to FeedManager (mock websockets)."""
    key = api_key if api_key is not None else _api_key()

    recorder = None
    if os.getenv("LIVE_GRID_CHART", "true").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from kdb_store.config import resolve_config
            from kdb_store.session import KdbSession
            from kdb_store.store import KdbStore

            from app.recorder import TickRecorder

            config = resolve_config()
            session = KdbSession(config)
            window = timedelta(seconds=int(os.getenv("LIVE_TICK_WINDOW_SECONDS", "86400")))
            recorder = TickRecorder(KdbStore(session), window=window)
        except Exception as exc:  # noqa: BLE001 - the grid works without kdb
            log.warning("tick recording disabled: %s", exc)
            recorder = None

    quotes = QuoteTable(
        on_tick=recorder.record if recorder else None,
        snapshots=recorder.store if recorder else None,
    )
    manager_kwargs = {} if client_factory is None else {"client_factory": client_factory}
    manager = FeedManager(key or "", quotes, recorder=recorder, **manager_kwargs)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(manager.run())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(lifespan=lifespan)
    app.state.quotes = quotes
    app.state.manager = manager

    # The Workspace origin (pro.openbb.co) fetches cross-origin from the
    # browser; BDOBB's non-streaming calls come via plugin-http (no CORS).
    cors_kwargs = dict(allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    try:
        app.add_middleware(CORSMiddleware, allow_private_network=True, **cors_kwargs)
    except TypeError:  # older starlette without PNA support
        app.add_middleware(CORSMiddleware, **cors_kwargs)

    # APPEND, do not add_middleware. Starlette's add_middleware inserts at
    # index 0 and index 0 is OUTERMOST, so calling it here would put auth
    # outside CORS. A browser preflight carries no credentials by definition,
    # so it would get a bare 401 with no Access-Control-Allow-* headers and
    # every cross-origin caller -- including OpenBB Workspace -- would be
    # locked out. curl never sends a preflight, so no amount of curl testing
    # catches it. Appending makes auth INNERMOST, which still covers every
    # path: middleware wraps the whole app, not a router.
    app.user_middleware.append(Middleware(BasicAuthMiddleware))

    def _seed_client():
        nonlocal seed_client
        if seed_client is None:
            if not key:
                raise HTTPException(
                    status_code=500,
                    detail="EODHD_API_KEY is not set. Put it in credentials.env "
                    "(the public demo websocket key covers the default watchlist).",
                )
            seed_client = _rest_client(key)
        return seed_client

    _STATIC = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")

    def _window(start: str | None, end: str | None) -> tuple[str, str]:
        today = date.today()
        return (start or str(today - timedelta(days=365)), end or str(today))

    def _tick_window() -> timedelta:
        return recorder.window if recorder is not None else timedelta(0)

    @app.get("/widgets.json")
    def widgets() -> JSONResponse:
        return JSONResponse(json.loads(WIDGETS_PATH.read_text()))

    @app.get("/live_grid")
    def live_grid(symbol: str = Query(default="")):
        symbols = _parse_symbols(symbol)
        if not symbols:
            return []
        client = _seed_client()
        return quotes.seed(symbols, client)

    @app.get("/series")
    async def series(symbol: str = "AAPL", interval: str = "1d",
                     start: str | None = None, end: str | None = None,
                     provider: str = "kdb"):
        s, e = _window(start, end)
        try:
            bars, meta = await build_series(
                symbol, interval, s, e, recorder, _tick_window(), provider
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("series failed for %s: %s", symbol, exc)
            return JSONResponse(
                {"symbol": symbol, "interval": interval, "start": s, "end": e, "bars": [],
                 "cache": {"cache": "error", "error": str(exc), "rows_from_cache": 0,
                           "rows_from_upstream": 0, "rows_from_ticks": 0,
                           "gaps_fetched": 0, "upstream_ms": 0.0, "kdb_ms": 0.0,
                           "seam": None}},
                status_code=502,
            )
        return {"symbol": symbol, "interval": interval, "start": s, "end": e,
                "bars": bars, "cache": meta}

    @app.get("/chart")
    async def chart(symbol: str = "AAPL", interval: str = "1d",
                    start: str | None = None, end: str | None = None,
                    provider: str = "kdb"):
        s, e = _window(start, end)
        try:
            bars, _ = await build_series(
                symbol, interval, s, e, recorder, _tick_window(), provider
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("chart failed for %s: %s", symbol, exc)
            return JSONResponse({"data": [], "layout": {"title": {"text": f"{symbol}: {exc}"}}},
                                status_code=502)
        return JSONResponse(build_figure(symbol, bars))

    @app.get("/demo", response_class=HTMLResponse)
    async def demo():
        return HTMLResponse((_STATIC / "demo.html").read_text())

    @app.get("/health")
    def health():
        body = {"status": "ok", "feeds": manager.status()}
        if recorder is not None:
            body["ticks"] = recorder.stats()
            body["ticks"]["endpoint"] = getattr(recorder.store.session, "endpoint", None)
        return body

    @app.websocket("/live_grid_ws")
    async def live_grid_ws(ws: WebSocket) -> None:
        await ws.accept()
        conn_id = uuid.uuid4().hex
        stop = asyncio.Event()

        async def consume() -> None:
            """Workspace sends {"params": {"symbol": "A,B"}} on connect and on
            every param change."""
            try:
                while True:
                    msg = await ws.receive_json()
                    params = msg.get("params") if isinstance(msg, dict) else None
                    raw = params.get("symbol", "") if isinstance(params, dict) else ""
                    symbols = _parse_symbols(str(raw))
                    manager.register(conn_id, symbols)
                    # Seed baselines for symbols the GET never saw (a param
                    # change adds symbols without a fresh GET) — off-loop, the
                    # SDK REST client is blocking.
                    missing = [s for s in symbols if s not in quotes._prev_close]
                    if missing and key:
                        try:
                            client = _seed_client()
                            await asyncio.to_thread(quotes.seed, missing, client)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("ws baseline seed failed: %s", exc)
            except (WebSocketDisconnect, RuntimeError):
                stop.set()

        async def produce() -> None:
            """Coalesced flush: EODHD trades can tick hundreds of times a
            second; the latest row per dirty symbol every ~250ms is plenty."""
            try:
                while not stop.is_set():
                    await asyncio.sleep(FLUSH_INTERVAL)
                    for sym in sorted(manager.pop_dirty(conn_id)):
                        row = quotes.rows.get(sym)
                        if row is not None:
                            await ws.send_json(row)
            except (WebSocketDisconnect, RuntimeError):
                stop.set()

        if key is None and seed_client is None:
            await ws.close(code=1011, reason="EODHD_API_KEY is not set")
            return
        consumer = asyncio.create_task(consume())
        producer = asyncio.create_task(produce())
        await stop.wait()
        for t in (consumer, producer):
            t.cancel()
        manager.unregister(conn_id)

    return app


app = create_app()

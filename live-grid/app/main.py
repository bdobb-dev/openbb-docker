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
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware import Middleware

from app.auth import BasicAuthMiddleware
from app.classify import split_by_feed
from app.feeds import FeedManager
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


def create_app(*, api_key: str | None = None, seed_client=None, client_factory=None) -> FastAPI:
    """Build the app. Test seams: `seed_client` replaces the SDK REST client,
    `client_factory` is passed through to FeedManager (mock websockets)."""
    key = api_key if api_key is not None else _api_key()
    quotes = QuoteTable()
    manager_kwargs = {} if client_factory is None else {"client_factory": client_factory}
    manager = FeedManager(key or "", quotes, **manager_kwargs)

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

    @app.get("/health")
    def health():
        return {"status": "ok", "feeds": manager.status()}

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

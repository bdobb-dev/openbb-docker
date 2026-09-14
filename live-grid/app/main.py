"""FastAPI app tying the pieces together: widgets.json, REST seeding, the
websocket stream, and health. One service, loopback-only; Tailscale Serve is
the ingress (see the repo compose file)."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware import Middleware

from app.auth import BasicAuthMiddleware
from app.classify import FEEDS, classify
from app.feeds import FeedManager
from app.figure import build_figure
from app.leases import DEFAULT_TTL, LeaseRegistry
from app.openbb_client import fetch_series
from app.quotes import QuoteTable
from app.symbol_meta import get_meta
from app.ta.figure import delta as ta_delta
from app.ta.macros import load_all as load_macros_all
from app.ta.payload import (
    ChartParams,
    any_repaints,
    bars_to_frame,
    build_payload,
    revised_from,
    with_anchor,
)
from app.ta.sources import EodhdSource

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
    leases = LeaseRegistry(manager, ttl=float(os.getenv("LIVE_GRID_LEASE_TTL_S", DEFAULT_TTL)))

    from app.watchlist import DEFAULT_PATH as WATCHLIST_DEFAULT, Watchlist

    watchlist = Watchlist(os.getenv("LIVE_GRID_WATCHLIST", WATCHLIST_DEFAULT))
    _raw_max_symbols = os.getenv("LIVE_GRID_MAX_SYMBOLS", "50")
    try:
        max_symbols = int(_raw_max_symbols)
    except ValueError:
        log.warning(
            "LIVE_GRID_MAX_SYMBOLS=%r is not an integer; using the default of 50",
            _raw_max_symbols,
        )
        max_symbols = 50

    # The desktop client passes an iframe widget's `endpoint` straight to
    # `new URL(raw)` with no base -- a relative path is refused outright, so
    # the subscriptions widget needs an absolute URL rewritten in at request
    # time (GET /widgets.json below). Without this set there is no way to
    # advertise a working iframe endpoint, so the widget is omitted instead
    # of shipping one that always errors.
    public_url = (os.getenv("LIVE_GRID_PUBLIC_URL") or "").strip().rstrip("/") or None
    if public_url is None:
        log.warning(
            "LIVE_GRID_PUBLIC_URL is not set; the subscriptions widget will be "
            "omitted from /widgets.json (it cannot render without an absolute endpoint)"
        )

    async def _sweep_leases() -> None:
        interval = float(os.getenv("LIVE_GRID_LEASE_SWEEP_S", "30"))
        while True:
            await asyncio.sleep(interval)
            try:
                dropped = leases.sweep(asyncio.get_running_loop().time())
                if dropped:
                    log.info("leases lapsed: %s", dropped)
            except Exception:  # noqa: BLE001 - the sweeper must outlive its errors
                log.warning("lease sweep failed", exc_info=True)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Restore the pinned subscriptions before the drain loop starts, so the
        # first _sync_feeds already has them and a restart does not drop the feed.
        _apply_watchlist()
        task = asyncio.create_task(manager.run())
        sweep_task = asyncio.create_task(_sweep_leases())
        try:
            yield
        finally:
            task.cancel()
            sweep_task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            try:
                await sweep_task
            except asyncio.CancelledError:
                pass

    app = FastAPI(lifespan=lifespan)
    app.state.quotes = quotes
    app.state.manager = manager
    app.state.leases = leases
    app.state.watchlist = watchlist

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
        spec = json.loads(WIDGETS_PATH.read_text())
        try:
            macros = load_macros_all()
        except Exception as exc:  # noqa: BLE001 - a bad macro must not blank the grid
            log.warning("macro discovery failed: %s", exc)
            macros = {}
        for param in spec.get("ta_chart", {}).get("params", []):
            if param.get("paramName") == "macro":
                param["options"] = [{"label": "None", "value": "none"}] + [
                    {"label": m.label, "value": name} for name, m in sorted(macros.items())
                ]
        if public_url is not None:
            if "subscriptions" in spec:
                spec["subscriptions"]["endpoint"] = f"{public_url}/subscriptions"
        else:
            spec.pop("subscriptions", None)
        return JSONResponse(spec)

    @app.get("/live_grid")
    def live_grid(symbol: str = Query(default="")):
        symbols = _parse_symbols(symbol)
        if not symbols:
            return []
        client = _seed_client()
        return quotes.seed(symbols, client)

    @app.get("/symbol_meta")
    def symbol_meta(symbol: str = Query(default="")):
        """Display metadata (logo, name, 52-week levels) — the favicon
        pattern: consumers fetch once on mount, best-effort."""
        symbols = _parse_symbols(symbol)
        if not symbols:
            return []
        return get_meta(symbols, _seed_client())

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

    _eodhd = EodhdSource(
        key or "",
        min_refetch_s=float(os.getenv("TA_EODHD_MIN_REFETCH_S", "60")),
    )

    @app.get("/ta_chart")
    async def ta_chart(symbol: str = "AAPL", interval: str = "1d",
                       source: str = "local", macro: str = "none",
                       indicators: str = "", anchor: str | None = None,
                       start: str | None = None,
                       end: str | None = None, provider: str = "kdb"):
        s, e = _window(start, end)
        indicators = with_anchor(indicators, anchor)
        params = ChartParams(symbol, interval, source, macro, indicators, s, e, provider)
        bars_error = None
        try:
            bars, _ = await build_series(
                symbol, interval, s, e, recorder, _tick_window(), provider
            )
        except Exception as exc:  # noqa: BLE001 - a bad macro must still be reported below
            log.warning("ta_chart bars unavailable for %s: %s", symbol, exc)
            bars, bars_error = [], exc
        try:
            figure, _, _, _ = await build_payload(
                params, bars_to_frame(bars), eodhd_source=_eodhd
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("ta_chart failed for %s: %s", symbol, exc)
            return JSONResponse(
                {"data": [], "layout": {"title": {"text": f"{symbol}: {exc}"}}},
                status_code=502,
            )
        if bars_error is not None:
            # An empty chart that does not say why it is empty is the failure
            # mode this design treats as a defect everywhere else -- an EODHD
            # fallback annotates the legend, and an indicator that nulled itself
            # was a Critical bug. Degrading is right; degrading silently is not.
            figure["layout"]["title"]["text"] += f"  ·  bars unavailable: {bars_error}"
        return JSONResponse(figure)

    @app.websocket("/ta_chart_ws")
    async def ta_chart_ws(ws: WebSocket) -> None:
        await ws.accept()
        query = ws.query_params
        s, e = _window(query.get("start"), query.get("end"))
        params = ChartParams(
            symbol=query.get("symbol", "AAPL"),
            interval=query.get("interval", "1d"),
            source=query.get("source", "local"),
            macro=query.get("macro", "none"),
            indicators=with_anchor(query.get("indicators", ""), query.get("anchor")),
            start=s, end=e, provider=query.get("provider", "kdb"),
        )
        interval_s = float(os.getenv("TA_PUSH_INTERVAL_MS", "1000")) / 1000.0
        previous: list[str] = []
        previous_marks: tuple[str, ...] = ()
        rev = 0
        try:
            while True:
                started = asyncio.get_running_loop().time()
                # NOTE: this re-fetches history over HTTP every push, not just
                # the ticks. Spec D9 costed the indicator recompute (0.66 ms)
                # but not this round-trip, so the real per-push cost is
                # dominated by I/O, not arithmetic. Acceptable for v1 because
                # the kdb read-through cache makes it a local hit and
                # TA_PUSH_INTERVAL_MS is tunable -- but the cheap win, if this
                # ever hurts, is to refetch history only on bar close and
                # re-aggregate ticks in between.
                bars_error = None
                try:
                    bars, _ = await build_series(
                        params.symbol, params.interval, s, e, recorder,
                        _tick_window(), params.provider
                    )
                # Degrade like /ta_chart: keep the connection open with a
                # blank-and-say-why chart instead of going silently dark
                # (Task 11 review).
                except Exception as exc:  # noqa: BLE001
                    log.warning("ta_chart_ws bars unavailable for %s: %s", params.symbol, exc)
                    bars, bars_error = [], exc
                try:
                    figure, panes, frame, annotations = await build_payload(
                        params, bars_to_frame(bars), eodhd_source=_eodhd
                    )
                # Mirrors /ta_chart's 502: not a missing-bars degradation, this
                # is unrecoverable (bad macro, bad indicator params), so end
                # the stream rather than looping on a permanent failure.
                except Exception as exc:  # noqa: BLE001
                    log.warning("ta_chart_ws failed for %s: %s", params.symbol, exc)
                    await ws.close(code=1011, reason=str(exc)[:120])
                    return
                if bars_error is not None:
                    figure["layout"]["title"]["text"] += f"  ·  bars unavailable: {bars_error}"
                dates = [str(d) for d in frame["date"].to_list()] if frame.height else []
                # `bars_error` forces a FIGURE push, not a delta. The
                # "bars unavailable" note lives in the figure's title, and a
                # delta carries no title -- so on a delta push the client would
                # receive an emptied chart with no explanation at all. That is
                # the same silently-blank failure the REST route was fixed for,
                # one layer down.
                # A change in ANNOTATIONS forces a figure too. They live only
                # in the title, so an EODHD fetch that starts failing mid-stream
                # would otherwise swap the series to local values while the
                # title, frozen at rev 0, still reads "eodhd".
                marks = tuple(sorted({a.column for a in annotations}))
                if (rev == 0 or any_repaints(panes) or bars_error is not None
                        or marks != previous_marks):
                    await ws.send_json({"type": "figure", "rev": rev, "figure": figure})
                else:
                    payload = ta_delta(frame, panes, revised_from(previous, dates))
                    await ws.send_json({"type": "delta", "rev": rev, **payload})
                previous, previous_marks, rev = dates, marks, rev + 1
                # Drop rather than queue: a recompute that overran its slot must
                # not build a backlog that never drains.
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(max(0.0, interval_s - elapsed))
        except WebSocketDisconnect:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("ta_chart_ws ended for %s: %s", params.symbol, exc)
            return

    @app.get("/demo", response_class=HTMLResponse)
    async def demo():
        return HTMLResponse((_STATIC / "demo.html").read_text())

    @app.get("/subscriptions", response_class=HTMLResponse)
    async def subscriptions_page():
        return HTMLResponse((_STATIC / "subscriptions.html").read_text())

    @app.get("/health")
    def health():
        body = {"status": "ok", "feeds": manager.status(), "leases": len(leases)}
        if recorder is not None:
            body["ticks"] = recorder.stats()
            body["ticks"]["endpoint"] = getattr(recorder.store.session, "endpoint", None)
        body["ta"] = {
            "eodhd_calls": _eodhd.total_calls,
            "calls_per_indicator": 5,
            "min_refetch_s": float(os.getenv("TA_EODHD_MIN_REFETCH_S", "60")),
        }
        return body

    @app.post("/subscribe")
    async def subscribe(body: dict):
        """Lease a live feed for symbols, keyed by symbol and renewable.

        Tailnet-only, like every live-grid route: it confers no power a caller
        does not already have by opening /live_grid_ws. Never funnel it.
        """
        raw_symbols = body.get("symbols")
        if not isinstance(raw_symbols, list):
            # A bare string ("AAPL") is iterable too -- without this check it
            # silently leases "A", "P", "L" instead of 422ing.
            raise HTTPException(status_code=422, detail="symbols must be a list of strings")
        symbols = [s for s in raw_symbols if str(s).strip()]
        if not symbols:
            raise HTTPException(status_code=422, detail="symbols must be a non-empty list")
        ttl = body.get("ttl")
        if ttl is not None:
            try:
                ttl = float(ttl)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="ttl must be a number") from None
            # float() accepts "nan" and "inf". A NaN expiry never satisfies
            # `exp <= now`, so the sweeper can never reclaim that lease and it
            # pins an EODHD subscription for the life of the process; an
            # infinite one does the same. A non-positive ttl is already expired,
            # which is a request that cannot mean what it says.
            if not math.isfinite(ttl) or ttl <= 0:
                raise HTTPException(
                    status_code=422, detail="ttl must be a finite positive number"
                ) from None
        granted = leases.renew(
            symbols,
            now=asyncio.get_running_loop().time(),
            ttl=ttl,
        )
        base = datetime.now(timezone.utc)
        loop_now = asyncio.get_running_loop().time()
        return {
            "leases": {
                sym: (base + timedelta(seconds=exp - loop_now)).isoformat()
                for sym, exp in granted.items()
            }
        }

    _GROUP_NAMES = {"us": "Equity", "crypto": "Crypto", "forex": "Forex"}

    def _apply_watchlist() -> None:
        """Register every pinned symbol with the feed manager, up to the cap.

        One synthetic id per symbol -- `watchlist:<SYMBOL>` -- mirroring the lease
        registry, because `_sync_feeds` unions symbols across every `_conns` entry
        and does not care which of them came from a websocket. Unregistering the
        symbols that are no longer pinned is what makes a removal take effect.

        The watchlist file is hand-editable (see its docstring), so its size is
        not itself bounded by the cap -- truncate here rather than trust the
        file, or an oversized file registers in full and the widget reports a
        `used` count past `cap`.
        """
        all_pinned = watchlist.symbols()
        wanted = set(all_pinned[:max_symbols])
        if len(all_pinned) > max_symbols:
            log.warning(
                "watchlist has %d symbols but the cap is %d; %d dropped: %s",
                len(all_pinned), max_symbols, len(all_pinned) - max_symbols,
                all_pinned[max_symbols:],
            )
        current = {
            conn_id[len("watchlist:"):]
            for conn_id in list(manager._conns)
            if conn_id.startswith("watchlist:")
        }
        for sym in wanted - current:
            manager.register(f"watchlist:{sym}", [sym])
        for sym in current - wanted:
            manager.unregister(f"watchlist:{sym}")

    def _subscribed() -> set[str]:
        """Every symbol actually subscribed at EODHD right now.

        The feed manager's `_conns` is the vendor's real view: pins and leases
        are already registered there under their own connection ids, but so is
        anything registered by a `/live_grid_ws` client -- those never show up
        in `pinned` or `leased` yet still cost a slot. Reading `_union` per
        feed is a superset of (and replaces) unioning `pinned` and `leased`.
        """
        return set().union(*(manager._union(feed) for feed in FEEDS))

    def _subscription_payload() -> dict:
        """Pinned, leased, and the budget they share.

        `used` is the count of everything actually subscribed at the vendor
        (see `_subscribed`), not merely the union of `pinned` and `leased` --
        a websocket grid connection subscribes symbols too, without pinning or
        leasing them.
        """
        pinned = watchlist.symbols()
        leased = leases.symbols()
        groups: dict[str, list[str]] = {name: [] for name in _GROUP_NAMES.values()}
        for sym in pinned:
            groups[_GROUP_NAMES[classify(sym)]].append(sym)
        return {
            "service": "EODHD",
            "cap": max_symbols,
            "used": len(_subscribed()),
            "pinned": pinned,
            "leased": leased,
            "groups": groups,
        }

    def _reject_foreign_origin(request: Request) -> None:
        """403 a mutating request whose browser-supplied Origin is not this
        widget's own public URL.

        CORS here is wide open (`allow_origins=["*"]`, needed for the
        Workspace origin's read-only fetches), so with no check any page an
        operator's browser has open could POST or DELETE against the durable
        watchlist. A request with no Origin header (curl, server-side
        callers) is unaffected -- only a browser sets one.
        """
        origin = request.headers.get("origin")
        if origin is None:
            return
        if public_url is None or origin.rstrip("/") != public_url:
            raise HTTPException(status_code=403, detail="origin not allowed")

    @app.get("/api/subscriptions")
    async def list_subscriptions():
        return _subscription_payload()

    @app.post("/api/subscriptions", status_code=201)
    async def add_subscription(body: dict, request: Request):
        _reject_foreign_origin(request)
        sym = str(body.get("symbol") or "").strip().upper()
        if not sym:
            raise HTTPException(status_code=422, detail="symbol must be a non-empty string")
        if sym in set(watchlist.symbols()):
            raise HTTPException(status_code=409, detail=f"{sym} is already subscribed")
        # Union again: adding a symbol that is ALREADY subscribed (pinned,
        # leased, or held by a grid websocket) costs no new slot.
        projected = _subscribed() | {sym}
        if len(projected) > max_symbols:
            raise HTTPException(
                status_code=507,
                detail=f"cap of {max_symbols} reached for EODHD; remove a symbol first",
            )
        watchlist.add(sym)
        _apply_watchlist()
        return _subscription_payload()

    @app.delete("/api/subscriptions/{symbol}")
    async def remove_subscription(symbol: str, request: Request):
        _reject_foreign_origin(request)
        sym = symbol.strip().upper()
        if not watchlist.remove(sym):
            raise HTTPException(status_code=404, detail=f"{sym} is not subscribed")
        _apply_watchlist()
        return _subscription_payload()

    @app.get("/snapshot")
    async def snapshot(symbol: str):
        """The delayed REST snapshot for one symbol.

        Exists so the kdb quote provider has a fallback without taking on the
        eodhd client, the AAPL -> AAPL.US mapping and the snapshot cache that
        already live here. `delayed` is always true: this is EODHD's REST
        endpoint, roughly 15-20 minutes behind, never the websocket.
        """
        sym = symbol.strip().upper()
        # _seed_client(), not the closure variable directly: `seed_client` is
        # a test seam that stays None in production until _seed_client()
        # lazily builds it (same as /live_grid and the websocket baseline
        # seed do), otherwise this route 404s forever on a cold process.
        client = _seed_client()
        rows = await asyncio.to_thread(quotes.seed, [sym], client)
        row = rows[0] if rows else None
        price = (row or {}).get("price")
        if price is None:
            raise HTTPException(status_code=404, detail=f"no snapshot for {sym}")
        return {
            "symbol": sym,
            "price": float(price),
            # seed() stashes the vendor's previous close in the table's own
            # private cache, not on the row -- read it from there rather
            # than deriving it (price - change), which would drift.
            "prev_close": quotes._prev_close.get(sym),
            "delayed": True,
        }

    @app.get("/ticks")
    async def ticks(symbol: str, limit: int = 200):
        """The newest ticks held in the kdb cache for one symbol, newest first.

        kdb is in-memory at this version: a restarted store holds nothing, and
        a deployment may carry no store at all. Both answer an empty list with
        a 200 -- the widget then reads as "no ticks" rather than as an error
        card, which is the honest rendering of an empty cache.
        """
        if limit < 1:
            raise HTTPException(status_code=422, detail="limit must be a positive integer")
        # Capped rather than 422'd: a caller asking for more than a card can
        # show has made no error, it just must not be able to pull the whole
        # rolling window over the wire.
        limit = min(limit, 1000)
        sym = symbol.strip().upper()
        if not sym:
            raise HTTPException(status_code=422, detail="symbol must be a non-empty string")
        if recorder is None:
            return {"symbol": sym, "ticks": []}
        try:
            # A blocking IPC round-trip, off the loop like /series's tick
            # aggregation above -- otherwise this stalls the grid's websocket
            # flush for every open connection.
            rows = await asyncio.to_thread(recorder.store.read_ticks, sym, limit)
        except Exception as exc:  # noqa: BLE001 - the widget still works without ticks
            log.warning("tick read failed for %s: %s", sym, exc)
            return {"symbol": sym, "ticks": []}
        return {
            "symbol": sym,
            "ticks": [
                {"time": r["time"].isoformat(), "price": r["price"], "size": r["size"]}
                for r in rows
            ],
        }

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

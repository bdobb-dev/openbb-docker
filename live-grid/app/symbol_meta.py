"""Per-symbol display metadata: logo, name, 52-week levels.

One filtered /fundamentals call per symbol — the multi-path filter returns
exactly the four fields, nothing else — cached in-process for a day (logos
never move; 52-week levels move at most daily). Consumers follow the news
rail's favicon pattern: fetch once on mount, best-effort, render without it
on failure.

Logo URLs must come from the payload verbatim: the static filenames are
case-inconsistent (aapl.png but MSFT.png), so deriving them from the symbol
404s. The static host is public — no api_token, no credit cost — so the
absolutized URL drops straight into an <img src>.
"""

import logging
import time
from typing import Any

from app.classify import snapshot_ticker

log = logging.getLogger("live-grid")

_FILTER = "General::LogoURL,General::Name,Technicals::52WeekHigh,Technicals::52WeekLow"
_TTL = 24 * 3600.0
_NEG_TTL = 3600.0  # failed lookups retry hourly, not per request

_cache: dict[str, tuple[dict[str, Any], float]] = {}


def _f(value: Any) -> float | None:
    if value in (None, "", "NA"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _blank(symbol: str) -> dict[str, Any]:
    return {"symbol": symbol, "name": None, "logo_url": None,
            "week52_high": None, "week52_low": None}


def _reset_cache_for_tests() -> None:
    _cache.clear()


def get_meta(symbols: list[str], client) -> list[dict[str, Any]]:
    """Metadata rows in request order; failures yield blank rows, never raise."""
    out: list[dict[str, Any]] = []
    now = time.monotonic()
    for sym in symbols:
        cached = _cache.get(sym)
        if cached and cached[1] > now:
            out.append(cached[0])
            continue
        row = _blank(sym)
        ttl = _NEG_TTL
        try:
            resp = client.get_fundamentals_data(
                ticker=snapshot_ticker(sym), filter=_FILTER
            )
            if isinstance(resp, dict):
                logo = resp.get("General::LogoURL")
                if not isinstance(logo, str) or logo in ("", "NA"):
                    logo = None  # forex/crypto answer the literal string "NA"
                name = resp.get("General::Name")
                row["name"] = None if name in (None, "", "NA") else name
                row["logo_url"] = (
                    f"https://eodhd.com{logo}" if logo and logo.startswith("/") else logo
                )
                row["week52_high"] = _f(resp.get("Technicals::52WeekHigh"))
                row["week52_low"] = _f(resp.get("Technicals::52WeekLow"))
                ttl = _TTL
        except Exception:  # noqa: BLE001 — metadata is decoration, never a failure
            log.info("symbol_meta lookup failed for %s", sym)
        _cache[sym] = (row, now + ttl)
        out.append(row)
    return out

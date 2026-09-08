# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""In-memory latest-quote table shared by REST seeding and websocket ticks."""

from datetime import datetime, timezone
from typing import Any

from app.classify import snapshot_ticker


def _f(value: Any) -> float | None:
    """Coerce EODHD numerics (often strings, sometimes 'NA') to float or None."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _blank_row(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "price": None,
        "change": None,
        "change_percent": None,
        "bid": None,
        "ask": None,
        "last_size": None,
        "volume": None,
        "updated_at": None,
    }


class QuoteTable:
    """symbol -> latest row dict; prev-closes cached for change math."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self._prev_close: dict[str, float] = {}

    def seed(self, symbols: list[str], client) -> list[dict[str, Any]]:
        """Fill rows from the SDK real-time snapshot; returns rows in request order."""
        out: list[dict[str, Any]] = []
        for sym in symbols:
            row = self.rows.setdefault(sym, _blank_row(sym))
            try:
                snap: Any = client.get_live_stock_prices(ticker=snapshot_ticker(sym))
            except Exception:  # noqa: BLE001 -- degrade to a blank row, don't fail the GET
                snap = None
            if isinstance(snap, list):
                snap = snap[0] if snap else None
            if isinstance(snap, dict):
                price = _f(snap.get("close"))
                prev = _f(snap.get("previousClose"))
                if prev:
                    self._prev_close[sym] = prev
                if price is not None:
                    row["price"] = price
                if price is not None and prev:
                    row["change"] = price - prev
                    row["change_percent"] = (price - prev) / prev
                else:
                    row["change"] = _f(snap.get("change"))
                    cp = _f(snap.get("change_p"))
                    row["change_percent"] = cp / 100 if cp is not None else None
                vol = _f(snap.get("volume"))
                if vol is not None:
                    row["volume"] = vol
            out.append(row)
        return out

    def apply_tick(self, feed: str, msg: dict) -> str | None:
        """Update a row from a websocket tick; returns the symbol iff it changed.

        Trades/crypto carry p (price) and q (size); forex carries b/a (bid/ask)
        and no p — price becomes the mid (or whichever side is present).
        """
        sym = msg.get("s")
        if not sym:
            return None
        row = self.rows.setdefault(sym, _blank_row(sym))
        if feed == "forex":
            bid, ask = _f(msg.get("b")), _f(msg.get("a"))
            if bid is None and ask is None:
                return None
            if bid is not None:
                row["bid"] = bid
            if ask is not None:
                row["ask"] = ask
            price = (bid + ask) / 2 if bid is not None and ask is not None else (
                bid if bid is not None else ask
            )
        else:
            price = _f(msg.get("p"))
            if price is None:
                return None
            size = _f(msg.get("q"))
            if size is not None:
                row["last_size"] = size
        row["price"] = price
        prev = self._prev_close.get(sym)
        if prev:
            row["change"] = price - prev
            row["change_percent"] = (price - prev) / prev
        ts = msg.get("t")
        stamp = (
            datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            if isinstance(ts, (int, float))
            else datetime.now(tz=timezone.utc)
        )
        row["updated_at"] = stamp.strftime("%H:%M:%S")
        return sym

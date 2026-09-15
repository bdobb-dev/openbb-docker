# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""OpenBB Platform router: /api/v1/calendar/trading and /trading_table.

Both are plain-JSON commands, not OBBject ones. The data route must return
EODHD's exchange-details v2 document as-is (`{"data": {...}}`), which an OBBject
would wrap in `results`. The table follows it so one computation feeds both.
Both sit behind the stack's Basic auth like every other path, because that
middleware wraps the whole app."""

# No `from __future__ import annotations` here. Router.command decides between
# an OBBject command and plain JSON with isclass() on the return annotation,
# and a string annotation is not a class.
from datetime import date

from fastapi import HTTPException
from openbb_core.app.router import Router
from pydantic import BaseModel

from openbb_trading_calendar.calendar import NAMES, table_rows, trading_calendar

router = Router(prefix="", description="Exchange trading calendars (pandas-market-calendars).")


class TradingDay(BaseModel):
    """One Trading calendar row. openbb-platform-api turns these fields into the
    table's columns, so the model exists for widgets.json, not for validation."""

    date: str
    holiday: str
    type: str
    early_close: str | None = None


def _calendar(exchange: str, year: int) -> dict:
    cal = trading_calendar(exchange, year)
    if cal is None:
        # HTTPException passes through openbb_core's CommandRunner with its own
        # status; any other exception is re-raised as OpenBBError and answered
        # 400. A 404 is what lets the app remember "this backend has no
        # calendar for that" and fall back without retrying.
        raise HTTPException(status_code=404, detail=f"No trading calendar for {exchange} {year}")
    return cal


@router.command(methods=["GET"], widget_config={"exclude": True})
async def trading(exchange: str, year: int) -> dict:
    """Trading hours and holidays for one exchange and year, in EODHD v2 shape.

    Machine-facing: bdobb's period expressions read it. Excluded from
    widgets.json; the Trading calendar widget shows the same data."""
    return _calendar(exchange, year)


@router.command(
    methods=["GET"],
    widget_config={
        "name": "Trading calendar",
        "params": [
            {
                "paramName": "exchange",
                "value": "XNYS",
                "options": [{"label": f"{mic} {name}", "value": mic} for mic, name in NAMES.items()],
            },
            # ponytail: the default is the year this process started; a
            # container running across New Year shows last year until restart.
            # Resolve it per request if that ever matters.
            {"paramName": "year", "type": "number", "value": date.today().year},
        ],
    },
)
async def trading_table(exchange: str, year: int) -> list[TradingDay]:
    """Holidays and early closes for one exchange and year."""
    return table_rows(_calendar(exchange, year))

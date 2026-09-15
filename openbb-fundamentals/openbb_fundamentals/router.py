# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""OpenBB Platform router: /api/v1/fundamentals/fiscal_year_end.

A plain-JSON command, not an OBBject one: bdobb reads `{"fiscalYearEnd":
"September"}` as-is, which an OBBject would wrap in `results`. It sits behind
the stack's Basic auth like every other path, because that middleware wraps
the whole app."""

# No `from __future__ import annotations` here. Router.command decides between
# an OBBject command and plain JSON with isclass() on the return annotation,
# and a string annotation is not a class.
import asyncio

from fastapi import HTTPException
from openbb_core.app.router import Router

from openbb_fundamentals import sec

router = Router(prefix="", description="Company fundamentals from SEC EDGAR.")


@router.command(methods=["GET"], widget_config={"exclude": True})
async def fiscal_year_end(symbol: str) -> dict:
    """The month a company's fiscal year ends in, from SEC EDGAR.

    Machine-facing: bdobb resolves fiscal quarters and years with it. Excluded
    from widgets.json."""
    try:
        # urllib blocks, and openbb_core calls a sync command on the event loop
        # itself (maybe_coroutine), which would stall every other request for
        # the length of an SEC round trip. A worker thread does not.
        month = await asyncio.to_thread(sec.fiscal_year_end, symbol)
    except sec.UpstreamError as exc:
        # HTTPException passes through openbb_core's CommandRunner with its own
        # status. 502, not 404: the app remembers a 404 for the session, and
        # SEC being down says nothing about the ticker.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if month is None:
        raise HTTPException(status_code=404, detail=f"No fiscal year end for {symbol}")
    return {"fiscalYearEnd": month}

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""One envelope for every failure. FastAPI's own validation errors are re-wrapped."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from security_master_api.errors import DomainError


def request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if not rid:
        rid = "req_" + uuid.uuid4().hex[:12]
        request.state.request_id = rid
    return rid


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def _stamp(request: Request, call_next):
        request_id(request)
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        return response

    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status, content=exc.envelope(request_id(request)))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        err = DomainError("QUERY_REJECTED", "request body or parameters are invalid",
                          {"errors": [{"loc": [str(p) for p in e.get("loc", [])],
                                       "msg": e.get("msg")} for e in exc.errors()]})
        return JSONResponse(status_code=422, content=err.envelope(request_id(request)))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # A handler for Exception runs OUTSIDE the `_stamp` middleware -- Starlette's
        # ServerErrorMiddleware is the outermost layer -- so this response never passes
        # through the stamping code and must set `x-request-id` itself. The message says
        # nothing about `exc`: a traceback or a driver's string is not the client's business.
        # The exception still reaches the server log through Starlette's own re-raise path.
        rid = request_id(request)
        err = DomainError("INTERNAL_ERROR", "the service failed to answer this request")
        return JSONResponse(status_code=500, content=err.envelope(rid),
                            headers={"x-request-id": rid})

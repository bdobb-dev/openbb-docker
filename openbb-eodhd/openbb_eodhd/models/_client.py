# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Official EODHD SDK client helpers shared by all fetchers.

Every HTTP call in this extension goes through the official `eodhd` package
(`APIClient`) — https://github.com/EodHistoricalData/EODHD-APIs-Python-Financial-Library.
The dependency is pinned to a GitHub commit because the PyPI release (1.0.32)
predates the SDK's typed errors (it can sys.exit() on API errors), request
timeouts, and the fundamentals `filter` parameter.
"""

from typing import NoReturn

from openbb_core.app.model.abstract.error import OpenBBError
from openbb_core.provider.utils.errors import UnauthorizedError


def get_client(credentials: dict[str, str] | None):
    """Build an official-SDK APIClient from OpenBB credentials."""
    api_key = (credentials or {}).get("eodhd_api_key")
    if not api_key:
        raise UnauthorizedError("Missing EODHD credential. Set EODHD_API_KEY.")
    # pylint: disable=import-outside-toplevel
    from eodhd import APIClient

    try:
        client = APIClient(api_key)
    except ValueError as exc:  # the SDK rejects malformed keys up front
        raise UnauthorizedError(f"EODHD API key rejected: {exc}") from exc
    apply_openbb_http_settings(client)
    return client


def apply_openbb_http_settings(client) -> None:
    """Make the vendored SDK honour OpenBB's ``system_settings.json`` http block.

    Every other provider in the Platform reaches the network through
    ``openbb_core.provider.utils.helpers``, so a proxy, a custom CA bundle, a
    client certificate or a request timeout configured under
    ``python_settings.http`` applies to all of them. This extension calls the
    official EODHD SDK instead -- a deliberate Ep. 9 choice -- and the SDK
    builds its own bare ``requests.Session``, so without this those settings
    were silently ignored for ``provider="eodhd"`` alone.

    ``_session`` and ``_timeout`` are SDK internals. That is tolerable here in
    a way core internals are not: the SDK is pinned to an exact commit, so it
    cannot drift underneath us without someone editing the pin. ``tests/
    test_client.py`` asserts both attributes still exist, so moving the pin
    fails CI rather than quietly reverting to unconfigured HTTP.

    Best-effort at runtime: if anything here fails we keep the SDK's defaults
    rather than take the provider down over transport preferences.
    """
    # pylint: disable=import-outside-toplevel
    try:
        from openbb_core.provider.utils.helpers import (
            get_python_request_settings,
            get_requests_session,
        )

        if hasattr(client, "_session"):
            # NB: do NOT pass session=... -- get_requests_session returns a
            # supplied Session untouched, which would apply nothing.
            client._session = get_requests_session()  # noqa: SLF001

        timeout = get_python_request_settings().get("timeout")
        if timeout is not None and hasattr(client, "_timeout"):
            # requests takes (connect, read); OpenBB exposes a single number.
            client._timeout = (  # noqa: SLF001
                (float(timeout), float(timeout))
                if isinstance(timeout, (int, float))
                else tuple(timeout)
            )
    except Exception:  # noqa: BLE001 - transport prefs must never break a fetch
        pass


def raise_sdk_error(exc: Exception, context: str) -> NoReturn:
    """Map an official-SDK exception onto OpenBB's error types.

    Checked via the `status_code` attribute (EODHDHTTPError) rather than the
    SDK's exception classes so `eodhd` stays a lazy import.
    """
    status = getattr(exc, "status_code", None)
    if status in (401, 402, 403):
        raise UnauthorizedError(
            f"EODHD {context}: HTTP {status} — verify EODHD_API_KEY is valid and"
            " the token/plan has access to this data."
        ) from exc
    raise OpenBBError(f"EODHD {context} failed: {exc}") from exc


def sdk_call(credentials: dict[str, str] | None, call, context: str):
    """One SDK call with the extension's standard error mapping.

    `call` receives the connected client; OpenBB errors pass through untouched
    and anything else maps via `raise_sdk_error`.
    """
    client = get_client(credentials)
    try:
        with client:
            return call(client)
    except (OpenBBError, UnauthorizedError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise_sdk_error(exc, context)


def rest_json(client, endpoint: str, params: dict) -> "object":
    """Parsed-JSON GET against an endpoint the pinned SDK has no wrapper for.

    Rides the SDK's own BaseAPI (same session, timeout, typed errors, and
    parsed-JSON returns as every official wrapper) with the client's key.
    None-valued params are omitted; call inside `sdk_call` so its errors map
    through `raise_sdk_error` like any other SDK call.
    """
    # pylint: disable=import-outside-toplevel
    from eodhd.APIs.BaseAPI import BaseAPI

    api = BaseAPI(
        session=getattr(client, "_session", None),
        timeout=getattr(client, "_timeout", (5.0, 30.0)),
    )
    qs = "".join(api._param(k, v) for k, v in params.items())  # pylint: disable=protected-access
    return api._rest_get_method(  # pylint: disable=protected-access
        api_key=client._api_key,  # pylint: disable=protected-access
        endpoint=endpoint,
        querystring=qs,
    )

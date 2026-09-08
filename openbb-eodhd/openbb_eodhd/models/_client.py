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
        return APIClient(api_key)
    except ValueError as exc:  # the SDK rejects malformed keys up front
        raise UnauthorizedError(f"EODHD API key rejected: {exc}") from exc


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

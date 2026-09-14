"""EODHD through this stack's OpenBB REST API.

The point of the round trip: the EODHD key lives on the server, so this
laptop holds no provider credential and needs no OpenBB install -- just a
URL and the Basic-auth pair that already guards the API. That is the payoff
of running the stack: the per-minute 2023 comparison yfinance cannot serve
becomes possible without ever putting a provider key on the laptop.

Only `requests`' OWN exception hierarchy (`requests.exceptions.RequestException`
-- connection errors, timeouts, malformed responses) is caught and classified.
A bug in our code (AttributeError, TypeError, a mistyped attribute, ...) is not
a data-availability problem and must propagate unchanged rather than being
misreported as "no interval could serve this symbol". See
`tick_lab.reference.yfinance_adapter` for the same rule applied to yfinance.
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd

from tick_lab.reference.base import ReferenceError
from tick_lab.rollup import BAR_COLUMNS

_SUPPORTED = ("1m", "5m", "1h", "1d")

_PATH = "/api/v1/equity/price/historical"


def classify_status(status: int, body: str) -> ReferenceError:
    """Map an HTTP status onto a classified ReferenceError.

    401/403/404/5xx are different facts (bad key, unentitled plan, unknown
    symbol, server-side transport failure) and must not collapse into one
    message. A 404 means the symbol or route is not available from this source
    — a data-coverage fact. A 5xx or connection error is a transport failure
    — a network fact. These are distinct: "not covered" tells the reader to
    pick a different symbol or source; "transport" tells them to retry or
    check the stack.
    """
    if status == 401:
        return ReferenceError("auth", f"401 from the OpenBB API — check Basic auth ({body[:120]})")
    if status == 403:
        return ReferenceError(
            "entitlement",
            f"403 Forbidden — the provider plan does not cover this request ({body[:120]})",
        )
    if status == 404:
        return ReferenceError("not_covered", f"404 — symbol or route not available from this source ({body[:120]})")
    return ReferenceError("transport", f"HTTP {status} from the OpenBB API ({body[:120]})")


class EodhdApiAdapter:
    name = "eodhd-api"
    supported_intervals = _SUPPORTED

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self._auth = (username, password)

    def _get(self, url: str, params: dict) -> Any:
        import requests

        return requests.get(url, params=params, auth=self._auth, timeout=60)

    def fetch(self, symbol: str, start: Any, end: Any, interval: str) -> pd.DataFrame:
        from requests.exceptions import RequestException

        try:
            response = self._get(
                f"{self.base_url}{_PATH}",
                {
                    "symbol": symbol,
                    "provider": "eodhd",
                    "interval": interval,
                    "start_date": str(start),
                    "end_date": str(end),
                },
            )
        except RequestException as err:
            raise ReferenceError(
                "transport", f"{symbol} at {interval}: request to the OpenBB API failed: {err}"
            ) from err

        if response.status_code != 200:
            err = classify_status(response.status_code, response.text)
            raise ReferenceError(err.kind, f"{symbol} at {interval}: {err.detail}")

        try:
            payload = response.json()
        except RequestException as err:
            raise ReferenceError(
                "transport", f"{symbol} at {interval}: could not decode the response: {err}"
            ) from err

        rows = (payload or {}).get("results") or []
        if not rows:
            raise ReferenceError(
                "empty", f"the OpenBB API returned no rows for {symbol} at {interval}"
            )

        frame = pd.DataFrame(rows)
        index = pd.DatetimeIndex(pd.to_datetime(frame.pop("date")))
        frame.index = (
            index.tz_convert("UTC") if index.tz is not None else index.tz_localize("UTC")
        )
        missing = [c for c in BAR_COLUMNS if c not in frame.columns]
        if missing:
            raise ReferenceError(
                "transport", f"response is missing column(s): {', '.join(missing)}"
            )
        return frame[BAR_COLUMNS].sort_index()


def from_env() -> EodhdApiAdapter:
    """Build the adapter from OPENBB_URL / OPENBB_API_USERNAME / OPENBB_API_PASSWORD."""
    url = os.getenv("OPENBB_URL")
    user = os.getenv("OPENBB_API_USERNAME")
    password = os.getenv("OPENBB_API_PASSWORD")
    if not (url and user and password):
        raise ReferenceError(
            "auth",
            "set OPENBB_URL, OPENBB_API_USERNAME and OPENBB_API_PASSWORD to use "
            "--reference eodhd-api",
        )
    return EodhdApiAdapter(url, user, password)

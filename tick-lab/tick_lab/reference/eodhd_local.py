"""EODHD via an in-process OpenBB Platform.

This is the same request as `eodhd_api`, made locally instead of through this
stack's REST API. It is here for the contrast: `eodhd_api` needs nothing on
your laptop but a URL and the Basic-auth pair, because the provider key lives
on the server. This path needs `openbb` (and `openbb-eodhd`) installed here
AND the provider key present here -- on your own machine -- which is exactly
what the server-side path spares you.

The committed demo key covers MSFT and not GOOG (verified against the live
API: MSFT 1-minute intraday for 2023-05-12 returns real bars, GOOG returns
HTTP 403), so a GOOG request through this adapter is expected to surface a
classified `entitlement` error naming the 403 rather than an empty frame.
See `tick-lab/README.md`'s "Three ways to ask the same question" section for
the real output.

Only openbb_core's own declared exception boundary
(`openbb_core.app.model.abstract.error.OpenBBError`, raised by the Python
interface when a command's provider fetch fails) is caught and classified,
plus `ImportError` for the case where `openbb`/`openbb-eodhd` are not
installed at all. A bug in our own call -- a typo'd keyword argument, a wrong
attribute -- is outside that boundary and must propagate unchanged rather
than being misreported as "no interval could serve this symbol". This is the
same rule `eodhd_api` applies to `requests.exceptions.RequestException` and
`yfinance_adapter` applies to `yfinance.exceptions.YFException`; an in-process
call makes it more dangerous to get wrong, not less, because a bare
`except Exception` here would turn our own bugs into a fake "no data" result.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from tick_lab.reference.base import ReferenceError
from tick_lab.rollup import BAR_COLUMNS

_SUPPORTED = ("1m", "5m", "1h", "1d")


def classify_exception(err: Exception) -> ReferenceError:
    """Turn whatever the local OpenBB call raised into a classified error.

    Only positively-recognised shapes get a steppable kind (`empty`) or a
    specific permanent kind (`auth`, `entitlement`). Anything unrecognised
    defaults to `transport` -- non-steppable -- so `fetch_finest` cannot
    silently step past a failure this classifier has never seen before. See
    `yfinance_adapter.classify` for the same default.
    """
    message = str(err)
    lowered = message.lower()
    if "403" in message or "forbidden" in lowered:
        return ReferenceError(
            "entitlement",
            f"403 Forbidden — this API key's plan does not cover the request ({message[:160]})",
        )
    if "401" in message or "unauthorized" in lowered or "invalid api" in lowered:
        return ReferenceError("auth", f"credentials rejected ({message[:160]})")
    if "no data" in lowered or "no results" in lowered or "empty" in lowered:
        return ReferenceError("empty", message[:200])
    return ReferenceError("transport", message[:200])


class EodhdLocalAdapter:
    name = "eodhd-local"
    supported_intervals = _SUPPORTED

    def _historical(self, symbol: str, start: Any, end: Any, interval: str) -> pd.DataFrame:
        from openbb import obb

        return obb.equity.price.historical(
            symbol,
            provider="eodhd",
            interval=interval,
            start_date=str(start),
            end_date=str(end),
        ).to_df()

    def fetch(self, symbol: str, start: Any, end: Any, interval: str) -> pd.DataFrame:
        try:
            from openbb_core.app.model.abstract.error import OpenBBError
        except ImportError:
            # openbb_core absent; ImportError alone narrows the catch below.
            OpenBBError = ()

        try:
            frame = self._historical(symbol, start, end, interval)
        except ImportError as err:
            raise ReferenceError(
                "transport",
                f"{symbol} at {interval}: openbb is not installed in this environment — "
                "pip install 'openbb' 'openbb-eodhd' to use --reference eodhd-local",
            ) from err
        except OpenBBError as err:
            classified = classify_exception(err)
            raise ReferenceError(
                classified.kind, f"{symbol} at {interval}: {classified.detail}"
            ) from err

        if frame is None or frame.empty:
            raise ReferenceError("empty", f"openbb returned no rows for {symbol} at {interval}")

        missing = [c for c in BAR_COLUMNS if c not in frame.columns]
        if missing:
            raise ReferenceError(
                "transport", f"result is missing column(s): {', '.join(missing)}"
            )

        index = pd.DatetimeIndex(frame.index)
        frame.index = (
            index.tz_convert("UTC") if index.tz is not None else index.tz_localize("UTC")
        )
        return frame[BAR_COLUMNS].sort_index()

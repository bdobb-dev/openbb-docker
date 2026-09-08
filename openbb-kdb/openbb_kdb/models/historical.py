# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""OpenBB fetchers backed by the read-through cache.

One shared session per process: the q child process and its connection are
created once and reused by every fetcher.
"""

import threading
from datetime import date as dateType, datetime
from typing import Any

from openbb_core.app.model.abstract.error import OpenBBError
from openbb_core.provider.abstract.annotated_result import AnnotatedResult
from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.crypto_historical import (
    CryptoHistoricalData,
    CryptoHistoricalQueryParams,
)
from openbb_core.provider.standard_models.currency_historical import (
    CurrencyHistoricalData,
    CurrencyHistoricalQueryParams,
)
from openbb_core.provider.standard_models.equity_historical import (
    EquityHistoricalData,
    EquityHistoricalQueryParams,
)
from openbb_core.provider.standard_models.etf_historical import (
    EtfHistoricalData,
    EtfHistoricalQueryParams,
)
from openbb_core.provider.standard_models.index_historical import (
    IndexHistoricalData,
    IndexHistoricalQueryParams,
)

_SESSION = None
_CACHE = None
_LOCK = threading.Lock()


def _cache(credentials: dict | None):
    """Build (once) the shared session, store and cache.

    Guarded by a lock so concurrent first-callers cannot both pass the
    ``_CACHE is None`` check and each build a full config/session/cache; the
    loser's objects would otherwise just be orphaned (the session does not
    spawn q until its first `connection()` call, so this was waste rather
    than corruption -- the lock makes it non-wasteful too).
    """
    global _SESSION, _CACHE  # noqa: PLW0603
    from openbb_kdb.cache import ReadThroughCache
    from kdb_store.config import resolve_config
    from kdb_store.session import KdbSession
    from kdb_store.store import KdbStore

    if _CACHE is None:
        with _LOCK:
            if _CACHE is None:
                config = resolve_config(credentials)
                _SESSION = KdbSession(config)
                _CACHE = ReadThroughCache(KdbStore(_SESSION), config)
    return _CACHE


def _as_date(value) -> dateType:
    """Coerce a str/date/datetime query bound to a plain `date` for arithmetic."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, dateType):
        return value
    return datetime.fromisoformat(str(value)).date()


def _default_window(params: dict) -> dict:
    """Fill in a missing start/end bound; one year back from today when neither is given.

    A missing bound is derived from the OTHER bound, not from today: given only
    `end_date`, `start_date` defaults to one year before it; given only
    `start_date`, `end_date` defaults to today. Deriving both from "today"
    regardless of an explicit `end_date` produced an inverted range for
    end-only requests (e.g. `end_date=2015-06-01` paired with a `start_date`
    of "now minus a year"), which `ranges.subtract` treats as trivially
    satisfied -- a false cache "hit" with zero rows. With neither bound given,
    behaviour is unchanged: one year back from today, the demo chart's opening
    window.
    """
    from dateutil.relativedelta import relativedelta

    out = dict(params)
    start = out.get("start_date")
    end = out.get("end_date")
    today = datetime.now().date()

    if start is None and end is None:
        out["start_date"] = today - relativedelta(years=1)
        out["end_date"] = today
    elif start is None:
        out["start_date"] = _as_date(end) - relativedelta(years=1)
    elif end is None:
        out["end_date"] = today
    else:
        start_d, end_d = _as_date(start), _as_date(end)
        if start_d > end_d:
            raise OpenBBError(
                f"start_date ({start_d}) must not be after end_date ({end_d})."
            )
    return out


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, dateType):
        return datetime(value.year, value.month, value.day)
    return datetime.fromisoformat(str(value))


async def _extract(query, credentials: dict | None, model: str) -> dict:
    """Run the read-through cache and hand back rows plus telemetry."""
    cache = _cache(credentials)
    params = query.model_dump(exclude_none=True)
    interval = getattr(query, "interval", None) or "1d"
    rows, meta = await cache.get(
        symbol=query.symbol,
        interval=interval,
        start=_as_datetime(query.start_date),
        end=_as_datetime(query.end_date),
        model=model,
        params=params,
        credentials=credentials,
    )
    return {"rows": rows, "meta": meta}


def _annotate(data: dict, data_cls) -> AnnotatedResult:
    results = [data_cls.model_validate(r) for r in data["rows"]]
    return AnnotatedResult(result=results, metadata=data["meta"])


class KdbEquityHistoricalFetcher(
    Fetcher[EquityHistoricalQueryParams, list[EquityHistoricalData]]
):
    """Equity bars served from the kdb+ cache, filled from the upstream provider."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EquityHistoricalQueryParams:
        return EquityHistoricalQueryParams(**_default_window(params))

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:
        return await _extract(query, credentials, "EquityHistorical")

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> AnnotatedResult:
        return _annotate(data, EquityHistoricalData)


class KdbEtfHistoricalFetcher(Fetcher[EtfHistoricalQueryParams, list[EtfHistoricalData]]):
    """ETF bars served from the kdb+ cache."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EtfHistoricalQueryParams:
        return EtfHistoricalQueryParams(**_default_window(params))

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:
        return await _extract(query, credentials, "EtfHistorical")

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> AnnotatedResult:
        return _annotate(data, EtfHistoricalData)


class KdbCryptoHistoricalFetcher(
    Fetcher[CryptoHistoricalQueryParams, list[CryptoHistoricalData]]
):
    """Crypto bars served from the kdb+ cache."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> CryptoHistoricalQueryParams:
        return CryptoHistoricalQueryParams(**_default_window(params))

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:
        return await _extract(query, credentials, "CryptoHistorical")

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> AnnotatedResult:
        return _annotate(data, CryptoHistoricalData)


class KdbCurrencyHistoricalFetcher(
    Fetcher[CurrencyHistoricalQueryParams, list[CurrencyHistoricalData]]
):
    """FX bars served from the kdb+ cache."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> CurrencyHistoricalQueryParams:
        return CurrencyHistoricalQueryParams(**_default_window(params))

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:
        return await _extract(query, credentials, "CurrencyHistorical")

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> AnnotatedResult:
        return _annotate(data, CurrencyHistoricalData)


class KdbIndexHistoricalFetcher(
    Fetcher[IndexHistoricalQueryParams, list[IndexHistoricalData]]
):
    """Index bars served from the kdb+ cache."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> IndexHistoricalQueryParams:
        return IndexHistoricalQueryParams(**_default_window(params))

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:
        return await _extract(query, credentials, "IndexHistorical")

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> AnnotatedResult:
        return _annotate(data, IndexHistoricalData)

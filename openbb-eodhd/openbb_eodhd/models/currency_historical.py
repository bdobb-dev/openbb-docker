# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""EODHD Currency (Forex) Historical Price Model."""

from datetime import datetime
from typing import Any, Literal

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.currency_historical import (
    CurrencyHistoricalData,
    CurrencyHistoricalQueryParams,
)
from openbb_core.provider.utils.descriptions import QUERY_DESCRIPTIONS
from pydantic import Field

from openbb_eodhd.models._bars import INTERVAL_CHOICES, fetch_bars, rows_from_bars


def _qualify_forex(symbol: str) -> list[str]:
    """Normalize FX pairs to EODHD's `EURUSD.FOREX` form (EUR/USD, EURUSD)."""
    out = []
    for s in symbol.split(","):
        s = s.strip().upper()
        if not s:
            continue
        out.append(s if "." in s else f"{s.replace('/', '')}.FOREX")
    return out


class EODHDCurrencyHistoricalQueryParams(CurrencyHistoricalQueryParams):
    """EODHD Currency Historical Price Query."""

    # OpenBB core reads this dunder directly (registry_map / package_builder) for
    # multi-symbol + choices; pydantic's model_config json_schema_extra is a
    # different mechanism core never inspects, so it must stay this attribute.
    __json_schema_extra__ = {
        "symbol": {"multiple_items_allowed": True},
        "interval": {"choices": INTERVAL_CHOICES},
    }

    interval: Literal["1m", "5m", "1h", "1d", "1W", "1M"] = Field(
        default="1d", description=QUERY_DESCRIPTIONS.get("interval", "")
    )


class EODHDCurrencyHistoricalData(CurrencyHistoricalData):
    """EODHD Currency Historical Price Data."""

    adjusted_close: float | None = Field(
        default=None, description="Adjusted closing price (EOD endpoint only)."
    )


class EODHDCurrencyHistoricalFetcher(
    Fetcher[EODHDCurrencyHistoricalQueryParams, list[EODHDCurrencyHistoricalData]]
):
    """Extract and transform FX bars from EODHD (.FOREX symbols)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDCurrencyHistoricalQueryParams:  # pylint: disable=unused-argument
        # pylint: disable=import-outside-toplevel
        from dateutil.relativedelta import relativedelta

        transformed = dict(params)
        now = datetime.now().date()
        if transformed.get("start_date") is None:
            transformed["start_date"] = now - relativedelta(years=1)
        if transformed.get("end_date") is None:
            transformed["end_date"] = now
        return EODHDCurrencyHistoricalQueryParams(**transformed)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        return await fetch_bars(
            query.interval, _qualify_forex(query.symbol),
            query.start_date, query.end_date, credentials,
        )

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDCurrencyHistoricalData]:  # pylint: disable=unused-argument
        rows = rows_from_bars(query.interval, "," in query.symbol, data)
        return [EODHDCurrencyHistoricalData.model_validate(r) for r in rows]

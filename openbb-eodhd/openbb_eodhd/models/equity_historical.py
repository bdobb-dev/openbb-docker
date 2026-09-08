# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""EODHD Equity Historical Price Model."""

from datetime import datetime
from typing import Any, Literal

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.equity_historical import (
    EquityHistoricalData,
    EquityHistoricalQueryParams,
)
from openbb_core.provider.utils.descriptions import QUERY_DESCRIPTIONS
from pydantic import Field

from openbb_eodhd.models._bars import INTERVAL_CHOICES, fetch_bars, rows_from_bars


def _qualify_equity(symbol: str, exchange: str) -> list[str]:
    """Qualify bare symbols with an exchange code (AAPL -> AAPL.US)."""
    out = []
    for s in symbol.split(","):
        s = s.strip().upper()
        if s:
            out.append(s if "." in s else f"{s}.{exchange.upper()}")
    return out


class EODHDEquityHistoricalQueryParams(EquityHistoricalQueryParams):
    """EODHD Equity Historical Price Query.

    Source: https://eodhd.com/financial-apis/api-for-historical-data-and-volumes
    """

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
    exchange: str = Field(
        default="US",
        description="EODHD exchange code appended to bare symbols (e.g. 'US', 'LSE',"
        " 'TO'). Ignored when the symbol is already qualified, like 'AAPL.US'.",
    )


class EODHDEquityHistoricalData(EquityHistoricalData):
    """EODHD Equity Historical Price Data."""

    adjusted_close: float | None = Field(
        default=None,
        description="Split/dividend-adjusted closing price (EOD endpoint only).",
    )


class EODHDEquityHistoricalFetcher(
    Fetcher[EODHDEquityHistoricalQueryParams, list[EODHDEquityHistoricalData]]
):
    """Transform the query, extract and transform the data from EODHD endpoints."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEquityHistoricalQueryParams:  # pylint: disable=unused-argument
        """Transform the query params; default to a 1-year window."""
        # pylint: disable=import-outside-toplevel
        from dateutil.relativedelta import relativedelta

        transformed = dict(params)
        now = datetime.now().date()
        if transformed.get("start_date") is None:
            transformed["start_date"] = now - relativedelta(years=1)
        if transformed.get("end_date") is None:
            transformed["end_date"] = now
        return EODHDEquityHistoricalQueryParams(**transformed)

    @staticmethod
    async def aextract_data(
        query: EODHDEquityHistoricalQueryParams,
        credentials: dict[str, str] | None,
        **kwargs: Any,
    ) -> list[dict]:
        """Return raw bars from the EODHD eod/intraday endpoints."""
        symbols = _qualify_equity(query.symbol, query.exchange)
        return await fetch_bars(
            query.interval, symbols, query.start_date, query.end_date, credentials
        )

    @staticmethod
    def transform_data(
        query: EODHDEquityHistoricalQueryParams,
        data: list[dict],
        **kwargs: Any,
    ) -> list[EODHDEquityHistoricalData]:  # pylint: disable=unused-argument
        """Map EODHD bar fields to the standard model."""
        rows = rows_from_bars(query.interval, "," in query.symbol, data)
        return [EODHDEquityHistoricalData.model_validate(r) for r in rows]

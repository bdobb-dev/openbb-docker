# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""EODHD Crypto Historical Price Model."""

from datetime import datetime
from typing import Any, Literal

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.crypto_historical import (
    CryptoHistoricalData,
    CryptoHistoricalQueryParams,
)
from openbb_core.provider.utils.descriptions import QUERY_DESCRIPTIONS
from pydantic import Field

from openbb_eodhd.models._bars import INTERVAL_CHOICES, fetch_bars, rows_from_bars


def _qualify_crypto(symbol: str) -> list[str]:
    """Normalize crypto symbols to EODHD's `BASE-QUOTE.CC` form.

    Accepts BTC-USD, BTC/USD, BTCUSD -> BTC-USD.CC (already-dotted pass through).
    """
    out = []
    for s in symbol.split(","):
        s = s.strip().upper()
        if not s:
            continue
        if "." in s:
            out.append(s)
            continue
        s = s.replace("/", "-")
        if "-" not in s and s.endswith("USD") and len(s) > 3:
            s = f"{s[:-3]}-USD"
        out.append(f"{s}.CC")
    return out


class EODHDCryptoHistoricalQueryParams(CryptoHistoricalQueryParams):
    """EODHD Crypto Historical Price Query."""

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


class EODHDCryptoHistoricalData(CryptoHistoricalData):
    """EODHD Crypto Historical Price Data."""

    adjusted_close: float | None = Field(
        default=None, description="Adjusted closing price (EOD endpoint only)."
    )


class EODHDCryptoHistoricalFetcher(
    Fetcher[EODHDCryptoHistoricalQueryParams, list[EODHDCryptoHistoricalData]]
):
    """Extract and transform crypto bars from EODHD (.CC symbols)."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDCryptoHistoricalQueryParams:  # pylint: disable=unused-argument
        # pylint: disable=import-outside-toplevel
        from dateutil.relativedelta import relativedelta

        transformed = dict(params)
        now = datetime.now().date()
        if transformed.get("start_date") is None:
            transformed["start_date"] = now - relativedelta(years=1)
        if transformed.get("end_date") is None:
            transformed["end_date"] = now
        return EODHDCryptoHistoricalQueryParams(**transformed)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        return await fetch_bars(
            query.interval, _qualify_crypto(query.symbol),
            query.start_date, query.end_date, credentials,
        )

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDCryptoHistoricalData]:  # pylint: disable=unused-argument
        rows = rows_from_bars(query.interval, "," in query.symbol, data)
        return [EODHDCryptoHistoricalData.model_validate(r) for r in rows]

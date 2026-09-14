"""EODHD historical market capitalization (/historical-market-cap/{symbol}).

The endpoint answers weekly observations as an index-keyed dict
({"0": {"date", "value"}, …}) and an empty {} for a window with no weekly
point in it.
"""

from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.historical_market_cap import (
    HistoricalMarketCapData, HistoricalMarketCapQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError
from pydantic import Field

from openbb_eodhd.models._client import sdk_call
from openbb_eodhd.models._fundamentals import qualify


class EODHDHistoricalMarketCapQueryParams(HistoricalMarketCapQueryParams):
    """EODHD Historical Market Cap Query."""

    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDHistoricalMarketCapData(HistoricalMarketCapData):
    """EODHD Historical Market Cap Data (weekly observations)."""


class EODHDHistoricalMarketCapFetcher(
    Fetcher[EODHDHistoricalMarketCapQueryParams, list[EODHDHistoricalMarketCapData]]
):
    """EODHD historical market cap."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDHistoricalMarketCapQueryParams:
        return EODHDHistoricalMarketCapQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        sym = qualify(query.symbol, query.exchange)

        def _sync():
            resp = sdk_call(
                credentials,
                lambda c: c.get_historical_market_capitalization_data(
                    ticker=sym,
                    from_date=str(query.start_date) if query.start_date else None,
                    to_date=str(query.end_date) if query.end_date else None,
                ),
                f"market cap for '{sym}'",
            )
            rows = list(resp.values()) if isinstance(resp, dict) else list(resp or [])
            rows = [r for r in rows if isinstance(r, dict) and r.get("date") and r.get("value") is not None]
            if not rows:
                raise EmptyDataError(
                    f"EODHD returned no market-cap points for '{sym}' — observations"
                    " are weekly, so widen a very narrow window."
                )
            return rows

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDHistoricalMarketCapData]:  # pylint: disable=unused-argument
        symbol = query.symbol.upper()
        return [
            EODHDHistoricalMarketCapData.model_validate({
                "date": it["date"],
                "symbol": symbol,
                "market_cap": it["value"],
            })
            for it in sorted(data, key=lambda r: r["date"])
        ]

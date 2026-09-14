"""EODHD live/delayed quote (/real-time/{symbol}) — EquityQuote.

Multiple symbols ride one request via the endpoint's `s=` parameter. EODHD
serves OHLC, volume, previous close and change — no bid/ask depth on this
endpoint; those standard fields stay null.
"""

from datetime import datetime, timezone
from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.equity_quote import (
    EquityQuoteData, EquityQuoteQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError
from pydantic import Field

from openbb_eodhd.models._client import sdk_call
from openbb_eodhd.models._fundamentals import qualify


class EODHDEquityQuoteQueryParams(EquityQuoteQueryParams):
    """EODHD Equity Quote Query."""

    __json_schema_extra__ = {"symbol": {"multiple_items_allowed": True}}

    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDEquityQuoteData(EquityQuoteData):
    """EODHD Equity Quote Data."""


class EODHDEquityQuoteFetcher(
    Fetcher[EODHDEquityQuoteQueryParams, list[EODHDEquityQuoteData]]
):
    """EODHD real-time quote."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEquityQuoteQueryParams:
        return EODHDEquityQuoteQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        symbols = [
            qualify(s, query.exchange)
            for s in query.symbol.split(",")
            if s.strip()
        ]

        def _sync():
            resp = sdk_call(
                credentials,
                lambda c: c.get_live_stock_prices(
                    ticker=symbols[0], s=",".join(symbols[1:]) or None
                ),
                f"quote for '{query.symbol}'",
            )
            if isinstance(resp, dict):  # single-symbol answers one object
                resp = [resp]
            rows = [r for r in resp if r.get("close") not in (None, "NA")]
            if not rows:
                raise EmptyDataError(f"EODHD returned no quote for '{query.symbol}'.")
            return rows

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDEquityQuoteData]:  # pylint: disable=unused-argument
        rows = []
        for it in data:
            ts = it.get("timestamp")
            rows.append(EODHDEquityQuoteData.model_validate({
                "symbol": (it.get("code") or query.symbol).upper(),
                "last_price": it.get("close"),
                "last_timestamp": datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
                "open": it.get("open"),
                "high": it.get("high"),
                "low": it.get("low"),
                "close": it.get("close"),
                "volume": it.get("volume"),
                "prev_close": it.get("previousClose"),
                "change": it.get("change"),
                "change_percent": it.get("change_p"),
            }))
        return rows

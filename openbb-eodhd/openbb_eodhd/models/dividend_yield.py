"""EODHD trailing dividend yield — computed series.

EODHD has no trailing-yield endpoint, so this derives it the way tiingo's
provider defines the model: for each of the last `limit` trading days,
trailing 12-month dividends per share divided by that day's close. Two
dedicated calls per request (dividends + EOD closes); both are cheap
single-credit endpoints and the series is date-windowed to the limit.
"""

from datetime import date as dateType, timedelta
from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.trailing_dividend_yield import (
    TrailingDivYieldData, TrailingDivYieldQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError
from pydantic import Field

from openbb_eodhd.models._client import sdk_call
from openbb_eodhd.models._fundamentals import qualify


class EODHDTrailingDivYieldQueryParams(TrailingDivYieldQueryParams):
    """EODHD Trailing Dividend Yield Query."""

    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDTrailingDivYieldData(TrailingDivYieldData):
    """EODHD Trailing Dividend Yield Data."""


class EODHDTrailingDivYieldFetcher(
    Fetcher[EODHDTrailingDivYieldQueryParams, list[EODHDTrailingDivYieldData]]
):
    """EODHD trailing 12-month dividend yield."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDTrailingDivYieldQueryParams:
        return EODHDTrailingDivYieldQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> dict:  # pylint: disable=unused-argument
        from asyncio import to_thread
        from datetime import date as _today_src

        sym = qualify(query.symbol, query.exchange)
        limit = query.limit or 252
        today = _today_src.today()
        # ~1.5 calendar days per trading day, plus the 12-month dividend
        # lookback behind the first price date.
        price_from = today - timedelta(days=int(limit * 1.5) + 7)
        div_from = price_from - timedelta(days=370)

        def _sync():
            def _call(c):
                dividends = c.get_historical_dividends_data(
                    ticker=sym, date_from=str(div_from)
                )
                prices = c.get_eod_historical_stock_market_data(
                    symbol=sym, period="d", from_date=str(price_from),
                    to_date=str(today), order="a",
                )
                return {"dividends": dividends, "prices": prices}

            out = sdk_call(credentials, _call, f"trailing yield for '{sym}'")
            if isinstance(out["prices"], dict) or not out["prices"]:
                raise EmptyDataError(f"EODHD returned no prices for '{sym}'.")
            if isinstance(out["dividends"], dict):
                out["dividends"] = []
            return out

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: dict, **kwargs) -> list[EODHDTrailingDivYieldData]:  # pylint: disable=unused-argument
        from pandas import isna, to_datetime

        def _d(v):
            ts = to_datetime(v, errors="coerce")
            return None if isna(ts) else ts.date()

        dividends: list[tuple[dateType, float]] = []
        for it in data.get("dividends") or []:
            d, value = _d(it.get("date")), it.get("value")
            if d is not None and value is not None:
                dividends.append((d, float(value)))

        rows = []
        for bar in data["prices"]:
            d, close = _d(bar.get("date")), bar.get("close")
            if d is None or not close:
                continue
            ttm = sum(v for dd, v in dividends if d - timedelta(days=365) < dd <= d)
            rows.append(EODHDTrailingDivYieldData.model_validate({
                "date": d,
                "trailing_dividend_yield": ttm / float(close),
            }))
        if not rows:
            raise EmptyDataError(f"No usable price data for '{query.symbol}'.")
        limit = query.limit or 252
        return rows[-limit:]

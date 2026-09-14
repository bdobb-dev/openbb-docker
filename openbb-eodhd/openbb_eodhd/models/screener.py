"""EODHD stock screener (/screener).

The endpoint takes `filters=[["field","op",value],…]`, `signals`, `sort`,
`limit` (max 100) and `offset`. The standard EquityScreener model declares no
query fields of its own, so every filter here is an EODHD-specific param
mapped onto the screener's filter fields.
"""

from datetime import date as dateType
from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.equity_screener import (
    EquityScreenerData, EquityScreenerQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError, UnauthorizedError
from pydantic import Field

from openbb_eodhd.models._client import sdk_call


class EODHDEquityScreenerQueryParams(EquityScreenerQueryParams):
    """EODHD Equity Screener Query."""

    exchange: str | None = Field(default=None, description="Exchange code filter, e.g. 'us', 'NYSE'.")
    sector: str | None = Field(default=None, description="Exact sector filter, e.g. 'Technology'.")
    industry: str | None = Field(default=None, description="Exact industry filter.")
    market_cap_min: float | None = Field(default=None, description="Minimum market capitalization.")
    market_cap_max: float | None = Field(default=None, description="Maximum market capitalization.")
    price_min: float | None = Field(default=None, description="Minimum last close.")
    price_max: float | None = Field(default=None, description="Maximum last close.")
    dividend_yield_min: float | None = Field(default=None, description="Minimum dividend yield (fraction).")
    volume_min: float | None = Field(default=None, description="Minimum 1-day average volume.")
    signals: str | None = Field(
        default=None,
        description="EODHD calculated signals, comma-joined; e.g."
        " 'bookvalue_neg', '200d_new_hi', 'cross_above_50d_ma'.",
    )
    sort: str = Field(
        default="market_capitalization.desc",
        description="Sort as 'field.(asc|desc)' over a numeric screener field.",
    )
    limit: int = Field(default=50, description="Results per call (API cap 100).")
    offset: int = Field(default=0, description="Result offset (API cap 1000).")

    def to_filters(self) -> list[list]:
        pairs = [
            ("exchange", "=", self.exchange),
            ("sector", "=", self.sector),
            ("industry", "=", self.industry),
            ("market_capitalization", ">=", self.market_cap_min),
            ("market_capitalization", "<=", self.market_cap_max),
            ("adjusted_close", ">=", self.price_min),
            ("adjusted_close", "<=", self.price_max),
            ("dividend_yield", ">=", self.dividend_yield_min),
            ("avgvol_1d", ">=", self.volume_min),
        ]
        return [[f, op, v] for f, op, v in pairs if v is not None]


class EODHDEquityScreenerData(EquityScreenerData):
    """EODHD Equity Screener Data."""

    exchange: str | None = Field(default=None, description="Exchange code.")
    sector: str | None = Field(default=None, description="Sector.")
    industry: str | None = Field(default=None, description="Industry.")
    market_cap: float | None = Field(default=None, description="Market capitalization.")
    price: float | None = Field(default=None, description="Last adjusted close.")
    eps: float | None = Field(default=None, description="Earnings per share.")
    dividend_yield: float | None = Field(default=None, description="Dividend yield.")
    avg_volume_1d: float | None = Field(default=None, description="1-day average volume.")
    avg_volume_200d: float | None = Field(default=None, description="200-day average volume.")
    last_day_data_date: dateType | None = Field(default=None, description="Date of the priced data.")


class EODHDEquityScreenerFetcher(
    Fetcher[EODHDEquityScreenerQueryParams, list[EODHDEquityScreenerData]]
):
    """EODHD stock screener."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEquityScreenerQueryParams:
        return EODHDEquityScreenerQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        def _sync():
            filters = query.to_filters()
            resp = sdk_call(
                credentials,
                lambda c: c.stock_market_screener(
                    sort=query.sort,
                    filters=filters or None,
                    limit=query.limit,
                    signals=query.signals,
                    offset=query.offset,
                ),
                "screener",
            )
            if isinstance(resp, dict):
                rows = resp.get("data")
                if rows is None:
                    raise UnauthorizedError(
                        f"EODHD (screener): {resp.get('message') or resp.get('error') or resp}"
                    )
                resp = rows
            if not resp:
                raise EmptyDataError("EODHD screener matched nothing.")
            return resp

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDEquityScreenerData]:  # pylint: disable=unused-argument
        rows = []
        for it in data:
            code, exchange = it.get("code") or "", it.get("exchange") or ""
            if not code:
                continue
            rows.append(EODHDEquityScreenerData.model_validate({
                "symbol": f"{code}.{exchange}".strip(".").upper(),
                "name": it.get("name"),
                "exchange": exchange or None,
                "sector": it.get("sector"),
                "industry": it.get("industry"),
                "market_cap": it.get("market_capitalization"),
                "price": it.get("adjusted_close"),
                "eps": it.get("earnings_share"),
                "dividend_yield": it.get("dividend_yield"),
                "avg_volume_1d": it.get("avgvol_1d"),
                "avg_volume_200d": it.get("avgvol_200d"),
                "last_day_data_date": it.get("last_day_data_date") or None,
            }))
        return rows

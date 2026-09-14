"""EODHD news (/news) — WorldNews (general feed or topic tag) and
CompanyNews (symbol-scoped via `s=`), one endpoint behind both.
"""

from typing import Any

from openbb_core.app.model.abstract.error import OpenBBError
from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.company_news import (
    CompanyNewsData, CompanyNewsQueryParams,
)
from openbb_core.provider.standard_models.world_news import (
    WorldNewsData, WorldNewsQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError, UnauthorizedError
from pydantic import Field

from openbb_eodhd.models._client import rest_json, sdk_call
from openbb_eodhd.models._fundamentals import qualify


def _fetch_news(credentials, params: dict, context: str) -> list[dict]:
    """One /news call via the raw endpoint (the SDK wrapper refuses the
    general feed the API itself serves)."""
    resp = sdk_call(credentials, lambda c: rest_json(c, "news", params), context)
    if isinstance(resp, dict):
        raise UnauthorizedError(
            f"EODHD ({context}): {resp.get('message') or resp.get('error') or resp}"
        )
    if not resp:
        raise EmptyDataError(f"EODHD returned no {context}.")
    return resp


class EODHDWorldNewsQueryParams(WorldNewsQueryParams):
    """EODHD World News Query."""

    topic: str | None = Field(
        default=None,
        description="EODHD topic tag, e.g. 'technology', 'mergers and acquisitions'."
        " Omitted, the general financial feed is served.",
    )


class EODHDWorldNewsData(WorldNewsData):
    """EODHD World News Data."""

    symbols: list[str] | None = Field(default=None, description="Tickers the article mentions.")
    tags: list[str] | None = Field(default=None, description="EODHD topic tags on the article.")
    sentiment: dict | None = Field(default=None, description="EODHD polarity/pos/neu/neg scores.")


class EODHDWorldNewsFetcher(
    Fetcher[EODHDWorldNewsQueryParams, list[EODHDWorldNewsData]]
):
    """EODHD world news."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDWorldNewsQueryParams:
        return EODHDWorldNewsQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        return await to_thread(
            _fetch_news,
            credentials,
            {
                "t": query.topic,
                "from": str(query.start_date) if query.start_date else None,
                "to": str(query.end_date) if query.end_date else None,
                "limit": query.limit,
            },
            "world news",
        )

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDWorldNewsData]:  # pylint: disable=unused-argument
        rows = []
        for it in data:
            if not it.get("date") or not it.get("title"):
                continue
            content = it.get("content") or ""
            rows.append(EODHDWorldNewsData.model_validate({
                "date": it["date"],
                "title": it["title"],
                "body": content or None,
                "excerpt": (content[:280] or None) if content else None,
                "url": it.get("link"),
                "symbols": it.get("symbols") or None,
                "tags": it.get("tags") or None,
                "sentiment": it.get("sentiment") or None,
            }))
        return rows


class EODHDCompanyNewsQueryParams(CompanyNewsQueryParams):
    """EODHD Company News Query."""

    __json_schema_extra__ = {"symbol": {"multiple_items_allowed": True}}

    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDCompanyNewsData(CompanyNewsData):
    """EODHD Company News Data."""

    tags: list[str] | None = Field(default=None, description="EODHD topic tags on the article.")
    sentiment: dict | None = Field(default=None, description="EODHD polarity/pos/neu/neg scores.")


class EODHDCompanyNewsFetcher(
    Fetcher[EODHDCompanyNewsQueryParams, list[EODHDCompanyNewsData]]
):
    """EODHD symbol-scoped news."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDCompanyNewsQueryParams:
        return EODHDCompanyNewsQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        if not query.symbol:
            raise OpenBBError("EODHD company news requires a symbol.")
        symbols = ",".join(
            qualify(s, query.exchange) for s in query.symbol.split(",") if s.strip()
        )
        return await to_thread(
            _fetch_news,
            credentials,
            {
                "s": symbols,
                "from": str(query.start_date) if query.start_date else None,
                "to": str(query.end_date) if query.end_date else None,
                "limit": query.limit,
            },
            f"news for '{query.symbol}'",
        )

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDCompanyNewsData]:  # pylint: disable=unused-argument
        rows = []
        for it in data:
            # date, title and url are all required by the standard model
            if not it.get("date") or not it.get("title") or not it.get("link"):
                continue
            content = it.get("content") or ""
            rows.append(EODHDCompanyNewsData.model_validate({
                "date": it["date"],
                "title": it["title"],
                "body": content or None,
                "excerpt": (content[:280] or None) if content else None,
                "url": it["link"],
                "symbols": ",".join(it.get("symbols") or []) or None,
                "tags": it.get("tags") or None,
                "sentiment": it.get("sentiment") or None,
            }))
        return rows

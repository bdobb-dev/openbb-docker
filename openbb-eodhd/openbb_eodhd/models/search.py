"""EODHD instrument search (/search/{query}) — equity, ETF and crypto.

One endpoint, three standard models, filtered by the endpoint's `type`
parameter. Rows come back with a bare Code + Exchange; symbols are emitted
qualified (CODE.EXCHANGE) so a result can be fed straight into any other
EODHD fetcher.
"""

from datetime import date as dateType
from typing import Any

from openbb_core.app.model.abstract.error import OpenBBError
from openbb_core.provider.abstract.data import Data
from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.crypto_search import (
    CryptoSearchData, CryptoSearchQueryParams,
)
from openbb_core.provider.standard_models.equity_search import (
    EquitySearchData, EquitySearchQueryParams,
)
from openbb_core.provider.standard_models.etf_search import (
    EtfSearchData, EtfSearchQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError
from pydantic import Field

from openbb_eodhd.models._client import sdk_call


async def _search(query: str | None, type_: str, limit: int, credentials) -> list[dict]:
    from asyncio import to_thread

    if not (query or "").strip():
        raise OpenBBError("EODHD search requires a non-empty query.")

    def _sync():
        resp = sdk_call(
            credentials,
            lambda c: c.search(query.strip(), limit=limit, type=type_),
            f"search for '{query}'",
        )
        if not resp:
            raise EmptyDataError(f"EODHD search found nothing for '{query}'.")
        return resp

    return await to_thread(_sync)


def _row(it: dict) -> dict:
    code, exchange = it.get("Code") or "", it.get("Exchange") or ""
    return {
        "symbol": f"{code}.{exchange}".strip(".").upper(),
        "name": it.get("Name"),
        "exchange": exchange or None,
        "security_type": it.get("Type"),
        "country": it.get("Country"),
        "currency": it.get("Currency"),
        "isin": it.get("ISIN"),
        "is_primary": it.get("isPrimary"),
        "previous_close": it.get("previousClose"),
        "previous_close_date": it.get("previousCloseDate") or None,
    }


class _SearchExtras(Data):
    """EODHD fields shared by every search result row."""

    exchange: str | None = Field(default=None, description="EODHD exchange code.")
    security_type: str | None = Field(default=None, description="Instrument type as EODHD classifies it.")
    country: str | None = Field(default=None, description="Country of the listing.")
    currency: str | None = Field(default=None, description="Trading currency.")
    isin: str | None = Field(default=None, description="ISIN, when known.")
    is_primary: bool | None = Field(default=None, description="True for the primary listing.")
    previous_close: float | None = Field(default=None, description="Previous closing price.")
    previous_close_date: dateType | None = Field(default=None, description="Date of the previous close.")


class EODHDEquitySearchQueryParams(EquitySearchQueryParams):
    """EODHD Equity Search Query."""
    limit: int = Field(default=25, description="Maximum results (API cap 500).")


class EODHDEquitySearchData(EquitySearchData, _SearchExtras):
    """EODHD Equity Search Data."""


class EODHDEquitySearchFetcher(
    Fetcher[EODHDEquitySearchQueryParams, list[EODHDEquitySearchData]]
):
    """EODHD equity search."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEquitySearchQueryParams:
        return EODHDEquitySearchQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        return await _search(query.query, "stock", query.limit, credentials)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDEquitySearchData]:  # pylint: disable=unused-argument
        return [EODHDEquitySearchData.model_validate(_row(it)) for it in data]


class EODHDEtfSearchQueryParams(EtfSearchQueryParams):
    """EODHD ETF Search Query."""
    limit: int = Field(default=25, description="Maximum results (API cap 500).")


class EODHDEtfSearchData(EtfSearchData, _SearchExtras):
    """EODHD ETF Search Data."""


class EODHDEtfSearchFetcher(
    Fetcher[EODHDEtfSearchQueryParams, list[EODHDEtfSearchData]]
):
    """EODHD ETF search."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDEtfSearchQueryParams:
        return EODHDEtfSearchQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        return await _search(query.query, "etf", query.limit, credentials)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDEtfSearchData]:  # pylint: disable=unused-argument
        return [EODHDEtfSearchData.model_validate(_row(it)) for it in data]


class EODHDCryptoSearchQueryParams(CryptoSearchQueryParams):
    """EODHD Crypto Search Query."""
    limit: int = Field(default=25, description="Maximum results (API cap 500).")


class EODHDCryptoSearchData(CryptoSearchData, _SearchExtras):
    """EODHD Crypto Search Data."""


class EODHDCryptoSearchFetcher(
    Fetcher[EODHDCryptoSearchQueryParams, list[EODHDCryptoSearchData]]
):
    """EODHD crypto search."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDCryptoSearchQueryParams:
        return EODHDCryptoSearchQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        return await _search(query.query, "crypto", query.limit, credentials)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDCryptoSearchData]:  # pylint: disable=unused-argument
        return [EODHDCryptoSearchData.model_validate(_row(it)) for it in data]

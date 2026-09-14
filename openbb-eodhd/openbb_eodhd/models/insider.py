"""EODHD insider transactions (/api/insider-transactions)."""

from typing import Any

from openbb_core.app.model.abstract.error import OpenBBError
from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.insider_trading import (
    InsiderTradingData, InsiderTradingQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError, UnauthorizedError
from pydantic import Field

from openbb_eodhd.models._client import get_client, raise_sdk_error
from openbb_eodhd.models._fundamentals import qualify


def _date(v):
    from pandas import isna, to_datetime
    if not v:
        return None
    ts = to_datetime(v, errors="coerce")
    return None if isna(ts) else ts.date()


class EODHDInsiderTradingQueryParams(InsiderTradingQueryParams):
    """EODHD Insider Trading Query."""
    exchange: str = Field(default="US", description="EODHD exchange code for bare symbols.")


class EODHDInsiderTradingData(InsiderTradingData):
    """EODHD Insider Trading Data."""
    post_transaction_amount: int | None = Field(default=None, description="Shares held after the transaction.")


class EODHDInsiderTradingFetcher(
    Fetcher[EODHDInsiderTradingQueryParams, list[EODHDInsiderTradingData]]
):
    """EODHD insider transactions."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDInsiderTradingQueryParams:
        return EODHDInsiderTradingQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        def _sync():
            client = get_client(credentials)
            sym = qualify(query.symbol, query.exchange)
            try:
                with client:
                    resp = client.get_insider_transactions_data(
                        code=sym, limit=query.limit or 100
                    )
            except (OpenBBError, UnauthorizedError):
                raise
            except Exception as exc:  # noqa: BLE001
                raise_sdk_error(exc, f"insider for '{sym}'")
            if isinstance(resp, dict):
                raise UnauthorizedError(f"EODHD ({sym}): {resp.get('message') or resp}")
            if not resp:
                raise EmptyDataError(f"EODHD returned no insider data for '{sym}'.")
            return resp

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDInsiderTradingData]:  # pylint: disable=unused-argument
        rows = []
        for it in data:
            rows.append(EODHDInsiderTradingData.model_validate({
                "symbol": (it.get("code") or query.symbol).upper(),
                "owner_name": it.get("ownerName"),
                "owner_title": it.get("ownerRelationship"),
                "transaction_date": _date(it.get("transactionDate")),
                "filing_date": _date(it.get("date")),
                "transaction_type": it.get("transactionCode"),
                "securities_transacted": it.get("transactionAmount"),
                "transaction_price": it.get("transactionPrice"),
                "acquisition_or_disposition": it.get("transactionAcquiredDisposed"),
                "post_transaction_amount": it.get("postTransactionAmount"),
                "filing_url": it.get("secLink"),
            }))
        return rows

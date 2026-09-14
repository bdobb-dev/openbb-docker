"""EODHD congressional trades (/congressional-trades).

The SDK has no wrapper for this endpoint yet, so the request rides
`_client.rest_json` (the SDK's own BaseAPI machinery — parsed-JSON returns,
typed errors — under the pinned commit).

Response is JSON:API-shaped: {"data": [...], "meta", "links"}. One page is
fetched (page[limit] = query.limit); disclosure rows can carry garbage
transaction dates (e.g. year 3031) straight from the filings — those parse to
None rather than dropping the row.
"""

from datetime import date as dateType
from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.government_trades import (
    GovernmentTradesData, GovernmentTradesQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError, UnauthorizedError
from pydantic import Field

from openbb_eodhd.models._client import rest_json, sdk_call


def _date(v):
    from pandas import isna, to_datetime
    if not v:
        return None
    ts = to_datetime(v, errors="coerce")
    return None if isna(ts) else ts.date()


def _get_trades(client, query) -> Any:
    return rest_json(client, "congressional-trades", {
        "symbol": query.symbol.upper().split(".")[0] if query.symbol else None,
        "chamber": query.chamber if query.chamber != "all" else None,
        "page[limit]": query.limit or 100,
    })


class EODHDGovernmentTradesQueryParams(GovernmentTradesQueryParams):
    """EODHD Government Trades Query."""


class EODHDGovernmentTradesData(GovernmentTradesData):
    """EODHD Government Trades Data."""

    chamber: str | None = Field(default=None, description="house or senate.")
    transaction_type: str | None = Field(default=None, description="purchase, sale, or exchange.")
    owner: str | None = Field(default=None, description="Self, Spouse, Joint, or Dependent.")
    asset_description: str | None = Field(default=None, description="Asset as described in the filing.")
    asset_type: str | None = Field(default=None, description="Asset class of the trade.")
    amount_range: str | None = Field(default=None, description="Disclosed transaction amount range.")
    amount_low: float | None = Field(default=None, description="Low bound parsed from the range.")
    amount_high: float | None = Field(default=None, description="High bound parsed from the range.")
    party: str | None = Field(default=None, description="Member's party, when known.")
    state: str | None = Field(default=None, description="Member's state.")
    district: int | None = Field(default=None, description="House district, when applicable.")
    days_to_disclose: int | None = Field(default=None, description="Days between trade and disclosure.")
    is_late: bool | None = Field(default=None, description="True when the filing missed the 45-day window.")
    filing_url: str | None = Field(default=None, description="Link to the original filing.")


class EODHDGovernmentTradesFetcher(
    Fetcher[EODHDGovernmentTradesQueryParams, list[EODHDGovernmentTradesData]]
):
    """EODHD congressional trades."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDGovernmentTradesQueryParams:
        return EODHDGovernmentTradesQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        def _sync():
            resp = sdk_call(
                credentials,
                lambda c: _get_trades(c, query),
                "congressional trades",
            )
            rows = resp.get("data") if isinstance(resp, dict) else resp
            if rows is None and isinstance(resp, dict):
                raise UnauthorizedError(
                    f"EODHD (congressional trades): {resp.get('message') or resp.get('errors') or resp}"
                )
            if not rows:
                raise EmptyDataError("EODHD returned no congressional trades.")
            return rows

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDGovernmentTradesData]:  # pylint: disable=unused-argument
        rows = []
        for it in data:
            member = it.get("member") or {}
            asset = it.get("asset") or {}
            txn = it.get("transaction") or {}
            source = it.get("source") or {}
            disclosed = _date(txn.get("disclosure_date"))
            if disclosed is None:  # `date` is required by the standard model
                continue
            rows.append(EODHDGovernmentTradesData.model_validate({
                "symbol": (asset.get("symbol") or None),
                "date": disclosed,
                "transaction_date": _date(txn.get("transaction_date")),
                "representative": member.get("full_name"),
                "chamber": it.get("chamber"),
                "transaction_type": txn.get("type"),
                "owner": txn.get("owner"),
                "asset_description": asset.get("description"),
                "asset_type": asset.get("asset_type"),
                "amount_range": txn.get("amount_range"),
                "amount_low": txn.get("amount_low"),
                "amount_high": txn.get("amount_high"),
                "party": member.get("party"),
                "state": member.get("state"),
                "district": member.get("district"),
                "days_to_disclose": txn.get("days_to_disclose"),
                "is_late": txn.get("is_late"),
                "filing_url": source.get("filing_url"),
            }))
        return rows

"""EODHD index reference and history.

AvailableIndices lists the INDX pseudo-exchange
(/exchange-symbol-list/INDX); IndexHistorical reuses the shared bars
machinery against `CODE.INDX` symbols (e.g. `GSPC.INDX`).
"""

from typing import Any, Literal

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.available_indices import (
    AvailableIndicesData, AvailableIndicesQueryParams,
)
from openbb_core.provider.standard_models.index_historical import (
    IndexHistoricalData, IndexHistoricalQueryParams,
)
from openbb_core.provider.utils.descriptions import QUERY_DESCRIPTIONS
from openbb_core.provider.utils.errors import EmptyDataError
from pydantic import Field

from openbb_eodhd.models._bars import fetch_bars, rows_from_bars
from openbb_eodhd.models._client import sdk_call


class EODHDAvailableIndicesQueryParams(AvailableIndicesQueryParams):
    """EODHD Available Indices Query."""

    query: str | None = Field(
        default=None, description="Filter results by symbol or name substring."
    )


class EODHDAvailableIndicesData(AvailableIndicesData):
    """EODHD Available Indices Data."""

    country: str | None = Field(default=None, description="Country of the index.")


class EODHDAvailableIndicesFetcher(
    Fetcher[EODHDAvailableIndicesQueryParams, list[EODHDAvailableIndicesData]]
):
    """EODHD index list."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDAvailableIndicesQueryParams:
        return EODHDAvailableIndicesQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        def _sync():
            resp = sdk_call(
                credentials,
                lambda c: c.get_exchange_symbols("INDX"),
                "index list",
            )
            if resp is None or len(resp) == 0:
                raise EmptyDataError("EODHD returned no indices.")
            return resp.to_dict("records") if hasattr(resp, "to_dict") else resp

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDAvailableIndicesData]:  # pylint: disable=unused-argument
        needle = (query.query or "").upper()
        rows = []
        for it in data:
            code, name = it.get("Code") or "", it.get("Name") or ""
            if not code:
                continue
            if needle and needle not in code.upper() and needle not in name.upper():
                continue
            rows.append(EODHDAvailableIndicesData.model_validate({
                # Qualified so a row feeds straight into index history/quotes.
                "symbol": f"{code.upper()}.INDX",
                "name": name or None,
                "currency": it.get("Currency") or None,
                "country": it.get("Country") if it.get("Country") not in (None, "Unknown") else None,
            }))
        return rows


class EODHDIndexHistoricalQueryParams(IndexHistoricalQueryParams):
    """EODHD Index Historical Query."""

    __json_schema_extra__ = {
        "symbol": {"multiple_items_allowed": True},
        "interval": {"choices": ["1d", "1W", "1M"]},
    }

    # Index bars are EOD-only here; the intraday endpoint is patchy for INDX.
    interval: Literal["1d", "1W", "1M"] = Field(
        default="1d", description=QUERY_DESCRIPTIONS.get("interval", "")
    )


class EODHDIndexHistoricalData(IndexHistoricalData):
    """EODHD Index Historical Data."""

    adjusted_close: float | None = Field(
        default=None, description="Adjusted closing value."
    )


def _qualify_index(symbols: str) -> list[str]:
    out = []
    for raw in symbols.split(","):
        sym = raw.strip().upper().lstrip("^")
        if not sym:
            continue
        out.append(sym if "." in sym else f"{sym}.INDX")
    return out


class EODHDIndexHistoricalFetcher(
    Fetcher[EODHDIndexHistoricalQueryParams, list[EODHDIndexHistoricalData]]
):
    """EODHD index history over the shared bars path."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDIndexHistoricalQueryParams:
        return EODHDIndexHistoricalQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        return await fetch_bars(
            query.interval,
            _qualify_index(query.symbol),
            query.start_date,
            query.end_date,
            credentials,
        )

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDIndexHistoricalData]:  # pylint: disable=unused-argument
        rows = rows_from_bars(query.interval, "," in query.symbol, data)
        return [EODHDIndexHistoricalData.model_validate(r) for r in rows]

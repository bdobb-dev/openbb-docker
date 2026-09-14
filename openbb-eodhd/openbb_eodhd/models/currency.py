"""EODHD forex reference data and snapshots.

CurrencyPairs lists the FOREX pseudo-exchange's symbols
(/exchange-symbol-list/FOREX); CurrencySnapshots quotes a base currency
against a set of counters in one /real-time call.
"""

from datetime import datetime
from typing import Any, Literal

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.currency_pairs import (
    CurrencyPairsData, CurrencyPairsQueryParams,
)
from openbb_core.provider.standard_models.currency_snapshots import (
    CurrencySnapshotsData, CurrencySnapshotsQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError
from pydantic import Field

from openbb_eodhd.models._client import sdk_call

# The default counter set for a snapshot with no counter_currencies given.
_DEFAULT_COUNTERS = ["EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "MXN", "INR"]


class EODHDCurrencyPairsQueryParams(CurrencyPairsQueryParams):
    """EODHD Currency Pairs Query."""


class EODHDCurrencyPairsData(CurrencyPairsData):
    """EODHD Currency Pairs Data."""

    currency: str | None = Field(default=None, description="Quote currency, when EODHD reports one.")


class EODHDCurrencyPairsFetcher(
    Fetcher[EODHDCurrencyPairsQueryParams, list[EODHDCurrencyPairsData]]
):
    """EODHD forex pair list."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDCurrencyPairsQueryParams:
        return EODHDCurrencyPairsQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        def _sync():
            resp = sdk_call(
                credentials,
                lambda c: c.get_exchange_symbols("FOREX"),
                "forex pair list",
            )
            if resp is None or len(resp) == 0:
                raise EmptyDataError("EODHD returned no forex pairs.")
            # get_exchange_symbols answers a DataFrame; normalize to records.
            return resp.to_dict("records") if hasattr(resp, "to_dict") else resp

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDCurrencyPairsData]:  # pylint: disable=unused-argument
        needle = (query.query or "").upper()
        rows = []
        for it in data:
            code, name = it.get("Code") or "", it.get("Name") or ""
            if not code:
                continue
            if needle and needle not in code.upper() and needle not in name.upper():
                continue
            currency = it.get("Currency")
            rows.append(EODHDCurrencyPairsData.model_validate({
                "symbol": code.upper(),
                "name": name or None,
                "currency": None if currency in (None, "NA", "Unknown") else currency,
            }))
        return rows


class EODHDCurrencySnapshotsQueryParams(CurrencySnapshotsQueryParams):
    """EODHD Currency Snapshots Query.

    The standard model's own validator canonicalizes `counter_currencies`
    to one upper-cased comma-joined string; `counters()` is the list view.
    """

    def counters(self) -> list[str]:
        raw = self.counter_currencies
        if not raw:
            return list(_DEFAULT_COUNTERS)
        items = raw.split(",") if isinstance(raw, str) else raw
        return [c.strip().upper() for c in items if c.strip()]


class EODHDCurrencySnapshotsData(CurrencySnapshotsData):
    """EODHD Currency Snapshots Data."""

    change: float | None = Field(default=None, description="Change from the previous close.")
    change_percent: float | None = Field(default=None, description="Percent change from the previous close.")
    last_rate_time: datetime | None = Field(default=None, description="Timestamp of the last rate (UTC).")


def _pair(base: str, counter: str, quote_type: str) -> str:
    """EODHD pair symbol for one base/counter under the requested quote type."""
    return f"{counter}{base}" if quote_type == "direct" else f"{base}{counter}"


class EODHDCurrencySnapshotsFetcher(
    Fetcher[EODHDCurrencySnapshotsQueryParams, list[EODHDCurrencySnapshotsData]]
):
    """EODHD forex snapshots via /real-time."""

    @staticmethod
    def transform_query(params: dict[str, Any]) -> EODHDCurrencySnapshotsQueryParams:
        return EODHDCurrencySnapshotsQueryParams(**params)

    @staticmethod
    async def aextract_data(query, credentials, **kwargs) -> list[dict]:  # pylint: disable=unused-argument
        from asyncio import to_thread

        base = query.base.upper()
        counters = [c for c in query.counters() if c != base]
        symbols = [f"{_pair(base, c, query.quote_type)}.FOREX" for c in counters]

        def _sync():
            resp = sdk_call(
                credentials,
                lambda c: c.get_live_stock_prices(
                    ticker=symbols[0], s=",".join(symbols[1:]) or None
                ),
                f"forex snapshot for {base}",
            )
            if isinstance(resp, dict):  # a single-symbol request answers one object
                resp = [resp]
            rows = [r for r in resp if r.get("close") not in (None, "NA")]
            if not rows:
                raise EmptyDataError(f"EODHD returned no forex snapshot for {base}.")
            return rows

        return await to_thread(_sync)

    @staticmethod
    def transform_data(query, data: list[dict], **kwargs) -> list[EODHDCurrencySnapshotsData]:  # pylint: disable=unused-argument
        from datetime import timezone

        base = query.base.upper()
        rows = []
        for it in data:
            pair = (it.get("code") or "").split(".")[0]
            # direct built counter+base, indirect built base+counter
            if query.quote_type == "direct" and pair.endswith(base):
                counter = pair[: -len(base)]
            elif query.quote_type == "indirect" and pair.startswith(base):
                counter = pair[len(base):]
            else:
                counter = pair
            ts = it.get("timestamp")
            rows.append(EODHDCurrencySnapshotsData.model_validate({
                "base_currency": base,
                "counter_currency": counter or pair,
                "last_rate": it.get("close"),
                "open": it.get("open"),
                "high": it.get("high"),
                "low": it.get("low"),
                "close": it.get("close"),
                "prev_close": it.get("previousClose"),
                "change": it.get("change"),
                "change_percent": it.get("change_p"),
                "last_rate_time": datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
            }))
        return rows

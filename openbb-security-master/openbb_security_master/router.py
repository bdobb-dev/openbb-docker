# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The /reference router: three commands, all delegating to security-master-api or openbb-eodhd.

The router's own prefix is empty on purpose: OpenBB's RouterLoader includes a
core extension under `/{entry-point name}`, and this package's entry point is
named `reference`, so the commands land on `/api/v1/reference/...` and on
`obb.reference.*`. A prefix here would nest a second segment under that.
"""

from __future__ import annotations

from datetime import date as dateType
from datetime import datetime, timezone
from typing import Literal

from openbb_core.app.model.abstract.error import OpenBBError
from openbb_core.app.model.obbject import OBBject
from openbb_core.app.router import Router

from openbb_security_master.client import SecurityMasterClient
from openbb_security_master.models import (
    ExchangeDetailsData,
    MarketCalendarData,
    MarketCalendarQueryParams,
    ResolveData,
)

router = Router(prefix="", description="Security-master reference data")


def _eodhd_client() -> dict:
    """The EODHD credential mapping `sdk_call` builds its client from.

    The provider extension owns key handling, the SDK pin and the error
    mapping; this router only borrows the one endpoint OpenBB has no command
    for. Imported lazily so the extension imports without openbb-eodhd present
    (the image installs it, CI does not).

    `model_dump(mode="json")` is required, not the bare python-mode dump:
    `UserService.read_from_file()` stores credentials as pydantic `SecretStr`,
    and a python-mode `model_dump()` leaves those fields as `SecretStr`
    objects (whose `str()` is the masked `'**********'`) instead of unwrapping
    them to the real value.
    """
    from openbb_core.app.service.user_service import UserService

    creds = UserService.read_from_file().credentials.model_dump(mode="json")
    return {"eodhd_api_key": creds.get("eodhd_api_key")}


def _eodhd_rest_json(credentials: dict, endpoint: str, params: dict):
    from openbb_eodhd.models._client import rest_json, sdk_call

    return sdk_call(credentials, lambda client: rest_json(client, endpoint, params), endpoint)


def _knowledge_at_iso(knowledge_at: datetime | None) -> str | None:
    """UTC ISO-8601 with a `Z` suffix; a naive value is treated as UTC.

    The service rejects a `known_at` sent without an offset, and a bare
    `datetime` (no explicit `Z`/`+00:00` in the query string) parses naive.
    """
    if knowledge_at is None:
        return None
    if knowledge_at.tzinfo is None:
        knowledge_at = knowledge_at.replace(tzinfo=timezone.utc)
    return knowledge_at.isoformat().replace("+00:00", "Z")


@router.command(methods=["GET"])
def market_calendar(
    start_date: dateType,
    end_date: dateType,
    calendar_id: str | None = None,
    exchange: str | None = None,
    mic: str | None = None,
    include_closed: bool = False,
    include_breaks: bool = False,
    include_interruptions: bool = False,
    session_label: Literal["session_date", "trade_date"] = "session_date",
    timezone: str | None = None,
    as_of: dateType | None = None,
    knowledge_at: datetime | None = None,
    provider: str = "local_security_master",
) -> OBBject[list[MarketCalendarData]]:
    """Exchange sessions with holiday, authority and market-effect evidence under a temporal context."""
    params = MarketCalendarQueryParams(**locals())
    client = SecurityMasterClient.from_env()
    context = client.context_for(
        params.as_of.isoformat() if params.as_of else None,
        _knowledge_at_iso(params.knowledge_at))
    if context["mode"] != "current_corrected" and "effective_at" not in context:
        context["effective_at"] = f"{params.start_date.isoformat()}T00:00:00Z"
    body = params.model_dump(exclude={"as_of", "knowledge_at", "provider"}, exclude_none=True)
    for key in ("start_date", "end_date"):
        body[key] = body[key].isoformat()
    out = client.odp_query("MarketCalendar", body, context)
    results = out.get("results")
    if not isinstance(results, list):
        raise OpenBBError("security-master-api answered without results")
    return OBBject(results=[MarketCalendarData(**row) for row in results],
                   extra=out.get("extra", {}))


@router.command(methods=["GET"])
def exchange_details(code: str) -> OBBject[ExchangeDetailsData]:
    """EODHD exchange details (trading hours, holidays) as maintained calendar input."""
    raw = _eodhd_rest_json(_eodhd_client(), f"exchange-details/{code}", {})
    raw = raw if isinstance(raw, dict) else {}
    holidays = list((raw.get("ExchangeHolidays") or {}).values())
    return OBBject(results=ExchangeDetailsData(code=code, timezone=raw.get("Timezone"),
                                               holidays=holidays, raw=raw))


@router.command(methods=["GET"])
def security_master_resolve(
    identifier: str,
    identifier_type: str | None = None,
    as_of: dateType | None = None,
    knowledge_at: datetime | None = None,
) -> OBBject[list[ResolveData]]:
    """Resolve a ticker, CUSIP, ISIN, FIGI or internal id under an explicit temporal context."""
    client = SecurityMasterClient.from_env()
    context = client.context_for(as_of.isoformat() if as_of else None, _knowledge_at_iso(knowledge_at))
    out = client.resolve(identifier, context, identifier_type)
    candidates = out.get("candidates")
    if not isinstance(candidates, list):
        raise OpenBBError("security-master-api answered without results")
    fields = ("listing_id", "instrument_id", "security_id", "symbol", "reason")
    return OBBject(results=[ResolveData(**{k: c.get(k) for k in fields}) for c in candidates],
                   extra={"security_master_receipt": out.get("receipt", {})})

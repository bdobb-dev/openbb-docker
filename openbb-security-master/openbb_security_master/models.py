# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Query and data models, typed from the ODP registry this package ships a copy of.

The field set and the types come from `odp_registry.json`'s MarketCalendar
model -- the same registry the service projects from, which is why
`tests/test_registry_parity.py` asserts the two copies are byte-equal. A field
added there and not here would silently ride along in `extra` instead of being
typed, so the copy is the contract, not a convenience.
"""

from __future__ import annotations

from datetime import date as dateType
from datetime import datetime
from typing import Literal

from openbb_core.provider.abstract.data import Data
from openbb_core.provider.abstract.query_params import QueryParams


class MarketCalendarQueryParams(QueryParams):
    """The thirteen MarketCalendar parameters the ODP registry declares."""

    calendar_id: str | None = None
    exchange: str | None = None
    mic: str | None = None
    start_date: dateType
    end_date: dateType
    include_closed: bool = False
    include_breaks: bool = False
    include_interruptions: bool = False
    session_label: Literal["calendar_date", "trade_date"] = "calendar_date"
    timezone: str | None = None
    # as_of/knowledge_at are the temporal context, not filters: the router
    # turns them into the request's `context` block and excludes them from
    # `parameters` (the service would reject them there).
    as_of: dateType | None = None
    knowledge_at: datetime | None = None
    provider: str = "local_security_master"


class MarketCalendarData(Data):
    """One projected session row: the thirty-four registry result fields."""

    calendar_id: str | None = None
    exchange_id: str | None = None
    exchange_name: str | None = None
    mic: str | None = None
    calendar_alias: str | None = None
    session_date: dateType
    trade_date: dateType | None = None
    timezone: str | None = None
    session_status: str | None = None
    calendar_system: str | None = None
    holiday_family: str | None = None
    assertion_domain: str | None = None
    evidence_status: str | None = None
    market_open: datetime | None = None
    market_close: datetime | None = None
    break_start: datetime | None = None
    break_end: datetime | None = None
    interruptions: list[dict] = []
    holiday_name: str | None = None
    special_open: bool | None = None
    special_close: bool | None = None
    market_effect: str | None = None
    authority_type: str | None = None
    authority_name: str | None = None
    authority_verified_at: datetime | None = None
    source_kind: str | None = None
    source_version: str | None = None
    rule_id: str | None = None
    supersedes_assertion_id: str | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    known_from: datetime | None = None
    known_to: datetime | None = None
    capture_id: str | None = None


class ExchangeDetailsData(Data):
    """EODHD exchange details, kept whole in `raw` alongside the two fields used."""

    code: str
    timezone: str | None = None
    holidays: list[dict] = []
    raw: dict = {}


class ResolveData(Data):
    """One identity candidate from the service's resolver."""

    listing_id: str | None = None
    instrument_id: str | None = None
    security_id: str | None = None
    symbol: str | None = None
    reason: str | None = None

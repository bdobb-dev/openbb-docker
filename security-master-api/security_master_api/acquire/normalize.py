# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""OpenBB responses become Silver assertion rows. Nothing here claims more than the source did."""

from __future__ import annotations

from datetime import UTC, date, datetime


def _interval(capture_id: str, observed_at: datetime, assertion_id: str,
              effective_from: datetime) -> dict:
    return {"effective_from": effective_from, "effective_to": None, "observed_at": observed_at,
            "available_at": observed_at, "system_from": observed_at, "system_to": None,
            "capture_id": capture_id, "assertion_id": assertion_id, "assertion_status": "current",
            "supersedes_assertion_id": None}


def _results(payload) -> list[dict]:
    """The `results` list, or nothing. A payload that is not the shape we expect - a malformed
    body, an error envelope, a provider that changed its mind - yields NO rows rather than a
    guess; `validate` then refuses the promotion instead of storing invented evidence."""
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return [r for r in payload["results"] if isinstance(r, dict)]
    return []


def _num(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC)


def normalize_price_daily(payload, listing_id: str, capture_id: str, observed_at: datetime,
                          price_basis: str) -> list[dict]:
    rows = []
    for r in _results(payload):
        try:
            day = date.fromisoformat(str(r.get("date"))[:10])
        except ValueError:
            continue
        rows.append({
            "listing_id": listing_id, "market_date": day, "open": _num(r.get("open")),
            "high": _num(r.get("high")), "low": _num(r.get("low")), "close": _num(r.get("close")),
            "volume": _num(r.get("volume")), "price_basis": price_basis,
            **_interval(capture_id, observed_at, f"as_px_{listing_id}_{day.isoformat()}_{capture_id}",
                        _midnight(day)),
        })
    return rows


_EFFECTS = {"official": "closed", "closed": "closed", "early_close": "early_close",
            "half_day": "early_close", "late_open": "late_open"}


def normalize_exchange_calendar(payload, calendar_id: str, capture_id: str,
                                observed_at: datetime) -> list[dict]:
    """Provider holidays as `market_session` assertions - `estimated`, from a calendar package.

    A data vendor's holiday list is not the exchange speaking, so nothing here is marked
    `authority_confirmed` or `market_final`: promoting a vendor guess to an authority is how a
    wrong close date becomes an unchallenged fact.
    """
    results = payload.get("results") if isinstance(payload, dict) else None
    holidays = (results or {}).get("ExchangeHolidays") if isinstance(results, dict) else None
    rows = []
    for entry in (holidays or {}).values() if isinstance(holidays, dict) else []:
        if not isinstance(entry, dict):
            continue
        try:
            day = date.fromisoformat(str(entry.get("Date"))[:10])
        except ValueError:
            continue
        effect = _EFFECTS.get(str(entry.get("Type", "official")).lower(), "closed")
        rows.append({
            "calendar_id": calendar_id, "session_date": day, "assertion_domain": "market_session",
            "holiday_family": None, "calendar_system": "gregorian", "evidence_status": "estimated",
            "authority_type": "calendar_package", "authority_name": "EODHD exchange details",
            "authority_verified_at": None, "source_kind": "eodhd", "source_version": capture_id,
            "rule_id": None, "market_effect": effect, "holiday_name": entry.get("Holiday"),
            "special_open": effect == "late_open", "special_close": effect == "early_close",
            "market_open": None, "market_close": None, "candidate_branches": None,
            "selected_branch": None, "expiry_date": None, "settlement_status": None,
            **_interval(capture_id, observed_at,
                        f"as_cal_{calendar_id}_{day.isoformat()}_{capture_id}", _midnight(day)),
        })
    return rows


def normalize_shares_outstanding(payload, issuer_id: str, capture_id: str,
                                 observed_at: datetime) -> list[dict]:
    rows = []
    for r in _results(payload):
        value = _num(r.get("shares_outstanding"))
        if value is None:
            continue
        period = r.get("period_end") or r.get("date") or observed_at.date().isoformat()
        try:
            period_end = date.fromisoformat(str(period)[:10])
        except ValueError:
            continue
        rows.append({
            "issuer_id": issuer_id, "fact": "shares_outstanding", "period_end": period_end,
            "value": value, "unit": "shares", "filing_kind": r.get("filing_kind") or "provider",
            **_interval(capture_id, observed_at,
                        f"as_so_{issuer_id}_{period_end.isoformat()}_{capture_id}",
                        _midnight(period_end)),
        })
    return rows


def validate(relation: str, rows: list[dict]) -> list[str]:
    """Everything wrong with these rows. Empty means promotable.

    This is the last gate before Silver, and it answers with problems rather than raising:
    the worker needs the whole list to put in the job's terminal event, so an operator can
    see what the provider actually sent without reading the Bronze payload by hand.
    """
    if not rows:
        return ["no rows"]
    problems = []
    if relation == "silver.prices_normalized":
        for r in rows:
            d = r["market_date"]
            if r["close"] is None:
                problems.append(f"{d}: close is missing")
            if r["high"] is not None and r["low"] is not None and r["high"] < r["low"]:
                problems.append(f"{d}: high {r['high']} below low {r['low']}")
            if r["volume"] is not None and r["volume"] < 0:
                problems.append(f"{d}: negative volume")
    if relation == "silver.fundamental_facts":
        problems += [f"{r['period_end']}: non-positive value" for r in rows if r["value"] <= 0]
    return problems

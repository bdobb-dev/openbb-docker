# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Preflight: what an acquisition would touch, signed so the job cannot drift from it.

Every question here is answered from LOCAL evidence. No provider is contacted: a preflight
that fetched would already be the acquisition it is supposed to describe.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import date, timedelta

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import Context
from security_master_api.resolver.identity import resolve
from security_master_api.sql.session import open_session

DATASETS = ("price_daily", "exchange_calendar", "shares_outstanding", "security_lookup")
POLICIES = ("missing_only", "refresh", "force")
# Evidence a caller can act on. Anything else - provisional, estimated, authority_confirmed -
# is a session the market has not yet spoken for, and a fetch against it may need redoing.
_FINAL = ("market_final", "observed")


def _canonical(request: dict) -> bytes:
    return json.dumps(request, sort_keys=True, separators=(",", ":"), default=str).encode()


def fingerprint_of(settings: Settings, request: dict) -> str:
    return hashlib.sha256(_canonical(request) + settings.code_version.encode()).hexdigest()[:24]


def _sign(settings: Settings, claims: dict) -> str:
    body = base64.urlsafe_b64encode(_canonical(claims)).decode().rstrip("=")
    mac = hmac.new(settings.preflight_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def verify_token(settings: Settings, token: str, request: dict) -> dict:
    """The claims this token carries, or a refusal. Signature first, THEN the payload: an
    unverified body is attacker-controlled, and parsing one to decide anything is the bug."""
    body, _, mac = token.partition(".")
    if not mac:
        raise DomainError("QUERY_REJECTED", "malformed preflight token")
    expected = hmac.new(settings.preflight_secret.encode(), body.encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise DomainError("QUERY_REJECTED", "preflight token signature does not match")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if claims["exp"] < time.time():
        raise DomainError("QUERY_REJECTED", "preflight token has expired",
                          {"expired_at": claims["exp"]})
    if claims["fingerprint"] != fingerprint_of(settings, request):
        raise DomainError("QUERY_REJECTED", "the request differs from its preflight")
    return claims


def _missing_ranges(settings: Settings, ctx: Context, listing_id: str, start: date, end: date,
                    policy: str) -> list[dict]:
    """The spans of [start, end] a fetch would have to cover for one listing.

    Under `missing_only` a WEEKDAY the store has no row for opens a span; a day the store DOES
    hold closes it. A weekend does neither: it is not missing evidence, and splitting a range
    across every Saturday would turn one provider call into ten.

    `refresh` and `force` both re-fetch the WHOLE requested range - that is what they are for,
    and pricing them off the gaps would quote a caller a fraction of the fetch they asked for.
    """
    if policy in ("refresh", "force"):
        return [{"listing_id": listing_id, "start": start.isoformat(), "end": end.isoformat()}]
    with open_session(settings, ctx, ["gold.price_daily"]) as s:
        have = {r["market_date"] for r in s.run(
            "SELECT DISTINCT market_date FROM gold.price_daily WHERE listing_id = ? "
            "AND market_date BETWEEN ? AND ?",
            [listing_id, start.isoformat(), end.isoformat()]).to_pylist()}
    ranges: list[dict] = []
    cursor: date | None = None
    day = start
    while day <= end:
        if day in have:
            if cursor is not None:
                ranges.append({"listing_id": listing_id, "start": cursor.isoformat(),
                               "end": (day - timedelta(days=1)).isoformat()})
                cursor = None
        elif day.weekday() < 5 and cursor is None:
            cursor = day
        day += timedelta(days=1)
    if cursor is not None:
        ranges.append({"listing_id": listing_id, "start": cursor.isoformat(),
                       "end": end.isoformat()})
    return ranges


def _calendar(settings: Settings, ctx: Context, calendar_id: str | None, start: date,
              end: date) -> tuple[dict | None, list[str]]:
    if not calendar_id:
        return None, []
    with open_session(settings, ctx, ["gold.market_calendar"]) as s:
        rows = s.run("SELECT session_date, evidence_status, market_effect FROM gold.market_calendar "
                     "WHERE calendar_id = ? AND session_date BETWEEN ? AND ?",
                     [calendar_id, start.isoformat(), end.isoformat()]).to_pylist()
        manifest = s.manifest
    warnings = [f"{r['session_date']}: market effect {r['market_effect']} "
                f"({r['evidence_status']}) is not final"
                for r in rows
                if r["market_effect"] == "pending" or r["evidence_status"] not in _FINAL]
    return {"calendar_id": calendar_id, "sessions": len(rows), "manifest": manifest}, warnings


def _provider_symbol(settings: Settings, ctx: Context, listing_id: str) -> str | None:
    with open_session(settings, ctx, ["gold.security_master"]) as s:
        rows = s.run("SELECT provider_symbol FROM gold.security_master WHERE listing_id = ?",
                     [listing_id]).to_pylist()
    return rows[0]["provider_symbol"] if rows else None


def preflight(settings: Settings, ctx: Context, request: dict) -> dict:
    if settings.acquisition_policy == "disabled":
        raise DomainError("ACQUISITION_REVIEW_REQUIRED", "acquisition is disabled by policy",
                          {"policy": "disabled"})
    dataset = request.get("dataset")
    if dataset not in DATASETS:
        raise DomainError("QUERY_REJECTED", f"unknown dataset {dataset!r}",
                          {"supported": list(DATASETS)})
    policy = request.get("policy", "missing_only")
    if policy not in POLICIES:
        raise DomainError("QUERY_REJECTED", f"unknown policy {policy!r}",
                          {"supported": list(POLICIES)})
    identities = []
    for ident in request.get("identifiers", []):
        # resolve() raises IDENTITY_UNRESOLVED rather than guessing, and that refusal is the
        # answer: a preflight that invented a symbol would acquire the wrong security.
        candidates = resolve(settings, ctx, ident)["candidates"]
        if not candidates:
            raise DomainError("IDENTITY_UNRESOLVED",
                              f"{ident} has no candidate under this context",
                              {"identifier": ident})
        candidate = candidates[0]
        identities.append({
            "identifier": ident, "listing_id": candidate["listing_id"],
            "instrument_id": candidate["instrument_id"], "security_id": candidate["security_id"],
            "issuer_id": candidate["issuer_id"],
            "provider_symbol": _provider_symbol(settings, ctx, candidate["listing_id"]),
            "reason": candidate["reason"],
        })
    missing: list[dict] = []
    calendar, warnings = None, []
    if dataset == "price_daily":
        # `request` is a free-form dict off the wire. A missing or malformed date is the
        # caller's mistake and must answer as one, not as an unhandled KeyError/ValueError.
        try:
            start = date.fromisoformat(request["start_date"])
            end = date.fromisoformat(request["end_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError("QUERY_REJECTED",
                              "price_daily needs start_date and end_date as ISO dates",
                              {"dataset": dataset}) from exc
        if end < start:
            raise DomainError("QUERY_REJECTED", "end_date precedes start_date")
        for identity in identities:
            missing += _missing_ranges(settings, ctx, identity["listing_id"], start, end, policy)
        calendar, warnings = _calendar(settings, ctx, request.get("calendar_id"), start, end)
        if policy == "force":
            # `force` is the caller saying the calendar is not the question: it fetches the
            # range whatever the sessions say, so a warning it cannot act on is noise.
            warnings = []
    # One provider call per LISTING, not per range: an EOD endpoint answers a whole date span
    # for a symbol in a single request, so several gaps in one listing still cost one call.
    expected = (len({r["listing_id"] for r in missing}) if dataset == "price_daily"
                else max(1, len(identities)))
    fingerprint = fingerprint_of(settings, request)
    claims = {"fingerprint": fingerprint, "exp": int(time.time()) + settings.preflight_ttl_s,
              "dataset": dataset, "expected_requests": expected}
    return {
        "token": _sign(settings, claims), "expires_at": claims["exp"], "fingerprint": fingerprint,
        "dataset": dataset, "request": request, "identities": identities,
        "missing_ranges": missing, "expected_requests": expected, "calendar": calendar,
        "warnings": warnings, "policy": settings.acquisition_policy,
        "code_version": settings.code_version, "context": ctx.as_dict(),
    }

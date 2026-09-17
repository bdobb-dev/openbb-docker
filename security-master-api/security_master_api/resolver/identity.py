# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Resolve an external identifier to stable ids under a context. Never guesses."""

from __future__ import annotations

from datetime import datetime

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import Context, iso_utc
from security_master_api.resolver.views import assertions_sql
from security_master_api.sql.session import QuerySession, open_session

_TYPES = ("ticker", "eodhd_symbol", "cusip", "isin", "figi", "cik", "listing_id",
          "instrument_id", "security_id", "issuer_id")
_STABLE = ("listing_id", "instrument_id", "security_id", "issuer_id")
_FIELDS = ("listing_id", "instrument_id", "issuer_id", "security_id", "symbol", "name",
           "identifier_type", "identifier", "reason", "effective_from", "effective_to",
           "successor_security_id", "predecessor_security_id", "assertion_id", "capture_id")
_RELATIONS = ("gold.security_master", "silver.identifiers", "silver.securities",
              "silver.issuers", "silver.instruments")
# Where a stable id still lives when no listing carries it: a security, issuer or instrument
# the store holds is resolved, even though gold.security_master is built from listings.
_SILVER_BY_KIND = {"security_id": "silver.securities", "issuer_id": "silver.issuers",
                   "instrument_id": "silver.instruments"}


def _guess_type(identifier: str) -> str | None:
    prefix = identifier.split("_", 1)[0] + "_"
    return {"lst_": "listing_id", "ins_": "instrument_id", "sec_": "security_id",
            "iss_": "issuer_id"}.get(prefix)


def _effective(row: dict, at: datetime | None) -> bool:
    """Is this interval row the one in force under the context?

    With an `effective_at` the question has a date to answer it: the interval must contain
    that instant. Without one the context asks for what is in force now, which for an
    append-only interval table is exactly the row nobody has closed.
    """
    if at is None:
        return row.get("effective_to") is None
    frm, to = row.get("effective_from"), row.get("effective_to")
    return (frm is None or frm <= at) and (to is None or to > at)


def _pick(rows: list[dict], at: datetime | None) -> dict | None:
    """The row that speaks for the current identity: the one in force, else the latest."""
    for row in rows:
        if _effective(row, at):
            return row
    dated = [r for r in rows if r.get("effective_from") is not None]
    if dated:
        return max(dated, key=lambda r: r["effective_from"])
    return rows[0] if rows else None


def _rows(session: QuerySession, relation: str, column: str, value: str,
          extra: str = "", params: list | None = None) -> list[dict]:
    """Rows of one Silver relation under the context's knowledge time, effective time left open.

    The effective filter is deliberately not applied: a closed alias is still evidence, and
    whether it counts as `active` or `historical_alias` is decided per row by `_effective`.
    """
    body = assertions_sql(relation, session.ctx, effective=False)
    return session.run(f'SELECT * FROM ({body}) WHERE "{column}" = ? {extra}',
                       [value, *(params or [])]).to_pylist()


def _candidate(row: dict, reason: str) -> dict:
    out = {k: row.get(k) for k in _FIELDS}
    out["reason"] = reason
    out["effective_from"] = iso_utc(out["effective_from"])
    out["effective_to"] = iso_utc(out["effective_to"])
    return out


def _by_identifier(session: QuerySession, ident: str, kind: str | None,
                   include_historical: bool) -> tuple[list[dict], bool]:
    at = session.ctx.effective_at
    extra, params = ("AND identifier_type = ?", [kind]) if kind else ("", [])
    rows = _rows(session, "silver.identifiers", "identifier", ident, extra, params)
    if not rows:
        return [], False
    live = [r for r in rows if _effective(r, at)]
    # A closed alias only answers for an identity nothing current claims; when a live row
    # exists it is the answer, and the closed row is just its history.
    chosen = [(r, "active") for r in live]
    if not chosen and include_historical:
        chosen = [(r, "historical_alias") for r in rows if r.get("effective_to") is not None]
        if not chosen:
            # Known to the store, but every interval for it starts after the context asked.
            # Returning [] here would read as "never heard of it"; it is a different answer.
            raise _not_effective(ident, rows)
    out = []
    for row, reason in chosen:
        out.append(_candidate({**_identity(session, row), **_present(row)}, reason))
    return out, True


def _not_effective(ident: str, rows: list[dict]) -> DomainError:
    starts = [r["effective_from"] for r in rows if r.get("effective_from") is not None]
    return DomainError("IDENTITY_UNRESOLVED",
                       f"{ident} is not effective under the requested context",
                       {"identifier": ident, "status": "not_effective",
                        "effective_from": iso_utc(min(starts)) if starts else None})


def _present(row: dict) -> dict:
    """The identifier row's own claims. They win over the current identity's: the candidate
    reports which security the alias pointed at, not which one replaced it."""
    return {k: v for k, v in row.items() if v is not None}


def _identity(session: QuerySession, row: dict) -> dict:
    """The identity fields as they stand under the context, for the ids the alias names."""
    at = session.ctx.effective_at
    out: dict = {}
    if row.get("listing_id"):
        master = _pick(session.run(
            "SELECT * FROM gold.security_master WHERE listing_id = ?",
            [row["listing_id"]]).to_pylist(), at)
        if master:
            out.update({k: v for k, v in master.items() if k in _FIELDS})
    if row.get("security_id"):
        security = _pick(_rows(session, "silver.securities", "security_id", row["security_id"]), at)
        if security:
            out.update({k: security.get(k) for k in
                        ("successor_security_id", "predecessor_security_id")})
    # `reason`, `effective_from` and `effective_to` belong to the alias, never to the identity.
    for key in ("reason", "effective_from", "effective_to", "assertion_id", "capture_id"):
        out.pop(key, None)
    return out


def _by_stable_id(session: QuerySession, ident: str, kind: str,
                  include_historical: bool) -> tuple[list[dict], bool]:
    at = session.ctx.effective_at
    rows = session.run(f'SELECT * FROM gold.security_master WHERE "{kind}" = ?',
                       [ident]).to_pylist()
    by_listing: dict[str, list[dict]] = {}
    for row in rows:
        by_listing.setdefault(row.get("listing_id"), []).append(row)
    out = []
    for group in by_listing.values():
        row = _pick(group, at)
        if row is None:
            continue
        reason = "active" if _effective(row, at) else "historical_alias"
        if reason == "historical_alias" and not include_historical:
            continue
        out.append(_candidate({**row, "identifier_type": kind, "identifier": ident}, reason))
    if not rows and kind in _SILVER_BY_KIND:
        # No listing carries this id, which is not the same as not holding it: the security,
        # issuer or instrument row itself answers, with only the fields it carries.
        row = _pick(_rows(session, _SILVER_BY_KIND[kind], kind, ident), at)
        if row is not None:
            return [_candidate({**row, "identifier_type": kind, "identifier": ident},
                               "active")], True
    return out, bool(rows)


def resolve(settings: Settings, ctx: Context, identifier: str, identifier_type: str | None = None,
            include_historical: bool = True) -> dict:
    ident = identifier.strip()
    if not ident:
        raise DomainError("QUERY_REJECTED", "identifier is required")
    kind = identifier_type or _guess_type(ident)
    if kind is not None and kind not in _TYPES:
        raise DomainError("QUERY_REJECTED", f"unknown identifier_type {kind!r}",
                          {"supported": list(_TYPES)})
    with open_session(settings, ctx, _RELATIONS) as session:
        # `known` is whether the store has ever heard of this identifier, which is a
        # different question from whether the context has a candidate to offer: an alias
        # excluded by include_historical is still resolved, just not currently in force.
        resolver = _by_stable_id if kind in _STABLE else _by_identifier
        candidates, known = resolver(session, ident, kind, include_historical)
        manifest = dict(session.manifest)
    if not known:
        raise DomainError("IDENTITY_UNRESOLVED", f"{ident} resolves to no local security",
                          {"identifier": ident})
    stable = {(c["listing_id"], c["security_id"]) for c in candidates}
    if len(stable) > 1 and ctx.effective_at is None:
        raise DomainError("IDENTITY_AMBIGUOUS", f"{ident} matches {len(stable)} securities",
                          {"identifier": ident, "candidates": candidates})
    return {"candidates": candidates, "manifest": manifest}

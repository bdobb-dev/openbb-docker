# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Context-bound SQL: which assertion rows count, and the Gold views built from them."""

from __future__ import annotations

from datetime import datetime

from security_master_api.resolver.context import Context

GOLD_NAMES = ("security_master", "price_daily", "corporate_actions", "universe_membership",
              "market_calendar", "odp_equity_info")

LATEST_PER_ASSERTION = (
    "QUALIFY row_number() OVER (PARTITION BY assertion_id ORDER BY system_from DESC) = 1"
)


def _ts(value: datetime) -> str:
    return f"TIMESTAMPTZ '{value.isoformat()}'"


def knowledge_filters(ctx: Context) -> tuple[str, str]:
    """(pre-qualify filter, post-qualify filter) for one Silver relation."""
    if ctx.mode == "known_at":
        k = _ts(ctx.known_at)
        return (f"system_from <= {k}",
                f"available_at <= {k} AND (system_to IS NULL OR system_to > {k})")
    if ctx.mode in ("current_corrected", "effective_on"):
        return ("TRUE", "system_to IS NULL AND assertion_status = 'current'")
    return ("TRUE", "TRUE")


def effective_filter(ctx: Context) -> str:
    if ctx.effective_at is None or ctx.mode in ("delta_snapshot", "captured_by"):
        return "TRUE"
    e = _ts(ctx.effective_at)
    return f"effective_from <= {e} AND (effective_to IS NULL OR effective_to > {e})"


def assertions_sql(relation: str, ctx: Context) -> str:
    layer, _, name = relation.partition(".")
    pre, post = knowledge_filters(ctx)
    return (f'SELECT * FROM (SELECT * FROM {layer}."{name}" WHERE {pre} {LATEST_PER_ASSERTION}) '
            f"WHERE {post} AND {effective_filter(ctx)}")


def _a(relation: str, ctx: Context, alias: str) -> str:
    return f"({assertions_sql(relation, ctx)}) AS {alias}"


def gold_sql(name: str, ctx: Context) -> str:
    if name == "security_master":
        return f"""
            SELECT l.listing_id, l.instrument_id, l.issuer_id, l.security_id, l.exchange_id, l.mic,
                   l.symbol, l.provider_symbol, l.currency, l.status, l.name, l.instrument_type,
                   i.name AS issuer_name, s.cusip, s.successor_security_id, s.predecessor_security_id,
                   l.assertion_id, l.capture_id, l.effective_from, l.effective_to,
                   l.available_at, l.system_from
            FROM {_a("silver.listings", ctx, "l")}
            LEFT JOIN {_a("silver.issuers", ctx, "i")} ON i.issuer_id = l.issuer_id
            LEFT JOIN {_a("silver.securities", ctx, "s")} ON s.security_id = l.security_id
        """
    if name == "price_daily":
        return f"""
            SELECT p.listing_id, p.market_date, p.open, p.high, p.low, p.close, p.volume,
                   p.price_basis, p.assertion_id, p.capture_id, p.available_at, p.system_from,
                   p.supersedes_assertion_id
            FROM (SELECT * FROM (SELECT * FROM silver."prices_normalized"
                  WHERE {knowledge_filters(ctx)[0]} {LATEST_PER_ASSERTION})
                  WHERE {knowledge_filters(ctx)[1]}) AS p
        """
    if name == "corporate_actions":
        return f"""
            SELECT c.listing_id, c.action_type, c.ex_date, c.ratio, c.amount, c.currency,
                   c.assertion_id, c.capture_id, c.available_at
            FROM {_a("silver.corporate_actions", ctx, "c")}
        """
    if name == "universe_membership":
        return f"""
            SELECT u.universe_id, u.listing_id, u.effective_from, u.effective_to,
                   u.assertion_id, u.capture_id, u.available_at
            FROM {_a("silver.universe_membership", ctx, "u")}
        """
    if name == "odp_equity_info":
        return f"""
            SELECT l.listing_id, l.symbol, l.name, l.issuer_id, l.mic, l.currency,
                   f.value AS shares_outstanding, f.period_end, f.filing_kind,
                   f.assertion_id AS shares_assertion_id, f.capture_id AS shares_capture_id,
                   f.available_at AS shares_available_at,
                   l.assertion_id, l.capture_id
            FROM {_a("silver.listings", ctx, "l")}
            LEFT JOIN (
                SELECT * FROM (SELECT * FROM (SELECT * FROM silver."fundamental_facts"
                    WHERE {knowledge_filters(ctx)[0]} {LATEST_PER_ASSERTION})
                    WHERE {knowledge_filters(ctx)[1]} AND fact = 'shares_outstanding')
                QUALIFY row_number() OVER (PARTITION BY issuer_id ORDER BY period_end DESC,
                                           available_at DESC) = 1
            ) AS f ON f.issuer_id = l.issuer_id
        """
    if name == "market_calendar":
        pre, post = knowledge_filters(ctx)
        ex = (f"(SELECT * FROM (SELECT * FROM silver.\"calendar_exceptions\" WHERE {pre} "
              f"{LATEST_PER_ASSERTION}) WHERE {post})")
        return f"""
            WITH holiday AS (
                SELECT * FROM {ex} WHERE assertion_domain = 'holiday_date'
                QUALIFY row_number() OVER (PARTITION BY calendar_id, session_date
                                           ORDER BY available_at DESC, system_from DESC) = 1
            ), market AS (
                SELECT * FROM {ex} WHERE assertion_domain = 'market_session'
                QUALIFY row_number() OVER (PARTITION BY calendar_id, session_date
                                           ORDER BY available_at DESC, system_from DESC) = 1
            ), sessions AS (
                SELECT * FROM {_a("silver.market_sessions", ctx, "s")}
            ), days AS (
                SELECT calendar_id, session_date FROM holiday
                UNION SELECT calendar_id, session_date FROM market
                UNION SELECT calendar_id, session_date FROM sessions
            )
            SELECT d.calendar_id, e.exchange_id, e.name AS exchange_name, e.mic, e.calendar_alias,
                   d.session_date, coalesce(s.trade_date, d.session_date) AS trade_date,
                   e.timezone,
                   CASE WHEN m.market_effect = 'closed' THEN 'closed'
                        WHEN m.market_effect IN ('early_close', 'late_open', 'special_session')
                             THEN 'special'
                        WHEN m.market_effect = 'interrupted' THEN 'interrupted'
                        WHEN m.market_effect IS NULL AND h.calendar_id IS NOT NULL THEN 'unknown'
                        WHEN m.market_effect = 'none' OR s.calendar_id IS NOT NULL THEN 'open'
                        ELSE 'unknown' END AS session_status,
                   coalesce(h.calendar_system, m.calendar_system, 'gregorian') AS calendar_system,
                   h.holiday_family,
                   CASE WHEN m.calendar_id IS NOT NULL THEN 'market_session'
                        WHEN h.calendar_id IS NOT NULL THEN 'holiday_date' END AS assertion_domain,
                   coalesce(m.evidence_status, h.evidence_status, 'estimated') AS evidence_status,
                   coalesce(m.market_open, s.market_open) AS market_open,
                   coalesce(m.market_close, s.market_close) AS market_close,
                   s.break_start, s.break_end,
                   h.holiday_name,
                   coalesce(m.special_open, FALSE) AS special_open,
                   coalesce(m.special_close, FALSE) AS special_close,
                   CASE WHEN m.market_effect IS NOT NULL THEN m.market_effect
                        WHEN h.calendar_id IS NOT NULL THEN 'pending'
                        ELSE 'none' END AS market_effect,
                   coalesce(m.authority_type, h.authority_type) AS authority_type,
                   coalesce(m.authority_name, h.authority_name) AS authority_name,
                   coalesce(m.authority_verified_at, h.authority_verified_at) AS authority_verified_at,
                   coalesce(m.source_kind, h.source_kind, s.source_version) AS source_kind,
                   coalesce(m.source_version, h.source_version, s.source_version) AS source_version,
                   coalesce(m.rule_id, h.rule_id, s.rule_id) AS rule_id,
                   coalesce(m.supersedes_assertion_id, h.supersedes_assertion_id) AS supersedes_assertion_id,
                   coalesce(m.effective_from, h.effective_from, s.effective_from) AS effective_from,
                   coalesce(m.effective_to, h.effective_to, s.effective_to) AS effective_to,
                   coalesce(m.available_at, h.available_at, s.available_at) AS known_from,
                   coalesce(m.system_to, h.system_to, s.system_to) AS known_to,
                   coalesce(m.capture_id, h.capture_id, s.capture_id) AS capture_id,
                   h.assertion_id AS holiday_assertion_id, m.assertion_id AS market_assertion_id,
                   m.candidate_branches, m.selected_branch, m.expiry_date, m.settlement_status
            FROM days d
            LEFT JOIN holiday h ON h.calendar_id = d.calendar_id AND h.session_date = d.session_date
            LEFT JOIN market m ON m.calendar_id = d.calendar_id AND m.session_date = d.session_date
            LEFT JOIN sessions s ON s.calendar_id = d.calendar_id AND s.session_date = d.session_date
            LEFT JOIN {_a("silver.exchanges", ctx, "e")} ON e.calendar_id = d.calendar_id
        """
    raise KeyError(name)

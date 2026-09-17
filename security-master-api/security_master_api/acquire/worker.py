# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The acquisition worker: claim a job, retain Bronze, normalize, validate, promote. Restart-safe."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import time
import uuid
from datetime import UTC, datetime

from security_master_api.acquire.client import OpenbbClient, classify
from security_master_api.acquire.normalize import (
    is_malformed_payload,
    normalize_exchange_calendar,
    normalize_price_daily,
    normalize_shares_outstanding,
    validate,
)
from security_master_api.acquire.preflight import preflight
from security_master_api.config import Settings, settings_from_env
from security_master_api.errors import DomainError
from security_master_api.resolver.context import parse_context
from security_master_api.resolver.manifest import resolve_manifest
from security_master_api.resolver.views import assertions_sql
from security_master_api.sql.session import open_session
from security_master_api.store.jobs import append_event, claim, events, in_state, job, queued
from security_master_api.store.receipts import new_receipt, record_receipt
from security_master_api.store.tables import append

log = logging.getLogger("security-master-worker")

_ROUTES = {
    "price_daily": "/api/v1/equity/price/historical",
    "exchange_calendar": "/api/v1/reference/exchange_details",
    "shares_outstanding": "/api/v1/equity/profile",
    "security_lookup": "/api/v1/equity/search",
}

# The Silver key a new assertion replaces, per relation. Same keys the catalog declares.
_KEYS = {"silver.prices_normalized": ("listing_id", "market_date"),
         "silver.fundamental_facts": ("issuer_id", "fact", "period_end"),
         "silver.calendar_exceptions": ("calendar_id", "session_date", "assertion_domain")}


_MAX_PAYLOAD = 2_000_000


def _cancelled(settings: Settings, job_id: str) -> bool:
    return any(e["stage"] == "cancel_pending" for e in events(settings, job_id))


def _capture(settings: Settings, job_id: str, endpoint: str, params: dict, resp,
             attempt: int) -> str:
    """Retain the response verbatim BEFORE anything is made of it. Bronze is what makes a
    later normalizer fix replayable, so it is written for every answer the provider gave -
    a 403 and a malformed 200 included.

    `content_hash` is taken over the STORED payload, not the wire body: a hash over bytes we
    then truncate would describe evidence nobody can replay against what is actually on disk.
    """
    now = datetime.now(UTC)
    capture_id = "cap_" + uuid.uuid4().hex[:12]
    fingerprint = hashlib.sha256(
        json.dumps([endpoint, params], sort_keys=True).encode()).hexdigest()[:16]
    truncated = len(resp.body) > _MAX_PAYLOAD
    payload = resp.body[:_MAX_PAYLOAD]
    if truncated:
        error = f"payload truncated to {_MAX_PAYLOAD} bytes (original {len(resp.body)})"
    elif not (200 <= resp.status < 300):
        error = resp.body[:200]
    else:
        error = None
    append(settings, "bronze.source_captures", [{
        "capture_id": capture_id, "provider": "openbb-api/eodhd", "endpoint": endpoint,
        "request_fingerprint": fingerprint, "captured_at": now,
        "content_hash": hashlib.sha256(payload.encode()).hexdigest(), "status": resp.status,
        "payload": payload, "job_id": job_id,
    }])
    append(settings, "bronze.request_log", [{
        "request_id": "rq_" + uuid.uuid4().hex[:12], "job_id": job_id, "capture_id": capture_id,
        "requested_at": now, "response_status": resp.status, "attempt": attempt,
        "retry_after_s": resp.retry_after_s, "error": error,
    }])
    return capture_id


def _requests_for(settings: Settings, job_row: dict) -> list[tuple[str, dict, dict]]:
    """(endpoint, params, identity) per HTTP call the job will make.

    The plan comes from `preflight` and nowhere else: the same function that priced the job
    for the caller decides what it fetches, so a job cannot quietly cost more than its quote.
    """
    req = job_row["request"]
    ctx = parse_context(None)
    pf = preflight(settings, ctx, req)
    out = []
    kind = job_row["kind"]
    if kind == "price_daily":
        # One call per LISTING, matching what preflight quoted in `expected_requests`: a gap
        # scan can split one listing into several missing ranges, and fetching each of those
        # separately would bill the caller more calls than they were priced for. The EOD
        # endpoint answers a whole span in one request, so the ranges collapse to their outer
        # bounds before anything is sent.
        by_listing = {i["listing_id"]: i for i in pf["identities"]}
        spans: dict[str, dict] = {}
        for rng in pf["missing_ranges"]:
            span = spans.setdefault(rng["listing_id"], {"start": rng["start"], "end": rng["end"]})
            span["start"] = min(span["start"], rng["start"])
            span["end"] = max(span["end"], rng["end"])
        for listing_id, span in spans.items():
            ident = by_listing[listing_id]
            out.append((_ROUTES[kind], {"symbol": ident["provider_symbol"], "provider": "eodhd",
                                        "interval": "1d", "start_date": span["start"],
                                        "end_date": span["end"]}, ident))
    elif kind == "shares_outstanding":
        for ident in pf["identities"]:
            out.append((_ROUTES[kind], {"symbol": ident["provider_symbol"], "provider": "eodhd"},
                        ident))
    elif kind == "exchange_calendar":
        out.append((_ROUTES[kind], {"code": req["exchange_code"], "provider": "eodhd"},
                    {"calendar_id": req["calendar_id"]}))
    elif kind == "security_lookup":
        out.append((_ROUTES[kind], {"query": req["query"], "provider": "eodhd"}, {}))
    return out


def _normalize(kind: str, payload, ident: dict, capture_id: str, now: datetime, req: dict):
    if kind == "price_daily":
        return "silver.prices_normalized", normalize_price_daily(
            payload, ident["listing_id"], capture_id, now, req.get("price_basis", "raw"))
    if kind == "shares_outstanding":
        # A share count belongs to the ISSUER, not the listing: two listings of one company
        # keyed separately would double-count the same shares.
        return "silver.fundamental_facts", normalize_shares_outstanding(
            payload, ident["issuer_id"], capture_id, now)
    if kind == "exchange_calendar":
        return "silver.calendar_exceptions", normalize_exchange_calendar(
            payload, ident["calendar_id"], capture_id, now)
    return None, []


def _supersede(settings: Settings, relation: str, rows: list[dict], now: datetime) -> list[dict]:
    """Under refresh/force, close the assertions the new rows replace.

    `assertions_sql` is what Gold itself reads with, so "the row this one replaces" is decided
    exactly as a reader would decide "the row that counts" - an already-superseded assertion
    whose opening row still has a NULL `system_to` is not a candidate, and must not be.
    """
    keys = _KEYS[relation]
    ctx = parse_context(None)
    with open_session(settings, ctx, [relation]) as s:
        current = s.run(assertions_sql(relation, ctx, effective=False)).to_pylist()
    by_key = {tuple(str(c[k]) for k in keys): c for c in current}
    closes = []
    for r in rows:
        old = by_key.get(tuple(str(r[k]) for k in keys))
        if old is not None and old["assertion_id"] != r["assertion_id"]:
            # The close row REPEATS the old assertion_id: it is the same assertion, ending.
            closed = dict(old)
            closed.update({"system_from": now, "system_to": now, "assertion_status": "superseded"})
            closes.append(closed)
            r["supersedes_assertion_id"] = old["assertion_id"]
    return closes


def process(settings: Settings, client: OpenbbClient, job_id: str, worker_id: str) -> None:
    row = job(settings, job_id)
    kind, req = row["kind"], row["request"]
    now = datetime.now(UTC)
    run_id = "run_" + uuid.uuid4().hex[:12]
    append(settings, "bronze.ingestion_runs", [{"run_id": run_id, "job_id": job_id,
                                                "code_version": settings.code_version,
                                                "started_at": now, "ended_at": None,
                                                "outcome": None}])
    captures: list[tuple[str, object, dict]] = []
    try:
        for attempt, (endpoint, params, ident) in enumerate(_requests_for(settings, row), start=1):
            if _cancelled(settings, job_id):
                append_event(settings, job_id, "cancelled", {"after_requests": attempt - 1},
                             worker_id)
                return
            resp = client.get(endpoint, params)
            capture_id = _capture(settings, job_id, endpoint, params, resp, attempt)
            outcome = classify(resp.status)
            if outcome == "rate_limited":
                append_event(settings, job_id, "rate_limited",
                             {"retry_after_s": resp.retry_after_s, "capture_id": capture_id},
                             worker_id)
                return
            if outcome == "entitlement":
                append_event(settings, job_id, "entitlement_denied", {"capture_id": capture_id},
                             worker_id)
                return
            if outcome != "ok":
                append_event(settings, job_id, "failed",
                             {"status": resp.status, "capture_id": capture_id, "outcome": outcome},
                             worker_id)
                return
            captures.append((capture_id, resp.json, ident))
        append_event(settings, job_id, "bronze_retained", {"captures": len(captures)}, worker_id)
        append_event(settings, job_id, "normalizing", {}, worker_id)
        relation, rows, malformed = None, [], False
        for capture_id, payload, ident in captures:
            if is_malformed_payload(kind, payload):
                # A body that doesn't parse as JSON, or has no `results` container at all, is
                # not "the provider said nothing" - it's "we can't tell what the provider
                # said". That is a validation failure, not a partial answer, even though it
                # also normalizes to zero rows: Bronze already has the raw evidence for an
                # operator to inspect.
                malformed = True
                continue
            relation, part = _normalize(kind, payload, ident, capture_id, now, req)
            rows += part
        append_event(settings, job_id, "validating", {"rows": len(rows)}, worker_id)
        if malformed:
            append_event(settings, job_id, "validation_failed",
                         {"problems": ["malformed payload"]}, worker_id)
            return
        problems = validate(relation, rows) if relation else ["nothing to promote"]
        if problems == ["no rows"]:
            # A provider that answers 200 with nothing to say is not a validation failure: the
            # capture is honest, there is simply no evidence in it. Bronze stays either way.
            append_event(settings, job_id, "partial",
                         {"problems": problems, "captures": [c[0] for c in captures]}, worker_id)
            return
        if problems:
            # Bronze stays. The capture is the evidence an operator needs to decide whether the
            # provider is wrong or this normalizer is, and deleting it would destroy both.
            append_event(settings, job_id, "validation_failed", {"problems": problems[:50]},
                         worker_id)
            return
        closes = (_supersede(settings, relation, rows, now)
                  if req.get("policy") in ("refresh", "force") else [])
        # ONE commit: closing the old assertion and writing the new one as two commits leaves
        # a window between them where a reader sees the old row closed and the new one not yet
        # written - a day that, until the second commit lands, is missing from Gold entirely.
        append(settings, relation, closes + rows)
        append_event(settings, job_id, "silver_ready", {"relation": relation, "rows": len(rows)},
                     worker_id)
        append_event(settings, job_id, "materializing", {}, worker_id)
        ctx = parse_context(None)
        manifest = resolve_manifest(settings, ctx, [relation])
        receipt = new_receipt(settings, "acquisition", ctx, manifest, job_id)
        record_receipt(settings, receipt)
        append_event(settings, job_id, "gold_ready", {"dependencies": receipt["dependencies"],
                                                      "execution_id": receipt["execution_id"]},
                     worker_id)
    except DomainError as exc:
        append_event(settings, job_id, "failed", {"code": exc.code, "message": exc.message},
                     worker_id)
    except Exception as exc:  # noqa: BLE001 - a job must end in a durable state
        log.exception("job %s failed", job_id)
        append_event(settings, job_id, "failed", {"error": str(exc)[:300]}, worker_id)


def run_once(settings: Settings, client: OpenbbClient, worker_id: str) -> int:
    done = 0
    for candidate in queued(settings):
        if claim(settings, candidate["job_id"], worker_id):
            process(settings, client, candidate["job_id"], worker_id)
            done += 1
    # A cancel that arrived before any worker claimed the job leaves it sitting at
    # `cancel_pending` forever, because it is no longer queued and nothing else will look at
    # it. Closing it here is the only place that can. A job that IS claimed is left alone:
    # the worker holding it sees the same event between its own requests, and two writers
    # ending one job would race.
    for candidate in in_state(settings, "cancel_pending"):
        if not any(s["stage"] == "fetching" for s in candidate["stage_history"]):
            append_event(settings, candidate["job_id"], "cancelled", {"after_requests": 0},
                         worker_id)
            done += 1
    return done


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = settings_from_env()
    client = OpenbbClient(settings.openbb_url, settings.openbb_username, settings.openbb_password)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    interval = float(os.environ.get("SECURITY_MASTER_WORKER_POLL_S", "2"))
    log.info("worker %s polling every %ss", worker_id, interval)
    while True:
        try:
            run_once(settings, client, worker_id)
        except Exception:  # noqa: BLE001 - the poll loop outlives any single failure
            log.exception("poll failed")
        time.sleep(interval)

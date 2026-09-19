# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""Durable job state as two append-only Delta tables. State is the latest event's stage."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from deltalake.exceptions import CommitFailedError

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver.context import iso_utc, parse_context
from security_master_api.sql.session import open_session
from security_master_api.store.tables import append

STAGES = ("queued", "fetching", "bronze_retained", "normalizing", "validating", "silver_ready",
          "materializing", "gold_ready")
TERMINAL = ("partial", "rate_limited", "entitlement_denied", "validation_failed", "cancel_pending",
            "cancelled", "failed", "gold_ready")
ALL_STAGES = STAGES + tuple(s for s in TERMINAL if s not in STAGES)

# `ops.*` is readable under exactly one temporal mode, and job state has no bitemporal axis of
# its own: every read here is the Delta snapshot as it stands right now.
_SNAPSHOT = {"mode": "delta_snapshot"}


def _now() -> datetime:
    return datetime.now(UTC)


def _events_table(settings: Settings, where: str, params: list) -> list[dict]:
    # Two workers appending at once can mint the same `seq`, and an arbitrary order between
    # the tied rows would let BOTH read themselves as the first `fetching` event and both
    # believe they hold the claim. `at` breaks the tie by what actually happened first, and
    # `event_id` only settles rows written in the same microsecond - a random uuid must never
    # be what decides which of two events is the later one.
    with open_session(settings, parse_context(_SNAPSHOT), ["ops.job_events"]) as s:
        # `at` is quoted: bare, DuckDB reads it as the AT keyword and the ORDER BY fails to
        # parse.
        return s.run(f'SELECT * FROM ops.job_events WHERE {where} ORDER BY seq, "at", event_id',
                     params).to_pylist()


def _jobs_table(settings: Settings, where: str, params: list) -> list[dict]:
    """The latest `ops.jobs` row per job. The table is append-only, so a job's newest row -
    the highest `seq` - is its current record and the earlier ones are its history."""
    with open_session(settings, parse_context(_SNAPSHOT), ["ops.jobs"]) as s:
        return s.run(f"SELECT * FROM ops.jobs WHERE {where} "
                     "QUALIFY row_number() OVER (PARTITION BY job_id ORDER BY seq DESC) = 1",
                     params).to_pylist()


def events(settings: Settings, job_id: str) -> list[dict]:
    rows = _events_table(settings, "job_id = ?", [job_id])
    return [{**{k: iso_utc(v) for k, v in r.items()}, "detail": json.loads(r["detail"] or "{}")}
            for r in rows]


def job(settings: Settings, job_id: str) -> dict:
    rows = _jobs_table(settings, "job_id = ?", [job_id])
    if not rows:
        raise DomainError("NO_MATCHING_FACTS", f"unknown job {job_id}", {"job_id": job_id})
    evs = events(settings, job_id)
    latest = evs[-1] if evs else None
    row = rows[0]
    # The EVENT log decides the state, not the job row: an event appended by any writer -
    # including one that never touched `ops.jobs` - is still something that happened to this
    # job. The row carries the summary and is the state of record when no event exists yet.
    return {
        "job_id": row["job_id"], "kind": row["kind"], "fingerprint": row["fingerprint"],
        "state": latest["stage"] if latest else row["state"],
        "created_at": iso_utc(row["created_at"]),
        "updated_at": latest["at"] if latest else iso_utc(row["updated_at"]),
        "request": json.loads(row["request"] or "{}"),
        "summary": json.loads(row["summary"] or "{}"),
        "stage_history": [{"stage": e["stage"], "at": e["at"], "detail": e["detail"],
                           "worker_id": e["worker_id"]} for e in evs],
    }


def by_fingerprint(settings: Settings, fingerprint: str) -> dict | None:
    """The job still working on this request, if one is. A terminal job answers nothing: the
    same request after a failure is a NEW job, not a resurrection of the dead one."""
    for row in _jobs_table(settings, "fingerprint = ?", [fingerprint]):
        summary = job(settings, row["job_id"])
        if summary["state"] not in TERMINAL:
            return summary
    return None


def in_state(settings: Settings, state: str) -> list[dict]:
    """Every job whose latest event puts it in `state`, oldest first.

    Ordered by creation, not by discovery: two workers scanning at once must agree on which
    job is next, or they would pull in different orders and fight over the same claims.
    """
    out = [job(settings, row["job_id"]) for row in _jobs_table(settings, "TRUE", [])]
    return sorted((j for j in out if j["state"] == state), key=lambda j: j["created_at"])


def queued(settings: Settings) -> list[dict]:
    return in_state(settings, "queued")


def create_job(settings: Settings, kind: str, request: dict, fingerprint: str,
               token_hash: str) -> dict:
    existing = by_fingerprint(settings, fingerprint)
    if existing is not None:
        return existing
    job_id = "job_" + uuid.uuid4().hex[:12]
    now = _now()
    append(settings, "ops.jobs", [{
        "job_id": job_id, "kind": kind, "fingerprint": fingerprint, "state": "queued",
        "created_at": now, "updated_at": now, "request": json.dumps(request, sort_keys=True),
        "summary": "{}", "preflight_token_hash": token_hash, "seq": 0,
    }])
    append_event(settings, job_id, "queued", {"kind": kind}, "api")
    return job(settings, job_id)


def append_event(settings: Settings, job_id: str, stage: str, detail: dict | None = None,
                 worker_id: str = "api") -> dict:
    if stage not in ALL_STAGES:
        raise DomainError("QUERY_REJECTED", f"unknown stage {stage}", {"stage": stage})
    seq = len(_events_table(settings, "job_id = ?", [job_id]))
    now = _now()
    encoded = json.dumps(detail or {}, sort_keys=True, default=str)
    row = {"event_id": "ev_" + uuid.uuid4().hex[:12], "job_id": job_id, "seq": seq,
           "stage": stage, "at": now, "detail": encoded, "worker_id": worker_id}
    append(settings, "ops.job_events", [row])
    _record(settings, job_id, stage, encoded, now)
    return {**{k: iso_utc(v) for k, v in row.items()}, "detail": detail or {}}


def _record(settings: Settings, job_id: str, stage: str, encoded: str, now: datetime) -> None:
    """Carry the job's own row forward, so `ops.jobs` states what happened without replaying
    the event log. A TERMINAL stage's detail is the job's summary - how it ended is the only
    detail that answers "what came of this job"; an intermediate stage leaves it alone."""
    rows = _jobs_table(settings, "job_id = ?", [job_id])
    if not rows:
        return
    row = rows[0]
    append(settings, "ops.jobs", [{**row, "seq": row["seq"] + 1, "state": stage,
                                   "updated_at": now,
                                   "summary": encoded if stage in TERMINAL else row["summary"]}])


def claim(settings: Settings, job_id: str, worker_id: str) -> bool:
    """Append `fetching`; the FIRST fetching event for the job names the winner.

    Two workers reading the same queued job both pass the pre-check, so the read is not the
    guard: one of the two appends loses the Delta commit (CommitFailedError), and if both
    somehow land, the earliest `seq` decides. Nothing here is held in process memory, so a
    worker that dies mid-claim leaves the same evidence a live one would.
    """
    evs = _events_table(settings, "job_id = ?", [job_id])
    if not evs or evs[-1]["stage"] != "queued" or any(e["stage"] == "fetching" for e in evs):
        return False
    try:
        append_event(settings, job_id, "fetching", {}, worker_id)
    except CommitFailedError:
        return False
    first = _events_table(settings, "job_id = ? AND stage = 'fetching'", [job_id])
    return bool(first) and first[0]["worker_id"] == worker_id

"""Bronze capture writer (task-1 brief): immutable payload store + capture-row
append for the `bronze.*_capture` tables (baseline design doc Sec 7.1, Sec 6.1).

Every vendor HTTP fetch is captured as (a) the raw response body, written
once, gzip-compressed, immutable, under `<root>/_payloads/YYYY/MM/
<capture_id>.json.gz`, and (b) exactly one audit row in the relevant
`bronze.*_capture` Delta table (schema per `tick_vault.schemas.SCHEMAS`),
carrying the payload's URI/sha256 (or, on failure, the hashed error body and
a NULL payload URI) plus request/response bookkeeping.

Split for testability (no pyarrow/deltalake/pytest in the authoring
sandbox, network blocked):

  - Pure, dependency-free logic (payload path layout, gzip write + the
    immutable-write guard, sha256, choosing which body to hash/persist,
    and `build_capture_row` filling an arbitrary schema's column list from
    a plain dict) needs nothing beyond the stdlib and is exercised directly
    by the sandbox mini-runner (`tests/test_capture_core.py`, plain
    asserts, no pytest import).
  - The Delta-backed parts (`append_rows`, and `CaptureStore.record`'s use
    of it, which needs `tick_vault.schemas.SCHEMAS` - itself a `pyarrow`-
    importing module) do lazy imports *inside* the functions that need
    them, so this module imports bare with no pyarrow/deltalake/schemas
    dependency at module scope. Those paths are verified by
    `tests/deferred/test_capture.py` (pytest, real tmp_path Delta lake).

`Transport` (a `get(url, timeout) -> (status, body)` protocol),
`FakeTransport` (canned in-order responses for tests) and `UrlLibTransport`
(the live stdlib-`urllib` implementation) live here too, since
`CaptureStore.fetch_and_capture` is defined in terms of a `Transport`.
`UrlLibTransport` retries on 5xx only (3 attempts, `5 * attempt` second
sleeps, an injectable sleeper for tests); 429 (rate-limit) handling is
deliberately NOT implemented here - that cross-request budget/backoff
behavior belongs to Task 7's `Budget` class, which wraps a `Transport` and
owns pacing across many calls, not to this per-call retry loop.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import datetime as dt
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import pandas as pd

from tick_vault.ids import new_id

_PAYLOAD_DIR = "_payloads"


# ---------------------------------------------------------------------------
# pure helpers (no pyarrow/deltalake)
# ---------------------------------------------------------------------------

def payload_path(root: str, capture_id: str, when: dt.datetime) -> str:
    """`<root>/_payloads/YYYY/MM/<capture_id>.json.gz`, partitioned by the
    UTC year/month of `when` (the capture's `observed_at`)."""
    return os.path.join(
        root, _PAYLOAD_DIR, f"{when:%Y}", f"{when:%m}", f"{capture_id}.json.gz"
    )


def write_payload(path: str, body: bytes) -> None:
    """Gzip-compress `body` and write it to `path`.

    The payload store is immutable: if `path` already exists this raises
    `FileExistsError` rather than overwriting it. Since `path` is always
    derived from a freshly-minted `capture_id` (uuid7, effectively unique),
    this only fires if something tries to reuse a capture_id/path, which
    the store treats as a bug.
    """
    if os.path.exists(path):
        raise FileExistsError(f"payload path already exists (immutable store): {path}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wb") as f:
        f.write(body)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve_capture_body(
    payload_bytes: bytes | None, error_body: bytes | None
) -> tuple[bytes, bool]:
    """Decide which body a capture hashes, and whether it gets persisted.

    Success (`payload_bytes` is not None): that payload is both hashed and
    persisted to the payload store -> `(payload_bytes, True)`.

    Failure (`payload_bytes` is None): the error body is hashed (so the
    capture row's `raw_payload_sha256` still ties back to exactly what the
    vendor returned) but never written to the payload store - failure
    captures keep `raw_payload_uri`/`payload_path` as `None`. Returns
    `(error_body or b"", False)`.
    """
    if payload_bytes is not None:
        return payload_bytes, True
    return (error_body if error_body is not None else b""), False


def row_count_hint(payload_bytes: bytes | None) -> int | None:
    """Best-effort row count for a JSON-array payload; `None` if the
    payload is missing, not valid JSON, or not a top-level list."""
    if not payload_bytes:
        return None
    try:
        parsed = json.loads(payload_bytes)
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(parsed, list):
        return len(parsed)
    return None


def build_capture_row(
    *,
    columns: list[str],
    capture_id: str,
    ingestion_run_id: str,
    source_system: str,
    endpoint: str,
    parser_version: str,
    request_parameters_json: str,
    http_status: int,
    payload_uri: str | None,
    payload_sha256: str,
    row_count_hint: int | None,
    observed_at: dt.datetime,
    committed_at: dt.datetime,
    created_at: dt.datetime,
    extra_cols: dict | None = None,
) -> dict:
    """Build one capture row as a plain dict, keyed by exactly `columns`
    (every schema column name for the target table), filling every column
    present in `columns` and leaving the rest `None`.

    This is schema-agnostic on purpose: it never imports
    `tick_vault.schemas` (which needs pyarrow), so it can be exercised
    directly by the sandbox mini-runner against a hardcoded column list.
    Callers (`CaptureStore.record`) pass `columns=[f.name for f in
    SCHEMAS[table]]` and layer table-specific fields (e.g.
    `request_symbol`, `request_from_sec`, `index_vendor_symbol`, ...) in
    via `extra_cols`; any key in `extra_cols` that isn't a real column for
    this table is silently dropped rather than raising, since the generic
    bronze capture envelope varies per endpoint.
    """
    row = {c: None for c in columns}
    common = {
        "capture_id": capture_id,
        "ingestion_run_id": ingestion_run_id,
        "source_system": source_system,
        "endpoint": endpoint,
        "parser_version": parser_version,
        "request_parameters_json": request_parameters_json,
        "http_status": http_status,
        "raw_payload_uri": payload_uri,
        "raw_payload_sha256": payload_sha256,
        "raw_row_count": row_count_hint,
        "observed_at_ts": observed_at,
        "committed_at_ts": committed_at,
        "created_at_ts": created_at,
    }
    if "committed_date" in row:
        common["committed_date"] = committed_at.date()
    for key, value in common.items():
        if key in row:
            row[key] = value
    if extra_cols:
        for key, value in extra_cols.items():
            if key in row:
                row[key] = value
    return row


# ---------------------------------------------------------------------------
# Delta-backed helpers (lazy pyarrow/deltalake/schemas imports)
# ---------------------------------------------------------------------------

def append_rows(root: str, table: str, df: "pd.DataFrame") -> None:
    """Append `df` (one row per capture, or more) to `<root>/<layer>/<name>`
    (the Delta table registered as `table`, e.g. `"bronze.eodhd_tick_capture"`
    -> `<root>/bronze/eodhd_tick_capture`), casting to the exact registered
    schema (`tick_vault.schemas.SCHEMAS[table]` - no `schema_mode="merge"`;
    an exact cast, so a row missing/mistyping a column fails loudly instead
    of silently evolving the table). Shared by later tasks' writers, not
    just `CaptureStore`.
    """
    import pyarrow as pa
    from deltalake import write_deltalake

    from tick_vault.schemas import SCHEMAS

    layer, name = table.split(".", 1)
    path = f"{root}/{layer}/{name}"
    schema = SCHEMAS[table]
    arrow_table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    write_deltalake(path, arrow_table, mode="append")


@dataclass(frozen=True)
class CaptureRecord:
    capture_id: str
    payload_path: str | None
    sha256: str
    row_count_hint: int | None


class CaptureStore:
    """Writes bronze `*_capture` rows + their immutable payload files."""

    def __init__(self, root: str):
        self.root = root

    def record(
        self,
        *,
        table: str,
        endpoint: str,
        request_params: dict,
        http_status: int,
        payload_bytes: bytes | None,
        error_body: bytes | None,
        observed_at: dt.datetime,
        ingestion_run_id: str,
        parser_version: str,
        extra_cols: dict | None = None,
        source_system: str = "eodhd",
    ) -> CaptureRecord:
        """Capture one vendor fetch: write its payload (if any) to the
        immutable payload store and append exactly one row to `table`.

        Every call mints a fresh `capture_id` (`ids.new_id("cap")`), so a
        retried fetch is always a brand-new capture (new payload path, new
        row) - the bronze layer never overwrites, only appends.
        """
        from tick_vault.schemas import SCHEMAS  # lazy: pyarrow-backed

        capture_id = new_id("cap")
        body, should_persist = resolve_capture_body(payload_bytes, error_body)
        sha256 = sha256_hex(body)
        hint = row_count_hint(payload_bytes)

        payload_uri = None
        if should_persist:
            path = payload_path(self.root, capture_id, observed_at)
            write_payload(path, body)
            payload_uri = path

        now = dt.datetime.now(dt.timezone.utc)
        columns = [f.name for f in SCHEMAS[table]]
        row = build_capture_row(
            columns=columns,
            capture_id=capture_id,
            ingestion_run_id=ingestion_run_id,
            source_system=source_system,
            endpoint=endpoint,
            parser_version=parser_version,
            request_parameters_json=json.dumps(request_params, sort_keys=True, default=str),
            http_status=http_status,
            payload_uri=payload_uri,
            payload_sha256=sha256,
            row_count_hint=hint,
            observed_at=observed_at,
            committed_at=now,
            created_at=now,
            extra_cols=extra_cols,
        )
        append_rows(self.root, table, pd.DataFrame([row]))
        return CaptureRecord(
            capture_id=capture_id,
            payload_path=payload_uri,
            sha256=sha256,
            row_count_hint=hint,
        )

    def fetch_and_capture(
        self,
        transport: "Transport",
        *,
        url: str,
        table: str,
        endpoint: str,
        request_params: dict,
        observed_at: dt.datetime,
        ingestion_run_id: str,
        parser_version: str,
        extra_cols: dict | None = None,
        timeout: int = 30,
        source_system: str = "eodhd",
    ) -> tuple[bytes | None, CaptureRecord]:
        """`transport.get(url)`, then `record()` the result. Returns the
        raw payload bytes on a 2xx response (`None` on failure) alongside
        the `CaptureRecord` either way."""
        status, body = transport.get(url, timeout=timeout)
        is_success = 200 <= status < 300
        payload_bytes = body if is_success else None
        error_body = None if is_success else body
        record = self.record(
            table=table,
            endpoint=endpoint,
            request_params=request_params,
            http_status=status,
            payload_bytes=payload_bytes,
            error_body=error_body,
            observed_at=observed_at,
            ingestion_run_id=ingestion_run_id,
            parser_version=parser_version,
            extra_cols=extra_cols,
            source_system=source_system,
        )
        return payload_bytes, record


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

@runtime_checkable
class Transport(Protocol):
    def get(self, url: str, timeout: int) -> tuple[int, bytes]:
        ...


class FakeTransport:
    """Test double: pops canned `(status, body)` responses in order and
    records every URL it was asked to fetch."""

    def __init__(self, responses: list[tuple[int, bytes]]):
        self._responses = list(responses)
        self.requested_urls: list[str] = []

    def get(self, url: str, timeout: int = 30) -> tuple[int, bytes]:
        self.requested_urls.append(url)
        if not self._responses:
            raise AssertionError("FakeTransport: no more canned responses")
        return self._responses.pop(0)


def _urlopen(url: str, timeout: int):
    return urllib.request.urlopen(url, timeout=timeout)


class UrlLibTransport:
    """Live `Transport` over `urllib.request`.

    Retries up to `max_attempts` (default 3) total tries, only on 5xx
    responses, sleeping `5 * attempt` seconds between attempts (`attempt`
    is 1-based, so 5s then 10s by default) via an injectable `sleeper`
    (defaults to `time.sleep`) so tests never actually sleep. Any other
    status - including 429 - is returned immediately on the first attempt:
    429/backoff pacing across *many* calls is Task 7's `Budget` class's
    job, layered on top of a `Transport`, not this per-call retry loop's.

    `opener(url, timeout) -> response` is injectable for tests; it must
    return an object with `.read()` and either `.status` or `.getcode()`,
    or raise `urllib.error.HTTPError` (whose `.code`/`.read()` are used as
    the status/body instead). Defaults to `urllib.request.urlopen`.
    """

    def __init__(self, opener=None, sleeper=None, max_attempts: int = 3):
        self._opener = opener or _urlopen
        self._sleeper = sleeper or time.sleep
        self._max_attempts = max_attempts

    def get(self, url: str, timeout: int = 30) -> tuple[int, bytes]:
        status: int | None = None
        body: bytes = b""
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = self._opener(url, timeout)
                status = resp.status if hasattr(resp, "status") else resp.getcode()
                body = resp.read()
            except urllib.error.HTTPError as exc:
                status = exc.code
                body = exc.read()
            if status is not None and status >= 500 and attempt < self._max_attempts:
                self._sleeper(5 * attempt)
                continue
            return status, body
        return status, body

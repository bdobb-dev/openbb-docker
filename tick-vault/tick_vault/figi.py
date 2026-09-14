"""OpenFIGI resolution queue worker (task-6 brief): drains
`ops.openfigi_resolution_queue` rows in state `NEEDS_FIGI`, resolves them
against the OpenFIGI `/v3/mapping` bulk-mapping endpoint, and writes
`silver.figi_assignment_version` rows for anything matched (bronze-
capturing every request/response pair to `bronze.openfigi_mapping_capture`
via `tick_vault.capture.CaptureStore.fetch_and_capture_post`, added
alongside this module since `CaptureStore` previously only knew how to
capture GET-shaped vendor fetches).

Design seams (Plan-2 pattern, mirroring `tick_vault.master.MasterBuilder`):
`FigiWorker(transport, capture_store, root, api_key=None, *,
assignments_reader=None, queue_reader=None, queue_writer=None,
writer=None, sleeper=None, now=None)`. Every one of those keyword seams
has a real-Delta default (lazy-imported, matching this repo's no-pyarrow-
at-import-scope convention) and an injectable override so
`tests/test_figi_core.py` can drive `run_batch` end-to-end against plain
in-memory `pandas.DataFrame`s / a `FakeTransport` / a recording fake
sleeper, with no pyarrow/deltalake/pytest in the loop. The real-Delta
round trip (rows actually landing in a lake) is `tests/deferred/
test_figi.py`'s job.

Job-building priority (baseline §9.4's identifier order, brief-mandated):
CUSIP/ISIN (instrument-scoped identifiers - either already sitting on the
queue row itself, e.g. a CUSIP-reason queue row, or looked up for the
queue row's `instrument_id` via `silver.identifier_assignment_version`)
beat ticker+exchange (the queue row's own `id_value`/`exchange_code`,
used only when no CUSIP/ISIN is available). A queue row this module can't
build ANY job for (no CUSIP/ISIN and no ticker fallback either) is left
untouched - NEEDS_FIGI, not silently skipped-to-failure - since that is a
"we don't even have enough to try yet" state, distinct from "we tried and
OpenFIGI had nothing" (`NO_MATCH`).

OpenFIGI request/response shape (`POST https://api.openfigi.com/v3/
mapping`, body `{"jobs": [{"idType": ..., "idValue": ..., "exchCode":
...}, ...]}`, response a JSON array positionally aligned with `jobs`,
each entry either `{"data": [{...}, ...]}` (first element used - baseline
doesn't ask this module to disambiguate multiple candidates for one job)
or `{"error": "..."}`/no `data` key at all for a miss): `figi` ->
`FIGI_LEVEL_VENUE`, `compositeFIGI` -> `FIGI_LEVEL_COMPOSITE`,
`shareClassFigi` -> `FIGI_LEVEL_SHARE_CLASS`, each present-and-non-empty
field of the matched `data[0]` yielding its own `silver.
figi_assignment_version` row (so a job can yield 0-3 rows on a genuine
match; a job with no usable `data` yields 0 rows, ever - `NO_MATCH` is
recorded on the queue row, never a fabricated silver row).

Rate limiting (brief-mandated constants, `SLEEP_SECONDS_KEYLESS`/
`SLEEP_SECONDS_KEYED` below): an injectable `sleeper(seconds) -> None`
(default `time.sleep`) is called between successive `/v3/mapping`
batches (never after the last one) - `60 / 25` seconds keyless (OpenFIGI's
unauthenticated ~25-requests/minute budget) or `60 / 250` seconds with an
`api_key` (~250-requests/minute budget). This is a fixed inter-batch
pause, not a token-bucket/leaky-bucket budget across an entire process
lifetime (that class of cross-call pacing is `tick_vault.capture`'s
module docstring's explicitly-deferred Task 7 `Budget` territory) - it
only has to hold within one `run_batch` call's own batches.

Knowledge time (binding, per task brief): every `silver.
figi_assignment_version` row's `observed_at_ts`/`available_at_ts`/
`system_from_ts` are all stamped with the OpenFIGI capture's own
`observed_at` (the wall-clock time this worker made that POST, via the
injectable `now()` seam) - never a caller-supplied business date, since
OpenFIGI mapping responses carry no notion of "as of" beyond "now".
`system_to_ts` is always left `None` (this module never closes a row -
no append-only-with-exception story here, unlike `tick_vault.master`'s
symbol-change close/open pairs; a stale FIGI mapping is expected to be
superseded by a future task, not this one).

Queue mutation (binding, per task brief): `ops.openfigi_resolution_queue`
is ops/mutable state, NOT a bitemporal silver table - unlike
`tick_vault.master`'s append-only-plus-narrow-system-close convention for
`silver.identifier_assignment_version`, this module's `queue_writer`
plainly overwrites the entire table with its updated `pandas.DataFrame`
(read -> mutate the touched rows in memory -> write back the whole
table) via `deltalake.write_deltalake(..., mode="overwrite")`. This is
intentionally simpler than a targeted `DeltaTable.update` (`tick_vault.
master._default_system_closer`'s approach for silver): the queue has no
external readers relying on point-in-time semantics (`tick_vault.
reference_queries` never touches it), so a full overwrite is the
pragmatic choice, not a design compromise - flagged here per the task
brief's explicit ask to document this decision.

API-key handling (binding, per task brief): `api_key`, when present, is
sent ONLY via the `X-OPENFIGI-APIKEY` request header
(`build_headers`) - it is NEVER included in the `request_params` dict
this module hands to `CaptureStore.fetch_and_capture_post` (that dict is
built by `build_request_params`, which only ever holds the `{"jobs":
[...]}` body content), so the bronze capture row's
`request_parameters_json` is key-free by construction, not by a
redaction pass after the fact.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import time

import pandas as pd

from tick_vault.ids import new_id

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

OPENFIGI_MAPPING_URL = "https://api.openfigi.com/v3/mapping"
APIKEY_HEADER = "X-OPENFIGI-APIKEY"

BATCH_LIMIT = 100  # OpenFIGI's own cap on jobs-per-request.

# Brief-mandated rate-limit constants: fixed inter-batch sleep, derived
# from OpenFIGI's documented per-minute request budgets (25/minute
# keyless, 250/minute with an API key) as `60 seconds / requests-per-
# minute`.
RATE_LIMIT_KEYLESS_REQUESTS_PER_MINUTE = 25
RATE_LIMIT_KEYED_REQUESTS_PER_MINUTE = 250
SLEEP_SECONDS_KEYLESS = 60 / RATE_LIMIT_KEYLESS_REQUESTS_PER_MINUTE  # 2.4s
SLEEP_SECONDS_KEYED = 60 / RATE_LIMIT_KEYED_REQUESTS_PER_MINUTE  # 0.24s

FIGI_LEVEL_VENUE = "VENUE"
FIGI_LEVEL_COMPOSITE = "COMPOSITE"
FIGI_LEVEL_SHARE_CLASS = "SHARE_CLASS"

MATCH_STATUS_MATCH = "MATCH"
CONFIDENCE_MATCH = decimal.Decimal("0.9500")
SOURCE_SYSTEM = "OPENFIGI"

QUEUE_STATUS_NEEDS_FIGI = "NEEDS_FIGI"
QUEUE_STATUS_RESOLVED = "RESOLVED"
QUEUE_STATUS_NO_MATCH = "NO_MATCH"

NAMESPACE_CUSIP = "CUSIP"
NAMESPACE_ISIN = "ISIN"

IDTYPE_CUSIP = "ID_CUSIP"
IDTYPE_ISIN = "ID_ISIN"
IDTYPE_TICKER = "TICKER"

# Response field (on a matched job's `data[0]`) -> figi_level, in the
# priority order rows are built/returned (VENUE first, so the VENUE-level
# row's id is preferred as `resolved_figi_assignment_version_id` when a
# job matches at more than one level).
_FIGI_RESPONSE_FIELDS = (
    ("figi", FIGI_LEVEL_VENUE),
    ("compositeFIGI", FIGI_LEVEL_COMPOSITE),
    ("shareClassFigi", FIGI_LEVEL_SHARE_CLASS),
)

# Column list, transcribed from tick_vault.schemas._SILVER_FIGI_ASSIGNMENT_VERSION
# (this module cannot import tick_vault.schemas at module scope - bare
# pyarrow import - see tick_vault.master's module docstring for the same
# convention/rationale).
_FIGI_COLUMNS = [
    "figi_assignment_version_id", "instrument_id", "listing_id",
    "figi_level", "figi", "effective_from_ts", "effective_to_ts",
    "observed_at_ts", "available_at_ts", "system_from_ts", "system_to_ts",
    "source_system", "source_capture_id", "match_status", "confidence",
]

# Column list, transcribed from
# tick_vault.schemas._OPS_OPENFIGI_RESOLUTION_QUEUE.
_QUEUE_COLUMNS = [
    "queue_id", "instrument_id", "listing_id", "id_namespace", "id_value",
    "exchange_code", "status", "attempts", "priority", "last_error",
    "created_at_ts", "updated_at_ts", "resolved_figi_assignment_version_id",
]

# Column list, transcribed from
# tick_vault.schemas._SILVER_IDENTIFIER_ASSIGNMENT_VERSION (only the
# columns this module actually reads).
_ASSIGNMENT_LOOKUP_COLUMNS = [
    "instrument_id", "id_namespace", "id_value", "system_to_ts",
]


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------

def _is_missing(value) -> bool:
    """`True` for `None`, NaN/NaT, and the empty string."""
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return value == ""


def _to_utc_ts(value):
    if _is_missing(value):
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)
    if hasattr(value, "to_pydatetime"):
        return _to_utc_ts(value.to_pydatetime())
    return value


def _row(columns: list, **overrides) -> dict:
    row = {c: None for c in columns}
    for key, value in overrides.items():
        if key in row:
            row[key] = value
    return row


def _empty(columns: list) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def chunk(items: list, size: int) -> "list[list]":
    """Split `items` into consecutive sub-lists of at most `size` (the
    last chunk may be shorter). Used to cap OpenFIGI mapping requests at
    `BATCH_LIMIT` jobs each."""
    return [items[i:i + size] for i in range(0, len(items), size)]


# ---------------------------------------------------------------------------
# pure compute: job building
# ---------------------------------------------------------------------------

def lookup_issue_identifier(assignments_df: "pd.DataFrame | None", instrument_id):
    """Among currently-open (`system_to_ts IS NULL`) rows of an
    assignments frame shaped like `_ASSIGNMENT_LOOKUP_COLUMNS`, return
    `(idType, idValue)` for `instrument_id`'s CUSIP if one exists, else
    its ISIN, else `None`. CUSIP is preferred over ISIN per the task
    brief's stated identifier order."""
    if assignments_df is None or assignments_df.empty or _is_missing(instrument_id):
        return None
    if "system_to_ts" in assignments_df.columns:
        current = assignments_df[assignments_df["system_to_ts"].isna()]
    else:
        current = assignments_df
    scoped = current[current["instrument_id"] == instrument_id]
    if scoped.empty:
        return None
    for namespace, idtype in ((NAMESPACE_CUSIP, IDTYPE_CUSIP), (NAMESPACE_ISIN, IDTYPE_ISIN)):
        matches = scoped[scoped["id_namespace"] == namespace]
        if not matches.empty:
            value = matches.iloc[0]["id_value"]
            if not _is_missing(value):
                return idtype, value
    return None


def build_mapping_job(queue_row: dict, assignments_df: "pd.DataFrame | None" = None) -> "dict | None":
    """One `queue_row` (shaped like `_QUEUE_COLUMNS`) -> one OpenFIGI
    mapping job dict (`{"idType": ..., "idValue": ..., "exchCode": ...}`,
    `exchCode` present only for the ticker fallback and only when the
    queue row carries one), or `None` if nothing usable is available yet.

    Priority (baseline §9.4 / task brief, binding):
      1. The queue row's own `id_namespace`/`id_value`, if that namespace
         is already `CUSIP`/`ISIN` (e.g. a queue row raised directly
         against an identifier rather than a symbol code).
      2. A CUSIP/ISIN found for the row's `instrument_id` via
         `lookup_issue_identifier` against `assignments_df`.
      3. Ticker + exchange fallback: the row's own `id_value` (whatever
         namespace it's actually in - `EODHD_SYMBOL`/`TICKER`) as
         `idType: "TICKER"`, plus `exchCode` from `exchange_code` if set.
      4. `None` if the row has no `id_value` at all to fall back to.
    """
    namespace = queue_row.get("id_namespace")
    id_value = queue_row.get("id_value")

    if namespace == NAMESPACE_CUSIP and not _is_missing(id_value):
        return {"idType": IDTYPE_CUSIP, "idValue": id_value}
    if namespace == NAMESPACE_ISIN and not _is_missing(id_value):
        return {"idType": IDTYPE_ISIN, "idValue": id_value}

    found = lookup_issue_identifier(assignments_df, queue_row.get("instrument_id"))
    if found is not None:
        idtype, value = found
        return {"idType": idtype, "idValue": value}

    if not _is_missing(id_value):
        job = {"idType": IDTYPE_TICKER, "idValue": id_value}
        exch = queue_row.get("exchange_code")
        if not _is_missing(exch):
            job["exchCode"] = exch
        return job

    return None


def build_request_params(jobs: "list[dict]") -> dict:
    """The exact, key-free dict captured to bronze's
    `request_parameters_json` for a batch of `jobs` - deliberately holds
    NOTHING but the request body content, so an API key (which only ever
    travels in `build_headers`' output, never here) cannot leak into a
    captured/persisted request-params record."""
    return {"jobs": jobs}


def build_headers(api_key: "str | None") -> dict:
    """`Content-Type: application/json` always; `X-OPENFIGI-APIKEY` only
    when `api_key` is truthy - this is the ONLY place an api_key is ever
    placed, and it is placed in headers, never in a captured params dict
    (see `build_request_params`)."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers[APIKEY_HEADER] = api_key
    return headers


def sleep_seconds_for(api_key: "str | None") -> float:
    return SLEEP_SECONDS_KEYED if api_key else SLEEP_SECONDS_KEYLESS


# ---------------------------------------------------------------------------
# pure compute: response -> silver rows / queue updates
# ---------------------------------------------------------------------------

def matched_data(entry) -> "dict | None":
    """One OpenFIGI response entry (aligned by position with the request's
    `jobs`) -> its first `data[0]` dict if the job matched, else `None`
    (covers both an explicit `{"error": ...}` entry and one with an
    empty/missing `data` list - both are "no match", never fabricated)."""
    if not isinstance(entry, dict):
        return None
    data = entry.get("data")
    if isinstance(data, list) and data:
        return data[0]
    return None


def build_figi_rows(
    data_entry: dict,
    queue_row: dict,
    capture_id: str,
    observed_at,
) -> "list[dict]":
    """A matched `data[0]` dict -> 0-3 `silver.figi_assignment_version`
    rows (one per present-and-non-empty `figi`/`compositeFIGI`/
    `shareClassFigi` field - see `_FIGI_RESPONSE_FIELDS`), each carrying
    `queue_row`'s `instrument_id`/`listing_id`, `match_status="MATCH"`,
    `confidence=CONFIDENCE_MATCH` (a `decimal.Decimal`, never a float -
    the schema column is `decimal128(5,4)`), and knowledge time
    (`observed_at_ts`/`available_at_ts`/`system_from_ts`) all stamped
    with `observed_at` (the OpenFIGI capture's own observed time).
    """
    observed_at_ts = _to_utc_ts(observed_at)
    rows = []
    for field, level in _FIGI_RESPONSE_FIELDS:
        value = data_entry.get(field)
        if _is_missing(value):
            continue
        rows.append(_row(
            _FIGI_COLUMNS,
            figi_assignment_version_id=new_id("wrk"),
            instrument_id=queue_row.get("instrument_id"),
            listing_id=queue_row.get("listing_id"),
            figi_level=level,
            figi=value,
            observed_at_ts=observed_at_ts,
            available_at_ts=observed_at_ts,
            system_from_ts=observed_at_ts,
            source_system=SOURCE_SYSTEM,
            source_capture_id=capture_id,
            match_status=MATCH_STATUS_MATCH,
            confidence=CONFIDENCE_MATCH,
        ))
    return rows


def build_queue_update(
    queue_row: dict,
    figi_rows: "list[dict]",
    now,
    error_message: "str | None" = None,
) -> dict:
    """`queue_row` -> its updated dict after one resolution attempt:
    `RESOLVED` (with `resolved_figi_assignment_version_id` pointing at
    the first - i.e. highest-priority, VENUE-first - row in `figi_rows`)
    if `figi_rows` is non-empty, else `NO_MATCH` (kept visible - never
    deleted, never silently left as `NEEDS_FIGI` - with `last_error`
    explaining why, if known). `attempts` is always incremented;
    `updated_at_ts` is always stamped with `now`."""
    updated = dict(queue_row)
    updated["updated_at_ts"] = _to_utc_ts(now)
    updated["attempts"] = (queue_row.get("attempts") or 0) + 1
    if figi_rows:
        updated["status"] = QUEUE_STATUS_RESOLVED
        updated["resolved_figi_assignment_version_id"] = figi_rows[0]["figi_assignment_version_id"]
        updated["last_error"] = None
    else:
        updated["status"] = QUEUE_STATUS_NO_MATCH
        updated["last_error"] = error_message or "OpenFIGI: no match"
    return updated


# ---------------------------------------------------------------------------
# Delta-backed defaults (lazy pyarrow/deltalake imports)
# ---------------------------------------------------------------------------

def _default_assignments_reader(root: str) -> pd.DataFrame:
    from deltalake import DeltaTable

    return DeltaTable(f"{root}/silver/identifier_assignment_version").to_pandas()


def _default_queue_reader(root: str) -> pd.DataFrame:
    from deltalake import DeltaTable

    return DeltaTable(f"{root}/ops/openfigi_resolution_queue").to_pandas()


def _default_queue_writer(root: str, table: str, df: pd.DataFrame) -> None:
    """Plain overwrite of the whole `ops.openfigi_resolution_queue` table
    (see module docstring's "Queue mutation" section for why this is the
    deliberate, documented choice here, unlike silver's append-only-plus-
    narrow-system-close convention)."""
    import pyarrow as pa
    from deltalake import write_deltalake

    from tick_vault.schemas import SCHEMAS

    layer, name = table.split(".", 1)
    path = f"{root}/{layer}/{name}"
    schema = SCHEMAS[table]
    clean = df.astype(object).where(df.notna(), None)
    arrow_table = pa.Table.from_pylist(clean.to_dict("records"), schema=schema)
    write_deltalake(path, arrow_table, mode="overwrite", schema_mode="overwrite")


def _default_writer(root: str, table: str, df: pd.DataFrame) -> None:
    from tick_vault.capture import append_rows

    append_rows(root, table, df)


def _default_sleeper(seconds: float) -> None:
    time.sleep(seconds)


def _default_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------------------
# FigiWorker
# ---------------------------------------------------------------------------

class FigiWorker:
    """Drains `ops.openfigi_resolution_queue` `NEEDS_FIGI` rows, resolves
    them via OpenFIGI's bulk `/v3/mapping` endpoint, and writes
    `silver.figi_assignment_version` rows for matches (plus a bronze
    `openfigi_mapping_capture` row for every batch, matched or not).

    All Delta-backed I/O is injectable (Plan-2 pattern, mirroring
    `tick_vault.master.MasterBuilder`): `assignments_reader(root) ->
    DataFrame`, `queue_reader(root) -> DataFrame`, `queue_writer(root,
    table, df)`, `writer(root, table, df)` (silver writes; bronze writes
    go through `capture_store.fetch_and_capture_post`, not this seam),
    `sleeper(seconds)`, and `now()` all default to real Delta/stdlib
    implementations but can be swapped for fakes in tests.
    """

    def __init__(
        self,
        transport,
        capture_store,
        root: str,
        api_key: "str | None" = None,
        *,
        assignments_reader=None,
        queue_reader=None,
        queue_writer=None,
        writer=None,
        sleeper=None,
        now=None,
        ingestion_run_id: str = "run_figi",
        parser_version: str = "figi-v1",
    ):
        self.transport = transport
        self.capture_store = capture_store
        self.root = root
        self.api_key = api_key
        self._assignments_reader = assignments_reader or _default_assignments_reader
        self._queue_reader = queue_reader or _default_queue_reader
        self._queue_writer = queue_writer or _default_queue_writer
        self._writer = writer or _default_writer
        self._sleeper = sleeper or _default_sleeper
        self._now = now or _default_now
        self.ingestion_run_id = ingestion_run_id
        self.parser_version = parser_version

    def run_batch(self, limit: int = 100) -> dict:
        """Drain up to `limit` `NEEDS_FIGI` queue rows, resolve as many as
        can be turned into a job (see `build_mapping_job`), POST them to
        OpenFIGI in sub-batches of at most `BATCH_LIMIT` jobs each, write
        matched rows to `silver.figi_assignment_version`, and overwrite
        `ops.openfigi_resolution_queue` with every touched row's new
        state (`RESOLVED`/`NO_MATCH`). Rows this module can't even build
        a job for are left untouched (still `NEEDS_FIGI`).

        Returns a small summary dict: `{"considered", "jobs_built",
        "matched", "no_match", "batches"}`.
        """
        queue_df = self._queue_reader(self.root)
        if queue_df is None or queue_df.empty:
            return {"considered": 0, "jobs_built": 0, "matched": 0, "no_match": 0, "batches": 0}

        needs_figi = queue_df[queue_df["status"] == QUEUE_STATUS_NEEDS_FIGI].head(limit)
        if needs_figi.empty:
            return {"considered": 0, "jobs_built": 0, "matched": 0, "no_match": 0, "batches": 0}

        assignments_df = self._assignments_reader(self.root)

        pairs = []  # list of (queue_row_dict, job_dict)
        for _, row in needs_figi.iterrows():
            queue_row = row.to_dict()
            job = build_mapping_job(queue_row, assignments_df)
            if job is not None:
                pairs.append((queue_row, job))

        matched_count = 0
        no_match_count = 0
        batch_count = 0
        all_silver_rows: "list[dict]" = []
        updates_by_id: "dict[str, dict]" = {}

        batches = chunk(pairs, BATCH_LIMIT)
        for i, batch_pairs in enumerate(batches):
            batch_count += 1
            jobs = [job for _, job in batch_pairs]
            params = build_request_params(jobs)
            headers = build_headers(self.api_key)
            body = json.dumps(params).encode("utf-8")
            observed_at = self._now()

            payload_bytes, record = self.capture_store.fetch_and_capture_post(
                self.transport,
                url=OPENFIGI_MAPPING_URL,
                body=body,
                headers=headers,
                table="bronze.openfigi_mapping_capture",
                endpoint="/v3/mapping",
                request_params=params,
                observed_at=observed_at,
                ingestion_run_id=self.ingestion_run_id,
                parser_version=self.parser_version,
                source_system=SOURCE_SYSTEM,
            )

            if payload_bytes is not None:
                try:
                    response_list = json.loads(payload_bytes)
                except (ValueError, UnicodeDecodeError):
                    response_list = []
                for (queue_row, _job), entry in zip(batch_pairs, response_list):
                    data = matched_data(entry)
                    if data is not None:
                        figi_rows = build_figi_rows(data, queue_row, record.capture_id, observed_at)
                    else:
                        figi_rows = []
                    error_message = None
                    if not figi_rows and isinstance(entry, dict):
                        error_message = entry.get("error")
                    updated = build_queue_update(queue_row, figi_rows, observed_at, error_message)
                    updates_by_id[updated["queue_id"]] = updated
                    all_silver_rows.extend(figi_rows)
                    if figi_rows:
                        matched_count += 1
                    else:
                        no_match_count += 1
            # A failed HTTP call (payload_bytes is None) leaves this
            # batch's queue rows untouched (still NEEDS_FIGI) - a
            # transport/HTTP failure isn't evidence of "no match", so it
            # is never recorded as one.

            if i < len(batches) - 1:
                self._sleeper(sleep_seconds_for(self.api_key))

        if all_silver_rows:
            self._writer(
                self.root,
                "silver.figi_assignment_version",
                pd.DataFrame(all_silver_rows, columns=_FIGI_COLUMNS),
            )

        if updates_by_id:
            updated_queue_df = queue_df.copy()
            for idx in updated_queue_df.index:
                qid = updated_queue_df.at[idx, "queue_id"]
                if qid in updates_by_id:
                    for col, value in updates_by_id[qid].items():
                        if col in updated_queue_df.columns:
                            updated_queue_df.at[idx, col] = value
            self._queue_writer(self.root, "ops.openfigi_resolution_queue", updated_queue_df)

        return {
            "considered": len(needs_figi),
            "jobs_built": len(pairs),
            "matched": matched_count,
            "no_match": no_match_count,
            "batches": batch_count,
        }

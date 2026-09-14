"""ArcticDB connection helpers shared by the provider and the OBBject accessor."""

import os
import threading
from typing import Any


def default_uri() -> str:
    """Default LMDB store under the OpenBB home directory."""
    home = os.getenv("OPENBB_HOME") or os.path.expanduser("~/.openbb_platform")
    return f"lmdb://{os.path.join(home, 'arcticdb')}"


def redact_uri(uri: str) -> str:
    """Replace the `access`/`secret` query values in an ArcticDB URI with '***'.

    An S3 URI carries the MinIO credentials in its query string (see
    `s3_uri_from_env`), including the root/admin pair when that's what's
    configured. `provider="arcticdb"` is served by openbb-api, which
    `docker-compose.yml` documents may be published to the public internet
    via Tailscale Funnel -- so the raw URI must never reach an exception
    message or a log line. LMDB URIs have no query string and pass through
    unchanged.
    """
    # pylint: disable=import-outside-toplevel
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(uri)
    if not parts.query:
        return uri
    redacted = [
        (k, "***" if k in ("access", "secret") else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(redacted)))


_S3_REQUIRED = (
    "ARCTICDB_S3_ENDPOINT",
    "ARCTICDB_S3_BUCKET",
    "ARCTICDB_S3_ACCESS",
    "ARCTICDB_S3_SECRET",
)


def s3_uri_from_env(env: Any = None) -> str | None:
    """Assemble an ArcticDB S3 URI from ARCTICDB_S3_* parts.

    Returns None unless every required part is present, so a partially
    configured environment falls through to the LMDB default rather than
    producing a URI that fails deep inside ArcticDB later.

    Shape matters: host and bucket are separated by ':' and the port is a
    QUERY parameter. 'host:port:bucket' is not valid ArcticDB syntax.

    Values are stripped, matching tick_lab.config.from_env on the laptop
    side -- these are the same ARCTICDB_S3_* names read from the same
    minio.env, and Docker Compose's env_file parser does NOT strip trailing
    whitespace. Without stripping here, a stray trailing space in
    ARCTICDB_S3_SECRET (invisible in most editors) would work for tick-lab
    but build `secret=hunter2%20` in the container and fail auth deep
    inside ArcticDB with no hint that whitespace was the problem.
    """
    # pylint: disable=import-outside-toplevel
    from urllib.parse import quote

    e = os.environ if env is None else env
    if any(not str(e.get(k, "")).strip() for k in _S3_REQUIRED):
        return None

    endpoint = str(e["ARCTICDB_S3_ENDPOINT"]).strip()
    bucket = str(e["ARCTICDB_S3_BUCKET"]).strip()
    access = str(e["ARCTICDB_S3_ACCESS"]).strip()
    secret = str(e["ARCTICDB_S3_SECRET"]).strip()

    port_raw = str(e.get("ARCTICDB_S3_PORT") or "9000").strip()
    if not port_raw.isdigit():
        raise ValueError(f"ARCTICDB_S3_PORT must be a number, got {port_raw!r}")

    secure_raw = str(e.get("ARCTICDB_S3_SECURE") or "true").strip().lower()
    if secure_raw not in ("true", "false"):
        raise ValueError(
            f"ARCTICDB_S3_SECURE must be 'true' or 'false', got {secure_raw!r}"
        )

    scheme = "s3s" if secure_raw == "true" else "s3"
    return (
        f"{scheme}://{endpoint}:{bucket}"
        f"?port={port_raw}"
        f"&access={quote(access, safe='')}"
        f"&secret={quote(secret, safe='')}"
        f"&use_virtual_addressing=false"
    )


def resolve_config(
    uri: str | None = None,
    library: str | None = None,
    credentials: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Resolve the ArcticDB URI and library name.

    Precedence: explicit arg > OpenBB credential > ARCTICDB_URI env var >
    ARCTICDB_S3_* parts > default.
    """
    creds = credentials or {}
    uri = (
        uri
        or creds.get("arcticdb_uri")
        or os.getenv("ARCTICDB_URI")
        or s3_uri_from_env()
        or default_uri()
    )
    library = (
        library
        or creds.get("arcticdb_library")
        or os.getenv("ARCTICDB_LIBRARY")
        or "openbb"
    )
    return uri, library


def normalize_index(df):
    """Coerce a date/`datetime.date` column or index into a sorted DatetimeIndex.

    ArcticDB cannot normalize `datetime.date` values and stores time series most
    usefully with a DatetimeIndex (enables `date_range` filtering on read). Frames
    without any date-like index are returned unchanged.
    """
    # pylint: disable=import-outside-toplevel
    from pandas import DatetimeIndex, RangeIndex, to_datetime
    from pandas.api.types import is_numeric_dtype

    if isinstance(df.index, DatetimeIndex):
        return df.sort_index()

    # Explicit 'date' column wins.
    if "date" in df.columns:
        df = df.set_index("date")
        try:
            df.index = to_datetime(df.index)
            return df.sort_index()
        except (ValueError, TypeError):
            return df

    # Only coerce a genuinely date-like index. A numeric / RangeIndex is
    # positional (e.g. screener rows) — to_datetime would turn 0,1,2 into bogus
    # 1970 timestamps, so leave it alone.
    if not isinstance(df.index, RangeIndex) and not is_numeric_dtype(df.index):
        try:
            df.index = to_datetime(df.index)
            return df.sort_index()
        except (ValueError, TypeError):
            return df
    return df


def parse_temporal(v: Any):
    """Coerce str/date/datetime into a date or datetime, preserving the time-of-day.

    A string with a time component (`2026-06-01 09:30`) becomes a datetime; a
    date-only string (`2026-06-01`) becomes a date. date/datetime objects pass
    through unchanged. This lets start/end accept BOTH dates and datetimes.
    """
    # pylint: disable=import-outside-toplevel
    from datetime import date as dateType, datetime

    if v is None or isinstance(v, datetime):
        return v
    if isinstance(v, dateType):
        return v
    if isinstance(v, str):
        from dateutil import parser

        tail = v.split("T", 1)[1] if "T" in v else ""
        has_time = (":" in v) or any(ch.isdigit() for ch in tail)
        dt = parser.parse(v)
        return dt if has_time else dt.date()
    return v


def to_bounds(start: Any, end: Any):
    """Return (start_ts, end_ts) pandas Timestamps for an ArcticDB `date_range`.

    A pure-date `end` is widened to end-of-day so the whole day is inclusive
    (matters for intraday/tick data); a datetime `end` is used exactly.
    """
    # pylint: disable=import-outside-toplevel
    from datetime import date as dateType, datetime

    from pandas import Timedelta, Timestamp

    s = parse_temporal(start)
    e = parse_temporal(end)
    start_ts = None if s is None else Timestamp(s)
    if e is None:
        end_ts = None
    else:
        end_ts = Timestamp(e)
        if isinstance(e, dateType) and not isinstance(e, datetime):
            end_ts = end_ts.normalize() + Timedelta(days=1) - Timedelta(nanoseconds=1)
    return start_ts, end_ts


# Cache open Arctic connections keyed by URI so a session reuses them.
# Reads run under asyncio.to_thread, so guard construction with a lock: for
# LMDB, two Arctic handles to the same path in one process is exactly what the
# cache exists to prevent, and a check-then-act race could still create two.
_arctic_cache: dict = {}  # uri -> Arctic
_arctic_cache_lock = threading.Lock()


def get_library(uri: str, library: str, create_if_missing: bool = True):
    """Open (and optionally create) an ArcticDB library. Connections are cached by URI."""
    # pylint: disable=import-outside-toplevel
    from arcticdb import Arctic

    # LMDB needs the target directory to exist before connecting.
    if uri.startswith("lmdb://"):
        path = uri[len("lmdb://") :]
        if path:
            os.makedirs(path, exist_ok=True)

    with _arctic_cache_lock:
        ac = _arctic_cache.get(uri)
        if ac is None:
            ac = _arctic_cache[uri] = Arctic(uri)
    if not create_if_missing and not ac.has_library(library):
        raise FileNotFoundError(
            f"ArcticDB library '{library}' does not exist at '{redact_uri(uri)}'. "
            "Write some data first with `result.arcticdb.write(...)`."
        )
    return ac.get_library(library, create_if_missing=create_if_missing)

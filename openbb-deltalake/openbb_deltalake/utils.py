# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Delta Lake connection helpers shared by the provider and the OBBject accessor."""

import os
from typing import Any


_S3_REQUIRED = (
    "DELTA_S3_ENDPOINT",
    "DELTA_S3_BUCKET",
    "DELTA_S3_ACCESS",
    "DELTA_S3_SECRET",
)


def default_base() -> str:
    """Default local Delta store under the OpenBB home directory."""
    home = os.getenv("OPENBB_HOME") or os.path.expanduser("~/.openbb_platform")
    return os.path.join(home, "deltalake")


def s3_options_from_env(env: Any = None) -> tuple[str, dict[str, str]] | None:
    """(base_uri, storage_options) assembled from DELTA_S3_* parts.

    Returns None unless every required part is present, so a partially
    configured environment falls through to the local default. Values are
    stripped: Docker Compose's env_file parser does not strip trailing
    whitespace, and an invisible trailing space in DELTA_S3_SECRET would
    fail auth deep inside delta-rs with no hint.
    """
    e = os.environ if env is None else env
    if any(not str(e.get(k, "")).strip() for k in _S3_REQUIRED):
        return None

    endpoint = str(e["DELTA_S3_ENDPOINT"]).strip()
    bucket = str(e["DELTA_S3_BUCKET"]).strip()
    access = str(e["DELTA_S3_ACCESS"]).strip()
    secret = str(e["DELTA_S3_SECRET"]).strip()

    port_raw = str(e.get("DELTA_S3_PORT") or "9000").strip()
    if not port_raw.isdigit():
        raise ValueError(f"DELTA_S3_PORT must be a number, got {port_raw!r}")

    secure_raw = str(e.get("DELTA_S3_SECURE") or "true").strip().lower()
    if secure_raw not in ("true", "false"):
        raise ValueError(
            f"DELTA_S3_SECURE must be 'true' or 'false', got {secure_raw!r}"
        )

    scheme = "https" if secure_raw == "true" else "http"
    options = {
        "aws_endpoint": f"{scheme}://{endpoint}:{port_raw}",
        "aws_access_key_id": access,
        "aws_secret_access_key": secret,
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        # Commit atomicity on S3 without a lock service. MinIO supports
        # conditional puts; verified by the Task 6 integration test.
        "aws_conditional_put": "etag",
    }
    if scheme == "http":
        options["aws_allow_http"] = "true"
    return f"s3://{bucket}", options


def resolve_config(
    uri: str | None = None,
    library: str | None = None,
    credentials: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, str]]:
    """Resolve (base, library, storage_options).

    Precedence: explicit arg > OpenBB credential > DELTA_URI env var >
    DELTA_S3_* parts > local default. An s3:// base picks up storage
    options from DELTA_S3_* when they are set; a local path needs none.
    """
    creds = credentials or {}
    library = (
        library
        or creds.get("deltalake_library")
        or os.getenv("DELTA_LIBRARY")
        or "openbb"
    )
    explicit = uri or creds.get("deltalake_uri") or os.getenv("DELTA_URI")
    if explicit:
        explicit = str(explicit).rstrip("/")
        # Only evaluate S3 env if explicit is s3://; local paths need no S3 options.
        opts = {}
        if explicit.startswith("s3://"):
            s3 = s3_options_from_env()
            opts = s3[1] if s3 else {}
        return explicit, library, opts
    # No explicit arg; try S3 env, then fall back to local default.
    s3 = s3_options_from_env()
    if s3:
        return s3[0], library, s3[1]
    return default_base(), library, {}


def table_path(base: str, library: str, symbol: str) -> str:
    """Path of one symbol's Delta table. '/' join works for s3:// and POSIX paths."""
    return f"{base.rstrip('/')}/{library}/{symbol}"


def fs_and_root(base: str, options: dict[str, str]):
    """(pyarrow FileSystem, root path without scheme) for listing and deleting.

    delta-rs handles reads/writes itself; only list_symbols/delete need a
    filesystem handle, and pyarrow.fs covers both local and MinIO.
    """
    # pylint: disable=import-outside-toplevel
    from pyarrow import fs as pafs

    if base.startswith("s3://"):
        s3fs = pafs.S3FileSystem(
            access_key=options.get("aws_access_key_id"),
            secret_key=options.get("aws_secret_access_key"),
            endpoint_override=options.get("aws_endpoint"),
            region=options.get("aws_region", "us-east-1"),
            force_virtual_addressing=False,
        )
        return s3fs, base[len("s3://") :]
    return pafs.LocalFileSystem(), base


def normalize_index(df):
    """Coerce a date/`datetime.date` column or index into a sorted DatetimeIndex.

    Delta Lake cannot normalize `datetime.date` values and stores time series most
    usefully with a DatetimeIndex (enables date-range filtering on read). Frames
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
    """Return (start_ts, end_ts) pandas Timestamps for a Delta Lake date-range read.

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

"""S3 connection settings, read from the same ARCTICDB_S3_* names the container uses.

Keeping one convention on both sides means `minio.env` is the single source of
truth: the laptop and the Platform container cannot drift apart.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

REQUIRED = (
    "ARCTICDB_S3_ENDPOINT",
    "ARCTICDB_S3_BUCKET",
    "ARCTICDB_S3_ACCESS",
    "ARCTICDB_S3_SECRET",
)


class ConfigError(RuntimeError):
    """Raised when the environment cannot produce a usable connection."""


def _dotenv_candidates() -> tuple[Path, Path]:
    """Where a `.env` may live: the current directory, then the tick-lab
    package root (the directory `.env.example` and `pyproject.toml` live in).

    Checking the CWD first lets a reader who runs `tick-lab` from elsewhere
    override with a local `.env`; the package root is the fallback so the
    README's `cp .env.example .env` (run from `tick-lab/`) is found even
    when the command is later invoked from a different directory.
    """
    package_root = Path(__file__).resolve().parent.parent
    return Path.cwd() / ".env", package_root / ".env"


def _parse_dotenv(text: str) -> dict[str, str]:
    """A minimal `.env` parser: comments, blank lines, an `export ` prefix,
    and single/double-quoted values. No interpolation, no multi-line values
    -- .env.example in this repo needs none of that, and Docker Compose's
    own env_file parser doesn't strip inline comments either, so this
    doesn't either (a trailing `# comment` is part of the value, same as
    `credentials.env`).
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            quote_char = value[0]
            inner = value[1:-1]
            if quote_char == '"':
                inner = inner.replace('\\"', '"').replace("\\\\", "\\")
            value = inner
        if key:
            values[key] = value
    return values


def load_dotenv(target: MutableMapping[str, str] | None = None) -> Path | None:
    """Load the first `.env` found (see `_dotenv_candidates`) into `target`
    (`os.environ` by default), without overriding a key already set there --
    an explicitly exported variable always wins over the file.

    Returns the path that was loaded, or None if no `.env` was found in
    either location.
    """
    t = os.environ if target is None else target
    for candidate in _dotenv_candidates():
        if candidate.is_file():
            for key, value in _parse_dotenv(candidate.read_text()).items():
                t.setdefault(key, value)
            return candidate
    return None


@dataclass(frozen=True)
class S3Config:
    endpoint: str
    bucket: str
    access: str
    secret: str
    port: int = 9000
    secure: bool = True

    def __repr__(self) -> str:
        # A traceback from a CLI lands in terminal scrollback and pasted bug
        # reports; the secret has no business being in either.
        return (
            f"S3Config(endpoint={self.endpoint!r}, bucket={self.bucket!r}, "
            f"access={self.access!r}, secret='***', port={self.port}, "
            f"secure={self.secure})"
        )

    @property
    def uri(self) -> str:
        """The ArcticDB connection URI.

        Host and bucket are separated by ':' and the port is a QUERY parameter.
        'host:port:bucket' looks plausible and is not valid ArcticDB syntax.
        """
        scheme = "s3s" if self.secure else "s3"
        return (
            f"{scheme}://{self.endpoint}:{self.bucket}"
            f"?port={self.port}"
            f"&access={quote(self.access, safe='')}"
            f"&secret={quote(self.secret, safe='')}"
            f"&use_virtual_addressing=false"
        )


def from_env(env: Mapping[str, str] | None = None) -> S3Config:
    """Build an S3Config from ARCTICDB_S3_* variables.

    When `env` is not given (the normal CLI path), a `.env` file is loaded
    into the real process environment first via `load_dotenv` -- so the
    README's `cp .env.example .env` flow actually takes effect -- before
    falling back to `os.environ`.
    """
    dotenv_path: Path | None = None
    if env is None:
        dotenv_path = load_dotenv()
        e = os.environ
    else:
        e = env

    missing = [k for k in REQUIRED if not str(e.get(k, "")).strip()]
    if missing:
        if env is not None:
            # Caller supplied its own mapping (tests, embedding) -- the
            # .env file is not part of that story.
            hint = " — copy tick-lab/.env.example to .env and fill it in"
        elif dotenv_path is None:
            hint = (
                " — no .env file found (looked in the current directory and "
                "tick-lab/) — copy tick-lab/.env.example to .env and fill it in"
            )
        else:
            hint = (
                f" — {dotenv_path} was found but does not set all of these — "
                "fill in the missing value(s) there"
            )
        raise ConfigError(
            "missing or empty required environment variable(s): "
            + ", ".join(missing)
            + hint
        )

    port_raw = str(e.get("ARCTICDB_S3_PORT") or "9000").strip()
    if not port_raw.isdigit():
        raise ConfigError(f"ARCTICDB_S3_PORT must be a number, got {port_raw!r}")

    secure_raw = str(e.get("ARCTICDB_S3_SECURE") or "true").strip().lower()
    if secure_raw not in ("true", "false"):
        raise ConfigError(f"ARCTICDB_S3_SECURE must be 'true' or 'false', got {secure_raw!r}")

    return S3Config(
        endpoint=str(e["ARCTICDB_S3_ENDPOINT"]).strip(),
        bucket=str(e["ARCTICDB_S3_BUCKET"]).strip(),
        access=str(e["ARCTICDB_S3_ACCESS"]).strip(),
        secret=str(e["ARCTICDB_S3_SECRET"]).strip(),
        port=int(port_raw),
        secure=secure_raw == "true",
    )

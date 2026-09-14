"""credentials.env reader with docker-compose-dotenv semantics.

The parse rules replicate compose v2.29 as observed on the NAS: this module is
the widget's source of truth about what the NEXT container restart will load,
so it must agree with compose, warts included (empty value + inline comment
leaks the comment text in as the value).
"""
from __future__ import annotations

import os
import re

_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if not value.startswith("#"):
            # Inline comment after a real value is stripped; '#' with no
            # preceding whitespace is part of the value.
            cut = value.find(" #")
            if cut != -1:
                value = value[:cut].rstrip()
        out[key] = value
    return out


def load(path: str) -> dict[str, str] | None:
    try:
        with open(path, encoding="utf-8") as f:
            return parse_text(f.read())
    except OSError:
        return None


def load_with_warnings(path: str) -> tuple[dict[str, str] | None, list[str]]:
    """Like `load`, but also reports malformed lines (non-blank, non-comment,
    not matching KEY=VALUE) as ["line N", ...] instead of silently skipping
    them. Only the line number is reported — the line text could contain a
    partial secret, so it is never included."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None, []

    out: dict[str, str] = {}
    warnings: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            warnings.append(f"line {lineno}")
            continue
        key, value = m.group(1), m.group(2).strip()
        if not value.startswith("#"):
            cut = value.find(" #")
            if cut != -1:
                value = value[:cut].rstrip()
        out[key] = value
    return out, warnings


def set_value(path: str, env_var: str, value: str) -> None:
    """Update one variable in credentials.env, preserving comments, blank
    lines, ordering and every other entry.

    Written in place (truncate + write the existing path, no temp file) --
    NOT the usual tempfile-in-same-dir + os.replace atomic-rename pattern.
    In production this path is a single-file Docker bind mount, and
    os.replace() onto a path that is itself a mount target fails with
    EBUSY: the kernel refuses to swap out an active mountpoint. Writing
    into the existing inode sidesteps that -- the trade is that a crash
    mid-write can leave a truncated file, which a rename would have
    avoided.

    Known round-trip hazard: a value containing " #" (space then hash), or
    with leading/trailing whitespace, is written correctly but does not come
    back unchanged from parse_text -- parse_text strips inline " #..." as a
    comment and trims surrounding whitespace, per the compose semantics this
    module mirrors (see module docstring). set_value does not special-case
    or reject those values; callers must not assume
    parse_text(written) == value for them.
    """
    # A line break in value would inject a second, uncommented line below
    # this one, letting one call silently define an arbitrary extra variable
    # (or split an existing line in two). The dotenv format this module
    # mirrors has no escape for that, so refuse rather than write something
    # the reader would parse into more variables than the caller intended.
    #
    # "Line break" has to mean whatever str.splitlines() treats as one, not
    # just '\n'/'\r': parse_text and the re-read just below both split on
    # it, and splitlines() also breaks on \v, \f, \x1c, \x1d, \x1e, \x85,
    # and the unicode line/paragraph separators \u2028 and \u2029.
    # Checking only '\n'/'\r' left every one of those as a working
    # injection vector. An empty value has no lines at all
    # (''.splitlines() == []) and is a legal, common case (clearing a key),
    # so it is let through explicitly rather than compared against
    # splitlines() output.
    if value and value.splitlines() != [value]:
        raise ValueError(f"value for {env_var!r} must not contain a line break")

    exists = os.path.exists(path)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []

    replaced = False
    out: list[str] = []
    for raw in lines:
        m = _LINE.match(raw.strip())
        if m and m.group(1) == env_var and not replaced:
            out.append(f"{env_var}={value}")
            replaced = True
        else:
            out.append(raw)
    if not replaced:
        out.append(f"{env_var}={value}")

    # Writing into the existing inode leaves its mode/ownership untouched,
    # so there is nothing to carry over except for a brand-new file, which
    # gets a restrictive mode explicitly (matching mkstemp's old default).
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
        f.flush()
        os.fsync(f.fileno())
    if not exists:
        os.chmod(path, 0o600)

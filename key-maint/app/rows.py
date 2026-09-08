# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Assemble widget rows from parsed credentials, applying tier redaction.

`value` is ABSENT (not masked) below tier 3, so no client rendering bug can
ever leak it. `values=None` means the file was unreadable: emit a banner row
plus one 'unknown' row per registered provider — HTTP 200 always."""
from __future__ import annotations

from app.probes import TestResult
from app.registry import IGNORE, PROVIDERS, is_demo

_CRED_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET")


def _status(values: dict[str, str], env_var: str) -> str:
    if env_var not in values:
        return "missing"
    return "set" if values[env_var] else "empty"


def build_rows(
    values: dict[str, str] | None,
    tier: int,
    tests: dict[str, TestResult] | None,
    *,
    malformed: list[str] | None = None,
) -> list[dict]:
    if values is None:
        banner = {
            "provider": "⚠ credentials.env",
            "env_var": "",
            "status": "unknown",
            "demo": False,
        }
        unknowns = [
            {"provider": p.name, "env_var": var, "status": "unknown", "demo": False}
            for var, p in PROVIDERS.items()
        ]
        return [banner, *unknowns]

    rows: list[dict] = []
    for var, p in PROVIDERS.items():
        row = {
            "provider": p.name,
            "env_var": var,
            "status": _status(values, var),
            "demo": is_demo(var, values.get(var, "")),
        }
        if tier == 3 and var in values:
            row["value"] = values[var]
        if tests and var in tests:
            tr = tests[var]
            row["test"] = {"result": tr.result, "detail": tr.detail}
        rows.append(row)

    # Credential-shaped vars present in the file but unknown to the registry.
    for var, value in values.items():
        if var in PROVIDERS or var in IGNORE:
            continue
        if not var.endswith(_CRED_SUFFIXES):
            continue
        row = {
            "provider": var,
            "env_var": var,
            "status": "set" if value else "empty",
            "demo": False,
        }
        if tier == 3:
            row["value"] = value
        rows.append(row)

    if malformed:
        rows.extend(
            {
                "provider": "⚠ malformed line",
                "env_var": entry,
                "status": "unknown",
                "demo": False,
            }
            for entry in malformed
        )
    return rows


def build_summary(rows: list[dict]) -> str:
    """Key state as markdown, for the summary widget.

    Counts and provider names only, deliberately. Tier-3 rows carry the
    credential itself in row["value"]; this must never render it, which is
    also why its widget declares no raw view.
    """
    by_status: dict[str, list[str]] = {}
    for row in rows:
        by_status.setdefault(str(row.get("status", "unknown")), []).append(
            str(row.get("provider", "?"))
        )
    total = len(rows)
    n_set = len(by_status.get("set", []))
    lines = [f"**{n_set} of {total} provider keys configured**", ""]
    for status, label in (
        ("missing", "Missing"),
        ("empty", "Present but empty"),
        ("unknown", "Unreadable"),
    ):
        names = sorted(by_status.get(status, []))
        if names:
            lines.append(f"- **{label} ({len(names)}):** {', '.join(names)}")
    demo = sorted(str(r.get("provider", "?")) for r in rows if r.get("demo"))
    if demo:
        lines.append(
            f"- **Public demo key ({len(demo)}):** {', '.join(demo)}"
            " -- rate-limited and shared; replace before relying on it."
        )
    if n_set == total and not demo:
        lines.append("- Every provider is configured with a private key.")
    return "\n".join(lines)

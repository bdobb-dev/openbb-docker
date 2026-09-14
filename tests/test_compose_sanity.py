# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Runnable (pytest-free) YAML sanity check for the Task 11 docker-compose.yml
edit: the new `tick-vault` service exists, sits under an opt-in profile (so
`docker compose up` never autostarts the ~300k-call historical walk), and
its `tick-vault-status` sibling exists too. Plain `python3
tests/test_compose_sanity.py`, `python3 -c` + `yaml.safe_load` against the
repo root file via a relative path -- no docker/compose binary involved.
"""
from __future__ import annotations

import os

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE_PATH = os.path.join(REPO_ROOT, "docker-compose.yml")


def _load_compose() -> dict:
    with open(COMPOSE_PATH) as f:
        return yaml.safe_load(f)


def test_compose_file_parses():
    doc = _load_compose()
    assert isinstance(doc, dict)
    assert "services" in doc


def test_tick_vault_service_exists_under_profile():
    doc = _load_compose()
    services = doc["services"]
    assert "tick-vault" in services
    service = services["tick-vault"]
    assert service.get("profiles") == ["tick-vault"]
    # Not autostarted with a bare `docker compose up`.
    assert "profiles" in service


def test_tick_vault_status_service_exists_under_same_profile():
    doc = _load_compose()
    services = doc["services"]
    assert "tick-vault-status" in services
    service = services["tick-vault-status"]
    assert service.get("profiles") == ["tick-vault"]


def test_tick_vault_env_uses_variable_references_not_literals():
    """CI gotcha: no literal IPs/hosts in compose env -- VAULT_ROOT etc.
    come from ${VAR} substitutions (backed by a gitignored .env / the
    shell environment), never hardcoded infrastructure values."""
    doc = _load_compose()
    service = doc["services"]["tick-vault"]
    env = service.get("environment", [])
    assert env, "tick-vault service should declare environment"
    for entry in env:
        assert isinstance(entry, str)
        name, _, value = entry.partition("=")
        assert name in {"EOD_API_KEY", "VAULT_ROOT", "LEGACY_ROOT", "CALIBRATION_REPORT"}
        # A variable reference, not a bare literal -- e.g. "${VAULT_ROOT:-...}".
        assert "${" in value, f"{name} should reference an env var, got {value!r}"

    # No dotted-quad literal anywhere in the raw YAML text for either service.
    import re

    with open(COMPOSE_PATH) as f:
        raw = f.read()
    tick_vault_block_start = raw.index("\n  tick-vault:\n")
    tick_vault_status_start = raw.index("\n  tick-vault-status:\n")
    next_service_start = raw.index("\nvolumes:\n")
    block = raw[tick_vault_block_start:next_service_start]
    assert tick_vault_status_start > tick_vault_block_start
    ip_literal = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
    for match in ip_literal.finditer(block):
        # 0.0.0.0 is a bind address (all interfaces), not an assigned host
        # IP -- the same idiom stores-explorer's own command: line uses.
        assert match.group(0) == "0.0.0.0", f"unexpected literal IP in tick-vault block: {match.group(0)}"


def test_tick_vault_dockerfile_exists():
    assert os.path.isfile(os.path.join(REPO_ROOT, "tick-vault", "Dockerfile"))


if __name__ == "__main__":
    import traceback

    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS: {name}")
                passed += 1
            except Exception:
                print(f"FAIL: {name}")
                traceback.print_exc()
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(0 if failed == 0 else 1)

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from app.credfile import parse_text
from app.registry import IGNORE, PROVIDERS, is_demo

EXAMPLE = Path(__file__).resolve().parents[2] / "credentials.env.example"


class TestCoverage:
    def test_every_example_var_is_registered_or_ignored(self):
        """Registry and credentials.env.example may not drift."""
        example_vars = set(parse_text(EXAMPLE.read_text()).keys())
        covered = set(PROVIDERS) | set(IGNORE)
        assert example_vars <= covered, f"unregistered: {example_vars - covered}"

    def test_no_var_is_both_registered_and_ignored(self):
        assert not (set(PROVIDERS) & set(IGNORE))

    def test_registry_keys_match_env_var_field(self):
        for var, p in PROVIDERS.items():
            assert p.env_var == var


class TestDemo:
    def test_eodhd_demo_key(self):
        assert is_demo("EODHD_API_KEY", "demo") is True
        assert is_demo("EODHD_API_KEY", "DEMO") is True  # case-insensitive

    def test_real_key_not_demo(self):
        assert is_demo("EODHD_API_KEY", "abc123realkey") is False

    def test_unknown_var_never_demo(self):
        assert is_demo("SOMETHING_API_KEY", "demo") is False

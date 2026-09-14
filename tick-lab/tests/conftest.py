"""Test-wide isolation.

The CLI deliberately loads a `.env` (from the cwd, then the package root) so
the README's documented `cp .env.example .env` flow works. That is right for
users and wrong for tests: with a real `.env` present -- which any developer
who has actually used the tool will have -- tests that assert "missing
configuration is reported" instead resolve real credentials and TALK TO THE
LIVE STORE. That was not hypothetical: it connected to a real MinIO and read
real ticks during a unit-test run before this guard existed.

Real environment variables still work, so the `TICK_LAB_TEST_S3=1` integration
tests are unaffected -- they take their settings from the shell, not a file.
"""

import pytest

from tick_lab import config


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    monkeypatch.setattr(config, "_dotenv_candidates", tuple)

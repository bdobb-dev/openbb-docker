# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""The two DELTA_S3_* resolvers must agree on what they hand delta-rs.

`openbb_deltalake.utils.s3_options_from_env` and `tick_lab.config.from_env`
read the same four variables out of the same `minio.env` and build the same
`storage_options`. They are deliberately NOT one function: tick-lab is a
standalone tool whose dependencies are deltalake/pyarrow/pandas/yfinance,
while openbb-deltalake pulls in openbb-core, so importing one from the other
would drag an OpenBB provider stack into a laptop CLI to share ~25 lines of
pure dictionary building.

Their *behaviour* still has to match, because they point at one store: a
divergence means the laptop and the container authenticate differently
against the same MinIO, which shows up as an auth failure deep inside
delta-rs with no hint as to why. This test is what keeps them honest, and is
the reason neither file needs to import the other.

BOTH modules are loaded straight off disk rather than imported as packages.
Each depends on nothing outside the standard library, so this creates no
dependency edge in either direction -- and it keeps the test runnable without
openbb-core installed, which `import openbb_deltalake.utils` would otherwise
drag in through that package's __init__.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str):
    path = _REPO / relative
    if not path.is_file():
        pytest.skip(f"not present at {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves its own module out of
    # sys.modules while the class body runs, and blows up on a None entry.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_tick_lab_config():
    return _load("_tick_lab_config", "tick-lab/tick_lab/config.py")


def _s3_options_from_env(env):
    utils = _load("_openbb_deltalake_utils", "openbb-deltalake/openbb_deltalake/utils.py")
    return utils.s3_options_from_env(env)


s3_options_from_env = _s3_options_from_env


BASE = {
    "DELTA_S3_ENDPOINT": "minio.example.ts.net",
    "DELTA_S3_BUCKET": "openbb",
    "DELTA_S3_ACCESS": "root-user",
    "DELTA_S3_SECRET": "root-pass",
}


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"DELTA_S3_PORT": "9000"},
        {"DELTA_S3_PORT": "443"},
        {"DELTA_S3_SECURE": "true"},
        {"DELTA_S3_SECURE": "false"},
        {"DELTA_S3_SECURE": "false", "DELTA_S3_PORT": "9002"},
    ],
    ids=["defaults", "port-9000", "port-443", "tls", "plaintext", "plaintext-alt-port"],
)
def test_both_resolvers_build_identical_storage_options(extra):
    env = {**BASE, **extra}
    config = _load_tick_lab_config()

    mine = s3_options_from_env(env)
    theirs = config.from_env(env)

    assert mine is not None
    base_uri, options = mine
    assert base_uri == theirs.base_uri
    assert options == theirs.storage_options


def test_both_agree_that_plaintext_needs_allow_http():
    """delta-rs silently refuses an http:// endpoint without this, so the two
    must not disagree about when to set it."""
    env = {**BASE, "DELTA_S3_SECURE": "false"}
    config = _load_tick_lab_config()

    _, options = s3_options_from_env(env)
    assert options["aws_allow_http"] == "true"
    assert config.from_env(env).storage_options["aws_allow_http"] == "true"


def test_both_reject_the_same_malformed_values():
    """Same inputs rejected on both sides; only the exception TYPE differs
    (ValueError vs the CLI's ConfigError, which carries a .env hint)."""
    config = _load_tick_lab_config()

    for bad in ({"DELTA_S3_PORT": "nine-thousand"}, {"DELTA_S3_SECURE": "yes"}):
        env = {**BASE, **bad}
        with pytest.raises(ValueError):
            s3_options_from_env(env)
        with pytest.raises(config.ConfigError):
            config.from_env(env)

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

import pytest


def test_s3_options_from_env_complete():
    from openbb_deltalake.utils import s3_options_from_env

    env = {
        "DELTA_S3_ENDPOINT": "minio.tail.ts.net",
        "DELTA_S3_BUCKET": "openbb",
        "DELTA_S3_ACCESS": "user",
        "DELTA_S3_SECRET": "hunter2 ",  # trailing space must be stripped
        "DELTA_S3_PORT": "9000",
        "DELTA_S3_SECURE": "true",
    }
    base, opts = s3_options_from_env(env)
    assert base == "s3://openbb"
    assert opts["aws_endpoint"] == "https://minio.tail.ts.net:9000"
    assert opts["aws_secret_access_key"] == "hunter2"
    assert opts["aws_virtual_hosted_style_request"] == "false"
    assert opts["aws_conditional_put"] == "etag"
    assert "aws_allow_http" not in opts


def test_s3_options_insecure_allows_http():
    from openbb_deltalake.utils import s3_options_from_env

    env = {
        "DELTA_S3_ENDPOINT": "localhost",
        "DELTA_S3_BUCKET": "b",
        "DELTA_S3_ACCESS": "a",
        "DELTA_S3_SECRET": "s",
        "DELTA_S3_SECURE": "false",
    }
    base, opts = s3_options_from_env(env)
    assert opts["aws_endpoint"] == "http://localhost:9000"
    assert opts["aws_allow_http"] == "true"


def test_s3_options_incomplete_returns_none():
    from openbb_deltalake.utils import s3_options_from_env

    assert s3_options_from_env({"DELTA_S3_ENDPOINT": "x"}) is None


def test_s3_options_bad_port_raises():
    from openbb_deltalake.utils import s3_options_from_env

    env = {
        "DELTA_S3_ENDPOINT": "x", "DELTA_S3_BUCKET": "b",
        "DELTA_S3_ACCESS": "a", "DELTA_S3_SECRET": "s", "DELTA_S3_PORT": "nope",
    }
    with pytest.raises(ValueError):
        s3_options_from_env(env)


def test_resolve_config_explicit_arg_wins(monkeypatch, tmp_path):
    from openbb_deltalake.utils import resolve_config

    monkeypatch.setenv("DELTA_URI", "/somewhere/else")
    base, library, opts = resolve_config(str(tmp_path), "mylib", None)
    assert (base, library, opts) == (str(tmp_path), "mylib", {})


def test_resolve_config_local_default(monkeypatch, tmp_path):
    from openbb_deltalake.utils import resolve_config

    for var in ("DELTA_URI", "DELTA_LIBRARY", "DELTA_S3_ENDPOINT", "DELTA_S3_BUCKET",
                "DELTA_S3_ACCESS", "DELTA_S3_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENBB_HOME", str(tmp_path))
    base, library, opts = resolve_config()
    assert base == str(tmp_path / "deltalake")
    assert library == "openbb"
    assert opts == {}


def test_resolve_config_local_explicit_ignores_malformed_s3_env(monkeypatch, tmp_path):
    from openbb_deltalake.utils import resolve_config

    # Malformed S3 env should not affect local explicit path.
    monkeypatch.setenv("DELTA_S3_ENDPOINT", "minio.tail.ts.net")
    monkeypatch.setenv("DELTA_S3_BUCKET", "openbb")
    monkeypatch.setenv("DELTA_S3_ACCESS", "user")
    monkeypatch.setenv("DELTA_S3_SECRET", "secret")
    monkeypatch.setenv("DELTA_S3_PORT", "nope")  # malformed port
    base, library, opts = resolve_config(str(tmp_path), "mylib", None)
    assert (base, library, opts) == (str(tmp_path), "mylib", {})


def test_resolve_config_s3_explicit_raises_on_malformed_env(monkeypatch):
    from openbb_deltalake.utils import resolve_config

    # Malformed S3 env SHOULD fail when trying to use s3:// explicit path.
    monkeypatch.setenv("DELTA_S3_ENDPOINT", "minio.tail.ts.net")
    monkeypatch.setenv("DELTA_S3_BUCKET", "openbb")
    monkeypatch.setenv("DELTA_S3_ACCESS", "user")
    monkeypatch.setenv("DELTA_S3_SECRET", "secret")
    monkeypatch.setenv("DELTA_S3_PORT", "nope")  # malformed port
    with pytest.raises(ValueError):
        resolve_config("s3://bucket", "lib", None)


def test_table_path():
    from openbb_deltalake.utils import table_path

    assert table_path("s3://bucket", "lib", "AAPL") == "s3://bucket/lib/AAPL"
    assert table_path("/tmp/base/", "lib", "AAPL") == "/tmp/base/lib/AAPL"

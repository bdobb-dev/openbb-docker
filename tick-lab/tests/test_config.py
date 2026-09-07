# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""S3 connection settings read from the environment."""

import os

import pytest

from tick_lab.config import ConfigError, S3Config, from_env, load_dotenv

FULL = {
    "DELTA_S3_ENDPOINT": "minio.example.ts.net",
    "DELTA_S3_BUCKET": "openbb",
    "DELTA_S3_ACCESS": "someaccesskey",
    "DELTA_S3_SECRET": "somesecretkey",
}


def test_reads_a_complete_environment():
    cfg = from_env(FULL)
    assert cfg == S3Config(
        endpoint="minio.example.ts.net",
        bucket="openbb",
        access="someaccesskey",
        secret="somesecretkey",
        port=9000,
        secure=True,
    )


def test_base_uri_and_storage_options():
    cfg = S3Config(endpoint="minio.tail.ts.net", bucket="openbb",
                   access="u", secret="p", port=9000, secure=True)
    assert cfg.base_uri == "s3://openbb"
    opts = cfg.storage_options
    assert opts["aws_endpoint"] == "https://minio.tail.ts.net:9000"
    assert opts["aws_access_key_id"] == "u"
    assert "aws_allow_http" not in opts


def test_storage_options_insecure():
    cfg = S3Config(endpoint="localhost", bucket="b", access="u", secret="p", secure=False)
    assert cfg.storage_options["aws_allow_http"] == "true"
    assert cfg.storage_options["aws_endpoint"] == "http://localhost:9000"


def test_missing_variables_are_all_named_at_once():
    with pytest.raises(ConfigError) as exc:
        from_env({"DELTA_S3_ENDPOINT": "minio.example.ts.net"})
    message = str(exc.value)
    for name in ("DELTA_S3_BUCKET", "DELTA_S3_ACCESS", "DELTA_S3_SECRET"):
        assert name in message


def test_blank_values_count_as_missing():
    with pytest.raises(ConfigError, match="DELTA_S3_SECRET"):
        from_env({**FULL, "DELTA_S3_SECRET": "   "})


def test_rejects_non_numeric_port():
    with pytest.raises(ConfigError, match="DELTA_S3_PORT"):
        from_env({**FULL, "DELTA_S3_PORT": "nine"})


def test_rejects_unparseable_secure_flag():
    with pytest.raises(ConfigError, match="DELTA_S3_SECURE"):
        from_env({**FULL, "DELTA_S3_SECURE": "maybe"})


def test_secret_is_not_leaked_by_repr():
    assert "somesecretkey" not in repr(from_env(FULL))


# --- .env loading -----------------------------------------------------------
#
# These isolate `_dotenv_candidates` to a tmp_path (never the real repo, in
# case a developer has a genuine tick-lab/.env for their own use) and swap in
# a bare dict for os.environ so no test can leak a variable into the real
# process environment or into a later test.


def _isolate_dotenv(monkeypatch, tmp_path, environ=None):
    """Point .env lookup at tmp_path only, and os.environ at a scratch dict."""
    monkeypatch.setattr(
        "tick_lab.config._dotenv_candidates",
        lambda: (tmp_path / ".env", tmp_path / "package-root-.env"),
    )
    monkeypatch.setattr(os, "environ", {} if environ is None else dict(environ))


def test_dotenv_parses_comments_blank_lines_export_and_quotes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tick_lab.config._dotenv_candidates",
        lambda: (tmp_path / ".env", tmp_path / "package-root-.env"),
    )
    (tmp_path / ".env").write_text(
        "# a comment\n"
        "\n"
        "export DELTA_S3_ENDPOINT=minio.example.ts.net\n"
        'DELTA_S3_BUCKET="openbb"\n'
        "DELTA_S3_ACCESS='someaccesskey'\n"
        "DELTA_S3_SECRET=somesecretkey\n"
    )
    target: dict[str, str] = {}
    path = load_dotenv(target)
    assert path == tmp_path / ".env"
    assert target == {
        "DELTA_S3_ENDPOINT": "minio.example.ts.net",
        "DELTA_S3_BUCKET": "openbb",
        "DELTA_S3_ACCESS": "someaccesskey",
        "DELTA_S3_SECRET": "somesecretkey",
    }


def test_dotenv_missing_returns_none_and_leaves_target_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tick_lab.config._dotenv_candidates",
        lambda: (tmp_path / ".env", tmp_path / "package-root-.env"),
    )
    target = {"DELTA_S3_ENDPOINT": "already-set"}
    assert load_dotenv(target) is None
    assert target == {"DELTA_S3_ENDPOINT": "already-set"}


def test_dotenv_does_not_override_a_key_already_in_target(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tick_lab.config._dotenv_candidates",
        lambda: (tmp_path / ".env", tmp_path / "package-root-.env"),
    )
    (tmp_path / ".env").write_text("DELTA_S3_SECRET=from-dotenv\n")
    target = {"DELTA_S3_SECRET": "already-exported"}
    load_dotenv(target)
    assert target["DELTA_S3_SECRET"] == "already-exported"


def test_dotenv_values_are_picked_up_by_from_env(tmp_path, monkeypatch):
    _isolate_dotenv(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        "# a comment\n"
        "\n"
        "export DELTA_S3_ENDPOINT=minio.example.ts.net\n"
        'DELTA_S3_BUCKET="openbb"\n'
        "DELTA_S3_ACCESS='someaccesskey'\n"
        "DELTA_S3_SECRET=somesecretkey\n"
    )
    cfg = from_env()
    assert cfg == S3Config(
        endpoint="minio.example.ts.net",
        bucket="openbb",
        access="someaccesskey",
        secret="somesecretkey",
    )


def test_real_environment_wins_over_dotenv(tmp_path, monkeypatch):
    _isolate_dotenv(monkeypatch, tmp_path, environ={"DELTA_S3_SECRET": "from-real-env"})
    (tmp_path / ".env").write_text(
        "DELTA_S3_ENDPOINT=minio.example.ts.net\n"
        "DELTA_S3_BUCKET=openbb\n"
        "DELTA_S3_ACCESS=someaccesskey\n"
        "DELTA_S3_SECRET=from-dotenv\n"
    )
    cfg = from_env()
    assert cfg.secret == "from-real-env"


def test_missing_dotenv_is_not_an_error_by_itself(tmp_path, monkeypatch):
    _isolate_dotenv(monkeypatch, tmp_path, environ=FULL)
    # No .env file created in tmp_path at all -- the real environment alone
    # is sufficient and from_env must not treat a missing file as an error.
    cfg = from_env()
    assert cfg.endpoint == FULL["DELTA_S3_ENDPOINT"]


def test_error_message_when_no_dotenv_found(tmp_path, monkeypatch):
    _isolate_dotenv(monkeypatch, tmp_path)
    with pytest.raises(ConfigError, match="no .env file found"):
        from_env()


def test_error_message_when_dotenv_found_but_incomplete(tmp_path, monkeypatch):
    _isolate_dotenv(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("DELTA_S3_ENDPOINT=minio.example.ts.net\n")
    with pytest.raises(ConfigError, match="was found but does not set all of these"):
        from_env()

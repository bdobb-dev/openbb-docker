"""S3 connection settings read from the environment."""

import os

import pytest

from tick_lab.config import ConfigError, S3Config, from_env, load_dotenv

FULL = {
    "ARCTICDB_S3_ENDPOINT": "minio.example.ts.net",
    "ARCTICDB_S3_BUCKET": "openbb",
    "ARCTICDB_S3_ACCESS": "someaccesskey",
    "ARCTICDB_S3_SECRET": "somesecretkey",
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


def test_uri_matches_the_verified_arcticdb_shape():
    assert from_env(FULL).uri == (
        "s3s://minio.example.ts.net:openbb"
        "?port=9000&access=someaccesskey&secret=somesecretkey"
        "&use_virtual_addressing=false"
    )


def test_plain_scheme_when_insecure():
    assert from_env({**FULL, "ARCTICDB_S3_SECURE": "false"}).uri.startswith("s3://")


def test_credentials_are_url_encoded():
    assert "secret=a%2Fb%26c" in from_env({**FULL, "ARCTICDB_S3_SECRET": "a/b&c"}).uri


def test_missing_variables_are_all_named_at_once():
    with pytest.raises(ConfigError) as exc:
        from_env({"ARCTICDB_S3_ENDPOINT": "minio.example.ts.net"})
    message = str(exc.value)
    for name in ("ARCTICDB_S3_BUCKET", "ARCTICDB_S3_ACCESS", "ARCTICDB_S3_SECRET"):
        assert name in message


def test_blank_values_count_as_missing():
    with pytest.raises(ConfigError, match="ARCTICDB_S3_SECRET"):
        from_env({**FULL, "ARCTICDB_S3_SECRET": "   "})


def test_rejects_non_numeric_port():
    with pytest.raises(ConfigError, match="ARCTICDB_S3_PORT"):
        from_env({**FULL, "ARCTICDB_S3_PORT": "nine"})


def test_rejects_unparseable_secure_flag():
    with pytest.raises(ConfigError, match="ARCTICDB_S3_SECURE"):
        from_env({**FULL, "ARCTICDB_S3_SECURE": "maybe"})


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
        "export ARCTICDB_S3_ENDPOINT=minio.example.ts.net\n"
        'ARCTICDB_S3_BUCKET="openbb"\n'
        "ARCTICDB_S3_ACCESS='someaccesskey'\n"
        "ARCTICDB_S3_SECRET=somesecretkey\n"
    )
    target: dict[str, str] = {}
    path = load_dotenv(target)
    assert path == tmp_path / ".env"
    assert target == {
        "ARCTICDB_S3_ENDPOINT": "minio.example.ts.net",
        "ARCTICDB_S3_BUCKET": "openbb",
        "ARCTICDB_S3_ACCESS": "someaccesskey",
        "ARCTICDB_S3_SECRET": "somesecretkey",
    }


def test_dotenv_missing_returns_none_and_leaves_target_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tick_lab.config._dotenv_candidates",
        lambda: (tmp_path / ".env", tmp_path / "package-root-.env"),
    )
    target = {"ARCTICDB_S3_ENDPOINT": "already-set"}
    assert load_dotenv(target) is None
    assert target == {"ARCTICDB_S3_ENDPOINT": "already-set"}


def test_dotenv_does_not_override_a_key_already_in_target(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tick_lab.config._dotenv_candidates",
        lambda: (tmp_path / ".env", tmp_path / "package-root-.env"),
    )
    (tmp_path / ".env").write_text("ARCTICDB_S3_SECRET=from-dotenv\n")
    target = {"ARCTICDB_S3_SECRET": "already-exported"}
    load_dotenv(target)
    assert target["ARCTICDB_S3_SECRET"] == "already-exported"


def test_dotenv_values_are_picked_up_by_from_env(tmp_path, monkeypatch):
    _isolate_dotenv(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        "# a comment\n"
        "\n"
        "export ARCTICDB_S3_ENDPOINT=minio.example.ts.net\n"
        'ARCTICDB_S3_BUCKET="openbb"\n'
        "ARCTICDB_S3_ACCESS='someaccesskey'\n"
        "ARCTICDB_S3_SECRET=somesecretkey\n"
    )
    cfg = from_env()
    assert cfg == S3Config(
        endpoint="minio.example.ts.net",
        bucket="openbb",
        access="someaccesskey",
        secret="somesecretkey",
    )


def test_real_environment_wins_over_dotenv(tmp_path, monkeypatch):
    _isolate_dotenv(monkeypatch, tmp_path, environ={"ARCTICDB_S3_SECRET": "from-real-env"})
    (tmp_path / ".env").write_text(
        "ARCTICDB_S3_ENDPOINT=minio.example.ts.net\n"
        "ARCTICDB_S3_BUCKET=openbb\n"
        "ARCTICDB_S3_ACCESS=someaccesskey\n"
        "ARCTICDB_S3_SECRET=from-dotenv\n"
    )
    cfg = from_env()
    assert cfg.secret == "from-real-env"


def test_missing_dotenv_is_not_an_error_by_itself(tmp_path, monkeypatch):
    _isolate_dotenv(monkeypatch, tmp_path, environ=FULL)
    # No .env file created in tmp_path at all -- the real environment alone
    # is sufficient and from_env must not treat a missing file as an error.
    cfg = from_env()
    assert cfg.endpoint == FULL["ARCTICDB_S3_ENDPOINT"]


def test_error_message_when_no_dotenv_found(tmp_path, monkeypatch):
    _isolate_dotenv(monkeypatch, tmp_path)
    with pytest.raises(ConfigError, match="no .env file found"):
        from_env()


def test_error_message_when_dotenv_found_but_incomplete(tmp_path, monkeypatch):
    _isolate_dotenv(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("ARCTICDB_S3_ENDPOINT=minio.example.ts.net\n")
    with pytest.raises(ConfigError, match="was found but does not set all of these"):
        from_env()

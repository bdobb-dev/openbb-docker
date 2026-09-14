"""ARCTICDB_S3_* assembly and its precedence against ARCTICDB_URI."""

import pytest

from openbb_arcticdb.utils import resolve_config, s3_uri_from_env

FULL = {
    "ARCTICDB_S3_ENDPOINT": "minio.example.ts.net",
    "ARCTICDB_S3_BUCKET": "openbb",
    "ARCTICDB_S3_ACCESS": "someaccesskey",
    "ARCTICDB_S3_SECRET": "somesecretkey",
}


def test_returns_none_when_nothing_is_set():
    assert s3_uri_from_env({}) is None


def test_returns_none_when_partially_configured():
    partial = dict(FULL)
    del partial["ARCTICDB_S3_SECRET"]
    assert s3_uri_from_env(partial) is None


def test_assembles_secure_uri_by_default():
    uri = s3_uri_from_env(FULL)
    assert uri == (
        "s3s://minio.example.ts.net:openbb"
        "?port=9000&access=someaccesskey&secret=somesecretkey"
        "&use_virtual_addressing=false"
    )


def test_plain_scheme_when_secure_is_false():
    uri = s3_uri_from_env({**FULL, "ARCTICDB_S3_SECURE": "false"})
    assert uri.startswith("s3://")


def test_custom_port():
    uri = s3_uri_from_env({**FULL, "ARCTICDB_S3_PORT": "9443"})
    assert "port=9443" in uri


def test_credentials_are_url_encoded():
    uri = s3_uri_from_env({**FULL, "ARCTICDB_S3_SECRET": "a/b+c=d&e"})
    assert "secret=a%2Fb%2Bc%3Dd%26e" in uri


def test_trailing_whitespace_is_stripped_from_every_part():
    # Docker Compose's env_file parser preserves trailing whitespace, so a
    # stray space after a value in minio.env (invisible in most editors)
    # must not become part of the URI -- otherwise tick-lab (which strips,
    # see tick_lab.config.from_env) connects fine while the container
    # builds e.g. secret=hunter2%20 and fails auth with no hint that
    # whitespace was the cause.
    padded = {
        "ARCTICDB_S3_ENDPOINT": " minio.example.ts.net ",
        "ARCTICDB_S3_BUCKET": " openbb ",
        "ARCTICDB_S3_ACCESS": " someaccesskey ",
        "ARCTICDB_S3_SECRET": "somesecretkey ",
    }
    assert s3_uri_from_env(padded) == (
        "s3s://minio.example.ts.net:openbb"
        "?port=9000&access=someaccesskey&secret=somesecretkey"
        "&use_virtual_addressing=false"
    )


def test_whitespace_only_value_counts_as_missing():
    assert s3_uri_from_env({**FULL, "ARCTICDB_S3_SECRET": "   "}) is None


def test_rejects_non_numeric_port():
    with pytest.raises(ValueError, match="ARCTICDB_S3_PORT"):
        s3_uri_from_env({**FULL, "ARCTICDB_S3_PORT": "nine-thousand"})


def test_rejects_unparseable_secure_flag():
    with pytest.raises(ValueError, match="ARCTICDB_S3_SECURE"):
        s3_uri_from_env({**FULL, "ARCTICDB_S3_SECURE": "yes-please"})


def test_explicit_uri_wins_over_s3_parts(monkeypatch):
    for k, v in FULL.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ARCTICDB_URI", "lmdb:///tmp/explicit")
    uri, _ = resolve_config()
    assert uri == "lmdb:///tmp/explicit"


def test_s3_parts_used_when_no_explicit_uri(monkeypatch):
    for k, v in FULL.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ARCTICDB_URI", raising=False)
    uri, library = resolve_config()
    assert uri.startswith("s3s://minio.example.ts.net:openbb")
    assert library == "openbb"


def test_falls_back_to_lmdb_when_nothing_configured(monkeypatch):
    for k in list(FULL) + ["ARCTICDB_URI", "ARCTICDB_S3_SECURE", "ARCTICDB_S3_PORT"]:
        monkeypatch.delenv(k, raising=False)
    uri, _ = resolve_config()
    assert uri.startswith("lmdb://")

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import base64

import pytest
from fastapi.testclient import TestClient

from security_master_api.app.main import create_app
from security_master_api.config import Settings
from security_master_api.store.seed import seed


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "true")
    monkeypatch.setenv("OPENBB_API_USERNAME", "u")
    monkeypatch.setenv("OPENBB_API_PASSWORD", "p")
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return TestClient(create_app(s))


def basic(u="u", p="p"):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


def test_health_is_open_everything_else_is_not(client):
    assert client.get("/security-master/v1/health").status_code == 200
    r = client.get("/security-master/v1/catalog")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Basic"
    assert client.get("/widgets.json").status_code == 401


def test_header_and_query_credentials(client):
    assert client.get("/security-master/v1/catalog", headers=basic()).status_code == 200
    token = "Basic " + base64.b64encode(b"u:p").decode()
    assert client.get("/security-master/v1/catalog",
                      params={"authorization": token}).status_code == 200
    assert client.get("/security-master/v1/catalog", headers=basic("u", "x")).status_code == 401


def test_unconfigured_credentials_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "true")
    monkeypatch.delenv("OPENBB_API_USERNAME", raising=False)
    monkeypatch.delenv("OPENBB_API_PASSWORD", raising=False)
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    c = TestClient(create_app(s))
    assert c.get("/security-master/v1/catalog", headers=basic("", "")).status_code == 401

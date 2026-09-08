# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

import base64

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import make_guard


def app_with_guard(auth_file: str) -> TestClient:
    app = FastAPI()

    @app.get("/ping", dependencies=[Depends(make_guard(auth_file))])
    def ping():
        return {"ok": True}

    return TestClient(app)


def basic(user, pw):
    tok = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {tok}"}


def write_auth(tmp_path, user="u1", pw="p1"):
    p = tmp_path / "api-auth.env"
    p.write_text(f"OPENBB_API_AUTH=true\nOPENBB_API_USERNAME={user}\nOPENBB_API_PASSWORD={pw}\n")
    return str(p)


class TestGuard:
    def test_no_header_401(self, tmp_path):
        c = app_with_guard(write_auth(tmp_path))
        assert c.get("/ping").status_code == 401

    def test_wrong_password_401(self, tmp_path):
        c = app_with_guard(write_auth(tmp_path))
        assert c.get("/ping", headers=basic("u1", "wrong")).status_code == 401

    def test_correct_credentials_200(self, tmp_path):
        c = app_with_guard(write_auth(tmp_path))
        assert c.get("/ping", headers=basic("u1", "p1")).status_code == 200

    def test_missing_auth_file_denies_all(self, tmp_path):
        c = app_with_guard(str(tmp_path / "absent.env"))
        assert c.get("/ping", headers=basic("u1", "p1")).status_code == 401


class TestAdminIsNotExempt:
    """The admin (tier 3) surface is a unix socket with no port -- but it is NOT
    header-free. create_app applies the same Basic-auth guard for role="admin"
    as for role="network", so reaching the 0700 socket raises an ALREADY
    AUTHENTICATED caller to tier 3; it does not replace authentication.

    The README and the compose comment both claimed "no port and no header" for
    years. Nothing tested it, so nothing caught it."""

    def test_the_admin_role_is_not_exempt_from_basic_auth(self, tmp_path):
        from app.server import create_app

        auth = write_auth(tmp_path)
        cred = tmp_path / "credentials.env"
        cred.write_text("FMP_API_KEY=abc\n")
        c = TestClient(create_app(role="admin", cred_file=str(cred), auth_file=auth))
        assert c.get("/widgets.json").status_code == 401
        assert c.get("/widgets.json", headers=basic("u1", "p1")).status_code == 200

    def test_the_network_role_is_not_exempt_either(self, tmp_path):
        from app.server import create_app

        auth = write_auth(tmp_path)
        cred = tmp_path / "credentials.env"
        cred.write_text("FMP_API_KEY=abc\n")
        c = TestClient(create_app(role="network", cred_file=str(cred), auth_file=auth))
        assert c.get("/widgets.json").status_code == 401

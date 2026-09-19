# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The router against the REAL service, not a stub that echoes whatever it is sent.

Every stub in `test_router.py` answers 200 to any body, so a parameter vocabulary that
disagrees with the service passes all of them and fails on the first real call -- which is
what `session_label="calendar_date"` did. This test runs `security_master_api` under uvicorn
on a loopback port, seeded with the golden fixtures, and drives `market_calendar` through the
same `requests` call the image makes.

uvicorn rather than `fastapi.testclient`: uvicorn is a hard dependency of the service, while
TestClient needs the service's `dev` extra (httpx) in whatever environment runs these tests.

Skipped when the service is not importable: the extension's CI job installs this package
alone. Locally, `pip install -e ./security-master-api` makes it run.
"""

from __future__ import annotations

import importlib.util
import threading
import time
from datetime import date, datetime, timezone

import pytest

from openbb_security_master.router import market_calendar

SERVICE = importlib.util.find_spec("security_master_api") is not None


@pytest.mark.skipif(not SERVICE, reason="security_master_api is not installed here")
def test_market_calendar_against_the_real_service(monkeypatch, tmp_path):
    import uvicorn
    from security_master_api.app.main import create_app
    from security_master_api.config import Settings
    from security_master_api.store.seed import seed

    monkeypatch.setenv("OPENBB_API_AUTH", "false")
    settings = Settings(root=str(tmp_path), preflight_secret="k")
    seed(settings)
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=0,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "the service did not come up"
        port = server.servers[0].sockets[0].getsockname()[1]
        monkeypatch.setenv("SECURITY_MASTER_URL", f"http://127.0.0.1:{port}")
        out = market_calendar(calendar_id="cal_tadawul", start_date=date(2027, 3, 1),
                              end_date=date(2027, 3, 31), include_closed=True,
                              knowledge_at=datetime(2027, 3, 10, 8, 0, tzinfo=timezone.utc))
    finally:
        server.should_exit = True
        thread.join(timeout=30)
    row = next(r for r in out.results if str(r.session_date) == "2027-03-11")
    assert row.market_effect == "closed"

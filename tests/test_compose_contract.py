# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# The release this tree IS, read from the one place that states it. Spelling a
# version literal in an assertion makes the test a landmine: it passes on the
# day it is written and fails on every release bump thereafter, for a reason
# that has nothing to do with what it is guarding. Worse here, it would
# contradict scripts/check-image-tags.sh, which requires every locally-built
# image to carry THIS release's version -- two gates asserting opposite things
# about the same line. Deriving both from README.md makes them agree by
# construction.
RELEASE = re.search(
    r"^## What you get \(this release: v([0-9][0-9.]*)\)", (ROOT / "README.md").read_text(), re.M
).group(1)


def test_security_master_services_are_declared():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    api = compose["services"]["security-master-api"]
    worker = compose["services"]["security-master-worker"]
    # The point is that the two services share ONE image -- the worker is the
    # same build run with a different command -- and that it names this release.
    assert api["image"] == worker["image"] == f"openbb-security-master:{RELEASE}"
    assert api["build"] == {"context": ".", "dockerfile": "security-master-api/Dockerfile"}
    assert api["networks"] == ["openbb-internal"] and worker["networks"] == ["openbb-internal"]
    assert api["command"][:4] == ["uvicorn", "security_master_api.app.main:app", "--host", "0.0.0.0"]
    # Access logs carry the query string, and the SSE routes carry their credential there.
    assert "--no-access-log" in api["command"]
    assert "--no-access-log" in (ROOT / "security-master-api" / "Dockerfile").read_text()
    assert worker["command"] == ["python", "-m", "security_master_api.acquire"]
    for svc in (api, worker):
        files = {e["path"]: e["required"] for e in svc["env_file"]}
        assert files == {"./api-auth.env": True, "./minio.env": True, "./security-master.env": True}
        assert "tailscale" in svc["depends_on"] and "minio" in svc["depends_on"]
        assert svc["restart"] == "unless-stopped"
    assert "OPENBB_URL=http://openbb-api:6900" in api["environment"]
    assert "ports" not in api and "ports" not in worker


def test_serve_routes_publish_the_service():
    for name in ("serve.json", "serve-funnel.json"):
        cfg = json.loads((ROOT / "ts-config" / name).read_text())
        assert cfg["TCP"]["6905"] == {"HTTPS": True}
        assert cfg["Web"]["${TS_CERT_DOMAIN}:6905"]["Handlers"]["/"]["Proxy"] == "http://security-master-api:6905"
        svc = cfg["Services"]["svc:openbb-security-master"]
        assert svc["Web"]["openbb-security-master.<your-tailnet>.ts.net:443"]["Handlers"]["/"]["Proxy"] == "http://security-master-api:6905"


def test_ci_has_the_two_jobs():
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    for job in ("security-master-api", "openbb-security-master"):
        steps = ci["jobs"][job]["steps"]
        runs = [s.get("run", "") for s in steps]
        assert any("pip install -e ./openbb-deltalake" in r for r in runs)
        assert any("timeout -s ABRT 600 pytest -q --capture=sys" in r for r in runs)
        assert ci["jobs"][job]["timeout-minutes"] == 20

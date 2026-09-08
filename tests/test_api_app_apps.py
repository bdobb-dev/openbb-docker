# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""api_app.py's own /apps.json payload.

The route exists because openbb-platform-api's version is unusable here: it
EXTENDS the file with OpenBB's bundled default apps, and drops any app unless
some tab has every widget in this server's own widgets.json -- silently
discarding apps built on another backend's widgets or on bdobb's built-ins.

read_workspace_apps is module level precisely so this runs without an OpenBB
install: api_app.py has no top-level imports. The first cut inlined the body
in the route and referenced `os`, which is not in scope there, and every
request 500'd -- a NameError no import-time check would have caught.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api_app import read_workspace_apps  # noqa: E402


def test_reads_the_app_list_verbatim(tmp_path):
    apps = [{"name": "Example - Version 11.0.0", "tabs": {}}, {"name": "FRED", "tabs": {}}]
    p = tmp_path / "workspace_apps.json"
    p.write_text(json.dumps(apps), encoding="utf-8")

    assert read_workspace_apps(str(p)) == apps, "the catalogue must not be filtered or extended"


def test_a_missing_file_is_an_empty_catalogue_not_an_error(tmp_path):
    """The bind mount may not be in place yet; an app list is not worth a 500."""
    assert read_workspace_apps(str(tmp_path / "absent.json")) == []


def test_malformed_json_degrades_instead_of_raising(tmp_path):
    p = tmp_path / "workspace_apps.json"
    p.write_text("{ not json", encoding="utf-8")
    assert read_workspace_apps(str(p)) == []


def test_a_single_app_object_is_wrapped_in_a_list(tmp_path):
    p = tmp_path / "workspace_apps.json"
    p.write_text(json.dumps({"name": "Solo", "tabs": {}}), encoding="utf-8")
    assert read_workspace_apps(str(p)) == [{"name": "Solo", "tabs": {}}]


def test_the_env_var_overrides_the_default_path(tmp_path, monkeypatch):
    p = tmp_path / "elsewhere.json"
    p.write_text(json.dumps([{"name": "Env", "tabs": {}}]), encoding="utf-8")
    monkeypatch.setenv("WORKSPACE_APPS_PATH", str(p))
    assert read_workspace_apps() == [{"name": "Env", "tabs": {}}]

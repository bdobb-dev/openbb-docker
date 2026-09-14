# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

"""Runnable (pytest-free) tests for tick_vault.status_app (Task 11).

Plain `assert` + `test_*` functions, run directly with
`python3 tests/test_status_app_core.py` -- same convention as
tick-vault/tests/run_tests.py's own suite (no pytest import anywhere), so
this file has no new dependency even though it lives at the repo root
rather than under tick-vault/tests/ (task-11-brief.md's file list).

Covers: `build_status_payload`'s shape and counts passthrough,
`make_widgets_manifest`'s descriptor fields (checked against the field
names actually present in stores-explorer/widgets.json -- see
_EXPECTED_DESCRIPTOR_KEYS below), and a live end-to-end run of
`StatusServer` on an ephemeral port with fake readers (urllib GET /status
and GET /widgets.json).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request

TICK_VAULT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TICK_VAULT_ROOT not in sys.path:
    sys.path.insert(0, TICK_VAULT_ROOT)

from tick_vault.status_app import (  # noqa: E402
    StatusServer,
    build_status_payload,
    make_widgets_manifest,
)

# Field names read directly from stores-explorer/widgets.json's
# `delta_explorer` entry (see that file, checked into this repo): every
# widget descriptor there has these top-level keys, plus `gridData.w`/
# `gridData.h`. tick-vault's descriptor mirrors the same field NAMES per
# the task-11 brief, even though it is served by stdlib http.server, not
# stores-explorer's FastAPI app.
_EXPECTED_DESCRIPTOR_KEYS = {
    "name", "description", "category", "type", "endpoint", "dataKey",
    "gridData", "params", "source",
}


def test_build_status_payload_shape_and_passthrough():
    manifest_counts = {"PENDING": 3, "COMPLETE": 5, "BLOCKED_IDENTITY": 1}
    frontier = {"min_complete_week": "2020-01-06", "max_complete_week": "2020-01-13"}
    budget = {"calls_today": 42, "last_429": None}

    payload = build_status_payload(
        manifest_counts=manifest_counts,
        frontier=frontier,
        dq_open=7,
        budget=budget,
        calibration_present=True,
    )

    assert set(payload.keys()) == {
        "manifest", "frontier", "quality_issues_open", "budget", "calibration",
    }
    # Passthrough, not a re-derivation -- and not the SAME dict object (the
    # function copies rather than aliasing its caller's dict).
    assert payload["manifest"] == manifest_counts
    assert payload["manifest"] is not manifest_counts
    assert payload["frontier"] == frontier
    assert payload["quality_issues_open"] == 7
    assert payload["budget"] == budget
    assert payload["calibration"] == "present"


def test_build_status_payload_calibration_absent():
    payload = build_status_payload({}, {}, 0, {}, calibration_present=False)
    assert payload["calibration"] == "absent"
    assert payload["manifest"] == {}
    assert payload["quality_issues_open"] == 0


def test_make_widgets_manifest_descriptor_fields():
    manifest = make_widgets_manifest("http://127.0.0.1:8600")
    assert isinstance(manifest, dict)
    assert len(manifest) == 1
    (widget_id, descriptor), = manifest.items()
    assert isinstance(widget_id, str) and widget_id

    assert set(descriptor.keys()) == _EXPECTED_DESCRIPTOR_KEYS
    assert descriptor["endpoint"] == "http://127.0.0.1:8600/status"
    assert set(descriptor["gridData"].keys()) == {"w", "h"}
    assert isinstance(descriptor["params"], list)
    assert isinstance(descriptor["source"], list) and descriptor["source"]


def test_make_widgets_manifest_strips_trailing_slash():
    manifest = make_widgets_manifest("http://127.0.0.1:8600/")
    (descriptor,) = manifest.values()
    assert descriptor["endpoint"] == "http://127.0.0.1:8600/status"


def _fake_readers():
    return {
        "manifest_counts": lambda: {"PENDING": 2, "COMPLETE": 10},
        "frontier": lambda: {"min_complete_week": "2020-01-06", "max_complete_week": "2020-01-13"},
        "dq_open": lambda: 1,
        "budget": lambda: {"calls_today": 100, "last_429": "2026-09-13T00:00:00Z"},
        "calibration_present": lambda: True,
    }


def test_status_server_end_to_end_status_and_widgets():
    server = StatusServer("./unused_vault_root", _fake_readers(), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = server.base_url

        with urllib.request.urlopen(f"{base}/status", timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "application/json"
            body = json.loads(resp.read().decode("utf-8"))

        assert body == {
            "manifest": {"PENDING": 2, "COMPLETE": 10},
            "frontier": {"min_complete_week": "2020-01-06", "max_complete_week": "2020-01-13"},
            "quality_issues_open": 1,
            "budget": {"calls_today": 100, "last_429": "2026-09-13T00:00:00Z"},
            "calibration": "present",
        }

        with urllib.request.urlopen(f"{base}/widgets.json", timeout=5) as resp:
            assert resp.status == 200
            widgets = json.loads(resp.read().decode("utf-8"))
        assert set(next(iter(widgets.values())).keys()) == _EXPECTED_DESCRIPTOR_KEYS
        assert next(iter(widgets.values()))["endpoint"] == f"{base}/status"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_default_frontier_isoformats_real_dates_through_live_server():
    """I4 regression: `_default_frontier`'s real reader used to return raw
    `datetime.date`/pandas `Timestamp` objects, which `json.dumps` cannot
    serialize (`TypeError: Object of type date is not JSON serializable`).
    This drives the ACTUAL `_default_frontier` function (not a fake string
    reader like `_fake_readers` above) with a fake `ops.backfill_manifest`
    frame carrying real `datetime.date` values, through a real
    `StatusServer` HTTP round-trip, and asserts the JSON body carries
    ISO-8601 strings."""
    import datetime as dt

    import pandas as pd

    from tick_vault import cli as cli_mod
    from tick_vault.status_app import _default_frontier

    manifest_df = pd.DataFrame([
        {"work_id": "wrk_a", "status": "PENDING", "week_monday": dt.date(2020, 1, 6)},
        {"work_id": "wrk_b", "status": "PENDING", "week_monday": dt.date(2020, 1, 13)},
        {"work_id": "wrk_c", "status": "COMPLETE", "week_monday": dt.date(2019, 12, 30)},
    ])

    original_reader = cli_mod._default_manifest_reader
    cli_mod._default_manifest_reader = lambda root: manifest_df
    try:
        readers = _fake_readers()
        readers["frontier"] = lambda: _default_frontier("./unused_vault_root")

        server = StatusServer("./unused_vault_root", readers, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = server.base_url
            with urllib.request.urlopen(f"{base}/status", timeout=5) as resp:
                assert resp.status == 200
                body = json.loads(resp.read().decode("utf-8"))
            assert body["frontier"] == {
                "min_complete_week": "2020-01-06",
                "max_complete_week": "2020-01-13",
            }
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    finally:
        cli_mod._default_manifest_reader = original_reader


def test_status_server_unknown_path_is_404():
    server = StatusServer("./unused_vault_root", _fake_readers(), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = server.base_url
        try:
            urllib.request.urlopen(f"{base}/nope", timeout=5)
            raise AssertionError("expected HTTPError for unknown path")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    import traceback

    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS: {name}")
                passed += 1
            except Exception:
                print(f"FAIL: {name}")
                traceback.print_exc()
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(0 if failed == 0 else 1)

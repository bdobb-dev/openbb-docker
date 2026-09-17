# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import json
import subprocess
import sys
from pathlib import Path

import pytest

import security_master_api
from security_master_api.config import Settings
from security_master_api.store.seed import FIXTURE_DIR, golden_fixtures, seed
from security_master_api.store.tables import latest_version, open_table


@pytest.fixture
def settings(tmp_path):
    return Settings(root=str(tmp_path), preflight_secret="k")


def test_fixture_dir_is_packaged():
    assert FIXTURE_DIR == Path(security_master_api.__file__).parent / "fixtures"
    names = sorted(p.name for p in FIXTURE_DIR.glob("*.json"))
    assert names == [
        "cusip_change.json", "dubai_eid_al_fitr_2024.json", "eid_lunar_correction.json",
        "india_bakri_eid_2023.json", "lunar_new_year_special_session.json",
        "religious_observance_no_closure.json", "shares_outstanding.json",
        "ticker_change.json", "trade_correction.json",
    ]


def test_golden_fixtures_validate():
    fixtures = golden_fixtures()
    assert {f["fixture_id"] for f in fixtures} == {
        "trade-correction", "shares-outstanding", "cusip-change", "ticker-change",
        "eid-lunar-correction", "lunar-new-year-special-session",
        "religious-observance-no-closure", "india-bakri-eid-2023", "dubai-eid-al-fitr-2024",
    }
    for f in fixtures:
        for relation, rows in f["assertions"].items():
            assert relation.startswith("silver."), relation
            for row in rows:
                assert row["assertion_id"], relation


def test_seed_writes_every_relation_once_and_is_idempotent(settings):
    versions = seed(settings)
    assert versions["silver.prices_normalized"] >= 1
    assert versions["ops.jobs"] == 0
    prices = open_table(settings, "silver.prices_normalized").to_pyarrow_table().to_pylist()
    assert {r["assertion_id"] for r in prices} >= {
        "as_px_apple_20260914_1", "as_px_apple_20260914_2"
    }
    again = seed(settings)
    assert again == versions
    assert latest_version(settings, "silver.prices_normalized") == (
        versions["silver.prices_normalized"]
    )


def test_seed_command(tmp_path):
    out = subprocess.run(
        [sys.executable, "-m", "security_master_api.store", "--root", str(tmp_path)],
        capture_output=True, text=True, check=True,
        env={"SECURITY_MASTER_PREFLIGHT_SECRET": "k", "PATH": "", "PYTHONPATH": str(Path.cwd())},
    )
    report = json.loads(out.stdout)
    assert report["silver.listings"] >= 1

# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
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
from security_master_api.temporal_fixture import _instant


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


def _visible_at(rows: list[dict], known_at: str) -> set[tuple[str, str, str | None]]:
    """Project bitemporal `rows` (one relation) at `known_at`.

    Rule: take the latest row per assertion_id with system_from <= known_at,
    then keep it only if available_at <= known_at and (system_to is null or
    system_to > known_at).
    """
    cutoff = _instant(known_at)
    latest: dict[str, dict] = {}
    for row in rows:
        if _instant(row["system_from"]) > cutoff:
            continue
        current = latest.get(row["assertion_id"])
        if current is None or _instant(row["system_from"]) > _instant(current["system_from"]):
            latest[row["assertion_id"]] = row
    visible = set()
    for row in latest.values():
        if _instant(row["available_at"]) > cutoff:
            continue
        if row["system_to"] is not None and _instant(row["system_to"]) <= cutoff:
            continue
        visible.add((row["session_date"], row["assertion_domain"], row["market_effect"]))
    return visible


def test_india_t0_holiday_and_market_session_rows_project_correctly():
    # Regression for the reviewer finding that as_india_t0 mixed a holiday_date
    # assertion with market_effect: closed, which made the fixture's own T1
    # state (06-28 closed, 06-29 pending) briefly unreachable between T1's
    # supersession of T0 (06-26T12:00) and T2's market rows (06-27T12:00).
    fixtures = golden_fixtures()
    india = next(f for f in fixtures if f["fixture_id"] == "india-bakri-eid-2023")
    rows = india["assertions"]["silver.calendar_exceptions"]

    # T0: 2022-12-08T18:00:00Z -- exchange calendar projects 06-28 closed, no
    # exception at all for 06-29 (i.e. open by default).
    assert _visible_at(rows, "2022-12-08T18:00:00Z") == {
        ("2023-06-28", "holiday_date", None),
        ("2023-06-28", "market_session", "closed"),
    }

    # T1: 2023-06-26T12:00:00Z -- government notice moves the holiday to
    # 06-29; the exchange's market_session closure for 06-28 survives the
    # supersession of the T0 holiday_date row (this is the finding's fix).
    assert _visible_at(rows, "2023-06-26T12:00:00Z") == {
        ("2023-06-28", "market_session", "closed"),
        ("2023-06-29", "holiday_date", None),
    }

    # T2: 2023-06-27T12:00:00Z -- clearing notice: 06-28 open, 06-29 closed;
    # the holiday_date row for 06-29 persists alongside the market_session row.
    assert _visible_at(rows, "2023-06-27T12:00:00Z") == {
        ("2023-06-28", "market_session", "open"),
        ("2023-06-29", "holiday_date", None),
        ("2023-06-29", "market_session", "closed"),
    }


def test_seed_command(tmp_path):
    out = subprocess.run(
        [sys.executable, "-m", "security_master_api.store", "--root", str(tmp_path)],
        capture_output=True, text=True, check=True,
        env={"SECURITY_MASTER_PREFLIGHT_SECRET": "k", "PATH": "", "PYTHONPATH": str(Path.cwd())},
    )
    report = json.loads(out.stdout)
    assert report["silver.listings"] >= 1

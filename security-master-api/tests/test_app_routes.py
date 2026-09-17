# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from security_master_api.app.main import create_app
from security_master_api.config import Settings
from security_master_api.store.seed import seed

CONTRACT = Path(__file__).parent / "contract"
V1 = "/security-master/v1"


def record(name: str, payload) -> None:
    CONTRACT.mkdir(exist_ok=True)
    (CONTRACT / f"{name}.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "false")
    s = Settings(root=str(tmp_path), preflight_secret="k", first_page=3)
    seed(s)
    return TestClient(create_app(s))


def test_catalog(client):
    r = client.get(f"{V1}/catalog")
    assert r.status_code == 200
    body = r.json()
    assert body["layers"] == ["gold", "silver", "bronze", "ops"]
    assert body["policies"] == {"sql": "enabled", "acquisition": "review"}
    assert body["odp_version"] == "openbb==4.7.2"
    assert body["newer_data_available"] is False
    names = {f"{x['layer']}.{x['name']}" for x in body["relations"]}
    assert {"gold.market_calendar", "silver.listings", "bronze.source_captures",
            "ops.receipts"} <= names
    record("catalog", body)


def test_relation_and_versions(client):
    r = client.get(f"{V1}/relations/silver/listings")
    assert r.status_code == 200
    body = r.json()
    assert body["relation"]["modes"] == ["current_corrected", "known_at", "effective_on",
                                         "delta_snapshot"]
    assert {"name", "dtype"} <= set(body["schema"][0])
    assert body["latest_version"] >= 1 and body["count"]["quality"] == "exact"
    record("relation", body)
    v = client.get(f"{V1}/relations/silver/listings/versions").json()
    assert v["versions"][0]["version"] >= 1 and "retained" in v["versions"][0]
    record("versions", v)
    assert client.get(f"{V1}/relations/gold/nope").json()["error"]["code"] == "QUERY_REJECTED"


def test_odp_models_and_query(client):
    models = client.get(f"{V1}/odp/models").json()
    assert [m["model_id"] for m in models["models"]][:2] == ["EquityInfo", "EquitySearch"]
    record("odp_models", models)
    one = client.get(f"{V1}/odp/models/MarketCalendar").json()
    assert one["model"]["primary_temporal_scenario"] == "lunar_holiday_correction"
    assert one["scenario"]["scenario"] == "lunar_holiday_correction"
    record("odp_model_market_calendar", one)
    q = client.post(f"{V1}/odp/models/EquityHistorical/query", json={
        "parameters": {"symbol": "AAPL", "start_date": "2026-09-14", "end_date": "2026-09-14"},
        "context": {"mode": "known_at", "effective_at": "2026-09-14T20:00:00Z",
                    "known_at": "2026-09-14T20:00:00Z"}})
    assert q.status_code == 404 and q.json()["error"]["code"] == "NO_LOCAL_EVIDENCE"
    record("error_no_local_evidence", q.json())
    q = client.post(f"{V1}/odp/models/EquityHistorical/query", json={
        "parameters": {"symbol": "AAPL", "start_date": "2026-09-14", "end_date": "2026-09-14"},
        "context": {"mode": "known_at", "effective_at": "2026-09-14T20:00:00Z",
                    "known_at": "2026-09-15T09:20:00Z"}})
    assert q.status_code == 200 and q.json()["results"][0]["close"] == 231.74
    assert q.json()["extra"]["security_master_receipt"]["temporal_mode"] == "known_at"
    record("odp_query", q.json())


def test_preview_pages_with_a_context_bound_cursor(client):
    body = {"relation": "gold.security_master", "context": {"mode": "current_corrected"},
            "columns": ["listing_id", "symbol"], "filters": [],
            "sort": [{"field": "symbol", "direction": "asc"}],
            "page": {"limit": 3, "cursor": None}}
    r = client.post(f"{V1}/preview", json=body)
    assert r.status_code == 200
    page = r.json()
    assert len(page["rows"]) == 3 and page["next_cursor"] and page["receipt"]["kind"] == "preview"
    record("preview", page)
    body["page"]["cursor"] = page["next_cursor"]
    nxt = client.post(f"{V1}/preview", json=body).json()
    assert nxt["receipt"]["execution_id"] == page["receipt"]["execution_id"]
    body["context"] = {"mode": "effective_on", "effective_at": "2022-06-07T00:00:00Z"}
    assert client.post(f"{V1}/preview", json=body).json()["error"]["code"] == "QUERY_REJECTED"


def test_preview_refuses_unsupported_mode(client):
    r = client.post(f"{V1}/preview", json={
        "relation": "bronze.source_captures",
        "context": {"mode": "known_at", "known_at": "2026-01-01T00:00:00Z"}})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["supported_modes"] == ["captured_by", "delta_snapshot"]
    record("error_mode_unsupported", r.json())


def test_sql_plan_execute_page_cancel(client):
    plan = client.post(f"{V1}/sql/plan", json={
        "sql": "SELECT symbol FROM gold.security_master ORDER BY symbol",
        "context": {"mode": "current_corrected"}}).json()
    assert plan["relations"] == ["gold.security_master"] and plan["columns"][0]["name"] == "symbol"
    record("sql_plan", plan)
    ex = client.post(f"{V1}/sql/execute", json={
        "sql": "SELECT symbol FROM gold.security_master ORDER BY symbol",
        "context": {"mode": "current_corrected"},
        "budgets": {"max_rows": 100, "timeout_ms": 5000}}).json()
    assert ex["rows"] and ex["next_cursor"] and ex["receipt"]["kind"] == "sql"
    record("sql_execute", ex)
    page = client.get(f"{V1}/sql/executions/{ex['execution_id']}/pages",
                      params={"cursor": ex["next_cursor"]}).json()
    assert page["execution_id"] == ex["execution_id"]
    assert client.delete(f"{V1}/sql/executions/{ex['execution_id']}").status_code == 204
    assert client.delete(f"{V1}/sql/executions/qry_missing").status_code == 404
    bad = client.post(f"{V1}/sql/execute", json={"sql": "DROP TABLE silver.listings",
                                                 "context": {}}).json()
    assert bad["error"]["code"] == "QUERY_REJECTED"
    record("error_query_rejected", bad)


def test_sql_disabled_by_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBB_API_AUTH", "false")
    s = Settings(root=str(tmp_path), preflight_secret="k", sql_policy="disabled")
    seed(s)
    c = TestClient(create_app(s))
    r = c.post(f"{V1}/sql/execute", json={"sql": "SELECT 1", "context": {}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "QUERY_REJECTED"
    assert c.get(f"{V1}/catalog").json()["policies"]["sql"] == "disabled"


def test_resolve_lineage_compare(client):
    r = client.post(f"{V1}/resolve", json={
        "identifier": "FB",
        "context": {"mode": "effective_on", "effective_at": "2022-06-07T00:00:00Z"}}).json()
    assert r["candidates"][0]["listing_id"] == "lst_000042" and r["receipt"]["kind"] == "resolve"
    record("resolve", r)
    amb = client.post(f"{V1}/resolve", json={"identifier": "ZZZZ", "context": {}}).json()
    assert amb["error"]["code"] == "IDENTITY_UNRESOLVED"
    lin = client.post(f"{V1}/lineage", json={"relation": "silver.prices_normalized",
                                             "assertion_id": "as_px_apple_20260914_2",
                                             "context": {}}).json()
    assert lin["chain"][0]["kind"] == "assertion" and lin["receipt"]["kind"] == "lineage"
    record("lineage", lin)
    cmp_ = client.post(f"{V1}/compare", json={
        "relation": "gold.price_daily",
        "selection": {"listing_id": "lst_apple", "market_date": "2026-09-14"},
        "left": {"mode": "known_at", "known_at": "2026-09-14T20:05:00Z"},
        "right": {"mode": "known_at", "known_at": "2026-09-15T09:20:00Z"}}).json()
    assert cmp_["differences"][0]["classification"] in ("corrected", "newly_known")
    assert cmp_["left_receipt"]["kind"] == "compare" and cmp_["right_receipt"]["kind"] == "compare"
    record("compare", cmp_)


def test_compare_refuses_an_unknown_selection_key(client):
    bad = client.post(f"{V1}/compare", json={
        "relation": "gold.price_daily", "selection": {"nope": "x"},
        "left": {"mode": "current_corrected"}, "right": {"mode": "current_corrected"}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "QUERY_REJECTED"


def test_versions_compare_and_exchanges(client):
    vc = client.post(f"{V1}/versions/compare", json={"relation": "silver.issuers",
                                                     "left": 0, "right": 1}).json()
    assert vc["schema"]["added"] == [] and vc["rows"]["left"] == 0 and vc["rows"]["right"] >= 1
    record("versions_compare", vc)
    ex = client.get(f"{V1}/exchanges").json()
    assert any(e["calendar_id"] == "cal_tadawul" for e in ex["exchanges"])
    record("exchanges", ex)


def test_widgets_and_apps(client):
    w = client.get("/widgets.json").json()
    assert w["security_master_browser"]["type"] == "security_master_browser"
    assert [p["paramName"] for p in w["security_master_browser"]["params"]] == [
        "listing_id", "effective_date", "known_at", "layer", "temporal_mode"]
    assert isinstance(client.get("/apps.json").json(), list)

# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
import pytest

from security_master_api.config import Settings
from security_master_api.errors import DomainError
from security_master_api.resolver import catalog as catalog_module
from security_master_api.resolver.catalog import catalog, check_modes, expand, relation
from security_master_api.resolver.context import parse_context
from security_master_api.resolver.manifest import resolve_manifest
from security_master_api.store.seed import seed


@pytest.fixture
def settings(tmp_path):
    s = Settings(root=str(tmp_path), preflight_secret="k")
    seed(s)
    return s


def test_catalog_lists_every_declared_relation_with_capabilities(settings):
    rels = {r.full: r for r in catalog(settings)}
    assert rels["silver.listings"].modes == ("current_corrected", "known_at", "effective_on", "delta_snapshot")
    assert rels["bronze.source_captures"].modes == ("captured_by", "delta_snapshot")
    assert rels["gold.market_calendar"].kind == "view"
    assert set(rels["gold.market_calendar"].depends_on) == {
        "silver.market_sessions", "silver.calendar_exceptions", "silver.exchanges",
        "silver.session_interruptions",
    }
    assert rels["ops.receipts"].modes == ("delta_snapshot",)


def test_external_libraries_appear_as_bronze_snapshots(tmp_path):
    base = tmp_path / "bucket"
    (base / "openbb" / "AAPL" / "_delta_log").mkdir(parents=True)
    (base / "openbb" / "notatable").mkdir(parents=True)
    s = Settings(root=str(base / "security_master"), delta_base=str(base), preflight_secret="k")
    seed(s)
    rels = {r.full: r for r in catalog(s)}
    assert rels["bronze.openbb.AAPL"].kind == "external_delta"
    assert rels["bronze.openbb.AAPL"].modes == ("delta_snapshot", "captured_by")
    assert "bronze.openbb.notatable" not in rels


def test_unknown_relation_is_a_domain_error(settings):
    with pytest.raises(DomainError) as exc:
        relation(settings, "gold.nope")
    assert exc.value.code == "QUERY_REJECTED"


def test_check_modes_refuses_without_substitution(settings):
    ctx = parse_context({"mode": "known_at", "known_at": "2026-01-01T00:00:00Z"})
    with pytest.raises(DomainError) as exc:
        check_modes(settings, ctx, ["silver.listings", "bronze.source_captures"])
    assert exc.value.code == "TEMPORAL_MODE_UNSUPPORTED"
    assert exc.value.details["relation"] == "bronze.source_captures"
    assert "supported_modes" in exc.value.details


def test_expand_adds_gold_dependencies(settings):
    assert "silver.calendar_exceptions" in expand(settings, ["gold.market_calendar"])


def test_manifest_pins_latest_or_requested_versions(settings):
    ctx = parse_context(None)
    manifest = resolve_manifest(settings, ctx, ["gold.security_master"])
    assert set(manifest) >= {"silver.listings", "silver.identifiers"}
    assert all(isinstance(v, int) for v in manifest.values())
    snap = parse_context({"mode": "delta_snapshot", "versions": {"silver.listings": 0}})
    assert resolve_manifest(settings, snap, ["silver.listings"]) == {"silver.listings": 0}
    bad = parse_context({"mode": "delta_snapshot", "versions": {"silver.listings": 99}})
    with pytest.raises(DomainError) as exc:
        resolve_manifest(settings, bad, ["silver.listings"])
    assert exc.value.code == "VERSION_NOT_RETAINED"


def test_one_request_builds_the_catalog_once(settings, monkeypatch):
    """`external_relations` lists an object store; a fan-out over relations must not repeat it."""
    calls = []
    real = catalog_module.external_relations
    monkeypatch.setattr(catalog_module, "external_relations",
                        lambda s: (calls.append(s), real(s))[1])
    resolve_manifest(settings, parse_context(None), ["gold.security_master"])
    assert len(calls) <= 1

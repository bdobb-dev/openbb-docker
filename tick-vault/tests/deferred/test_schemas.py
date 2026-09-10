"""Deferred tests for tick_vault.schemas (task-2 brief, Step 1).

Requires pyarrow, deltalake, pytest - none of which are installable in the
authoring sandbox (network blocked). Placed under tests/deferred/ so the
sandbox mini-runner (tests/run_tests.py, which os.listdir()s tests/ non-
recursively for test_*.py) does not pick these up. Run with:

    pytest tick-vault/tests

once the dependencies are available. UNVERIFIED until then.
"""
import pyarrow as pa
from deltalake import DeltaTable

from tick_vault.schemas import SCHEMAS, PARTITIONING, create_all


def test_registry_covers_layers():
    layers = {n.split(".")[0] for n in SCHEMAS}
    assert layers == {"bronze", "silver", "gold", "ops"}
    assert "silver.us_trade_tick_version" in SCHEMAS


def test_tick_version_has_spec_delta_columns():
    f = {x.name for x in SCHEMAS["silver.us_trade_tick_version"]}
    assert {"session_seq", "sub_mkt_raw", "is_cancelled", "is_late_add",
            "origin", "revision_number", "supersedes_tick_version_id",
            "available_at_ts", "trade_ts_ms", "logical_tick_id"} <= f


def test_tick_version_is_superset_of_parser_columns():
    from tick_vault.tick_parser import COLUMNS
    f = {x.name for x in SCHEMAS["silver.us_trade_tick_version"]}
    assert set(COLUMNS) <= f


def test_tick_correction_event_matches_diffing_event_columns():
    from tick_vault.diffing import EVENT_COLUMNS
    f = {x.name for x in SCHEMAS["silver.tick_correction_event"]}
    assert set(EVENT_COLUMNS) == f


def test_create_all_and_partitioning(tmp_path):
    create_all(str(tmp_path))
    dt = DeltaTable(str(tmp_path / "silver" / "us_trade_tick_version"))
    assert dt.metadata().partition_columns == ["trade_date"]
    dt2 = DeltaTable(str(tmp_path / "ops" / "backfill_manifest"))
    assert dt2.metadata().partition_columns == ["week_monday"]


def test_create_all_writes_every_table(tmp_path):
    create_all(str(tmp_path))
    for name in SCHEMAS:
        layer, table = name.split(".", 1)
        dt = DeltaTable(str(tmp_path / layer / table))
        # `DeltaTable.schema().to_pyarrow()` was removed in deltalake>=1.0
        # (I3, final-review finding) - compare field NAMES only, via
        # `.fields`, which iterates stably across deltalake versions,
        # rather than round-tripping through a pyarrow schema.
        assert {f.name for f in dt.schema().fields} == \
            {f.name for f in SCHEMAS[name]}

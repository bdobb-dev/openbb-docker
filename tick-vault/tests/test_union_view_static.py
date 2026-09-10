"""Static (no-duckdb) consistency checks for tick_vault/union_view.py and
its wiring into tick_vault/contract.py (task-9 brief, preflight controller
ruling).

duckdb/deltalake aren't installable in this sandbox, so neither module can
be exercised end-to-end here - both import `duckdb` lazily specifically so
they stay importable without it. This test instead py-compiles both
modules, imports `tick_vault.union_view` bare, and inspects source text
(via plain substring checks, mirroring `tests/test_temporal_sql_static.py`
and `tests/test_contract_static.py`'s approach for Tasks 6/8) to give real
assurance about the union-view shape and the contract.py wiring without
ever importing duckdb.

Checks:
  - both modules py-compile
  - `tick_vault.union_view` imports bare and exposes `install_union_view`,
    `UnionViewHandle`, `LEGACY_ORIGIN`, `SILVER_COLUMNS`
  - `vault_trade_tick` and a `UNION ALL` are present in union_view.py's
    source
  - the `'LEGACY_UNVERSIONED'` literal is present
  - the `logical_tick_id` construction (symbol || '|' || trade_date || '|'
    || seq, via the `day`/`seq` legacy columns) is present
  - `tick_vault/contract.py` has a `legacy_root` parameter and references
    `vault_trade_tick`
  - the PIT-exclusion rule is documented: both availability-policy name
    literals (`AS_INGESTED_LOCAL_V1`, `HISTORICAL_VENDOR_FINAL_V1`) appear
    somewhere in union_view.py or contract.py's union-path source
"""
import importlib
import os
import py_compile

_TICK_VAULT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tick_vault"
)
_UNION_VIEW_PATH = os.path.join(_TICK_VAULT_DIR, "union_view.py")
_CONTRACT_PATH = os.path.join(_TICK_VAULT_DIR, "contract.py")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_union_view_module_py_compiles():
    py_compile.compile(_UNION_VIEW_PATH, doraise=True)


def test_contract_module_py_compiles():
    py_compile.compile(_CONTRACT_PATH, doraise=True)


def test_union_view_module_imports_without_duckdb():
    module = importlib.import_module("tick_vault.union_view")
    assert hasattr(module, "install_union_view")
    assert hasattr(module, "UnionViewHandle")
    assert hasattr(module, "LEGACY_ORIGIN")
    assert hasattr(module, "SILVER_COLUMNS")
    assert module.LEGACY_ORIGIN == "LEGACY_UNVERSIONED"


def test_union_view_handle_has_union_provenance_method():
    module = importlib.import_module("tick_vault.union_view")
    assert hasattr(module.UnionViewHandle, "union_provenance")


def test_vault_trade_tick_view_and_union_all_present():
    source = _read(_UNION_VIEW_PATH)
    assert "vault_trade_tick" in source
    assert "UNION ALL" in source


def test_legacy_origin_literal_present():
    source = _read(_UNION_VIEW_PATH)
    assert "LEGACY_UNVERSIONED" in source


def test_logical_tick_id_construction_present():
    source = _read(_UNION_VIEW_PATH)
    assert "logical_tick_id" in source
    # symbol || '|' || trade_date || '|' || seq, built from the legacy
    # `day`/`seq` columns.
    assert "'|'" in source
    assert "CAST(day AS DATE)" in source
    assert "seq" in source


def test_contract_has_legacy_root_param_and_vault_trade_tick_reference():
    source = _read(_CONTRACT_PATH)
    assert "legacy_root" in source
    assert "watermarks" in source
    assert "vault_trade_tick" in source
    assert "install_union_view" in source


def test_pit_exclusion_rule_documented_with_both_policy_names():
    combined_source = _read(_UNION_VIEW_PATH) + _read(_CONTRACT_PATH)
    assert "AS_INGESTED_LOCAL_V1" in combined_source
    assert "HISTORICAL_VENDOR_FINAL_V1" in combined_source
    # The actual exclusion condition in contract.py's union PIT path.
    assert "origin != 'LEGACY_UNVERSIONED'" in _read(_CONTRACT_PATH)

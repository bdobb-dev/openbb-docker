"""Static (no-duckdb) consistency checks for tick_vault/reference_queries.py.

duckdb/deltalake aren't installable in this sandbox, so
`tick_vault.reference_queries` cannot be exercised end-to-end here (it
imports `duckdb` lazily inside `install_reference_macros` specifically so
the *module* can still be imported without it). This test instead
py-compiles the module and inspects its source text (via the `ast` module
plus substring checks on the macro SQL literals) to give real, executable
assurance about the macros' shape without ever importing duckdb, mirroring
`tests/test_temporal_sql_static.py`'s approach for Task 6.

Checks:
  - the module py-compiles and imports cleanly (no duckdb import needed)
  - `pit_sp500_membership`'s SQL contains the baseline Sec 8.4 predicate
    set verbatim in spirit: `membership_effective_from <=`,
    `membership_effective_to IS NULL OR >`, `available_at_ts <=`,
    `system_to_ts IS NULL OR >`, and `resolution_status = 'RESOLVED'`
  - `pit_listing`'s SQL references both the `EODHD_SYMBOL` and `TICKER`
    namespaces and the effective-interval predicates
  - `current_sp500`'s SQL contains NO `available_at_ts` reference (the
    effective-only mode, no knowledge-time predicate)
"""
import ast
import importlib
import os
import py_compile

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tick_vault",
    "reference_queries.py",
)


def _read_source() -> str:
    with open(_MODULE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _parse_module() -> ast.Module:
    return ast.parse(_read_source(), filename=_MODULE_PATH)


def _collect_execute_sql_literals(tree: ast.Module, func_name: str) -> list:
    """Return every string literal passed to `con.execute(...)` inside the
    module-level function `func_name`, in source order."""
    func_node = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            func_node = node
            break
    assert func_node is not None, f"could not find function def {func_name!r}"

    literals = []
    for call_node in ast.walk(func_node):
        if (
            isinstance(call_node, ast.Call)
            and isinstance(call_node.func, ast.Attribute)
            and call_node.func.attr == "execute"
            and call_node.args
            and isinstance(call_node.args[0], ast.Constant)
            and isinstance(call_node.args[0].value, str)
        ):
            literals.append(call_node.args[0].value)
    return literals


def _macro_sql(literals, macro_name: str) -> str:
    marker = f"MACRO {macro_name}("
    sql = next((s for s in literals if marker in s), None)
    assert sql is not None, f"could not find {macro_name} macro SQL"
    return sql


def test_reference_queries_module_py_compiles():
    py_compile.compile(_MODULE_PATH, doraise=True)


def test_reference_queries_module_imports_without_duckdb():
    # `tick_vault.reference_queries` must not do `import duckdb` at module
    # scope - duckdb is not installed in this sandbox, and the module must
    # still be importable (duckdb is imported lazily inside
    # install_reference_macros()).
    module = importlib.import_module("tick_vault.reference_queries")
    assert hasattr(module, "install_reference_macros")


def test_pit_sp500_membership_sql_has_baseline_8_4_predicates():
    tree = _parse_module()
    literals = _collect_execute_sql_literals(tree, "install_reference_macros")
    sql = _macro_sql(literals, "pit_sp500_membership")

    assert "membership_effective_from <=" in sql
    assert "membership_effective_to IS NULL" in sql
    assert "OR" in sql and "membership_effective_to > market_date" in sql
    assert "available_at_ts <= decision_ts" in sql
    assert "system_to_ts IS NULL" in sql
    assert "system_to_ts > decision_ts" in sql
    assert "resolution_status = 'RESOLVED'" in sql
    assert "index_id = 'idx_sp500'" in sql
    assert "DISTINCT" in sql.upper()
    assert "listing_id" in sql and "instrument_id" in sql


def test_pit_listing_sql_has_namespaces_and_effective_interval():
    tree = _parse_module()
    literals = _collect_execute_sql_literals(tree, "install_reference_macros")
    sql = _macro_sql(literals, "pit_listing")

    assert "EODHD_SYMBOL" in sql
    assert "TICKER" in sql
    assert "id_namespace IN" in sql
    assert "id_value = symbol" in sql
    assert "effective_from_ts" in sql
    assert "effective_to_ts" in sql
    assert "available_at_ts <= decision_ts" in sql
    assert "system_to_ts IS NULL" in sql
    assert "system_to_ts > decision_ts" in sql
    assert "DISTINCT" in sql.upper()
    assert "listing_id" in sql and "instrument_id" in sql


def test_current_sp500_sql_has_no_availability_predicate():
    tree = _parse_module()
    literals = _collect_execute_sql_literals(tree, "install_reference_macros")
    sql = _macro_sql(literals, "current_sp500")

    # Effective-only mode: no knowledge-time predicate at all.
    assert "available_at_ts" not in sql
    assert "system_to_ts" not in sql
    assert "decision_ts" not in sql

    # But it keeps the effective-interval and resolution predicates.
    assert "membership_effective_from <=" in sql
    assert "membership_effective_to IS NULL" in sql
    assert "resolution_status = 'RESOLVED'" in sql
    assert "index_id = 'idx_sp500'" in sql

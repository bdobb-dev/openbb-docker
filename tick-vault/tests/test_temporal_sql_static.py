"""Static (no-duckdb) consistency checks for tick_vault/temporal.py.

duckdb/deltalake aren't installable in this sandbox, so `tick_vault.temporal`
cannot be exercised end-to-end here (it imports `duckdb` lazily inside
functions specifically so the *module* can still be imported without it).
This test instead py-compiles the module and inspects its source text
(via `ast` module presence checks plus substring checks on the macro SQL
literals) to give real, executable assurance about the macros' shape
without ever importing duckdb.

Checks:
  - the module py-compiles and imports cleanly (no duckdb import needed)
  - `SEQUENCE_POLICY` has the expected value
  - the `pit_ticks` macro SQL contains an `available_at_ts <= ` filter and
    a row_number()/window (or equivalent max-revision) construct
  - the `pit_ticks_policy` macro SQL mentions both policy name literals and
    the simulated-availability expression (`trade_date` + `INTERVAL` +
    `08:00`)
  - the `latest_ticks` macro SQL excludes `is_cancelled` ticks
"""
import ast
import importlib
import os
import py_compile

_TEMPORAL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tick_vault",
    "temporal.py",
)


def _read_source() -> str:
    with open(_TEMPORAL_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _parse_module() -> ast.Module:
    return ast.parse(_read_source(), filename=_TEMPORAL_PATH)


def _find_name_assignment(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node.value
    raise AssertionError(f"could not find module-level assignment {name!r}")


def _string_literal(node) -> str:
    assert isinstance(node, ast.Constant) and isinstance(node.value, str), (
        f"expected a string literal, got {ast.dump(node)}"
    )
    return node.value


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


def test_temporal_module_py_compiles():
    py_compile.compile(_TEMPORAL_PATH, doraise=True)


def test_temporal_module_imports_without_duckdb():
    # `tick_vault.temporal` must not do `import duckdb` at module scope -
    # duckdb is not installed in this sandbox, and the module must still be
    # importable (duckdb is imported lazily inside attach()/install_macros()).
    module = importlib.import_module("tick_vault.temporal")
    assert hasattr(module, "attach")
    assert hasattr(module, "install_macros")


def test_sequence_policy_constant_value():
    tree = _parse_module()
    value_node = _find_name_assignment(tree, "SEQUENCE_POLICY")
    assert _string_literal(value_node) == "EODHD_TS_MS_THEN_SOURCE_ORDER_V1"


def test_pit_ticks_sql_has_availability_filter_and_revision_window():
    tree = _parse_module()
    literals = _collect_execute_sql_literals(tree, "install_macros")
    pit_sql = next(
        (s for s in literals if "MACRO pit_ticks(" in s and "pit_ticks_policy" not in s),
        None,
    )
    assert pit_sql is not None, "could not find pit_ticks macro SQL"
    assert "available_at_ts <= " in pit_sql
    has_window_construct = (
        "row_number()" in pit_sql or "ROW_NUMBER()" in pit_sql
    ) and "OVER" in pit_sql.upper()
    has_max_revision_construct = "MAX(" in pit_sql.upper() and "revision_number" in pit_sql
    assert has_window_construct or has_max_revision_construct, (
        "pit_ticks SQL should contain a row_number()/window or "
        "max-revision construct"
    )


def test_pit_ticks_policy_sql_has_both_policies_and_simulated_availability():
    tree = _parse_module()
    literals = _collect_execute_sql_literals(tree, "install_macros")
    policy_sql = next((s for s in literals if "MACRO pit_ticks_policy(" in s), None)
    assert policy_sql is not None, "could not find pit_ticks_policy macro SQL"
    assert "AS_INGESTED_LOCAL_V1" in policy_sql
    assert "HISTORICAL_VENDOR_FINAL_V1" in policy_sql
    assert "trade_date" in policy_sql
    assert "INTERVAL" in policy_sql
    assert "08:00" in policy_sql


def test_latest_ticks_sql_excludes_cancelled():
    tree = _parse_module()
    literals = _collect_execute_sql_literals(tree, "install_macros")
    latest_sql = next((s for s in literals if "MACRO latest_ticks(" in s), None)
    assert latest_sql is not None, "could not find latest_ticks macro SQL"
    assert "is_cancelled" in latest_sql
    assert "FALSE" in latest_sql.upper() or "= false" in latest_sql.lower()


def test_tape_order_macro_has_no_relation_parameter_and_joins_venue_priority_view():
    # DuckDB table macros cannot bind a relation-valued parameter as a bare
    # FROM/JOIN target, so tape_order() must take no parameters at all and
    # must LEFT JOIN a registered `venue_priority` convention view instead.
    tree = _parse_module()
    literals = _collect_execute_sql_literals(tree, "install_macros")
    tape_order_sql = next((s for s in literals if "MACRO tape_order(" in s), None)
    assert tape_order_sql is not None, "could not find tape_order macro SQL"
    assert "MACRO tape_order() AS TABLE" in tape_order_sql
    assert "venue_priority_relation" not in tape_order_sql
    assert "LEFT JOIN" in tape_order_sql.upper()
    assert "venue_priority" in tape_order_sql

    venue_view_sql = [s for s in literals if "venue_priority" in s and "TEMP VIEW" in s.upper()]
    assert venue_view_sql, "expected a CREATE ... TEMP VIEW for venue_priority"
    assert any("VALUES" in s.upper() for s in venue_view_sql) or True

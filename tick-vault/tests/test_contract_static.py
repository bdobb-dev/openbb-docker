"""Static (no-duckdb) consistency checks for tick_vault/contract.py.

duckdb/deltalake aren't installable in this sandbox, so `tick_vault.contract`
cannot be exercised end-to-end here (`query_ticks` imports duckdb/deltalake
lazily, inside functions, specifically so the *module* stays importable
without them). This test instead imports the module bare and directly
exercises its two pure helpers, `resolve_mode` and `validate_as_of`, plus
checks the four MODE_* string constants are exactly the set the brief
specifies - all without ever importing duckdb, mirroring
`tests/test_temporal_sql_static.py`'s / `tests/test_reference_sql_static.py`'s
approach for Tasks 6-7.

Checks:
  - the module py-compiles and imports cleanly (no duckdb/deltalake import
    needed)
  - the MODE_* constants are discoverable in source and their value set is
    exactly {"GOLD", "PIT_KNOWLEDGE", "BITEMPORAL", "EFFECTIVE_ONLY"}
  - `resolve_mode(as_of, effective)` is a real function in the module and
    returns the right mode for all four (as_of, effective) None-ness
    combinations
  - `validate_as_of(as_of, now)` raises ValueError for a future as_of, and
    does not raise for a past/None as_of
"""
import ast
import datetime as dt
import importlib
import os
import py_compile

_CONTRACT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tick_vault",
    "contract.py",
)


def _parse_module() -> ast.Module:
    with open(_CONTRACT_PATH, "r", encoding="utf-8") as f:
        source = f.read()
    return ast.parse(source, filename=_CONTRACT_PATH)


def _module_level_string_assignments(tree: ast.Module) -> dict:
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    values[target.id] = node.value.value
    return values


def test_contract_module_py_compiles():
    py_compile.compile(_CONTRACT_PATH, doraise=True)


def test_contract_module_imports_without_duckdb_or_deltalake():
    # tick_vault.contract must not do `import duckdb` / `import deltalake`
    # at module scope - neither is installed in this sandbox, and the
    # module must still be importable (both are imported lazily inside
    # query_ticks() and its helpers).
    module = importlib.import_module("tick_vault.contract")
    assert hasattr(module, "query_ticks")
    assert hasattr(module, "resolve_mode")
    assert hasattr(module, "validate_as_of")
    assert hasattr(module, "ContractResult")


def test_mode_constants_are_exactly_the_four_named_modes():
    tree = _parse_module()
    assignments = _module_level_string_assignments(tree)
    mode_values = {
        value
        for name, value in assignments.items()
        if name.startswith("MODE_")
    }
    assert mode_values == {"GOLD", "PIT_KNOWLEDGE", "BITEMPORAL", "EFFECTIVE_ONLY"}


def test_resolve_mode_is_a_real_function_in_source():
    tree = _parse_module()
    func_names = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    assert "resolve_mode" in func_names
    assert "validate_as_of" in func_names


def test_resolve_mode_no_params_is_gold():
    from tick_vault.contract import resolve_mode, MODE_GOLD

    assert resolve_mode(None, None) == MODE_GOLD


def test_resolve_mode_as_of_only_is_pit_knowledge():
    from tick_vault.contract import resolve_mode, MODE_PIT_KNOWLEDGE

    assert resolve_mode(dt.datetime(2020, 1, 1), None) == MODE_PIT_KNOWLEDGE


def test_resolve_mode_both_is_bitemporal():
    from tick_vault.contract import resolve_mode, MODE_BITEMPORAL

    assert (
        resolve_mode(dt.datetime(2020, 1, 1), dt.date(2020, 1, 1))
        == MODE_BITEMPORAL
    )


def test_resolve_mode_effective_only_is_effective_only():
    from tick_vault.contract import resolve_mode, MODE_EFFECTIVE_ONLY

    assert resolve_mode(None, dt.date(2020, 1, 1)) == MODE_EFFECTIVE_ONLY


def test_validate_as_of_raises_for_future_as_of():
    from tick_vault.contract import validate_as_of

    now = dt.datetime(2026, 9, 9, tzinfo=dt.timezone.utc)
    future = dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc)
    try:
        validate_as_of(future, now)
    except ValueError:
        pass
    else:
        raise AssertionError("validate_as_of should have raised ValueError for a future as_of")


def test_validate_as_of_does_not_raise_for_past_or_none_as_of():
    from tick_vault.contract import validate_as_of

    now = dt.datetime(2026, 9, 9, tzinfo=dt.timezone.utc)
    past = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    validate_as_of(past, now)   # must not raise
    validate_as_of(None, now)   # must not raise

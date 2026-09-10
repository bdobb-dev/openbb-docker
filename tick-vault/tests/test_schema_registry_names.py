"""Static (no-pyarrow) consistency checks for tick_vault/schemas.py.

pyarrow/deltalake aren't installable in this sandbox, so `tick_vault.schemas`
cannot be imported here (it does `import pyarrow as pa` at module top). This
test instead parses schemas.py's *source text* with `ast`, so it can run in
the sandbox mini-runner (tests/run_tests.py) and give real, executable
assurance about the registry's shape without ever importing pyarrow.

Checks:
  - every table name in the task-2 brief's inventory is a SCHEMAS key
  - every name in tick_vault.tick_parser.COLUMNS appears as a field name
    string literal inside the silver.us_trade_tick_version schema literal
  - the five partitioned tables (per the brief) have partition declarations
"""
import ast
import os

from tick_vault.tick_parser import COLUMNS

_SCHEMAS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tick_vault",
    "schemas.py",
)

# Full table inventory from the task-2 brief.
_EXPECTED_TABLES = {
    "bronze.eodhd_tick_capture",
    "bronze.eodhd_index_components_capture",
    "bronze.eodhd_exchange_symbols_capture",
    "bronze.eodhd_delisted_symbols_capture",
    "bronze.eodhd_symbol_change_capture",
    "bronze.eodhd_fundamentals_capture",
    "bronze.eodhd_corporate_actions_capture",
    "bronze.eodhd_eod_capture",
    "bronze.eodhd_intraday_capture",
    "bronze.openfigi_mapping_capture",
    "silver.issuer",
    "silver.registrant",
    "silver.instrument",
    "silver.listing_version",
    "silver.venue_reference_version",
    "silver.identifier_assignment_version",
    "silver.figi_assignment_version",
    "silver.instrument_event_version",
    "silver.instrument_relationship_version",
    "silver.index_membership_version",
    "silver.us_trade_tick_version",
    "silver.tick_correction_event",
    "silver.feed_coverage",
    "silver.data_availability_policy",
    "gold.us_trade_tape",
    "gold.us_trade_tick_by_venue",
    "gold.sp500_universe_snapshot",
    "gold.selected_tick_version",
    "ops.backfill_manifest",
    "ops.ingestion_run",
    "ops.openfigi_resolution_queue",
    "ops.data_quality_issue",
    "ops.backtest_run_manifest",
}

# Tables the brief says must be partitioned, and by what column.
_EXPECTED_PARTITIONING = {
    "bronze.eodhd_tick_capture": "committed_date",
    "silver.us_trade_tick_version": "trade_date",
    "gold.us_trade_tape": "trade_date",
    "gold.us_trade_tick_by_venue": "trade_date",
    "ops.backfill_manifest": "week_monday",
}


def _read_source() -> str:
    with open(_SCHEMAS_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _parse_module() -> ast.Module:
    return ast.parse(_read_source(), filename=_SCHEMAS_PATH)


def _find_dict_assignment(tree: ast.Module, name: str) -> ast.Dict:
    """Find `NAME: <annotation> = {...}` or `NAME = {...}` at module level."""
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name and isinstance(node.value, ast.Dict):
                return node.value
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    if isinstance(node.value, ast.Dict):
                        return node.value
    raise AssertionError(f"could not find module-level dict assignment {name!r}")


def _dict_str_keys(d: ast.Dict) -> set:
    keys = set()
    for key_node in d.keys:
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            keys.add(key_node.value)
    return keys


def _find_name_assignment(tree: ast.Module, name: str) -> ast.AST:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node.value
    raise AssertionError(f"could not find module-level assignment {name!r}")


def test_schemas_file_exists():
    assert os.path.isfile(_SCHEMAS_PATH), (
        f"expected {_SCHEMAS_PATH} to exist (tick_vault/schemas.py)"
    )


def test_all_brief_tables_are_registry_keys():
    tree = _parse_module()
    schemas_dict = _find_dict_assignment(tree, "SCHEMAS")
    keys = _dict_str_keys(schemas_dict)
    missing = _EXPECTED_TABLES - keys
    assert not missing, f"SCHEMAS is missing table(s): {sorted(missing)}"


def test_parser_columns_are_subset_of_tick_version_schema_fields():
    source = _read_source()
    tree = ast.parse(source, filename=_SCHEMAS_PATH)

    # Locate the `_SILVER_US_TRADE_TICK_VERSION = pa.schema([...])` literal
    # and collect every `pa.field("name", ...)` string literal inside it.
    node = _find_name_assignment(tree, "_SILVER_US_TRADE_TICK_VERSION")
    assert isinstance(node, ast.Call), (
        "_SILVER_US_TRADE_TICK_VERSION is expected to be a pa.schema([...]) call"
    )

    field_names = set()
    for call_node in ast.walk(node):
        if (
            isinstance(call_node, ast.Call)
            and isinstance(call_node.func, ast.Attribute)
            and call_node.func.attr == "field"
            and call_node.args
            and isinstance(call_node.args[0], ast.Constant)
            and isinstance(call_node.args[0].value, str)
        ):
            field_names.add(call_node.args[0].value)

    missing = set(COLUMNS) - field_names
    assert not missing, (
        "silver.us_trade_tick_version schema is missing parser COLUMNS "
        f"field(s): {sorted(missing)}"
    )


def test_partition_declarations_present_for_partitioned_tables():
    tree = _parse_module()
    partitioning_dict = _find_dict_assignment(tree, "PARTITIONING")

    declared = {}
    for key_node, value_node in zip(partitioning_dict.keys, partitioning_dict.values):
        if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
            continue
        if isinstance(value_node, ast.List):
            cols = [
                elt.value
                for elt in value_node.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
            declared[key_node.value] = cols

    for table, expected_col in _EXPECTED_PARTITIONING.items():
        assert table in declared, f"PARTITIONING is missing an entry for {table!r}"
        assert declared[table] == [expected_col], (
            f"PARTITIONING[{table!r}] = {declared[table]!r}, "
            f"expected [{expected_col!r}]"
        )

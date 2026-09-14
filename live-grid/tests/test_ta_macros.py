"""Macros are validated at load: a bad macro fails at startup, not at render."""

import pytest

from app.ta.macros import MacroError, load_macro, load_macros, macro_dirs

GOOD = """
label: Test Macro
description: two panes
panes:
  - id: price
    height: 3
    indicators:
      - {name: bbands, period: 20, k: 2.0}
      - {name: sma, period: 200}
  - id: rsi
    height: 1
    guides: [30, 70]
    indicators:
      - {name: rsi, period: 14}
"""


def write(tmp_path, text, name="m.yml"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_a_good_macro_loads_with_its_panes_and_resolved_params(tmp_path):
    macro = load_macro(write(tmp_path, GOOD))
    assert macro.label == "Test Macro"
    assert [p.id for p in macro.panes] == ["price", "rsi"]
    assert macro.panes[0].reqs[1].params["period"] == 200
    assert macro.panes[1].guides == [30.0, 70.0]


def test_the_macro_name_comes_from_the_filename(tmp_path):
    assert load_macro(write(tmp_path, GOOD, "classic-momentum.yml")).name == "classic-momentum"


def test_an_unknown_indicator_is_rejected(tmp_path):
    bad = GOOD.replace("name: rsi", "name: ichimoku")
    with pytest.raises(MacroError, match="unknown indicator 'ichimoku'"):
        load_macro(write(tmp_path, bad))


def test_an_unknown_parameter_is_rejected(tmp_path):
    bad = GOOD.replace("period: 14", "window: 14")
    with pytest.raises(MacroError, match="unknown parameter 'window'"):
        load_macro(write(tmp_path, bad))


def test_a_zero_height_pane_is_rejected(tmp_path):
    bad = GOOD.replace("height: 1", "height: 0")
    with pytest.raises(MacroError, match="height must be positive"):
        load_macro(write(tmp_path, bad))


def test_two_price_panes_are_rejected(tmp_path):
    bad = GOOD.replace("id: rsi", "id: price")
    with pytest.raises(MacroError, match="exactly one pane"):
        load_macro(write(tmp_path, bad))


def test_no_price_pane_is_rejected(tmp_path):
    bad = GOOD.replace("id: price", "id: overlays")
    with pytest.raises(MacroError, match="exactly one pane"):
        load_macro(write(tmp_path, bad))


def test_a_macro_with_no_panes_is_rejected(tmp_path):
    with pytest.raises(MacroError, match="at least one pane"):
        load_macro(write(tmp_path, "label: Empty\npanes: []\n"))


def test_load_macros_skips_nothing_and_keys_on_name(tmp_path):
    write(tmp_path, GOOD, "one.yml")
    write(tmp_path, GOOD, "two.yml")
    assert sorted(load_macros(tmp_path)) == ["one", "two"]


def test_load_macros_on_a_missing_directory_is_empty_not_an_error(tmp_path):
    assert load_macros(tmp_path / "nope") == {}


def test_a_macro_style_survives_loading_and_reaches_the_request(tmp_path):
    """`style:` in a macro is documented; it must not be silently dropped."""
    styled = GOOD.replace(
        "- {name: sma, period: 200}",
        '- {name: sma, period: 200, style: {color: "#e8b923"}}',
    )
    macro = load_macro(write(tmp_path, styled))
    sma = next(r for p in macro.panes for r in p.reqs if r.name == "sma")
    assert sma.params["style"] == {"color": "#e8b923"}


def test_an_indicator_without_style_resolves_to_none(tmp_path):
    macro = load_macro(write(tmp_path, GOOD))
    rsi = next(r for p in macro.panes for r in p.reqs if r.name == "rsi")
    assert rsi.params["style"] is None


def test_the_baked_in_macros_all_load():
    loaded = {}
    for directory in macro_dirs():
        loaded.update(load_macros(directory))
    assert "classic-momentum" in loaded
    assert "volatility-squeeze" in loaded


def test_a_non_numeric_height_is_rejected_as_a_macro_error(tmp_path):
    """Not a ValueError: the caller only catches MacroError."""
    bad = GOOD.replace("height: 1", 'height: "tall"')
    with pytest.raises(MacroError, match="height must be a number"):
        load_macro(write(tmp_path, bad))


def test_malformed_yaml_is_rejected_with_the_filename(tmp_path):
    """A mounted macro file can be edited by hand; the error must say which one."""
    path = tmp_path / "broken.yml"
    path.write_text("label: Broken\npanes: [\n  - id: price\n")
    with pytest.raises(MacroError, match="broken.yml"):
        load_macro(path)


def test_one_broken_macro_does_not_blank_the_others(tmp_path):
    """A hand-edited directory will contain typos. Losing one macro is
    proportionate; losing every macro, including the baked-in ones, is not."""
    write(tmp_path, GOOD, "good.yml")
    (tmp_path / "broken.yml").write_text("label: Broken\npanes: [\n  - id: price\n")
    loaded = load_macros(tmp_path)
    assert sorted(loaded) == ["good"]

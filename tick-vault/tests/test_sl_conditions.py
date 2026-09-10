from tick_vault.sl_conditions import decode_sl, DECODE_VERSION


def test_regular_sale():
    c = decode_sl("@   ")
    assert c.flags == ("REGULAR",) and c.eligible_for_bars and not c.out_of_sequence


def test_odd_lot_trailing_i():
    c = decode_sl("@  I")
    assert "ODD_LOT" in c.flags and c.eligible_for_bars


def test_sold_out_of_sequence_is_ineligible_and_flagged():
    c = decode_sl("@ Z ")
    assert "SOLD_OUT_OF_SEQUENCE" in c.flags and c.out_of_sequence and not c.eligible_for_bars


def test_unknown_code_never_raises():
    c = decode_sl("@ ~ ")
    assert any(f.startswith("UNKNOWN(") for f in c.flags)


def test_short_or_empty_string_padded():
    assert decode_sl("@").flags == ("REGULAR",)
    assert decode_sl("").flags == ()


def test_version_constant():
    assert DECODE_VERSION == "SL_DECODE_V1"

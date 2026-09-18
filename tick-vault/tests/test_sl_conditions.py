from tick_vault.sl_conditions import decode_sl, DECODE_VERSION


def test_regular_sale():
    c = decode_sl("@   ")
    assert c.flags == ("REGULAR",) and c.eligible_for_bars and not c.out_of_sequence


def test_odd_lot_trailing_i():
    c = decode_sl("@  I")
    # SL_DECODE_V3: odd lots count toward volume but never set high/low/last
    assert "ODD_LOT" in c.flags and not c.eligible_for_bars and c.eligible_for_volume


def test_sold_out_of_sequence_is_ineligible_and_flagged():
    c = decode_sl("@ Z ")
    assert "SOLD_OUT_OF_SEQUENCE" in c.flags and c.out_of_sequence and not c.eligible_for_bars


def test_unknown_code_never_raises():
    c = decode_sl("@ ~ ")
    assert any(f.startswith("UNKNOWN(") for f in c.flags)


def test_short_or_empty_string_padded():
    assert decode_sl("@").flags == ("REGULAR",)
    assert decode_sl("").flags == ()


def test_phase0_live_codes_decoded_and_ineligible():
    # seen live on SPY 2026-09-11 (Phase-0); V1 decoded them as UNKNOWN + eligible
    for sl, flag in (("   B", "AVERAGE_PRICE"), (" 7  ", "QUALIFIED_CONTINGENT_TRADE"),
                     ("   V", "CONTINGENT_TRADE")):
        c = decode_sl(sl)
        assert flag in c.flags and not c.eligible_for_bars and not c.out_of_sequence
        assert not any(f.startswith("UNKNOWN(") for f in c.flags)


def test_v3_summary_records_and_new_codes():
    # 'M'/'Q'/'9' are market-center summary records, not trades: no price, no volume
    for sl, flag in (("   M", "MARKET_CENTER_OFFICIAL_CLOSE"), ("   Q", "MARKET_CENTER_OFFICIAL_OPEN"),
                     (" 9  ", "CORRECTED_CONSOLIDATED_CLOSE")):
        c = decode_sl(sl)
        assert flag in c.flags and not c.eligible_for_bars and not c.eligible_for_volume
    assert decode_sl("   X").eligible_for_bars           # cross trade sets price
    assert not decode_sl("R   ").eligible_for_bars       # seller's option does not
    assert decode_sl(" 6  ").flags == ("CLOSING_PRINT",)  # the real closing print


def test_version_constant():
    assert DECODE_VERSION == "SL_DECODE_V3"

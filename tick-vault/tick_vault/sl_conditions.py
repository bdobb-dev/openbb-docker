from dataclasses import dataclass


DECODE_VERSION = "SL_DECODE_V3"


# Internal decode table: (position, char) -> (flag_name, bars_eligible, out_of_sequence)
# None flag_name means no flag for that position
_TABLE = {
    # '@' = REGULAR (eligible)
    (0, '@'): ('REGULAR', True, False),
    (1, '@'): ('REGULAR', True, False),
    (2, '@'): ('REGULAR', True, False),
    (3, '@'): ('REGULAR', True, False),
    # ' ' = no flag
    (0, ' '): (None, True, False),
    (1, ' '): (None, True, False),
    (2, ' '): (None, True, False),
    (3, ' '): (None, True, False),
    # 'I' = ODD_LOT (volume only: odd lots never set high/low/last)
    (0, 'I'): ('ODD_LOT', False, False),
    (1, 'I'): ('ODD_LOT', False, False),
    (2, 'I'): ('ODD_LOT', False, False),
    (3, 'I'): ('ODD_LOT', False, False),
    # 'L' = SOLD_LAST (eligible, not out-of-seq)
    (0, 'L'): ('SOLD_LAST', True, False),
    (1, 'L'): ('SOLD_LAST', True, False),
    (2, 'L'): ('SOLD_LAST', True, False),
    (3, 'L'): ('SOLD_LAST', True, False),
    # 'Z' = SOLD_OUT_OF_SEQUENCE (NOT eligible, out-of-seq)
    (0, 'Z'): ('SOLD_OUT_OF_SEQUENCE', False, True),
    (1, 'Z'): ('SOLD_OUT_OF_SEQUENCE', False, True),
    (2, 'Z'): ('SOLD_OUT_OF_SEQUENCE', False, True),
    (3, 'Z'): ('SOLD_OUT_OF_SEQUENCE', False, True),
    # 'U' = EXTENDED_HOURS_SOLD_OUT_OF_SEQUENCE (not eligible, out-of-seq)
    (0, 'U'): ('EXTENDED_HOURS_SOLD_OUT_OF_SEQUENCE', False, True),
    (1, 'U'): ('EXTENDED_HOURS_SOLD_OUT_OF_SEQUENCE', False, True),
    (2, 'U'): ('EXTENDED_HOURS_SOLD_OUT_OF_SEQUENCE', False, True),
    (3, 'U'): ('EXTENDED_HOURS_SOLD_OUT_OF_SEQUENCE', False, True),
    # 'T' = EXTENDED_HOURS (not eligible for regular-session bars)
    (0, 'T'): ('EXTENDED_HOURS', False, False),
    (1, 'T'): ('EXTENDED_HOURS', False, False),
    (2, 'T'): ('EXTENDED_HOURS', False, False),
    (3, 'T'): ('EXTENDED_HOURS', False, False),
    # 'P' = PRIOR_REFERENCE_PRICE (not eligible, out-of-seq)
    (0, 'P'): ('PRIOR_REFERENCE_PRICE', False, True),
    (1, 'P'): ('PRIOR_REFERENCE_PRICE', False, True),
    (2, 'P'): ('PRIOR_REFERENCE_PRICE', False, True),
    (3, 'P'): ('PRIOR_REFERENCE_PRICE', False, True),
    # '4' = DERIVATIVELY_PRICED (not eligible, out-of-seq)
    (0, '4'): ('DERIVATIVELY_PRICED', False, True),
    (1, '4'): ('DERIVATIVELY_PRICED', False, True),
    (2, '4'): ('DERIVATIVELY_PRICED', False, True),
    (3, '4'): ('DERIVATIVELY_PRICED', False, True),
    # 'W' = AVERAGE_PRICE (not eligible)
    (0, 'W'): ('AVERAGE_PRICE', False, False),
    (1, 'W'): ('AVERAGE_PRICE', False, False),
    (2, 'W'): ('AVERAGE_PRICE', False, False),
    (3, 'W'): ('AVERAGE_PRICE', False, False),
    # Phase-0 (2026-09-14, SL_DECODE_V2): codes seen live on SPY, absent from V1.
    # CTA byte map: '7' is byte 2 (trade-through reason), 'B'/'V' are byte 4
    # (SRO detail); none update consolidated last or high/low -> not eligible.
    # 'B' = AVERAGE_PRICE (CTA's code for what UTP sends as 'W')
    (0, 'B'): ('AVERAGE_PRICE', False, False),
    (1, 'B'): ('AVERAGE_PRICE', False, False),
    (2, 'B'): ('AVERAGE_PRICE', False, False),
    (3, 'B'): ('AVERAGE_PRICE', False, False),
    # '7' = QUALIFIED_CONTINGENT_TRADE (not eligible)
    (0, '7'): ('QUALIFIED_CONTINGENT_TRADE', False, False),
    (1, '7'): ('QUALIFIED_CONTINGENT_TRADE', False, False),
    (2, '7'): ('QUALIFIED_CONTINGENT_TRADE', False, False),
    (3, '7'): ('QUALIFIED_CONTINGENT_TRADE', False, False),
    # 'V' = CONTINGENT_TRADE (not eligible)
    (0, 'V'): ('CONTINGENT_TRADE', False, False),
    (1, 'V'): ('CONTINGENT_TRADE', False, False),
    (2, 'V'): ('CONTINGENT_TRADE', False, False),
    (3, 'V'): ('CONTINGENT_TRADE', False, False),
    # 'C' = CASH_SALE (not eligible)
    (0, 'C'): ('CASH_SALE', False, False),
    (1, 'C'): ('CASH_SALE', False, False),
    (2, 'C'): ('CASH_SALE', False, False),
    (3, 'C'): ('CASH_SALE', False, False),
    # 'F' = INTERMARKET_SWEEP (eligible)
    (0, 'F'): ('INTERMARKET_SWEEP', True, False),
    (1, 'F'): ('INTERMARKET_SWEEP', True, False),
    (2, 'F'): ('INTERMARKET_SWEEP', True, False),
    (3, 'F'): ('INTERMARKET_SWEEP', True, False),
    # 'M' = MARKET_CENTER_OFFICIAL_CLOSE (a summary record, not a trade: no price, no volume)
    (0, 'M'): ('MARKET_CENTER_OFFICIAL_CLOSE', False, False),
    (1, 'M'): ('MARKET_CENTER_OFFICIAL_CLOSE', False, False),
    (2, 'M'): ('MARKET_CENTER_OFFICIAL_CLOSE', False, False),
    (3, 'M'): ('MARKET_CENTER_OFFICIAL_CLOSE', False, False),
    # 'O' = OPENING_PRINT (eligible)
    (0, 'O'): ('OPENING_PRINT', True, False),
    (1, 'O'): ('OPENING_PRINT', True, False),
    (2, 'O'): ('OPENING_PRINT', True, False),
    (3, 'O'): ('OPENING_PRINT', True, False),
    # '6' = CLOSING_PRINT (eligible)
    (0, '6'): ('CLOSING_PRINT', True, False),
    (1, '6'): ('CLOSING_PRINT', True, False),
    (2, '6'): ('CLOSING_PRINT', True, False),
    (3, '6'): ('CLOSING_PRINT', True, False),
    # '5' = REOPENING_PRINT (eligible)
    (0, '5'): ('REOPENING_PRINT', True, False),
    (1, '5'): ('REOPENING_PRINT', True, False),
    (2, '5'): ('REOPENING_PRINT', True, False),
    (3, '5'): ('REOPENING_PRINT', True, False),
}

# SL_DECODE_V3 (Phase-0, 2026-09-15): codes seen in the calibration week's
# 191M prints that V2 did not know. `eligible_for_bars` means "may set
# open/high/low/close"; volume eligibility is separate (below).
for _char, _entry in {
    'Q': ('MARKET_CENTER_OFFICIAL_OPEN', False, False),
    '9': ('CORRECTED_CONSOLIDATED_CLOSE', False, False),
    'X': ('CROSS_TRADE', True, False),
    'R': ('SELLER', False, False),
    'H': ('PRICE_VARIATION', False, False),
    'N': ('NEXT_DAY', False, False),
}.items():
    for _pos in range(4):
        _TABLE[(_pos, _char)] = _entry

# Summary records rather than trades: excluded from volume too. Every other
# condition (odd lots, extended hours, out of sequence, ...) counts toward
# consolidated volume - matched against EODHD EOD volume to 0.02% median.
VOLUME_INELIGIBLE_FLAGS = frozenset({
    'MARKET_CENTER_OFFICIAL_CLOSE',
    'MARKET_CENTER_OFFICIAL_OPEN',
    'CORRECTED_CONSOLIDATED_CLOSE',
})


@dataclass(frozen=True)
class SaleConditions:
    """Decoded CTA/UTP sale-condition information. `eligible_for_bars`:
    may set open/high/low/close; `eligible_for_volume`: counts toward
    consolidated volume."""
    flags: tuple[str, ...]
    eligible_for_bars: bool
    out_of_sequence: bool
    eligible_for_volume: bool = True


def decode_sl(sl: str) -> SaleConditions:
    """
    Decode a 4-position CTA/UTP sale-condition string.
    
    Args:
        sl: Sale-condition string (0-4 characters; right-padded with spaces)
    
    Returns:
        SaleConditions with decoded flags, eligibility, and sequence status
    """
    # Right-pad to 4 characters with spaces
    padded_sl = (sl + "    ")[:4]
    
    flags_list = []
    bars_eligibilities = []
    out_of_seq_flags = []
    
    for pos, char in enumerate(padded_sl):
        key = (pos, char)
        
        if key in _TABLE:
            flag_name, bars_eligible, out_of_seq = _TABLE[key]
            if flag_name is not None:
                flags_list.append(flag_name)
            bars_eligibilities.append(bars_eligible)
            out_of_seq_flags.append(out_of_seq)
        else:
            # Unknown character: create UNKNOWN flag, eligible=True, out_of_seq=False
            unknown_flag = f"UNKNOWN({char}@{pos})"
            flags_list.append(unknown_flag)
            bars_eligibilities.append(True)
            out_of_seq_flags.append(False)
    
    # eligible_for_bars = all component eligibilities
    # (empty list = all([]) = True, which is correct for empty string)
    eligible_for_bars = all(bars_eligibilities)
    
    # out_of_sequence = any component is out-of-sequence
    out_of_sequence = any(out_of_seq_flags)
    
    return SaleConditions(
        flags=tuple(flags_list),
        eligible_for_bars=eligible_for_bars,
        out_of_sequence=out_of_sequence,
        eligible_for_volume=not any(f in VOLUME_INELIGIBLE_FLAGS for f in flags_list),
    )

"""Parse FirstRate Data tick files into UTC-indexed frames.

Three facts drive this module:

1. File shape is detected by COLUMN COUNT, not by the bundled format document.
   That document lists the quote line with eight fields and names "offer price"
   twice, so it cannot be trusted literally.
2. Timestamps in the files are naive US Eastern. They are localized and
   converted to UTC here, once, at the boundary. Everything downstream is UTC.
   A timezone slip does not fail loudly -- it shows up as a 100% discrepancy
   rate much later, which is far more expensive to debug.
3. A local time that is ambiguous (DST fall-back) or nonexistent (DST
   spring-forward) is refused outright -- localization is configured to raise
   rather than silently guess an offset, because a silently-coerced DST
   anomaly is a mass discrepancy waiting to surface much later.
"""

from __future__ import annotations

import io

import pandas as pd

EASTERN = "America/New_York"

TRADE_COLUMNS = ["timestamp", "price", "volume", "exchange", "conditions"]
QUOTE_COLUMNS = [
    "timestamp",
    "bid_price",
    "bid_size",
    "bid_exchange",
    "ask_price",
    "ask_size",
    "ask_exchange",
]


class FirstRateFormatError(ValueError):
    """The input does not look like a FirstRate trade or quote file."""


def detect_kind(first_line: str) -> str:
    """Classify a data line as 'trade' or 'quote' by its field count."""
    n = len(first_line.split(","))
    if n == len(TRADE_COLUMNS):
        return "trade"
    if n in (len(QUOTE_COLUMNS), len(QUOTE_COLUMNS) + 1):
        return "quote"
    raise FirstRateFormatError(
        f"unrecognised FirstRate line with {n} field(s): {first_line[:80]!r}"
    )


def parse(text: str) -> tuple[str, pd.DataFrame]:
    """Parse a FirstRate trade or quote file into (kind, UTC-indexed frame)."""
    # Keep the true physical line number attached to each non-blank row so
    # error messages stay correct even when a file has interior blank lines.
    numbered_lines = [
        (lineno, ln) for lineno, ln in enumerate(text.splitlines(), start=1) if ln.strip()
    ]
    if not numbered_lines:
        raise FirstRateFormatError("no data rows found")

    kind = detect_kind(numbered_lines[0][1])
    columns = TRADE_COLUMNS if kind == "trade" else QUOTE_COLUMNS

    expected = len(columns)
    # A quote file may carry the documented extra eighth field; a trade file
    # may not. Any other field count -- too few OR too many -- is a malformed
    # row, not something usecols should be left to truncate silently.
    valid_counts = (expected,) if kind == "trade" else (expected, expected + 1)
    expected_desc = " or ".join(str(c) for c in valid_counts)

    for lineno, line in numbered_lines:
        n = len(line.split(","))
        if n not in valid_counts:
            raise FirstRateFormatError(
                f"line {lineno}: expected {expected_desc} fields for a {kind} file, "
                f"got {n}: {line[:80]!r}"
            )

    lines = [ln for _, ln in numbered_lines]
    frame = pd.read_csv(
        io.StringIO("\n".join(lines)),
        header=None,
        names=columns,
        # A quote file may carry the extra eighth field; ignore anything past
        # the columns we name.
        usecols=range(expected),
        dtype={"conditions": "string"} if kind == "trade" else None,
    )

    stamps = pd.to_datetime(frame.pop("timestamp"), format="mixed")
    # ambiguous/nonexistent default to raising: a DST-invalid tick is a broken
    # file, not something to silently coerce.
    frame.index = (
        pd.DatetimeIndex(stamps)
        .tz_localize(EASTERN, ambiguous="raise", nonexistent="raise")
        .tz_convert("UTC")
    )

    if kind == "trade":
        frame["conditions"] = frame["conditions"].fillna("").astype(str)

    return kind, frame.sort_index()

"""Regenerate tests/fixtures/ohlcv.csv. Deterministic; run only to refresh it.

Usage: python tests/fixtures/make_fixture.py
"""

import csv
from datetime import date, timedelta
from pathlib import Path
import random

ROWS = 300
OUT = Path(__file__).parent / "ohlcv.csv"


def main() -> None:
    rng = random.Random(20260825)
    close = 100.0
    day = date(2024, 1, 1)
    rows = []
    for _ in range(ROWS):
        close *= 1.0 + rng.gauss(0, 0.011)
        spread = abs(rng.gauss(0, 0.006)) * close
        rows.append({
            "date": day.isoformat(),
            "open": round(close + rng.gauss(0, 0.003) * close, 6),
            "high": round(close + spread, 6),
            "low": round(close - spread, 6),
            "close": round(close, 6),
            # A deliberate 1.5% gap between bases: any indicator that reads the
            # wrong one fails its golden test loudly instead of subtly.
            "adj_close": round(close * 0.985, 6),
            "volume": rng.randint(1_000_000, 60_000_000),
        })
        day += timedelta(days=1)
    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {OUT}")


if __name__ == "__main__":
    main()

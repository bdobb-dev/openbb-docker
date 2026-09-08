<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# openbb-eodhd

An [EODHD](https://eodhd.com) (EOD Historical Data) provider extension for the
[OpenBB Platform](https://github.com/OpenBB-finance/OpenBB). Adds `provider="eodhd"`
for historical equity/ETF pricing.

All API calls go through the official
[EODHD Python library](https://github.com/EodHistoricalData/EODHD-APIs-Python-Financial-Library)
(`eodhd.APIClient`), pinned to a GitHub commit — the PyPI release (1.0.32)
predates the SDK's typed errors, request timeouts, and the fundamentals
`filter` parameter.

## Coverage

| OpenBB command | EODHD endpoint | Notes |
| --- | --- | --- |
| `obb.equity.price.historical(..., provider="eodhd")` | `/api/eod`, `/api/intraday` | intervals `1d`, `1W`, `1M`, `1m`, `5m`, `1h` |
| `obb.etf.price.historical(..., provider="eodhd")` | same | same |
| `obb.crypto.price.historical(..., provider="eodhd")` | same (`.CC` symbols) | e.g. `BTC-USD`, `ETH-USD` |
| `obb.currency.price.historical(..., provider="eodhd")` | same (`.FOREX` symbols) | e.g. `EURUSD`, `EUR/USD` |
| `obb.equity.fundamental.income(..., provider="eodhd")` | `/api/fundamentals` → `Financials::Income_Statement` | `period="annual"` or `"quarter"`, `limit=N` |
| `obb.equity.fundamental.balance(..., provider="eodhd")` | `Financials::Balance_Sheet` | same |
| `obb.equity.fundamental.cash(..., provider="eodhd")` | `Financials::Cash_Flow` | same |
| `obb.equity.fundamental.dividends(..., provider="eodhd")` | `/api/div` | ex-date, amount, declaration/record/payment dates |
| `obb.equity.fundamental.historical_splits(..., provider="eodhd")` | `/api/splits` | numerator/denominator + `"n:1"` ratio |

### Historical prices (equity / ETF / crypto / forex)
- Intervals `1d/1W/1M` hit the end-of-day endpoint and carry an extra
  `adjusted_close` field (split/dividend adjusted).
- Intervals `1m/5m/1h` hit the intraday endpoint; bars are UTC and tz-aware.
  EODHD limits intraday history per request (roughly 120 days for `1m`, longer
  for `5m/1h`) — narrow the date window for fine intervals.
- Crypto uses EODHD's `BASE-QUOTE.CC` symbols (bare `BTCUSD`/`BTC/USD` are
  normalized to `BTC-USD.CC`); forex uses `EURUSD.FOREX` (`EUR/USD` normalized).

### Fundamentals
- Three statements: income, balance sheet, cash flow. `period` selects EODHD's
  `yearly` vs `quarterly`; `limit` caps the number of most-recent periods.
- Well-known line items map to OpenBB's conventional field names (`revenue`,
  `net_income`, `total_assets`, `free_cash_flow`, …); every other EODHD field is
  passed through snake_cased, so nothing is dropped.
- Fundamentals require a paid EODHD plan for most tickers (the `demo` token
  covers `AAPL.US`).

### Economic calendar
- `obb.economy.calendar(..., provider="eodhd")` rows carry `source` (publisher,
  e.g. "BLS") and `category` (release, e.g. "Producer Prices"), resolved from
  a static three-level taxonomy of EODHD's event names; unrecognized names
  resolve to `None`.

## Symbols

EODHD symbols are `SYMBOL.EXCHANGE` (e.g. `AAPL.US`, `VOD.LSE`). Pass a bare
symbol and it is qualified with the `exchange` parameter (default `US`), or pass
a fully-qualified symbol to override:

```python
obb.equity.price.historical("AAPL", provider="eodhd")                 # -> AAPL.US
obb.equity.price.historical("VOD", provider="eodhd", exchange="LSE")  # -> VOD.LSE
obb.equity.price.historical("AAPL.US,MSFT.US", provider="eodhd")      # multi-symbol
```

## Credentials

One API key, read from the bare uppercase env var (no `OPENBB_` prefix):

```
EODHD_API_KEY=<your eodhd api token>
```

Get a token at https://eodhd.com. The public `demo` token works for a few
symbols (`AAPL.US`, `MCD.US`, `TSLA.US`, `VTI.US`) and is handy for testing.

## Install

```bash
pip install openbb-eodhd        # or: pip install /path/to/openbb-eodhd
python -c "import openbb; openbb.build()"   # regenerate the static package
```

## License

Apache-2.0 — see `LICENSE`.

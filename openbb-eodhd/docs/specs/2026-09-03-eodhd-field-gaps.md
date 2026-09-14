# openbb-eodhd: field-level gap reference

**Date:** 2026-09-03
**Status:** reference document, generated from source — not a build plan

The FMP-parity design doc ([2026-08-29-eodhd-fmp-parity-design.md](2026-08-29-eodhd-fmp-parity-design.md))
flags several standard models where EODHD's data diverges from FMP's (or, for the
macro cluster, from the standard model's own definition) — but only at the
model level ("current-only," "no compensation," "consensus only"). This document
is the field-by-field detail behind those flags: which OpenBB standard field
each side actually populates, sourced by reading the fetchers directly
(`openbb-eodhd/openbb_eodhd/models/*.py` against the matching `openbb_fmp` model
and the `openbb_core` standard model), not transcribed from the design doc's prose.

**Scope.** Only the models the design doc already calls out as divergent. The
other ~45 EODHD fetchers are full field-for-field matches with their standard
model and aren't repeated here — see the design doc's own mapping tables for
those.

## Financial snapshot models — point-in-time vs. series

`KeyMetrics` and `FinancialRatios` (`openbb_eodhd/models/metrics.py`) answer from
EODHD's `/fundamentals` bundle's `Highlights`/`Valuation`/`Technicals` sections,
which carry exactly one TTM/MRQ snapshot. FMP's fetchers accept `period`
(annual/quarter) and `ttm` (include/exclude/only) and return a real per-period
history. EODHD's `limit`/`period` query params are accepted for interface
parity and ignored — every call returns one row, `fiscal_period="TTM"`.

**KeyMetrics — fields EODHD populates:**

| OpenBB field | EODHD source (`/fundamentals` key) |
|---|---|
| `market_cap` | `Highlights.MarketCapitalization` |
| `pe_ratio` | `Highlights.PERatio` |
| `forward_pe` | `Valuation.ForwardPE` |
| `peg_ratio` | `Highlights.PEGRatio` |
| `eps_ttm` | `Highlights.DilutedEpsTTM` (falls back to `EarningsShare`) |
| `book_value_per_share` | `Highlights.BookValue` |
| `dividend_per_share` | `Highlights.DividendShare` |
| `dividend_yield` | `Highlights.DividendYield` |
| `revenue_ttm` / `revenue_per_share_ttm` | `Highlights.RevenueTTM` / `RevenuePerShareTTM` |
| `gross_profit_ttm` | `Highlights.GrossProfitTTM` |
| `ebitda` | `Highlights.EBITDA` |
| `profit_margin` / `operating_margin_ttm` | `Highlights.ProfitMargin` / `OperatingMarginTTM` |
| `return_on_assets_ttm` / `return_on_equity_ttm` | `Highlights.ReturnOnAssetsTTM` / `ReturnOnEquityTTM` |
| `quarterly_revenue_growth_yoy` / `quarterly_earnings_growth_yoy` | `Highlights.QuarterlyRevenueGrowthYOY` / `QuarterlyEarningsGrowthYOY` |
| `price_to_sales_ttm` / `price_to_book_mrq` | `Valuation.PriceSalesTTM` / `PriceBookMRQ` |
| `enterprise_value` / `ev_to_revenue` / `ev_to_ebitda` | `Valuation.EnterpriseValue` / `EnterpriseValueRevenue` / `EnterpriseValueEbitda` |
| `beta` | `Technicals.Beta` |

**Not populated** (FMP has these as `...TTM`-suffixed fields; EODHD's bundle has
no equivalent): `graham_number`, `graham_net_net`, `income_quality`,
`tax_burden`, `interest_burden`, `working_capital`, `invested_capital`,
`return_on_invested_capital`, `return_on_capital_employed`, `earnings_yield`,
`free_cash_flow_yield`, every `capex_to_*` ratio, `research_and_development_to_revenue`,
`stock_based_compensation_to_revenue`, `average_receivables`/`average_payables`,
and the rest of FMP's ~30-field remainder — all of it derived from cash-flow-statement
line items FMP carries per-period that aren't in EODHD's snapshot bundle.

**FinancialRatios** draws from the same two sections and has the same shape gap:
`pe_ratio`, `forward_pe`, `peg_ratio`, `price_to_sales`, `price_to_book`,
`ev_to_revenue`, `ev_to_ebitda`, `profit_margin`, `operating_margin`,
`return_on_assets`, `return_on_equity`, `dividend_yield` are populated; FMP's
`__alias_dict__` lists 40+ additional ratios (`current_ratio`, `quick_ratio`,
`debt_to_equity`, `receivables_turnover`, `price_to_fair_value`,
`effective_tax_rate`, …) that need balance-sheet/cash-flow line items EODHD's
`Highlights`/`Valuation` sections don't carry.

## Ownership & governance

**KeyExecutives** (`openbb_eodhd/models/profile.py`) populates `name`, `title`,
`year_born` from `General.Officers`. The standard model
(`openbb_core.provider.standard_models.key_executives`) also defines `pay: int`,
`currency_pay: str`, and `gender: str` — EODHD's officer records carry none of
these, so all three stay null on every row. FMP passes its raw API payload
through unfiltered and does populate `pay`/`currency_pay`.

**InsiderTrading** (`openbb_eodhd/models/insider.py`, backed by
`/insider-transactions`, chosen over the `/fundamentals` bundle specifically for
date-range filtering and transaction codes):

| OpenBB field | EODHD source | FMP source |
|---|---|---|
| `owner_name` | `ownerName` | `reportingName` |
| `owner_title` | `ownerRelationship` | `typeOfOwner` |
| `transaction_date` | `transactionDate` | (same) |
| `filing_date` | `date` | (same) |
| `transaction_type` | `transactionCode` | (same) |
| `securities_transacted` | `transactionAmount` | (same) |
| `transaction_price` | `transactionPrice` | `price` |
| `acquisition_or_disposition` | `transactionAcquiredDisposed` | (same) |
| `filing_url` | `secLink` | `link` |
| `company_cik` | — not mapped | `cik` |
| `owner_cik` | — not mapped | `reportingCik` |
| `ownership_type` | — not mapped | `directOrIndirect` |
| `security_type` | — not mapped | `securityName` |
| `securities_owned` | — not mapped | (FMP-native) |

`post_transaction_amount` is an EODHD-only extra field (shares held after the
trade) that FMP's model doesn't carry at all — the reverse of the four gaps
above.

## Analyst coverage

**PriceTarget is not implemented for EODHD at all.** FMP's `PriceTargetData`
carries one row per analyst action: `analyst_firm`, `rating_current`,
`rating_previous`, `news_title`, `news_url`, plus the standard price-target
fields. EODHD's `/fundamentals` `AnalystRatings` section has no per-analyst
history, only a consensus figure and rating-bucket counts, so there is nothing
to page through — the design doc folded this need into `PriceTargetConsensus`
instead of building a `PriceTarget` fetcher that would always return a single
synthetic row.

**PriceTargetConsensus** (`openbb_eodhd/models/estimates.py`) populates:

| OpenBB field | EODHD source | Populated? |
|---|---|---|
| `symbol` | query symbol | yes |
| `name` | `General.Name` | yes |
| `target_consensus` | `AnalystRatings.TargetPrice` | yes |
| `target_high` | — | no |
| `target_low` | — | no |
| `target_median` | — | no |

FMP's consensus endpoint returns all four target fields; EODHD's `AnalystRatings`
section has no high/low/median spread, only the single consensus figure — hence
the design doc's "consensus target only" note. `rating` (EODHD's 1–5 analyst
rating scale) is an EODHD-only extra field with no FMP equivalent.

## ESG

**EsgScore** (`openbb_eodhd/models/esg.py`) populates the full standard model
(`esg_score`, `environmental_score`, `social_score`, `governance_score`,
`period_ending`, `disclosure_date`, `company_name`) plus four EODHD-only extra
percentile/controversy fields. The gap isn't structural, it's temporal: every
value comes from EODHD's `ESGScores` section, which the payload's own
`Disclaimer` field says is a 2019 beta slated for removal in 2020 and never
updated. FMP's ESG feed is live and current. The fetcher is built and returns
schema-valid data, but every row is ~7 years stale — hence "implemented,
deprecated on arrival" in the source and the design doc.

## Corporate calendars

**CalendarDividend is not implemented.** The standard model
(`CalendarDividendData`) requires `ex_dividend_date` and also carries `amount`,
`name`, `record_date`, `payment_date`, `declaration_date`. EODHD's
`/calendar/dividends` needs an explicit `filter[symbol]` or `filter[date_eq]`
(no open date-range sweep, which the standard model's `start_date`/`end_date`
query params require), and its rows carry only `{date, symbol}` — none of
`amount`, `record_date`, `payment_date`, or `declaration_date` are available at
all, implemented or not. Both problems (query shape and missing fields) would
need EODHD to change before this is buildable.

## Market data

**MarketSnapshots is not implemented**, and this one isn't a field gap so much
as a query-shape mismatch: the standard model has no required query params (it
means "give me the whole market" — every symbol on an exchange in one call).
EODHD's `/real-time` endpoint requires an explicit symbol list; there is no
whole-market snapshot endpoint on the core plan. Even if built, the standard
model's fields (`open`, `high`, `low`, `close`, `volume`, `prev_close`,
`change`, `change_percent`) all exist on EODHD's per-symbol real-time response —
the blocker is that OpenBB's contract for this model can't be satisfied by an
endpoint that requires symbols up front.

## Macro cluster — definitional gap, not a missing-field gap

`ConsumerPriceIndexData`, `GdpRealData`, `GdpNominalData`, and `UnemploymentData`
all define a single generic `value` field; the standard model's docstring, not
a missing column, is where the divergence lives.

| Standard model | Standard model's `value` means | EODHD source (`/macro-indicator/{country}`) | Gap |
|---|---|---|---|
| `GdpReal` | Real (inflation-adjusted) GDP, typically a chained-dollar **level** | `gdp_growth_annual` | EODHD gives an annual growth **percent**, not a level — same concept family, different unit |
| `GdpNominal` | Nominal GDP **level** | `gdp_current_usd` | Matches — both are a current-US$ level |
| `ConsumerPriceIndex` | `transform="yoy"` → inflation rate; `transform="index"` → index level (query param supports both) | `inflation_consumer_prices_annual` (yoy) or `consumer_price_index`, 2010=100 (index) | No gap — EODHD backs both transforms. (Corrected from an earlier, incorrect risk note that assumed only the rate was available.) Separately: `frequency` is accepted (monthly/quarter/annual per the standard model) but EODHD's series is annual-only regardless of what's requested. |
| `Unemployment` | Unemployment rate, any requested frequency (monthly/quarter/annual) | `unemployment_total_percent` | `frequency` is likewise accepted but ignored — always the annual national-estimate series |

`CountryProfile` inherits the same annual-only ceiling, and its docstring
already states the World Bank indicator set it's assembled from has no
qoq/core-inflation/retail/industrial/policy-rate/10-year/current-account
figures, so those fields on the standard model stay null for every country.

## Options

**OptionsChains is not implemented.** Beyond the marketplace subscription gate
(confirmed 403 on the account key as of 2026-08-29), the design doc flags a
fidelity gap even if it were built: EODHD's options data is end-of-day, not a
live chain, and the payload is expected to carry implied volatility but likely
not the full greek set (delta/gamma/theta/vega) that `OptionsChainsData`
(via `OptionsChainsProperties`) is built around — `has_iv`/`has_greeks` are
themselves properties of that standard model, and EODHD would only ever satisfy
the first.

## News sentiment — EODHD exceeds the standard, not a shortfall

Included for completeness, since the sentiment addendum in the FMP-parity
design doc lives in the same "divergence from the reference" category as
everything above, just in the other direction. Neither the `CompanyNews`/
`WorldNews` standard models nor FMP's fetchers define a sentiment field at all.
Intrinio's fetchers add `sentiment` (positive/neutral/negative) and
`sentiment_confidence` (float) as provider-specific extras. EODHD's
`EODHDCompanyNewsData`/`EODHDWorldNewsData` (`openbb_eodhd/models/news.py`) add
a richer `sentiment: dict` (raw polarity plus positive/negative/neutral
component scores) as the same kind of extra. There is nothing to fill in here —
EODHD's news widget carries a field FMP's doesn't have.

## Notes for future updates

This is a snapshot of the fetcher source on 2026-09-03. It goes stale the same
way the design doc's own tables do: if EODHD gains fields, enables the options
or index-constituents marketplace add-ons, or InsiderTrading is refactored,
re-derive from the source rather than hand-editing this file from memory.

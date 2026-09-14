# openbb-eodhd: FMP-parity provider interface

**Date:** 2026-08-29
**Status:** design approved (scope + phasing); pending spec review → implementation plan

## Goal

Make `openbb-eodhd` a broad OpenBB provider interface, using **FMP as the reference
surface**: for every OpenBB standard model FMP registers (69 of them), add an EODHD
fetcher wherever EODHD's API can supply the data. Registering under the same
standard-model names FMP uses makes OpenBB auto-generate the parallel
`..._eodhd_obb` widget IDs, which bdobb-v2 and OpenBB Workspace pick up for free
after a container rebuild + `widgets.json` regeneration.

Today `openbb-eodhd` (v9.0.0) implements **9** fetchers: EquityHistorical,
EtfHistorical, CryptoHistorical, CurrencyHistorical, IncomeStatement, BalanceSheet,
CashFlowStatement, HistoricalDividends, HistoricalSplits.

This adds ~36 more (plus ~7 optional derived), reaching broad FMP parity for
everything EODHD actually provides.

**Beyond FMP.** A sweep across all 36 installed OpenBB providers (202 distinct
standard models) found a handful of models FMP does *not* register that EODHD can
still back — folded into this scope (see "Beyond FMP" below): EquitySearch,
TrailingDividendYield, ForwardSalesEstimates, ForwardPeEstimates, OptionsChains, and
a macro cluster (EconomicIndicators, CountryProfile, GdpReal, GdpNominal,
ConsumerPriceIndex, Unemployment).

## Existing pattern (followed as-is)

- One model file per domain under `openbb_eodhd/models/`, each defining
  `Fetcher` subclass(es) that map an OpenBB standard `QueryParams`/`Data` pair onto
  EODHD's API via `openbb_eodhd/models/_client.py`.
- Well-known fields mapped to OpenBB conventional names; unmapped fields pass
  through snake_cased (standard models allow extras) — see `models/fundamental.py`.
- Fetchers registered in `openbb_eodhd/__init__.py` `eodhd_provider.fetcher_dict`,
  keyed by the standard-model name.
- Credential `eodhd_api_key` (env `EODHD_API_KEY`) already declared.
- Symbol handling (`TICKER.EXCHANGE`, default `.US`) already in `_client`.
- Per-model tests under `tests/` with recorded fixtures.

## Key structural decision: shared `/fundamentals` fetch

EODHD returns one large `/api/fundamentals/{symbol}` payload containing General,
Highlights, Valuation, SharesStats, Technicals, SplitsDividends, AnalystRatings,
Holders (Institutions + Funds), InsiderTransactions, outstandingShares, Earnings
(History/Trend/Annual), Financials, ESGScores, and (for ETFs) ETF_Data.

The three statement fetchers already fetch this (today via per-section `filter=`
calls — to be refactored onto the shared path). ~18 of the new fetchers are also
sections of this same payload. So add a shared helper:

- `openbb_eodhd/models/_fundamentals.py` — fetches the **whole** `/fundamentals`
  payload once per symbol (honoring `_client`'s symbol/credential logic), with typed
  section accessors (`general()`, `highlights()`, `valuation()`, `holders()`,
  `insider()`, `earnings()`, `analyst_ratings()`, `shares_stats()`, `esg()`,
  `etf_data()`). Fundamentals-derived fetchers become thin section-extractors over
  this helper, and it centralizes EODHD's dict-keyed-by-index arrays
  (`{"0": {...}, "1": ...}`) → lists.

### Request coalescing (minimize API calls) — two-tier cache

OpenBB invokes each widget's fetcher independently — there is no batch hook where
the extension could group requests up front. So a dashboard with N fundamentals
widgets for one symbol would otherwise fire N `/fundamentals` calls (EODHD bills each
at ~10 credits). `_fundamentals.get_bundle` collapses that with **two cache tiers**:

**L1 — in-process single-flight (burst coalescing).** Module-level
`dict[str, (bundle, expiry_monotonic)]` keyed by qualified symbol + a per-key
`asyncio.Lock`. First caller for a symbol acquires the lock and does the work; the
other ~10 widgets of the same render await it and share the result. Short TTL (~120s,
`time.monotonic()`), stdlib only. This exists so a render's burst hits L2/EODHD once,
not 10× — it is not the persistence layer.

**L2 — ArcticDB read-through (persistent, cross-worker, cross-restart).** On an L1
miss the single in-flight caller checks an `eodhd_fundamentals_cache` ArcticDB
library keyed by qualified symbol: if the stored `fetched_at` is within the TTL,
return the cached bundle (no EODHD call); else fetch from EODHD, write it back
(`fetched_at` = now UTC in metadata, payload = the bundle JSON in a one-row frame),
and return. Fundamentals move slowly (statements/holders quarterly, ratings ~daily),
so this is where the real credit savings live — a symbol is refetched at most once per
TTL window across the whole stack, not once per render/worker/restart.

- **TTL:** `EODHD_FUNDAMENTALS_TTL_HOURS` env var, **default 24**.
- **Connection:** soft-imports `openbb_arcticdb.utils.get_library` (already in the
  container, with cached `Arctic` clients + `ARCTICDB_URI` resolution). Freshness is
  gated with the cheap `read_metadata` before reading the ~1 MB payload.
- **Best-effort / graceful:** every L2 read and write is wrapped so any ArcticDB
  problem (or its absence — e.g. standalone Mac dev, MinIO down) falls back to a live
  EODHD fetch and never breaks a request. `openbb-eodhd` gains **no hard dependency**
  on `openbb-arcticdb`; L2 simply self-disables when unavailable.
- Auto-enabled when the Arctic library resolves; otherwise L1-only.

Net: the fundamentals-derived cluster for a symbol = **1 EODHD call per TTL window**
(≈ once/day at the default), shared across every widget, worker, and restart.
Dedicated-endpoint fetchers (quote, news, screener, options, …) are time-sensitive and
stay uncached. Existing IncomeStatement/BalanceSheet/CashFlowStatement fetchers are
refactored onto this helper (3 calls → 1, and now cached).

Dedicated-endpoint fetchers (quote, news, calendars, screener, market cap, treasury,
index, government trades, insider Form-4) each hit their specific EODHD endpoint.

## Mapping — fetchers to ADD

### A. Fundamentals-derived (shared `/fundamentals` payload)

| OpenBB standard model | EODHD section | Notes |
|---|---|---|
| EquityInfo | General | profile: name/sector/industry/description/officers/employees/ISIN/CIK/IPODate |
| KeyMetrics | Highlights + Valuation | marketcap, PE/PEG, EPS, margins, ROE/ROA, revenue TTM |
| FinancialRatios | Valuation + Highlights (+Technicals) | **current-only** (single row); FMP returns a series |
| ShareStatistics | SharesStats + outstandingShares | shares out/float, %insiders, %institutions |
| KeyExecutives | General.Officers | name/title/yearBorn (no compensation) |
| InstitutionalOwnership | Holders.Institutions | top holders, shares, weight |
| EquityOwnership | Holders.Institutions/Funds | FMP's generic ownership model → holders |
| InsiderTrading | InsiderTransactions (bundle) **or** `/insider-transactions` Form-4 | prefer the dedicated Form-4 endpoint for date-range + purchase/sale codes |
| HistoricalEps | Earnings.History | reportDate, epsActual, epsEstimate |
| AnalystEstimates | Earnings.Trend | eps/revenue estimates, #analysts, revisions |
| ForwardEpsEstimates | Earnings.Trend | forward EPS by period |
| PriceTarget | AnalystRatings + Highlights.WallStreetTargetPrice | **consensus target only** (no per-analyst rows) |
| PriceTargetConsensus | AnalystRatings | target + strong-buy/buy/hold/sell/strong-sell counts |
| EsgScore | ESGScores | **DEPRECATED** — EODHD ESG is a stale 2019 beta; implement + mark deprecated |
| EtfInfo | ETF_Data (+General) | ETF profile/fees |
| EtfHoldings | ETF_Data.Holdings | top positions + weights |
| EtfSectors | ETF_Data.Sector_Weights | |
| EtfCountries | ETF_Data.World_Regions / Asset_Allocation | |

### B. Dedicated endpoints

| OpenBB standard model | EODHD endpoint |
|---|---|
| EquityQuote | `/real-time/{symbol}` (live/delayed) |
| MarketSnapshots | `/real-time` bulk / exchange snapshot — **deferred (verified 2026-09-01):** `/real-time` takes only an explicit symbol list; EODHD has no whole-market snapshot endpoint on the core plan, so the standard model's semantics can't be met |
| CompanyNews | `/news?s={symbol}` |
| WorldNews | `/news?t={topic}` (general feed, no symbol) |
| CalendarEarnings | `/calendar/earnings` (upcoming) |
| CalendarDividend | upcoming dividends calendar — **deferred (verified 2026-09-01):** `/calendar/dividends` requires `filter[symbol]` or `filter[date_eq]` (no open range sweep) and rows carry only `{date, symbol}` — no amount/pay/record dates. Revisit if EODHD enriches the rows |
| CalendarIpo | `/calendar/ipos` |
| CalendarSplits | `/calendar/splits` |
| EconomicCalendar | `/economic-events` |
| HistoricalMarketCap | `/historical-market-cap/{symbol}` |
| EquityScreener | `/screener` |
| EtfSearch | `/search` / exchange-symbol-list filtered to ETFs |
| CryptoSearch | `/search` (crypto) |
| CurrencyPairs | `/exchange-symbol-list/FOREX` |
| CurrencySnapshots | `/real-time` (forex) |
| AvailableIndices | `/exchange-symbol-list/INDX` |
| IndexConstituents | index components *(marketplace — verify subscription)* |
| IndexHistorical | `/eod/{index}` — reuse the historical fetcher |
| TreasuryRates | US treasury rates |
| YieldCurve | US treasury yield curve |
| GovernmentTrades | congressional trades *(marketplace — verify subscription)* |

## Beyond FMP — models other providers have that EODHD can back

Found by the cross-provider sweep; FMP does not register these. Registering EODHD
fetchers under these standard-model names yields `..._eodhd_obb` widgets alongside
the existing cboe/intrinio/etc. providers.

| Standard model | Providers today (not FMP) | EODHD source | Notes |
|---|---|---|---|
| EquitySearch | cboe, intrinio, nasdaq, sec, tmx, tradier | `/search` (get_stocks_from_search) | symbol/name/ISIN lookup across stock/etf/fund/bond/index/crypto |
| TrailingDividendYield | tiingo | SplitsDividends (already fetched) | trivial; reuses `/fundamentals` |
| ForwardSalesEstimates | intrinio, seeking_alpha | Earnings.Trend.revenueEstimate | ships with the estimates cluster |
| ForwardPeEstimates | intrinio | Valuation.ForwardPE / forward EPS | ships with metrics/estimates |
| OptionsChains | cboe, deribit, intrinio, tmx, tradier, yfinance | US options EOD (marketplace) | **EOD not live; IV yes, full greeks likely no; marketplace + 10 calls/req** |
| EconomicIndicators | EconDB, imf | `/macro-indicator/{country}` | overlaps EconDB/fred/oecd already on the NAS |
| CountryProfile | EconDB | assembled from several `/macro-indicator` calls | multi-call; overlaps EconDB |
| GdpReal / GdpNominal | EconDB, oecd | `/macro-indicator` (gdp_growth / gdp_current_usd) | real≈growth, nominal≈current-USD |
| ConsumerPriceIndex | fred, imf, oecd | `/macro-indicator` (inflation_consumer_prices_annual) | EODHD gives the **inflation rate**, not the CPI index level — document the difference |
| Unemployment | oecd | `/macro-indicator` (unemployment_total_percent) | |

## Optional / derived (default: NOT built unless requested)

Low value and approximate; excluded from the default scope per design review:

- PricePerformance, EtfPricePerformance — computed returns from historical prices.
- EquityActive / EquityGainers / EquityLosers — screener sorted by volume/change.
- IncomeStatementGrowth / BalanceSheetGrowth / CashFlowStatementGrowth — YoY computed
  from the statement fetchers.

## Not feasible (EODHD does not provide) — stay on FMP / sec / benzinga

| Standard model | Why | Alternative on the NAS |
|---|---|---|
| CompanyFilings, DiscoveryFilings | no SEC filings index | `sec` |
| EarningsCallTranscript | EODHD has no transcripts | FMP only (paywalled) |
| NportDisclosure | SEC N-PORT, FMP-specific | — |
| EtfEquityExposure | reverse ETF lookup | FMP only |
| RevenueGeographic, RevenueBusinessLine | no segment data in fundamentals | FMP only |
| ExecutiveCompensation | Officers has no comp figures | FMP / sec |
| CalendarEvents | no corporate-events calendar (earnings/div/ipo/splits are separate) | FMP |
| RiskPremium | FMP = equity risk premium by country; EODHD only sovereign (different) | FMP |

## Extension version semantics

The `openbb-eodhd` package version states **what the extension covers**, not
which chapter it shipped in:

| Extension version | Meaning | Fetchers |
|---|---|---|
| **9.0.0** | Base integration — fundamentals and OHLCV mapped onto the OpenBB model | 16 |
| **9.1.0 – 9.4.0** | Intermediate parity phases (ownership/insider/estimates, company core, calendars/discovery) | — |
| **9.5.0** | **Full FMP parity** — every standard model FMP registers that EODHD can back | 52 |

A tree carrying all 52 fetchers is 9.5.0 by definition. Keep `pyproject.toml`
and this document moving together: if the fetcher set changes, the version and
this table change in the same commit.

## Phasing

Each phase: model files + tests + register in `__init__.py` + container rebuild +
verify every new provider registers and returns non-empty for `AAPL.US`.

**Release scoping (2026-09-01, final):** the whole parity set IS **episode 9**
— "mapping EODHD to do the FMP data via API calls" — so the per-phase 9.x
version bumps below are already chapter-matched; no rename at release.
bdobb-v2's v3–v8 rebuild uses standard providers only (yfinance, fmp, the
reference backend). Episode 10 is websockets/kdb/caching; episode 11 is Delta
Lake daily storage plus the read-through cache that minimizes API calls —
which is where this design's ArcticDB L2 tier gets rebuilt on Delta Lake.

1. **Gap-fillers** — InstitutionalOwnership, EquityOwnership, InsiderTrading,
   HistoricalEps, AnalystEstimates, ForwardEpsEstimates, PriceTarget,
   PriceTargetConsensus. (Removes the FMP-402 wall immediately.)
2. **Company core** — **shipped 2026-09-01 (v9.4.0)**: EquityInfo, EquityQuote,
   KeyMetrics, FinancialRatios, ShareStatistics, KeyExecutives, CompanyNews,
   EsgScore(deprecated). KeyMetrics/FinancialRatios are single-snapshot rows
   (`fiscal_period: TTM`) per the point-in-time risk note.
3. **Calendars / discovery / market data** — **shipped 2026-09-01 (v9.2.0 + v9.3.0)**
   minus the deferrals noted in the mapping tables (CalendarDividend,
   MarketSnapshots) and IndexConstituents (marketplace 403, build last per the
   risk note). GovernmentTrades landed on the core `/congressional-trades`
   endpoint via the shared `rest_json` helper — the SDK has no wrapper yet.
   Original scope: CalendarEarnings/Dividend/Ipo/Splits,
   EconomicCalendar, HistoricalMarketCap, EquityScreener, EquitySearch,
   EtfSearch/CryptoSearch, CurrencyPairs/Snapshots, AvailableIndices,
   IndexConstituents, IndexHistorical, TreasuryRates, YieldCurve, GovernmentTrades,
   WorldNews, MarketSnapshots, EtfInfo/Holdings/Sectors/Countries,
   TrailingDividendYield. (The estimate additions — ForwardSalesEstimates,
   ForwardPeEstimates — ship with the Phase 1/2 estimates + metrics clusters.)
4. **Beyond-FMP extras** — macro cluster **shipped 2026-09-01 (v9.5.0)**:
   EconomicIndicators, CountryProfile (assembled, six calls/country), GdpReal
   (= `gdp_growth_annual`, a growth-percent series), GdpNominal
   (`gdp_current_usd`), ConsumerPriceIndex, Unemployment. Correction to the
   macro-semantics risk note below: EODHD **does** carry the CPI index level
   (`consumer_price_index`, 2010 = 100) alongside the inflation rate, so the
   fetcher honors both `transform="index"` and `transform="yoy"`.
   Still open: OptionsChains (verify marketplace subscription first; document
   EOD/partial-greeks) and IndexConstituents — both 403 until the add-ons are
   purchased.

## Testing

- Follow the existing recorded-fixture pattern in `tests/` (one test module per new
  model file).
- Add a registration smoke check: each new standard-model key is present in
  `eodhd_provider.fetcher_dict` and its fetcher returns a non-empty, schema-valid
  result for `AAPL.US` (ETF models against a known ETF, forex/index against known
  symbols).
- Bump `openbb-eodhd` version and note new providers in the README/description.

## Risks / open items

- **Required standard-model fields:** some standard `Data` models have required
  fields EODHD may not populate; per fetcher, fill what EODHD gives and let extras
  pass through — surface any hard-required gap during TDD (may force a model to the
  "not feasible" list).
- **Marketplace access — CONFIRMED against the live key (2026-08-29):** the account
  is EODHD All-in-One (100k req/day). Verified working: search, screener, macro
  indicators, and **GovernmentTrades** (congressional trades, 200 OK). Verified
  **403 (not subscribed)**: **OptionsChains** (Options Data Feed) and
  **IndexConstituents** (Index Components marketplace). The public `demo` key does
  **not** unlock these — it 403s on options too and only serves the core APIs
  (fundamentals/eod/real-time/news/earnings-calendar/market-cap/div/splits/technicals)
  for AAPL/MSFT/TSLA/VTI/EURUSD.FOREX/BTC-USD.CC. So OptionsChains and
  IndexConstituents can be **coded but not verified or used** until those two add-ons
  are purchased — build them last, behind a clear "requires subscription" note, and
  leave their tests skipped/marked xfail until the feed is enabled. Everything else in
  scope is testable now with the account key.
- **OptionsChains fidelity:** EODHD options are **end-of-day, not a live chain**, and
  expose implied volatility but likely not the full greek set (delta/gamma/theta/
  vega). Populate what EODHD returns; leave unmapped greeks null rather than compute.
- **Macro semantics:** EODHD `/macro-indicator` gives an **inflation rate** for CPI,
  not the CPI index level fred/oecd return — map to the standard model's rate field
  and document; GdpReal maps to a growth series, GdpNominal to current-USD level.
  These overlap EconDB/fred/oecd already on the NAS (redundant coverage, by request).
- **Point-in-time vs series:** FinancialRatios / PriceTarget are single-snapshot from
  EODHD where FMP returns history — document the difference; don't fake a series.
- **API-call cost:** EODHD bills per call and some endpoints (insider Form-4, earnings
  trends) cost 10 calls each — the shared `/fundamentals` helper keeps the
  fundamentals-derived cluster to one call; dedicated endpoints are one call each.
- **Deployment:** the NAS runs `openbb-local:1.0.0` from `<nas-checkout>`.
  How that image is built/pulled (local build on the NAS vs. GHCR pull of a
  Mac-built image) must be confirmed in the implementation plan so the new providers
  actually reach production; the `update-openbb-docker` skill's `--container`/`--push`
  covers the Mac→GHCR path.
- **Standalone github repo drift:** `github.com/artcashin/openbb-eodhd` is a stale
  0.1.0; the live source is this in-repo copy (v9.0.0). Decide whether to re-sync the
  standalone repo or leave it (out of scope here).

## Addendum (2026-09-03): commodities — considered, dropped

Initially recorded as an episode-9 stretch goal after confirming EODHD's
`get_historical_commodity_prices` returns real, live data (WTI tested, full
monthly history to 1986) and that `CommoditySpotPrices` exists as a standard
model in `openbb_core`. Retracted on closer check, same day.

**The verification was incomplete the first time.** "No installed provider
backs it" was checked against `openbb_core`'s standard-model files, not
against actual fetcher registrations. `openbb_fred` — already installed in
this stack — registers `FredCommoditySpotPricesFetcher` against exactly this
standard model. The data is available today, no build required.

**And EODHD's version is that same FRED data, one hop removed.** The
response's own metadata carries `"source": "fred"` — EODHD is reselling the
FRED series through its marketplace endpoint, not publishing anything
original. Building an EODHD-backed fetcher for it would mean two paths to the
identical numbers, with the EODHD path adding its own marketplace auth and
rate limits as a second thing that can fail for zero additional data. Get it
from `fred` directly; there's nothing here for `openbb-eodhd` to add.

## Addendum (2026-09-03): sentiment — investigated, no new fetcher needed

CompanyNews and WorldNews shipped in the Company Core phase (v9.4.0, 2026-09-01) with
an extra `sentiment` field (EODHD's polarity/pos/neu/neg dict) on every article. After
release, EODHD's separate `/sentiment` endpoint (daily aggregated score per symbol)
looked like a plausible follow-on widget — it wasn't in the original FMP-parity scope
because FMP doesn't register anything like it, so it would have been a "Beyond FMP"
addition, the same category as the macro cluster.

**Sweep for precedent.** Grepped every installed OpenBB provider package plus
`openbb_core` for a standard model shaped like "sentiment, keyed by symbol and date."
Three hits, none a match:

- `TopRetailData` (`openbb_core.provider.standard_models.top_retail`) — `date`,
  `symbol`, `activity`, `sentiment` (-1..1). Closest shape, but it's retail order-flow
  imbalance (`nasdaq` provider, tracking ~$30B/day of individual-investor trades), not
  news-derived sentiment. Forcing EODHD's article-count/polarity data into `activity`/
  `sentiment` would misrepresent what the numbers mean.
- Intrinio's `CompanyNewsData`/`WorldNewsData` add `sentiment` (positive/neutral/
  negative) + `sentiment_confidence` directly onto the **news** standard models —
  no separate sentiment model or router. This is the real precedent, and
  `openbb-eodhd` already followed it independently (the polarity dict added in
  v9.4.0), before this sweep confirmed it was the established pattern.
- `UofMichiganData` — macro consumer-confidence survey, unrelated.

**Verified `/sentiment` adds no data over `/news`.** Pulled EODHD's raw `/news` for
AAPL.US across the same window already sampled from `/sentiment`, and compared
independently, per day:

| Date | Articles pulled | Mean article `sentiment.polarity` | `/sentiment` count | `/sentiment` normalized |
|---|---|---|---|---|
| 2026-09-02 | 57 | 0.651 | 57 | 0.6511 |
| 2026-09-01 | 66 | 0.615 | 66 | 0.6151 |
| 2026-08-31 | 59 | 0.595 | 59 | 0.5947 |

Every day where the full article count was captured matches to three decimals —
`/sentiment`'s `count` is the day's article count and its `normalized` score is the
plain mean of that day's `sentiment.polarity` values already returned by `/news`.
It is a server-side convenience recomputation of data `openbb-eodhd` already exposes,
not an independent signal.

**Decision: no new fetcher, no new standard model, no fork of `openbb_core` /
`openbb_news`.** The per-article `sentiment` field on `EODHDCompanyNewsData` /
`EODHDWorldNewsData` (shipped v9.4.0) is the complete data. A daily aggregate, if a
dashboard wants one, is a groupby-mean over data already in hand — a chart-side
computation, not a new API call or provider capability. Also checked whether Intrinio
exposes sentiment as its own widget distinct from its news widget: it does not —
`openbb_news`'s router defines only `company` and `world` commands, no sentiment
command, confirming sentiment-as-a-news-field (not sentiment-as-its-own-widget) is
the pattern across the platform, not just an EODHD shortcut.

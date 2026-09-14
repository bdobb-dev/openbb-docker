# Technical Analysis Chart — a Polars indicator engine behind a Workspace widget

Design spec for Adventures in OpenBB, chapter TBD. Companion release: TBD
(`live-grid` is currently `9.0.0`).

## Context

[StockCharts ChartSchool](https://chartschool.stockcharts.com) documents 112
indicators across three catalogues: 23 technical overlays, 66 technical
indicators, and 23 market (breadth) indicators. An audit of this stack's EODHD
entitlement against that list found:

- **16** are returned pre-calculated by EODHD's `/technical` endpoint
  (verified live: `SPY.US` `bbands` → `788.7273`).
- **71** need only single-symbol OHLCV, which the kdb+ read-through cache
  already serves.
- **19** are breadth indicators requiring constituent fan-out.
- **1** is directly chartable already: Volatility Indices (`VIX.INDX` verified).
- **5** are unavailable: Put/Call Ratio and anything options-derived (the US
  options marketplace add-on returns 403 — S9), the DecisionPoint Rydex Ratio
  (fund-flow data EODHD does not carry), SCTR and RRG Relative Strength (both
  proprietary — RRG's JdK RS-Ratio/RS-Momentum formulas are nowhere disclosed
  and the marks belong to RRG Research), and Pring's Inflation/Deflation
  Indexes (`!PRII`/`!PRDI`, StockCharts-only symbols with unpublished members).

Nothing in this stack charts any of them today. `live-grid`'s `figure.py` is 24
lines and draws a candlestick.

Two facts shaped the design more than any preference:

1. **`live-grid` is its own container.** `python:3.12-slim` with fastapi,
   uvicorn, eodhd, kdb-store, pandas, httpx. The `openbb-api` image's
   `openbb_technical` (27 routes) and `pandas_ta_openbb` are *not* importable
   from it — only reachable as HTTP calls. A compute dependency is being added
   either way, which is what makes writing the indicators a live option rather
   than an obvious mistake.
2. **`live-grid` already stitches ticks onto cached history** (`series.py`
   `stitch`/`seam_boundary`). Live indicators do not need new plumbing, only a
   recompute trigger.

## Goals

1. One Workspace widget renders any registered indicator over any symbol the
   kdb+ cache serves, in stacked Plotly panes.
2. Named **chart macros** package indicators, parameters, pane order and pane
   heights into a reusable layout, editable without rebuilding the image.
3. Indicators are selectable from either source — local compute or EODHD's
   native endpoint — and **the two agree**.
4. The chart updates live off the existing tick stream.
5. Every indicator's convention (smoothing, price basis, variant) is stated in
   the registry, never inherited from a library's undocumented default.

## Non-goals

- **No breadth indicators.** All 19 need 504-ticker fan-out, a precompute
  layer, and carry survivorship bias because the historical-constituents
  endpoint 403s (S9). Own spec, later.
- No replacement of `kdb_cache_chart`. It keeps its route and its `figure.py`.
- No new container. This extends `live-grid` per operator decision.
- No attempt at SCTR, RRG, or the Pring index family. See Context.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Polars, eager**, as the compute engine — not pandas_ta | 6.5x faster at realistic windows (S1). But the deciding reasons are that a dependency is added either way (`live-grid` is its own image), the deferred breadth phase is where Polars genuinely wins, and a library hides conventions rather than removing the need to own them (S4, S6). |
| D2 | **Eager, not lazy**, for single-symbol | Lazy measured at 0.52–0.98x of eager — no better, sometimes worse (S2). Predicate pushdown cannot fire: filtering a date window before a rolling computation eats the warmup bars. LazyFrame is reserved for the breadth phase. |
| D3 | Indicators declare **explicit dependencies**; shared bases are materialized once | Polars CSE does fire (23 `__POLARS_CSER` nodes) but captures only 1.20x of an available 2.06x (S3). Declaring beats inferring by ~1.7x. |
| D4 | **Conventions are pinned in the registry**, per indicator and per source | Two independent libraries silently return *slow* %K under the name "k" (S4, S6). ChartSchool documents Fast/Slow/Full as three distinct indicators. `smooth_k` is an explicit parameter, never a hidden default. |
| D5 | **`price_basis` is a registry field**, defaulting to whatever makes the two sources agree | EODHD switches basis per indicator: `adjusted_close` for RSI/BBands, raw OHLC for ATR (S5). Feeding one basis to both engines puts Bollinger Bands 1.7% out — enough to move a signal, small enough to go unnoticed. |
| D6 | Both sources emit **identical column names**; `panes.py`/`figure.py` never learn which ran | Toggling `source` must not move the line. That is the whole point of offering both. |
| D7 | EODHD source is **cached and bar-close-gated**, with a refetch floor | EODHD bills **5 API calls per indicator per request**. A six-indicator macro at 1 Hz is ~108,000 calls/hour against a 100,000/day limit. Local series tick; EODHD series step at bar close. |
| D8 | Unsupported-by-EODHD indicators **fall back to local and say so** in the legend | 16 of ~87 have native equivalents. A silent fallback recreates exactly the trap D5 exists to prevent. |
| D9 | **Approach A**: recompute the stitched window on a throttle; no incremental indicator state | Live and static call the same builder, so they cannot disagree. Incremental state would mean hand-writing ~87 streaming implementations whose seeding differs from the batch versions. Measured cost is 0.66 ms per recompute (S1) — 0.07% of a core at 1 Hz. |
| D10 | Deltas carry **only revised bars**; indicators that repaint are flagged | A revision to bar *t* changes only bar *t* for causal rolling/ewm indicators. ZigZag genuinely repaints. The `repaints` flag ships now even though ZigZag is phase 2, because retrofitting it would mean reworking the wire protocol. |
| D11 | Path-dependent indicators get an **imperative escape hatch** (`iterative.py`) | Parabolic SAR is a recurrence and cannot be a vectorized expression. SAR is in tier-1 deliberately, to force this seam in v1 rather than during phase 2. |
| D12 | Macros are **YAML in a mounted directory**, and populate the widget dropdown at serve time | `/widgets.json` is already a route (`main.py:166`), not a static file. Adding a macro file must not require editing JSON by hand. Mirrors the existing `rss-ticker-config/`, `ts-config/` mounts. |
| D13 | Parity tolerance is **2e-4 relative**, not 1e-9 | EODHD rounds its JSON to 4 decimals; that is the measured error floor (S7), not a guess. |
| D14 | The TA engine lives in **`app/ta/`**, a bounded package | Operator chose to extend `live-grid` rather than add a service. `main.py` is already 282 lines; it gains a route, not an engine. |

## Architecture

### Module layout

```
live-grid/app/ta/
  exprs.py      Polars expression library
  registry.py   indicator definitions: params, deps, outputs, pane,
                conventions, price_basis, repaints, EODHD map
  compute.py    dependency resolution + two-pass evaluation
  iterative.py  path-dependent indicators (SAR now; ZigZag, Chandelier later)
  sources.py    LocalSource | EodhdSource, normalised to identical columns
  macros.py     load + validate macro YAML
  panes.py      series -> pane assignment, heights, axis domains
  figure.py     multi-pane Plotly assembly + delta payloads
live-grid/macros/*.yml        baked-in macros; TA_MACRO_DIR overrides
```

`app/figure.py` (the existing candlestick builder) is untouched.

### Data flow

```
params (symbol, interval, source, macro | indicators, window)
  -> build_series()          EXISTING: kdb history + tick stitch
  -> pl.DataFrame
  -> registry: resolve deps  -> unique Base set
  -> pass 1: materialise each Base exactly once
  -> pass 2: indicator expressions referencing those Bases
  -> [source=eodhd: concurrent HTTP fetch, joined on date, same columns]
  -> panes.assign -> figure.build -> Plotly JSON
```

### The registry

```python
@dataclass(frozen=True)
class Base:
    kind: str            # "sma" | "ewm" | "wilder" | "std" | "max" | "min" | "tr"
    col: str
    period: int
    @property
    def key(self) -> str: return f"{self.kind}:{self.col}:{self.period}"

@dataclass(frozen=True)
class Indicator:
    name: str
    label: str
    params: dict[str, Any]
    pane: str                     # "price" | "own"
    price_basis: str              # "adjusted" | "raw"   (D5)
    repaints: bool                # (D10)
    deps: Callable                # params -> list[Base]
    build: Callable               # (params, bases) -> list[pl.Expr]
    render: dict                  # output column -> {"type": "line"|"bar"|"band"}
    guides: list[float]           # e.g. RSI [30, 70]
    convention: str               # pinned prose, surfaced in hover meta
    eodhd: EodhdMap | None        # None = local only (D8)
```

Compute is two passes — the shape that measured 2.06x (S3):

```python
bases = {b.key: b for ind in requested for b in ind.deps(ind.params)}
frame = df.with_columns([b.expr().alias(b.key) for b in bases.values()])
frame = frame.with_columns([e for ind in requested for e in ind.build(ind.params, bases)])
```

Path-dependent indicators (`iterative.py`) run after the vectorised pass.

### The dual-source adapter

```python
class Source(Protocol):
    name: str
    def series(self, bars: pl.DataFrame, reqs: list[Req]) -> pl.DataFrame: ...
```

An EODHD mapping states the wire contract explicitly:

```python
eodhd=EodhdMap(
    function="stochastic",
    params={"period": "k", "slow_kperiod": "smooth_k", "slow_dperiod": "d"},
    fields={"k_values": "stoch_k", "d_values": "stoch_d"},
    price_basis="raw",
    note="EODHD returns SLOW %K (smooth_k=3), on raw close. Local must match.",
)
```

Field names come from the wire, not the docs: the MCP tool's docstring claims
`slow_k`/`slow_d`; the API returns `k_values`/`d_values`.

Cache key is `(symbol, function, params, interval, last_closed_bar)`. Live
pushes reuse the cache; a refetch happens on bar close, subject to a floor
(`TA_EODHD_MIN_REFETCH_S`, default 60). A running call-budget counter is
exposed on `/health`. A 403 or timeout drops that series to local compute,
annotates the legend, and logs — never a blank chart.

### Macros and panes

```yaml
label: "Classic Momentum"
description: "Price with bands and the 200, momentum underneath"
panes:
  - id: price
    height: 3
    indicators:
      - {name: bbands, period: 20, k: 2.0}
      - {name: sma, period: 200, style: {color: "#e8b923"}}
  - id: rsi
    height: 1
    guides: [30, 70]
    indicators: [{name: rsi, period: 14}]
  - id: macd
    height: 1
    indicators: [{name: macd, fast: 12, slow: 26, signal: 9}]
  - id: vol
    height: 1
    indicators: [{name: volume}]
```

`height` is a relative weight, not pixels — a macro renders correctly at any
size Workspace gives the widget. `volume` is an ordinary registry entry
(`render: bar`), not a special case. Guides default from the registry; a macro
mentions them only to override.

**Macro and picker compose.** With `macro = none`, picker indicators
auto-assign — `pane: "price"` overlays onto the price pane, `pane: "own"`
oscillators each get a pane below in registry order. With a macro selected, the
macro defines the layout and any picker indicator not already in it appends as
a new bottom pane.

`panes.py` is pure and Plotly-free:

```python
def assign(macro, picks, registry) -> list[Pane]
def domains(panes, gap=0.02) -> list[tuple[float, float]]
```

`domains` is the only arithmetic: with weights `wᵢ` over `N` panes and gap `g`,
`avail = 1 − g(N−1)`, `hᵢ = wᵢ/Σw × avail`, stacked top-down.

Plotly: one `yaxis` per pane carrying its `domain`, x-axis shared via `matches`,
traces anchored `yaxis: "y2"` and so on. Only the bottom pane draws tick labels;
`rangeslider` stays off.

Macros are validated **at load**: every indicator name resolves, every param is
known and type-correct, heights are positive, exactly one pane carries price. An
overlay placed in an oscillator pane warns rather than fails.

### The live path

`/ta_chart` (REST) and `/ta_chart_ws` both call one `build_payload(params)`
(D9). On connect: the full figure. Then a throttled loop
(`TA_PUSH_INTERVAL_MS`, default 1000) rebuilds the stitched series, recomputes,
and sends a delta:

```json
{"rev": 42, "from": 1247, "x": ["..."], "traces": {"0": {"y": []}, "3": {"y": []}}}
```

The delta covers only revised bars (D10) — normally 1, or 2 at a bar close,
which is also when the EODHD cache invalidates. If a recompute overruns the push
interval the tick is **dropped, not queued**, so a slow frame cannot spiral into
backpressure.

Degradation follows house style: ws drop → client falls back to REST polling;
kdb unreachable → history-only, matching the existing "chart and tick recording
simply stay off" behaviour.

## Tier-1 indicator set

22 indicators, chosen to exercise every shape in the design rather than only the
popular ones. ◆ = has an EODHD native equivalent (13 of 22 — the parity oracle).

**Overlays (price pane):** SMA◆, EMA◆, WMA◆, Bollinger Bands◆, Keltner
Channels, Price Channels (Donchian), VWAP, Parabolic SAR◆, Volume

**Oscillators (own pane):** RSI◆, MACD + signal + histogram◆, Stochastic
(fast/slow/full explicit)◆, StochRSI◆, ATR◆, ADX + DMI◆, CCI◆, Williams %R,
ROC/Momentum, OBV, Standard Deviation◆, %B, Bollinger BandWidth

Three earn their place structurally: **Parabolic SAR** forces `iterative.py`
(D11); **%B** and **BandWidth** derive from the same Bollinger `Base` as the
bands themselves, so a macro using all three makes D3's dedup observable.

## Spike evidence

| # | Finding |
|---|---------|
| S1 | Polars eager vs pandas_ta, 12 tier-1 indicators, median of 30: **250 bars 0.60/4.01 ms (6.6x)**, 1000 bars 0.66/4.26 ms (6.5x), 5000 bars 1.26/6.67 ms (5.3x), 20000 bars 3.44/14.6 ms (4.3x). Speedup *rises* as the window shrinks — the cost is per-call overhead, not arithmetic. Both engines are fast enough for a live chart; the perf argument alone is not decisive. |
| S2 | Lazy vs eager at single-symbol scale: **0.98x and 0.52x** across two runs. Lazy never helped. |
| S3 | Polars CSE **does** dedupe rolling/ewm subtrees — 23 `__POLARS_CSER` nodes on a deliberately EMA-sharing macro (MACD+PPO+Keltner+Squeeze+ATR+BBands) — worth **1.20x**. Hand-deduping the same set: **2.06x**. |
| S4 | 12 of 13 hand-written Polars expressions match pandas_ta to ≤1e-10 relative (SMA, EMA, RSI, StdDev, ROC, BBands upper+mid, MACD line+signal, ATR, OBV, Williams %R). Stochastic %K diverged by 48 points: pandas_ta's `STOCHk_14_3_3` applies a hidden `smooth_k=3`. Raw fast %K matched to **4e-14** once smoothed. |
| S5 | **EODHD `/technical` switches price basis per indicator.** SPY, 912 bars: RSI(14) matches `adjusted_close` at 1.5e-5 (raw close: 3.1e-3 median); BBands(20,2) matches `adjusted_close` at 1.2e-7 (raw close: **1.7e-2 median**); ATR(14) matches **raw** OHLC at 3.8e-5 (adjusted: 9.6e-1). Undocumented. |
| S6 | EODHD stochastic is **Slow Stochastic on raw close**: `k_values` = SMA(3) of raw %K(14), matched at 5.0e-5; `d_values` = SMA(3) of `k_values`, matched at 6.7e-5. |
| S7 | EODHD rounds JSON output to 4 decimals (`{"rsi": 54.2842}`), setting a ~1e-5 relative error floor. Parity tolerance is set at 2e-4 (D13). |
| S8 | `pandas_ta_openbb` 0.4.24 has an import bug: `maps.py` calls `importlib.metadata` without importing it, so `import pandas_ta` raises `AttributeError` in a clean venv. It works in the OpenBB image only because something else imports `importlib.metadata` first. |
| S9 | Marketplace add-ons return **403** on this account: US options EOD (`get_us_options_eod`) and historical index constituents (`mp_index_components`). Current S&P membership *is* available via `get_fundamentals_data("GSPC.INDX")` → 504 members. `VIX.INDX` works. EODHD carries no pre-built breadth series (searches for "advance decline" and "McClellan" return 0 results). |

Spike code was throwaway, run in an isolated venv; the validated tier-1
expressions are reused as the starting point for `exprs.py`.

## Success criteria

1. A macro renders price + ≥3 oscillator panes with correct relative heights at
   two different widget sizes.
2. Toggling `source` between `local` and `eodhd` on an EODHD-mapped indicator
   **does not visibly move the line**; parity holds within 2e-4 for all 13.
3. An indicator with no EODHD equivalent renders under `source=eodhd` and is
   visibly marked as locally computed.
4. Dropping a new YAML file into `TA_MACRO_DIR` makes it selectable in
   Workspace without editing `widgets.json`.
5. A macro requesting Bollinger Bands + %B + BandWidth materialises the
   Bollinger `Base` **exactly once**.
6. Live pushes advance the chart off the tick stream; a delta carries only
   revised bars.
7. Fast, Slow, and Full stochastic produce three different series, each
   matching its ChartSchool definition.
8. With kdb+ unreachable, the widget still renders history and does not error.

## Testing

| Test | Scope |
|------|-------|
| `test_ta_exprs.py` | Golden values against a committed ~300-bar fixture CSV, tol 1e-9. Offline. |
| `test_ta_conventions.py` | Fast/Slow/Full stochastic differ and each matches its definition; RSI/ATR/ADX use Wilder (`alpha=1/n`); Bollinger `ddof`. Direct guard for the S4/S6 bug class. |
| `test_ta_parity.py` | Local vs EODHD for all 13 mapped indicators, tol 2e-4. Network-gated (`pytest -m network`), off by default. |
| `test_ta_compute.py` | Dependency dedup: MACD+PPO+BBands+%B+BandWidth materialises the expected `Base` count exactly. |
| `test_ta_panes.py` | Pane assignment and `domains` arithmetic. Pure, no Plotly. |
| `test_ta_macros.py` | Load-time validation: unknown indicator, bad param type, zero height, two price panes all raise. |
| `test_ta_live.py` | Fake tick feed: delta covers only revised bars; a `repaints=True` indicator sends the full series; overrun drops rather than queues. |

## Risks and follow-ups

- **EODHD's price-basis behaviour is undocumented and could change.** D5 pins
  it from measurement (S5). `test_ta_parity.py` is the tripwire; it needs to run
  on a schedule, not only on demand, or drift will land silently.
- **EODHD API budget.** D7's cache and floor are the guard; the `/health`
  counter makes burn visible. An intraday interval with a large macro is still
  capable of consuming the daily limit — a hard per-day cap is a follow-up.
- **Tier-1 is 22 of ~87.** The remaining single-symbol indicators are registry
  entries against a working harness, but the long tail (Special K, PMO, CTM,
  ConnorsRSI, Coppock, Vortex, Ulcer, Mass Index) is genuine implementation work
  that pandas_ta would have supplied free. Accepted under D1.
- **`live-grid` grows.** D14 bounds the engine to `app/ta/`, but the service now
  owns live streaming *and* a TA engine. If `main.py` starts absorbing TA logic,
  that is the signal to revisit the separate-service option.
- **Chapter/version numbering is unresolved.** Title says TBD.

## Out of scope

- All 19 breadth indicators; the constituent fan-out and precompute layer.
- ZigZag and the rest of the path-dependent family beyond SAR.
- SCTR, RRG Relative Strength, Pring's Inflation/Deflation Indexes, Put/Call
  Ratio and options-derived indicators (see Context).
- Any change to `kdb_cache_chart` or `app/figure.py`.

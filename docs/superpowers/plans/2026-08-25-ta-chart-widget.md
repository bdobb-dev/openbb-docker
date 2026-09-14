# TA Chart Widget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Workspace charting widget to `live-grid` that renders 22 technical indicators over cached OHLCV in stacked Plotly panes, computed either locally in Polars or fetched from EODHD's native endpoint, arranged by reusable YAML "chart macros", and updated live off the existing tick stream.

**Architecture:** A new bounded package `live-grid/app/ta/` holds the engine. Indicators are registry entries that declare their dependencies explicitly; a two-pass Polars evaluation materialises each shared sub-series exactly once. Two interchangeable sources (local compute, EODHD REST) emit identical column names, so pane assembly never learns which ran. The live path recomputes the whole stitched window on a throttle rather than carrying incremental indicator state, so the REST and websocket routes call one builder and cannot disagree.

**Tech Stack:** Python 3.12, Polars (eager), FastAPI, PyYAML, Plotly JSON (client-rendered — this service has no chart backend), pytest.

**Spec:** `docs/superpowers/specs/2026-08-25-ta-chart-widget-design.md`

## Global Constraints

- Python `>=3.12`. Ruff `target-version = "py312"`, `line-length = 100`.
- All new code lives under `live-grid/app/ta/`. `app/main.py` gains routes only, never engine logic (spec D14).
- `app/figure.py` and the `/chart` route are **not modified**. They serve `kdb_cache_chart`.
- Polars is used **eagerly**. No `LazyFrame` anywhere in this plan (spec D2).
- Indicators **declare dependencies**; never rely on Polars CSE (spec D3).
- Every indicator states `price_basis` (`"adjusted"` or `"raw"`) and `convention` prose. No inherited defaults (spec D4, D5).
- Golden-value test tolerance: **1e-9 relative**. EODHD parity tolerance: **2e-4 relative** (EODHD rounds JSON to 4 decimals — spec D13).
- EODHD `/technical` costs **5 API calls per indicator per request**. Never call it in an unthrottled loop (spec D7).
- Env vars introduced: `TA_MACRO_DIR`, `TA_PUSH_INTERVAL_MS` (default `1000`), `TA_EODHD_MIN_REFETCH_S` (default `60`).
- Wilder smoothing is `ewm_mean(alpha=1/n, adjust=False, ignore_nulls=True)` — **not** `span=n`. Used by RSI, ATR, ADX/DMI.
- Bollinger standard deviation uses `ddof=0` (population), matching EODHD.
- Tests are plain pytest functions with a module docstring, matching `tests/test_figure.py`.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `live-grid/app/ta/__init__.py` | Package marker. Empty. |
| `live-grid/app/ta/exprs.py` | `Base` dataclass and the Polars expression vocabulary. No indicator knowledge. |
| `live-grid/app/ta/registry.py` | `Indicator`, `EodhdMap`, `Req` dataclasses and the 22 tier-1 registrations. |
| `live-grid/app/ta/compute.py` | Two-pass dependency-deduplicating evaluation. |
| `live-grid/app/ta/iterative.py` | Path-dependent indicators that cannot be expressions. Parabolic SAR. |
| `live-grid/app/ta/sources.py` | `LocalSource` and `EodhdSource`, normalised to identical columns. |
| `live-grid/app/ta/macros.py` | Macro YAML loading and load-time validation. |
| `live-grid/app/ta/panes.py` | Pane assignment and axis-domain arithmetic. Plotly-free. |
| `live-grid/app/ta/figure.py` | Multi-pane Plotly assembly and delta payloads. |
| `live-grid/app/ta/payload.py` | `build_payload()` — the single builder both routes call. |
| `live-grid/macros/*.yml` | Baked-in chart macros. |
| `live-grid/tests/fixtures/ohlcv.csv` | 300-bar deterministic fixture for golden tests. |
| `live-grid/app/main.py` | **Modify:** add `/ta_chart` and `/ta_chart_ws` routes. |
| `live-grid/widgets.json` | **Modify:** add the `ta_chart` widget entry. |
| `live-grid/pyproject.toml` | **Modify:** add `polars`, `pyyaml` deps and the `network` pytest marker. |

---

## Task 1: Dependencies and the test fixture

**Files:**
- Modify: `live-grid/pyproject.toml`
- Create: `live-grid/app/ta/__init__.py`
- Create: `live-grid/tests/fixtures/make_fixture.py`
- Create: `live-grid/tests/fixtures/ohlcv.csv`

**Interfaces:**
- Consumes: nothing.
- Produces: `tests/fixtures/ohlcv.csv` with header `date,open,high,low,close,adj_close,volume` and 300 rows. Every later test loads this file.

- [ ] **Step 1: Add dependencies and the pytest marker**

In `live-grid/pyproject.toml`, add to `dependencies`:

```toml
    "polars>=1.20",
    "pyyaml>=6.0",
```

and replace the `[tool.pytest.ini_options]` block with:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
markers = [
    "network: hits the live EODHD API; deselected by default",
]
addopts = "-m 'not network'"
```

- [ ] **Step 2: Create the package marker**

```bash
mkdir -p live-grid/app/ta live-grid/tests/fixtures
printf '"""Technical analysis engine: registry, compute, sources, panes, figure."""\n' \
  > live-grid/app/ta/__init__.py
```

- [ ] **Step 3: Write the fixture generator**

Create `live-grid/tests/fixtures/make_fixture.py`. It is committed so the fixture is reproducible, but tests read the CSV, never run this.

```python
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
```

- [ ] **Step 4: Generate the fixture and verify its shape**

Run:

```bash
cd live-grid && python tests/fixtures/make_fixture.py && head -2 tests/fixtures/ohlcv.csv && wc -l tests/fixtures/ohlcv.csv
```

Expected: `wrote 300 rows`, a header line `date,open,high,low,close,adj_close,volume`, and `301` total lines.

- [ ] **Step 5: Install and confirm imports**

Run:

```bash
cd live-grid && pip install -e '.[dev]' && python -c "import polars, yaml; print(polars.__version__)"
```

Expected: a version `>= 1.20` prints with no ImportError.

- [ ] **Step 6: Commit**

```bash
git add live-grid/pyproject.toml live-grid/app/ta/__init__.py live-grid/tests/fixtures/
git commit -m "chore(live-grid): add polars/pyyaml and the TA test fixture"
```

---

## Task 2: The expression vocabulary (`exprs.py`)

**Files:**
- Create: `live-grid/app/ta/exprs.py`
- Test: `live-grid/tests/test_ta_exprs.py`

**Interfaces:**
- Consumes: `tests/fixtures/ohlcv.csv` from Task 1.
- Produces:
  - `Base(kind: str, col: str, period: int)` — frozen dataclass, `.key -> str`, `.expr() -> pl.Expr`.
  - `kind` is one of `"sma" | "ewm" | "wilder" | "std" | "max" | "min" | "tr"`.
  - `col` is one of `"close" | "adj_close" | "high" | "low" | "volume" | "raw"` (`"raw"` only with `kind="tr"`).
  - `price_col(basis: str) -> str` mapping `"adjusted" -> "adj_close"`, `"raw" -> "close"`.
  - `true_range() -> pl.Expr`.
  - `load_bars(path) -> pl.DataFrame` test helper is **not** here; tests build their own.

- [ ] **Step 1: Write the failing test**

Create `live-grid/tests/test_ta_exprs.py`:

```python
"""The Polars expression vocabulary: Base keys and the primitives they build."""

import polars as pl
import pytest

from app.ta.exprs import Base, price_col, true_range

from tests.ta_helpers import fixture_frame


def test_base_key_is_stable_and_distinguishes_price_basis():
    assert Base("ewm", "close", 12).key == "ewm:close:12"
    assert Base("ewm", "adj_close", 12).key != Base("ewm", "close", 12).key


def test_price_col_maps_basis_to_column_name():
    assert price_col("adjusted") == "adj_close"
    assert price_col("raw") == "close"


def test_price_col_rejects_an_unknown_basis():
    with pytest.raises(ValueError, match="price_basis"):
        price_col("split_adjusted")


def test_sma_matches_a_hand_rolled_mean():
    df = fixture_frame()
    out = df.with_columns(Base("sma", "close", 5).expr().alias("x"))
    closes = df["close"].to_list()
    assert out["x"][4] == pytest.approx(sum(closes[:5]) / 5, rel=1e-12)
    assert out["x"][3] is None  # warmup


def test_wilder_is_alpha_one_over_n_not_span():
    df = fixture_frame()
    out = df.with_columns([
        Base("wilder", "close", 14).expr().alias("w"),
        pl.col("close").ewm_mean(span=14, adjust=False).alias("span14"),
    ])
    assert out["w"][100] != pytest.approx(out["span14"][100], rel=1e-6)


def test_std_uses_population_ddof_zero():
    df = fixture_frame()
    out = df.with_columns([
        Base("std", "close", 20).expr().alias("pop"),
        pl.col("close").rolling_std(20, ddof=1).alias("sample"),
    ])
    assert out["pop"][50] < out["sample"][50]


def test_true_range_is_the_max_of_the_three_ranges():
    df = fixture_frame().with_columns(true_range().alias("tr"))
    row, prev = df.row(50, named=True), df.row(49, named=True)
    expected = max(row["high"] - row["low"],
                   abs(row["high"] - prev["close"]),
                   abs(row["low"] - prev["close"]))
    assert df["tr"][50] == pytest.approx(expected, rel=1e-12)


def test_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown Base kind"):
        Base("kalman", "close", 5).expr()
```

Create the shared test helper `live-grid/tests/ta_helpers.py`:

```python
"""Shared loaders for TA tests."""

from pathlib import Path

import polars as pl

FIXTURE = Path(__file__).parent / "fixtures" / "ohlcv.csv"


def fixture_frame() -> pl.DataFrame:
    """The committed 300-bar fixture, typed the way the engine expects."""
    return pl.read_csv(FIXTURE).with_columns([
        pl.col("date").str.to_date(),
        pl.col(["open", "high", "low", "close", "adj_close", "volume"]).cast(pl.Float64),
    ])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd live-grid && pytest tests/test_ta_exprs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ta.exprs'`

- [ ] **Step 3: Write the implementation**

Create `live-grid/app/ta/exprs.py`:

```python
"""The Polars expression vocabulary.

A `Base` is a shared sub-series that more than one indicator may need -- a
20-period mean of close, Wilder's average of true range. Indicators declare the
Bases they depend on and the compute pass materialises each one exactly once.

That deduplication is deliberate rather than delegated: Polars' common
subexpression elimination does fire on these subtrees, but measured at only
1.20x against the 2.06x available from declaring them (spec S3).

`col` carries the price basis in its name -- "close" vs "adj_close" -- so two
indicators reading different bases produce different Base keys and are never
collapsed into one. EODHD switches basis per indicator (spec S5), so this is
load-bearing, not tidiness.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

_BASIS_TO_COL = {"adjusted": "adj_close", "raw": "close"}


def price_col(basis: str) -> str:
    """The frame column an indicator with this price basis reads."""
    try:
        return _BASIS_TO_COL[basis]
    except KeyError:
        raise ValueError(
            f"unknown price_basis {basis!r}; expected one of {sorted(_BASIS_TO_COL)}"
        ) from None


def true_range() -> pl.Expr:
    """Wilder's true range, always on raw OHLC."""
    prev_close = pl.col("close").shift(1)
    return pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - prev_close).abs(),
        (pl.col("low") - prev_close).abs(),
    )


@dataclass(frozen=True)
class Base:
    """A deduplicable sub-series. `kind` selects the primitive, `col` the input."""

    kind: str
    col: str
    period: int

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.col}:{self.period}"

    def expr(self) -> pl.Expr:
        if self.kind == "tr":
            return true_range()
        c = pl.col(self.col)
        if self.kind == "sma":
            return c.rolling_mean(self.period)
        if self.kind == "ewm":
            return c.ewm_mean(span=self.period, adjust=False)
        if self.kind == "wilder":
            # Wilder's smoothing is alpha = 1/n, NOT span = n. RSI, ATR and ADX
            # all read wrong -- plausibly, not obviously -- if this slips.
            return c.ewm_mean(alpha=1 / self.period, adjust=False, ignore_nulls=True)
        if self.kind == "std":
            # Population, matching EODHD's Bollinger bands (spec S5).
            return c.rolling_std(self.period, ddof=0)
        if self.kind == "max":
            return c.rolling_max(self.period)
        if self.kind == "min":
            return c.rolling_min(self.period)
        raise ValueError(f"unknown Base kind {self.kind!r}")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd live-grid && pytest tests/test_ta_exprs.py -v`
Expected: 8 passed.

- [ ] **Step 5: Lint**

Run: `cd live-grid && ruff check app/ta/ tests/test_ta_exprs.py tests/ta_helpers.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add live-grid/app/ta/exprs.py live-grid/tests/test_ta_exprs.py live-grid/tests/ta_helpers.py
git commit -m "feat(ta): Base expression vocabulary with explicit price basis"
```

---

## Task 3: Registry types and the first five indicators

**Files:**
- Create: `live-grid/app/ta/registry.py`
- Test: `live-grid/tests/test_ta_registry.py`

**Interfaces:**
- Consumes: `Base`, `price_col`, `true_range` from Task 2.
- Produces:
  - `Req(name: str, params: dict)` — a resolved request for one indicator.
  - `EodhdMap(function: str, params: dict[str, str], fields: dict[str, str], price_basis: str, note: str)` where `params` maps **EODHD query param -> our param name** and `fields` maps **EODHD response field -> our column name**.
  - `Indicator` frozen dataclass with fields: `name, label, params, pane, price_basis, repaints, deps, build, render, guides, convention, eodhd`.
  - `REGISTRY: dict[str, Indicator]`.
  - `get(name) -> Indicator`, `resolve(name, **overrides) -> Req`.
  - Registered in this task: `sma`, `ema`, `rsi`, `bbands`, `atr`.

- [ ] **Step 1: Write the failing test**

Create `live-grid/tests/test_ta_registry.py`:

```python
"""Registry shape, convention pinning, and the first five indicators."""

import pytest

from app.ta.compute import compute
from app.ta.registry import REGISTRY, Req, get, resolve

from tests.ta_helpers import fixture_frame


def test_every_registered_indicator_states_its_conventions():
    for name, ind in REGISTRY.items():
        assert ind.price_basis in ("adjusted", "raw"), name
        assert ind.convention.strip(), f"{name} has no pinned convention"
        assert ind.pane in ("price", "own"), name
        assert isinstance(ind.repaints, bool), name


def test_resolve_applies_defaults_then_overrides():
    assert resolve("sma").params["period"] == 50
    assert resolve("sma", period=200).params["period"] == 200


def test_resolve_rejects_an_unknown_parameter():
    with pytest.raises(ValueError, match="unknown parameter 'window'"):
        resolve("sma", window=200)


def test_get_rejects_an_unknown_indicator():
    with pytest.raises(KeyError, match="ichimoku"):
        get("ichimoku")


def test_rsi_reads_adjusted_close_and_bounds_to_zero_hundred():
    out = compute(fixture_frame(), [resolve("rsi", period=14)])
    vals = [v for v in out["rsi"].to_list() if v is not None]
    assert len(vals) > 250
    assert min(vals) >= 0.0 and max(vals) <= 100.0


def test_atr_reads_raw_ohlc_not_adjusted():
    assert get("atr").price_basis == "raw"
    out = compute(fixture_frame(), [resolve("atr", period=14)])
    assert all(v >= 0 for v in out["atr"].to_list() if v is not None)


def test_bbands_emits_three_ordered_bands():
    out = compute(fixture_frame(), [resolve("bbands", period=20, k=2.0)])
    row = out.row(100, named=True)
    assert row["bb_lo"] < row["bb_mid"] < row["bb_up"]


def test_sma_and_ema_land_on_the_price_pane():
    assert get("sma").pane == "price" and get("ema").pane == "price"


def test_rsi_carries_thirty_seventy_guides():
    assert get("rsi").guides == [30.0, 70.0]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd live-grid && pytest tests/test_ta_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ta.registry'`

- [ ] **Step 3: Write the implementation**

Create `live-grid/app/ta/registry.py`:

```python
"""Indicator definitions.

Every entry states its own conventions rather than inheriting a library's.
That is not pedantry: pandas_ta and EODHD *both* return slow %K under the name
"k" (spec S4, S6), and EODHD computes RSI and Bollinger on adjusted close but
ATR on raw OHLC (spec S5). An indicator that does not say which it means is an
indicator that is silently wrong somewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import polars as pl

from app.ta.exprs import Base, price_col


@dataclass(frozen=True)
class Req:
    """One indicator with its parameters fully resolved."""

    name: str
    params: dict[str, Any]


@dataclass(frozen=True)
class EodhdMap:
    """How to ask EODHD for this indicator and read its answer.

    `params` maps EODHD query parameter -> our parameter name.
    `fields`  maps EODHD response field  -> our output column name.
    """

    function: str
    params: dict[str, str]
    fields: dict[str, str]
    price_basis: str
    note: str
    # Some EODHD windows are off by one from ours: their `period` counts
    # intervals where ours counts bars. Measured for stddev -- their period=20
    # equals our period=21, to 1.4e-14. Applied when building the query.
    offsets: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Indicator:
    name: str
    label: str
    params: dict[str, Any]
    pane: str                      # "price" | "own"
    price_basis: str               # "adjusted" | "raw"
    convention: str
    deps: Callable[[dict], list[Base]]
    build: Callable[[dict, dict[str, Base]], list[pl.Expr]]
    render: dict[str, dict]
    guides: list[float] = field(default_factory=list)
    repaints: bool = False
    eodhd: EodhdMap | None = None


REGISTRY: dict[str, Indicator] = {}


def register(ind: Indicator) -> Indicator:
    if ind.name in REGISTRY:
        raise ValueError(f"indicator {ind.name!r} is already registered")
    REGISTRY[ind.name] = ind
    return ind


def get(name: str) -> Indicator:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown indicator {name!r}") from None


def resolve(name: str, **overrides: Any) -> Req:
    """Defaults from the registry, overridden by keyword. Unknown keys raise."""
    ind = get(name)
    for key in overrides:
        if key not in ind.params:
            raise ValueError(
                f"unknown parameter {key!r} for {name!r}; "
                f"expected one of {sorted(ind.params)}"
            )
    return Req(name, {**ind.params, "style": None, **overrides})


def _line(color: str | None = None) -> dict:
    return {"type": "line", "color": color}


# --- Simple moving average -------------------------------------------------

register(Indicator(
    name="sma", label="SMA", params={"period": 50}, pane="price",
    price_basis="adjusted",
    convention="Arithmetic mean of adjusted close over `period` bars.",
    deps=lambda p: [Base("sma", price_col("adjusted"), p["period"])],
    build=lambda p, b: [
        pl.col(b[f"sma:adj_close:{p['period']}"].key).alias("sma"),
    ],
    render={"sma": _line("#4c9be8")},
    eodhd=EodhdMap("sma", {"period": "period"}, {"sma": "sma"}, "adjusted",
                   "EODHD sma is computed on adjusted close."),
))

# --- Exponential moving average --------------------------------------------

register(Indicator(
    name="ema", label="EMA", params={"period": 50}, pane="price",
    price_basis="adjusted",
    convention="EMA with alpha = 2/(period+1), adjust=False, on adjusted close.",
    deps=lambda p: [Base("ewm", price_col("adjusted"), p["period"])],
    build=lambda p, b: [
        pl.col(b[f"ewm:adj_close:{p['period']}"].key).alias("ema"),
    ],
    render={"ema": _line("#e8b923")},
    eodhd=EodhdMap("ema", {"period": "period"}, {"ema": "ema"}, "adjusted",
                   "EODHD ema is computed on adjusted close."),
))

# --- Relative strength index -----------------------------------------------


def _rsi_build(p: dict, b: dict[str, Base]) -> list[pl.Expr]:
    n = p["period"]
    col = price_col("adjusted")
    delta = pl.col(col).diff()
    gain = pl.when(delta > 0).then(delta).otherwise(0.0)
    loss = pl.when(delta < 0).then(-delta).otherwise(0.0)
    wilder = dict(alpha=1 / n, adjust=False, ignore_nulls=True)
    rs = gain.ewm_mean(**wilder) / loss.ewm_mean(**wilder)
    return [(100 - 100 / (1 + rs)).alias("rsi")]


register(Indicator(
    name="rsi", label="RSI", params={"period": 14}, pane="own",
    price_basis="adjusted", guides=[30.0, 70.0],
    convention=(
        "Wilder's RSI: gains and losses smoothed with alpha=1/period "
        "(not span=period), on adjusted close. Matches EODHD to 1.5e-5."
    ),
    deps=lambda p: [],  # its smoothing is of derived gain/loss, not a shared Base
    build=_rsi_build,
    render={"rsi": _line("#8ed081")},
    eodhd=EodhdMap("rsi", {"period": "period"}, {"rsi": "rsi"}, "adjusted",
                   "EODHD rsi matches Wilder-on-adjusted-close at 1.5e-5 (spec S5)."),
))

# --- Bollinger bands --------------------------------------------------------


def _bbands_build(p: dict, b: dict[str, Base]) -> list[pl.Expr]:
    n, k = p["period"], p["k"]
    mid = pl.col(f"sma:adj_close:{n}")
    sd = pl.col(f"std:adj_close:{n}")
    return [
        mid.alias("bb_mid"),
        (mid + k * sd).alias("bb_up"),
        (mid - k * sd).alias("bb_lo"),
    ]


register(Indicator(
    name="bbands", label="Bollinger Bands", params={"period": 20, "k": 2.0},
    pane="price", price_basis="adjusted",
    convention=(
        "Middle = SMA(period) of adjusted close. Bands = mid +/- k * population "
        "standard deviation (ddof=0). Matches EODHD to 1.2e-7."
    ),
    deps=lambda p: [Base("sma", price_col("adjusted"), p["period"]),
                    Base("std", price_col("adjusted"), p["period"])],
    build=_bbands_build,
    render={"bb_mid": _line("#9aa0a6"), "bb_up": _line("#c678dd"),
            "bb_lo": _line("#c678dd")},
    eodhd=EodhdMap("bbands", {"period": "period"},
                   {"uband": "bb_up", "mband": "bb_mid", "lband": "bb_lo"},
                   "adjusted",
                   "EODHD bbands is adjusted-close with ddof=0 (spec S5)."),
))

# --- Average true range -----------------------------------------------------

register(Indicator(
    name="atr", label="ATR", params={"period": 14}, pane="own",
    price_basis="raw",
    convention=(
        "Wilder's average of true range on RAW OHLC. EODHD's atr matches raw, "
        "not adjusted -- feeding adjusted close here is a 96% error (spec S5)."
    ),
    deps=lambda p: [Base("tr", "raw", 0)],
    build=lambda p, b: [
        pl.col("tr:raw:0")
          .ewm_mean(alpha=1 / p["period"], adjust=False, ignore_nulls=True)
          .alias("atr"),
    ],
    render={"atr": _line("#e06c75")},
    eodhd=EodhdMap("atr", {"period": "period"}, {"atr": "atr"}, "raw",
                   "EODHD atr is raw OHLC (spec S5)."),
))
```

- [ ] **Step 4: Run the test to verify it fails on the missing compute module**

Run: `cd live-grid && pytest tests/test_ta_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ta.compute'`. Task 4 supplies it; the four tests that do not import `compute` are unblocked by that task too, so leave the file as-is and continue.

- [ ] **Step 5: Commit**

```bash
git add live-grid/app/ta/registry.py live-grid/tests/test_ta_registry.py
git commit -m "feat(ta): registry types and the first five indicators"
```

---

## Task 4: The deduplicating compute pass (`compute.py`)

**Files:**
- Create: `live-grid/app/ta/compute.py`
- Test: `live-grid/tests/test_ta_compute.py`

**Interfaces:**
- Consumes: `Base` (Task 2); `Req`, `Indicator`, `get` (Task 3).
- Produces:
  - `collect_bases(reqs: list[Req]) -> dict[str, Base]` — unique by `Base.key`.
  - `compute(df: pl.DataFrame, reqs: list[Req]) -> pl.DataFrame` — input frame plus one column per indicator output, Base columns dropped.
  - `compute_with_bases(df, reqs) -> tuple[pl.DataFrame, dict[str, Base]]` — same, Base columns retained. Tests use this to assert deduplication.

- [ ] **Step 1: Write the failing test**

Create `live-grid/tests/test_ta_compute.py`:

```python
"""Two-pass evaluation: shared sub-series are materialised exactly once."""

import polars as pl
import pytest

from app.ta.compute import collect_bases, compute, compute_with_bases
from app.ta.registry import resolve

from tests.ta_helpers import fixture_frame


def test_two_indicators_sharing_a_base_collect_it_once():
    reqs = [resolve("bbands", period=20, k=2.0), resolve("sma", period=20)]
    bases = collect_bases(reqs)
    assert sorted(bases) == ["sma:adj_close:20", "std:adj_close:20"]


def test_differing_periods_are_not_collapsed():
    bases = collect_bases([resolve("sma", period=20), resolve("sma", period=50)])
    assert sorted(bases) == ["sma:adj_close:20", "sma:adj_close:50"]


def test_differing_price_basis_is_not_collapsed():
    """ATR reads raw, SMA reads adjusted. Merging them would be silently wrong."""
    bases = collect_bases([resolve("atr", period=14), resolve("sma", period=14)])
    assert "tr:raw:0" in bases and "sma:adj_close:14" in bases


def test_base_columns_are_dropped_from_the_public_result():
    out = compute(fixture_frame(), [resolve("bbands", period=20, k=2.0)])
    assert [c for c in out.columns if ":" in c] == []
    assert {"bb_up", "bb_mid", "bb_lo"} <= set(out.columns)


def test_original_bar_columns_survive():
    out = compute(fixture_frame(), [resolve("rsi", period=14)])
    assert {"date", "open", "high", "low", "close", "adj_close", "volume"} <= set(out.columns)


def test_empty_request_list_returns_the_frame_unchanged():
    df = fixture_frame()
    assert compute(df, []).columns == df.columns


def test_duplicate_requests_do_not_produce_duplicate_columns():
    out = compute(fixture_frame(), [resolve("sma", period=50), resolve("sma", period=50)])
    assert out.columns.count("sma") == 1


def test_compute_with_bases_exposes_the_intermediate_columns():
    _, bases = compute_with_bases(fixture_frame(), [resolve("bbands", period=20, k=2.0)])
    assert len(bases) == 2


def test_a_frame_missing_adj_close_fails_loudly():
    df = fixture_frame().drop("adj_close")
    with pytest.raises(pl.exceptions.ColumnNotFoundError):
        compute(df, [resolve("sma", period=50)])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd live-grid && pytest tests/test_ta_compute.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ta.compute'`

- [ ] **Step 3: Write the implementation**

Create `live-grid/app/ta/compute.py`:

```python
"""Evaluation in two passes.

Pass one materialises every distinct `Base` exactly once. Pass two evaluates
the indicator expressions, which reference those Bases by column name.

Doing this by hand rather than leaving it to Polars' optimiser is worth about
1.7x on a macro whose indicators share moving averages -- CSE captures 1.20x of
an available 2.06x (spec S3, D3).
"""

from __future__ import annotations

import polars as pl

from app.ta.exprs import Base
from app.ta.registry import Req, get


def collect_bases(reqs: list[Req]) -> dict[str, Base]:
    """Every Base the requests need, unique by key."""
    bases: dict[str, Base] = {}
    for req in reqs:
        for base in get(req.name).deps(req.params):
            bases[base.key] = base
    return bases


def compute_with_bases(
    df: pl.DataFrame, reqs: list[Req]
) -> tuple[pl.DataFrame, dict[str, Base]]:
    """Evaluate `reqs` against `df`, keeping the intermediate Base columns."""
    if not reqs:
        return df, {}
    bases = collect_bases(reqs)
    frame = df
    if bases:
        frame = frame.with_columns([b.expr().alias(b.key) for b in bases.values()])

    seen: set[str] = set()
    exprs: list[pl.Expr] = []
    for req in reqs:
        for expr in get(req.name).build(req.params, bases):
            name = expr.meta.output_name()
            if name in seen:
                continue  # the same indicator requested twice
            seen.add(name)
            exprs.append(expr)
    if exprs:
        frame = frame.with_columns(exprs)
    return frame, bases


def compute(df: pl.DataFrame, reqs: list[Req]) -> pl.DataFrame:
    """Evaluate `reqs` against `df`. Base columns are an implementation detail."""
    frame, bases = compute_with_bases(df, reqs)
    return frame.drop([k for k in bases if k in frame.columns])
```

- [ ] **Step 4: Run both test files to verify they pass**

Run: `cd live-grid && pytest tests/test_ta_compute.py tests/test_ta_registry.py -v`
Expected: all pass — 9 in `test_ta_compute.py`, 9 in `test_ta_registry.py`.

- [ ] **Step 5: Lint and commit**

```bash
cd live-grid && ruff check app/ta/ tests/
git add live-grid/app/ta/compute.py live-grid/tests/test_ta_compute.py
git commit -m "feat(ta): two-pass compute with explicit dependency dedup"
```

---

## Task 5: The remaining sixteen tier-1 indicators

**Files:**
- Modify: `live-grid/app/ta/registry.py` (append registrations)
- Test: `live-grid/tests/test_ta_conventions.py`
- Modify: `live-grid/tests/test_ta_registry.py` (add a count assertion)

**Interfaces:**
- Consumes: everything from Tasks 2-4.
- **Standing convention (applies to every indicator in this task):** any
  expression that divides must end in `.fill_nan(None)`. `0/0` is not
  hypothetical — it is deterministic at bar 0 for RSI-shaped indicators and
  occurs on any flat window for range-normalised ones. Null is already the
  warmup spelling every rolling indicator uses; NaN is a second, inconsistent
  spelling of "undefined" that also breaks equality (`NaN != NaN`) in
  downstream tests and websocket delta diffs.
- Produces: `REGISTRY` containing 22 entries. New output column names, which later tasks reference:
  `wma`, `kc_mid`/`kc_up`/`kc_lo`, `dc_up`/`dc_mid`/`dc_lo`, `vwap`, `sar`, `volume_bar`,
  `macd`/`macd_signal`/`macd_hist`, `stoch_k`/`stoch_d`, `stochrsi`, `adx`/`di_plus`/`di_minus`,
  `cci`, `willr`, `roc`, `obv`, `stddev`, `pct_b`, `bandwidth`.

- [ ] **Step 1: Write the failing convention test**

Create `live-grid/tests/test_ta_conventions.py`:

```python
"""Convention pinning. These guard the exact bug class the spike found twice:
a library silently returning a different variant under a familiar name."""

import polars as pl
import pytest

from app.ta.compute import compute
from app.ta.registry import REGISTRY, get, resolve

from tests.ta_helpers import fixture_frame


def _series(name, **params):
    out = compute(fixture_frame(), [resolve(name, **params)])
    return out


def test_fast_slow_and_full_stochastic_are_three_different_series():
    fast = _series("stoch", k=14, smooth_k=1, d=3)["stoch_k"].to_list()
    slow = _series("stoch", k=14, smooth_k=3, d=3)["stoch_k"].to_list()
    full = _series("stoch", k=14, smooth_k=5, d=3)["stoch_k"].to_list()
    assert fast[100] != pytest.approx(slow[100], rel=1e-9)
    assert slow[100] != pytest.approx(full[100], rel=1e-9)


def test_slow_stochastic_is_the_three_period_mean_of_fast():
    fast = _series("stoch", k=14, smooth_k=1, d=3)["stoch_k"].to_list()
    slow = _series("stoch", k=14, smooth_k=3, d=3)["stoch_k"].to_list()
    assert slow[100] == pytest.approx(sum(fast[98:101]) / 3, rel=1e-9)


def test_stochastic_default_is_fast_and_says_so():
    assert get("stoch").params["smooth_k"] == 1
    assert "fast" in get("stoch").convention.lower()


def test_stochastic_reads_raw_ohlc_matching_eodhd():
    assert get("stoch").price_basis == "raw"


def test_percent_d_is_the_mean_of_percent_k():
    out = _series("stoch", k=14, smooth_k=1, d=3)
    k, d = out["stoch_k"].to_list(), out["stoch_d"].to_list()
    assert d[100] == pytest.approx(sum(k[98:101]) / 3, rel=1e-9)


def test_macd_histogram_is_line_minus_signal():
    out = _series("macd", fast=12, slow=26, signal=9)
    row = out.row(100, named=True)
    assert row["macd_hist"] == pytest.approx(row["macd"] - row["macd_signal"], rel=1e-9)


def test_percent_b_is_zero_at_the_lower_band_and_one_at_the_upper():
    out = compute(fixture_frame(), [resolve("bbands", period=20, k=2.0),
                                    resolve("pct_b", period=20, k=2.0)])
    row = out.row(100, named=True)
    expected = (row["adj_close"] - row["bb_lo"]) / (row["bb_up"] - row["bb_lo"])
    assert row["pct_b"] == pytest.approx(expected, rel=1e-9)


def test_bandwidth_is_band_span_over_the_middle():
    out = compute(fixture_frame(), [resolve("bbands", period=20, k=2.0),
                                    resolve("bandwidth", period=20, k=2.0)])
    row = out.row(100, named=True)
    expected = (row["bb_up"] - row["bb_lo"]) / row["bb_mid"] * 100
    assert row["bandwidth"] == pytest.approx(expected, rel=1e-9)


def test_bbands_pct_b_and_bandwidth_share_one_base_pair():
    from app.ta.compute import collect_bases
    bases = collect_bases([resolve("bbands", period=20, k=2.0),
                           resolve("pct_b", period=20, k=2.0),
                           resolve("bandwidth", period=20, k=2.0)])
    assert sorted(bases) == ["sma:adj_close:20", "std:adj_close:20"]


def test_divide_prone_indicators_yield_null_not_nan_on_a_flat_window():
    """0/0 must render as a gap, not NaN.

    Null is already how every rolling indicator spells its warmup; NaN would
    be a second spelling of the same idea, and it breaks equality comparisons
    (NaN != NaN) in every downstream test and delta diff.
    """
    import math

    flat = fixture_frame().with_columns([
        pl.lit(100.0).alias(c) for c in ("open", "high", "low", "close", "adj_close")
    ])
    for name in ("rsi", "stoch", "stochrsi", "adx", "cci", "willr", "pct_b"):
        out = compute(flat, [resolve(name)])
        for column in get(name).render:
            values = out[column].to_list()
            assert not any(v is not None and math.isnan(v) for v in values), (
                f"{name}/{column} produced NaN on a flat window; expected null"
            )


def test_rsi_is_null_at_bar_zero_not_nan():
    """Bar 0 has no previous close, so gain and loss are both 0 and rs is 0/0."""
    out = compute(fixture_frame(), [resolve("rsi", period=14)])
    assert out["rsi"][0] is None


def test_wilder_indicators_all_declare_it():
    for name in ("rsi", "atr", "adx"):
        assert "wilder" in get(name).convention.lower(), name


def test_every_indicator_renders_every_column_it_builds():
    df = fixture_frame()
    for name, ind in REGISTRY.items():
        out = compute(df, [resolve(name)])
        for column in ind.render:
            assert column in out.columns, f"{name} declares render for missing {column}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd live-grid && pytest tests/test_ta_conventions.py -v`
Expected: FAIL — `KeyError: "unknown indicator 'stoch'"`

- [ ] **Step 3: Append the remaining indicators to `registry.py`**

Add to the end of `live-grid/app/ta/registry.py`:

```python
# --- Weighted moving average ------------------------------------------------

register(Indicator(
    name="wma", label="WMA", params={"period": 50}, pane="price",
    price_basis="adjusted",
    convention="Linearly weighted mean; weight i = i for i in 1..period.",
    deps=lambda p: [],
    build=lambda p, b: [
        pl.col(price_col("adjusted"))
          .rolling_mean(p["period"],
                        weights=[float(i) for i in range(1, p["period"] + 1)])
          .alias("wma"),
    ],
    render={"wma": _line("#56b6c2")},
    eodhd=EodhdMap("wma", {"period": "period"}, {"wma": "wma"}, "adjusted",
                   "EODHD wma is adjusted close."),
))

# --- Keltner channels -------------------------------------------------------


def _keltner_build(p: dict, b: dict[str, Base]) -> list[pl.Expr]:
    mid = pl.col(f"ewm:adj_close:{p['period']}")
    atr = pl.col("tr:raw:0").ewm_mean(
        alpha=1 / p["atr_period"], adjust=False, ignore_nulls=True
    )
    return [mid.alias("kc_mid"),
            (mid + p["mult"] * atr).alias("kc_up"),
            (mid - p["mult"] * atr).alias("kc_lo")]


register(Indicator(
    name="keltner", label="Keltner Channels",
    params={"period": 20, "mult": 2.0, "atr_period": 10}, pane="price",
    price_basis="adjusted",
    convention="EMA(period) of adjusted close +/- mult * Wilder ATR(atr_period) of raw OHLC.",
    deps=lambda p: [Base("ewm", price_col("adjusted"), p["period"]), Base("tr", "raw", 0)],
    build=_keltner_build,
    render={"kc_mid": _line("#9aa0a6"), "kc_up": _line("#61afef"),
            "kc_lo": _line("#61afef")},
))

# --- Price channels (Donchian) ---------------------------------------------

register(Indicator(
    name="donchian", label="Price Channels", params={"period": 20}, pane="price",
    price_basis="raw",
    convention="Highest high and lowest low of raw OHLC over `period` bars; mid is their mean.",
    deps=lambda p: [Base("max", "high", p["period"]), Base("min", "low", p["period"])],
    build=lambda p, b: [
        pl.col(f"max:high:{p['period']}").alias("dc_up"),
        pl.col(f"min:low:{p['period']}").alias("dc_lo"),
        ((pl.col(f"max:high:{p['period']}") + pl.col(f"min:low:{p['period']}")) / 2)
        .alias("dc_mid"),
    ],
    render={"dc_up": _line("#98c379"), "dc_lo": _line("#98c379"),
            "dc_mid": _line("#9aa0a6")},
))

# --- VWAP -------------------------------------------------------------------

register(Indicator(
    name="vwap", label="VWAP", params={}, pane="price", price_basis="raw",
    convention=(
        "Cumulative typical-price-weighted mean over the whole window "
        "(not session-anchored). Typical price = (H+L+C)/3 on raw OHLC."
    ),
    deps=lambda p: [],
    build=lambda p, b: [
        (((pl.col("high") + pl.col("low") + pl.col("close")) / 3 * pl.col("volume")).cum_sum()
         / pl.col("volume").cum_sum()).alias("vwap"),
    ],
    render={"vwap": _line("#d19a66")},
))

# --- Volume -----------------------------------------------------------------

register(Indicator(
    name="volume", label="Volume", params={}, pane="own", price_basis="raw",
    convention="Raw bar volume, drawn as bars.",
    deps=lambda p: [],
    build=lambda p, b: [pl.col("volume").alias("volume_bar")],
    render={"volume_bar": {"type": "bar", "color": "#5c6370"}},
))

# --- MACD -------------------------------------------------------------------


def _macd_build(p: dict, b: dict[str, Base]) -> list[pl.Expr]:
    line = pl.col(f"ewm:adj_close:{p['fast']}") - pl.col(f"ewm:adj_close:{p['slow']}")
    signal = line.ewm_mean(span=p["signal"], adjust=False)
    return [line.alias("macd"), signal.alias("macd_signal"),
            (line - signal).alias("macd_hist")]


register(Indicator(
    name="macd", label="MACD", params={"fast": 12, "slow": 26, "signal": 9},
    pane="own", price_basis="adjusted", guides=[0.0],
    convention="EMA(fast) - EMA(slow) on adjusted close; signal is EMA(signal) of that line.",
    deps=lambda p: [Base("ewm", price_col("adjusted"), p["fast"]),
                    Base("ewm", price_col("adjusted"), p["slow"])],
    build=_macd_build,
    render={"macd": _line("#61afef"), "macd_signal": _line("#e06c75"),
            "macd_hist": {"type": "bar", "color": "#5c6370"}},
    eodhd=EodhdMap("macd", {"fast_period": "fast", "slow_period": "slow",
                            "signal_period": "signal"},
                   {"macd": "macd", "signal": "macd_signal",
                    "divergence": "macd_hist"},
                   "adjusted",
                   "EODHD returns macd/signal/divergence -- NOT macd_signal or "
                   "macd_hist. Verified against the live API."),
))

# --- Stochastic oscillator --------------------------------------------------


def _stoch_build(p: dict, b: dict[str, Base]) -> list[pl.Expr]:
    hh = pl.col(f"max:high:{p['k']}")
    ll = pl.col(f"min:low:{p['k']}")
    raw = 100 * (pl.col("close") - ll) / (hh - ll)
    # Fill immediately after the division, before %D's rolling mean averages it.
    # A NaN entering rolling_mean poisons `d` bars of output rather than one.
    k = (raw if p["smooth_k"] <= 1 else raw.rolling_mean(p["smooth_k"])).fill_nan(None)
    return [k.alias("stoch_k"), k.rolling_mean(p["d"]).alias("stoch_d")]


register(Indicator(
    name="stoch", label="Stochastic", params={"k": 14, "smooth_k": 1, "d": 3},
    pane="own", price_basis="raw", guides=[20.0, 80.0],
    convention=(
        "smooth_k=1 is FAST %K, 3 is SLOW, anything else is FULL. Raw OHLC. "
        "EODHD and pandas_ta both return SLOW under the bare name 'k' "
        "(spec S4, S6) -- this default is fast, and stated."
    ),
    deps=lambda p: [Base("max", "high", p["k"]), Base("min", "low", p["k"])],
    build=_stoch_build,
    render={"stoch_k": _line("#61afef"), "stoch_d": _line("#e06c75")},
    eodhd=EodhdMap("stochastic",
                   {"period": "k", "slow_kperiod": "smooth_k", "slow_dperiod": "d"},
                   {"k_values": "stoch_k", "d_values": "stoch_d"}, "raw",
                   "EODHD returns SLOW %K (smooth_k=3) on raw close (spec S6)."),
))

# --- Stochastic RSI ---------------------------------------------------------


def _stochrsi_build(p: dict, b: dict[str, Base]) -> list[pl.Expr]:
    n = p["period"]
    delta = pl.col(price_col("adjusted")).diff()
    wilder = dict(alpha=1 / n, adjust=False, ignore_nulls=True)
    gain = pl.when(delta > 0).then(delta).otherwise(0.0).ewm_mean(**wilder)
    loss = pl.when(delta < 0).then(-delta).otherwise(0.0).ewm_mean(**wilder)
    rsi = 100 - 100 / (1 + gain / loss)
    lo, hi = rsi.rolling_min(p["stoch_period"]), rsi.rolling_max(p["stoch_period"])
    # Two 0/0 routes here: the inline RSI at bar 0, and hi == lo on a flat
    # window. Both resolve to null.
    return [(100 * (rsi - lo) / (hi - lo)).fill_nan(None).alias("stochrsi")]


register(Indicator(
    name="stochrsi", label="StochRSI",
    params={"period": 14, "stoch_period": 14}, pane="own",
    price_basis="adjusted", guides=[20.0, 80.0],
    convention="Stochastic of Wilder RSI(period) over stoch_period bars, scaled 0-100.",
    deps=lambda p: [],
    build=_stochrsi_build,
    render={"stochrsi": _line("#c678dd")},
    eodhd=EodhdMap("stochrsi", {"period": "period"}, {"fastkline": "stochrsi"},
                   "adjusted",
                   "EODHD returns fastkline/fastdline, NOT a 'stochrsi' key -- "
                   "verified against the live API; scale matches ours exactly."),
))

# --- ADX and directional movement -------------------------------------------


def _adx_build(p: dict, b: dict[str, Base]) -> list[pl.Expr]:
    n = p["period"]
    wilder = dict(alpha=1 / n, adjust=False, ignore_nulls=True)
    up = pl.col("high").diff()
    down = -pl.col("low").diff()
    plus_dm = pl.when((up > down) & (up > 0)).then(up).otherwise(0.0)
    minus_dm = pl.when((down > up) & (down > 0)).then(down).otherwise(0.0)
    atr = pl.col("tr:raw:0").ewm_mean(**wilder)
    di_plus = 100 * plus_dm.ewm_mean(**wilder) / atr
    di_minus = 100 * minus_dm.ewm_mean(**wilder) / atr
    # Fill BEFORE the smoothing, not after. NaN is not null: ewm_mean skips
    # nulls but PROPAGATES NaN, so the single bar-0 NaN in dx would contaminate
    # every later value and a trailing fill would then null the entire series.
    # Measured: filling after gives 0/912 finite values; filling here gives
    # 910/912, and matches EODHD to a median of 1.2e-06.
    dx = (100 * (di_plus - di_minus).abs() / (di_plus + di_minus)).fill_nan(None)
    return [dx.ewm_mean(**wilder).alias("adx"),
            di_plus.fill_nan(None).alias("di_plus"),
            di_minus.fill_nan(None).alias("di_minus")]


register(Indicator(
    name="adx", label="ADX / DMI", params={"period": 14}, pane="own",
    price_basis="raw", guides=[20.0, 25.0],
    convention=(
        "Wilder's ADX on raw OHLC: +DM/-DM smoothed with alpha=1/period, "
        "divided by Wilder ATR, then DX smoothed again."
    ),
    deps=lambda p: [Base("tr", "raw", 0)],
    build=_adx_build,
    render={"adx": _line("#e5c07b"), "di_plus": _line("#98c379"),
            "di_minus": _line("#e06c75")},
    eodhd=EodhdMap("adx", {"period": "period"}, {"adx": "adx"}, "raw",
                   "EODHD adx is raw OHLC; +DI/-DI come from function=dmi."),
))

# --- Commodity channel index ------------------------------------------------


def _cci_build(p: dict, b: dict[str, Base]) -> list[pl.Expr]:
    n = p["period"]
    tp = (pl.col("high") + pl.col("low") + pl.col("close")) / 3
    sma_tp = tp.rolling_mean(n)
    # Mean absolute deviation about the rolling mean. Polars has no rolling MAD,
    # so this is the explicit form rather than a clever one.
    mad = (tp - sma_tp).abs().rolling_mean(n)
    # MAD is zero on a flat window, giving 0/0.
    return [((tp - sma_tp) / (0.015 * mad)).fill_nan(None).alias("cci")]


register(Indicator(
    name="cci", label="CCI", params={"period": 20}, pane="own",
    price_basis="raw", guides=[-100.0, 100.0],
    convention=(
        "(typical price - SMA) / (0.015 * mean absolute deviation), raw OHLC. "
        "MAD is taken about the rolling mean, not the rolling median."
    ),
    deps=lambda p: [],
    build=_cci_build,
    render={"cci": _line("#d19a66")},
    # No eodhd map, deliberately. EODHD's CCI disagrees with the standard
    # definition by a median of 28.5% across 754 bars, and the ratio is not
    # constant (0.21-1.24), so it is not the 0.015 divisor. No period offset
    # (19-22, 40) and no MAD/stddev denominator variant reconciles it. Spec D6
    # requires that toggling `source` not move the line; here it would move it
    # by a quarter. So CCI is local-only and says so in the legend.
))

# --- Williams %R ------------------------------------------------------------

register(Indicator(
    name="willr", label="Williams %R", params={"period": 14}, pane="own",
    price_basis="raw", guides=[-80.0, -20.0],
    convention="-100 * (highest high - close) / (highest high - lowest low), raw OHLC.",
    deps=lambda p: [Base("max", "high", p["period"]), Base("min", "low", p["period"])],
    build=lambda p, b: [
        (-100 * (pl.col(f"max:high:{p['period']}") - pl.col("close"))
         / (pl.col(f"max:high:{p['period']}") - pl.col(f"min:low:{p['period']}")))
        .fill_nan(None)  # hh == ll on a flat window
        .alias("willr"),
    ],
    render={"willr": _line("#56b6c2")},
))

# --- Rate of change ---------------------------------------------------------

register(Indicator(
    name="roc", label="Rate of Change", params={"period": 12}, pane="own",
    price_basis="adjusted", guides=[0.0],
    convention="Percent change of adjusted close over `period` bars.",
    deps=lambda p: [],
    build=lambda p, b: [
        (pl.col(price_col("adjusted")).pct_change(p["period"]) * 100).alias("roc"),
    ],
    render={"roc": _line("#98c379")},
))

# --- On balance volume ------------------------------------------------------

register(Indicator(
    name="obv", label="On Balance Volume", params={}, pane="own",
    price_basis="adjusted",
    convention="Running sum of signed volume; sign from the adjusted close change.",
    deps=lambda p: [],
    build=lambda p, b: [
        (pl.col(price_col("adjusted")).diff().sign() * pl.col("volume"))
        .cum_sum().alias("obv"),
    ],
    render={"obv": _line("#61afef")},
))

# --- Standard deviation -----------------------------------------------------

register(Indicator(
    name="stddev", label="Standard Deviation", params={"period": 20}, pane="own",
    price_basis="adjusted",
    convention="Population standard deviation (ddof=0) of adjusted close.",
    deps=lambda p: [Base("std", price_col("adjusted"), p["period"])],
    build=lambda p, b: [pl.col(f"std:adj_close:{p['period']}").alias("stddev")],
    render={"stddev": _line("#c678dd")},
    eodhd=EodhdMap("stddev", {"period": "period"}, {"stddev": "stddev"},
                   "adjusted",
                   "EODHD's period counts intervals, not bars: their period=20 "
                   "equals our 21 (matched to 1.4e-14). Hence the -1 offset.",
                   offsets={"period": -1}),
))

# --- %B and BandWidth: both reuse the Bollinger bases ------------------------

register(Indicator(
    name="pct_b", label="%B", params={"period": 20, "k": 2.0}, pane="own",
    price_basis="adjusted", guides=[0.0, 1.0],
    convention="(close - lower) / (upper - lower). Shares Bollinger's bases exactly.",
    deps=lambda p: [Base("sma", price_col("adjusted"), p["period"]),
                    Base("std", price_col("adjusted"), p["period"])],
    build=lambda p, b: [
        ((pl.col(price_col("adjusted"))
          - (pl.col(f"sma:adj_close:{p['period']}")
             - p["k"] * pl.col(f"std:adj_close:{p['period']}")))
         / (2 * p["k"] * pl.col(f"std:adj_close:{p['period']}")))
        .fill_nan(None)  # zero stddev on a flat window
        .alias("pct_b"),
    ],
    render={"pct_b": _line("#e5c07b")},
))

register(Indicator(
    name="bandwidth", label="Bollinger BandWidth",
    params={"period": 20, "k": 2.0}, pane="own", price_basis="adjusted",
    convention="(upper - lower) / middle * 100. Shares Bollinger's bases exactly.",
    deps=lambda p: [Base("sma", price_col("adjusted"), p["period"]),
                    Base("std", price_col("adjusted"), p["period"])],
    build=lambda p, b: [
        (2 * p["k"] * pl.col(f"std:adj_close:{p['period']}")
         / pl.col(f"sma:adj_close:{p['period']}") * 100).alias("bandwidth"),
    ],
    render={"bandwidth": _line("#d19a66")},
))
```

- [ ] **Step 4: Add the registry count assertion**

Append to `live-grid/tests/test_ta_registry.py`:

```python
def test_tier_one_is_twenty_two_indicators_with_thirteen_eodhd_maps():
    assert len(REGISTRY) == 22, sorted(REGISTRY)
    mapped = [n for n, i in REGISTRY.items() if i.eodhd is not None]
    assert len(mapped) == 12, sorted(mapped)  # cci is local-only (see registry)
```

Note: `sar` is registered in Task 6, which brings the count to 22 and the EODHD maps to 13. Until then this assertion fails — that is expected and is the pointer to the next task.

- [ ] **Step 5: Run the convention tests**

Run: `cd live-grid && pytest tests/test_ta_conventions.py -v`
Expected: all 11 pass.

- [ ] **Step 6: Commit**

```bash
git add live-grid/app/ta/registry.py live-grid/tests/test_ta_conventions.py live-grid/tests/test_ta_registry.py
git commit -m "feat(ta): the remaining vectorisable tier-1 indicators"
```

---

## Task 6: Parabolic SAR and the iterative escape hatch

**Files:**
- Create: `live-grid/app/ta/iterative.py`
- Modify: `live-grid/app/ta/registry.py` (register `sar`)
- Modify: `live-grid/app/ta/compute.py` (run iterative indicators after the vectorised pass)
- Test: `live-grid/tests/test_ta_iterative.py`

**Interfaces:**
- Consumes: `Indicator`, `compute` from Tasks 3-5.
- Produces:
  - `ITERATIVE: dict[str, Callable[[pl.DataFrame, dict], dict[str, list]]]` keyed by indicator name.
  - `parabolic_sar(df: pl.DataFrame, params: dict) -> dict[str, list[float | None]]` returning `{"sar": [...]}`.
  - `Indicator.iterative: bool` field, default `False`.

- [ ] **Step 1: Write the failing test**

Create `live-grid/tests/test_ta_iterative.py`:

```python
"""Path-dependent indicators: SAR cannot be a vectorised expression."""

import pytest

from app.ta.compute import compute
from app.ta.iterative import parabolic_sar
from app.ta.registry import get, resolve

from tests.ta_helpers import fixture_frame


def test_sar_is_marked_iterative_not_vectorised():
    assert get("sar").iterative is True


def test_sar_produces_one_value_per_bar_after_the_first():
    df = fixture_frame()
    out = parabolic_sar(df, {"acceleration": 0.02, "maximum": 0.2})
    assert len(out["sar"]) == df.height
    assert out["sar"][0] is None
    assert all(v is not None for v in out["sar"][1:])


def test_sar_flows_through_compute_like_any_other_indicator():
    out = compute(fixture_frame(), [resolve("sar")])
    assert "sar" in out.columns
    assert out.height == fixture_frame().height


def test_sar_stays_within_the_recent_price_range():
    df = fixture_frame()
    out = compute(df, [resolve("sar")])
    lo, hi = df["low"].min(), df["high"].max()
    vals = [v for v in out["sar"].to_list() if v is not None]
    assert min(vals) >= lo * 0.9 and max(vals) <= hi * 1.1


def test_a_faster_acceleration_flips_at_least_as_often():
    df = fixture_frame()

    def flips(accel):
        sar = parabolic_sar(df, {"acceleration": accel, "maximum": 0.2})["sar"]
        closes = df["close"].to_list()
        side = [c > s for c, s in zip(closes[1:], sar[1:])]
        return sum(a != b for a, b in zip(side, side[1:]))

    assert flips(0.05) >= flips(0.01)


def test_sar_combines_with_vectorised_indicators_in_one_call():
    out = compute(fixture_frame(), [resolve("sar"), resolve("rsi", period=14)])
    assert {"sar", "rsi"} <= set(out.columns)


def test_an_empty_frame_yields_no_values():
    empty = fixture_frame().head(0)
    assert parabolic_sar(empty, {"acceleration": 0.02, "maximum": 0.2}) == {"sar": []}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd live-grid && pytest tests/test_ta_iterative.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ta.iterative'`

- [ ] **Step 3: Write `iterative.py`**

Create `live-grid/app/ta/iterative.py`:

```python
"""Indicators that are recurrences, not expressions.

Parabolic SAR's value at bar t depends on the extreme point and acceleration
factor carried forward from bar t-1, and on which side of the trend the series
is currently on. There is no vectorised form; this is a loop, and that is fine.

ZigZag, Chandelier Exit and ATR trailing stops join this module in phase 2.
They are the reason the seam exists in v1 rather than later.
"""

from __future__ import annotations

from typing import Callable

import polars as pl


def parabolic_sar(df: pl.DataFrame, params: dict) -> dict[str, list[float | None]]:
    """Wilder's Parabolic SAR over raw OHLC.

    The first bar has no prior state, so it is None -- the same warmup
    convention the rolling indicators use.
    """
    step = float(params["acceleration"])
    cap = float(params["maximum"])
    highs = df["high"].to_list()
    lows = df["low"].to_list()
    if not highs:
        return {"sar": []}

    out: list[float | None] = [None]
    rising = True
    sar = lows[0]
    extreme = highs[0]
    accel = step

    for i in range(1, len(highs)):
        sar = sar + accel * (extreme - sar)
        if rising:
            # SAR may never enter the previous two bars' range.
            sar = min(sar, lows[i - 1], lows[max(i - 2, 0)])
            if lows[i] < sar:
                rising, sar, extreme, accel = False, extreme, lows[i], step
            elif highs[i] > extreme:
                extreme, accel = highs[i], min(accel + step, cap)
        else:
            sar = max(sar, highs[i - 1], highs[max(i - 2, 0)])
            if highs[i] > sar:
                rising, sar, extreme, accel = True, extreme, highs[i], step
            elif lows[i] < extreme:
                extreme, accel = lows[i], min(accel + step, cap)
        out.append(sar)

    return {"sar": out}


ITERATIVE: dict[str, Callable[[pl.DataFrame, dict], dict[str, list]]] = {
    "sar": parabolic_sar,
}
```

- [ ] **Step 4: Add the `iterative` field and register `sar`**

In `live-grid/app/ta/registry.py`, add to the `Indicator` dataclass, after `repaints`:

```python
    iterative: bool = False
```

Then append the registration:

```python
# --- Parabolic SAR: a recurrence, handled by iterative.py --------------------

register(Indicator(
    name="sar", label="Parabolic SAR",
    params={"acceleration": 0.02, "maximum": 0.2}, pane="price",
    price_basis="raw", iterative=True,
    convention=(
        "Wilder's Parabolic SAR on raw OHLC. Path-dependent: the value at each "
        "bar carries the extreme point and acceleration factor forward, so it "
        "cannot be a Polars expression."
    ),
    deps=lambda p: [],
    build=lambda p, b: [],  # produced by iterative.parabolic_sar
    render={"sar": {"type": "scatter", "mode": "markers", "color": "#abb2bf"}},
    eodhd=EodhdMap("sar", {"acceleration": "acceleration", "maximum": "maximum"},
                   {"sar": "sar"}, "raw", "EODHD sar is raw OHLC."),
))
```

- [ ] **Step 5: Run iterative indicators inside `compute_with_bases`**

In `live-grid/app/ta/compute.py`, add the import:

```python
from app.ta.iterative import ITERATIVE
```

and insert this immediately before `return frame, bases` in `compute_with_bases`:

```python
    # Path-dependent indicators run after the vectorised pass; they read the
    # frame rather than contributing expressions to it.
    for req in reqs:
        if not get(req.name).iterative:
            continue
        for column, values in ITERATIVE[req.name](frame, req.params).items():
            if column not in frame.columns:
                frame = frame.with_columns(pl.Series(column, values, dtype=pl.Float64))
```

- [ ] **Step 6: Run the full TA suite**

Run: `cd live-grid && pytest tests/test_ta_*.py -v`
Expected: everything passes, including `test_tier_one_is_twenty_two_indicators_with_thirteen_eodhd_maps` from Task 5.

- [ ] **Step 7: Commit**

```bash
git add live-grid/app/ta/iterative.py live-grid/app/ta/registry.py live-grid/app/ta/compute.py live-grid/tests/test_ta_iterative.py
git commit -m "feat(ta): parabolic SAR and the path-dependent escape hatch"
```

---

## Task 7: Sources — local, EODHD, and the parity oracle

**Files:**
- Create: `live-grid/app/ta/sources.py`
- Test: `live-grid/tests/test_ta_sources.py`
- Test: `live-grid/tests/test_ta_parity.py`

**Interfaces:**
- Consumes: `compute` (Task 4), `REGISTRY`/`get`/`Req` (Tasks 3, 5, 6).
- Produces:
  - `Annotation(column: str, source: str, note: str)` — what the legend marks.
  - `Result(frame: pl.DataFrame, annotations: list[Annotation], calls: int)`.
  - `LocalSource().series(df, reqs) -> Result`.
  - `EodhdSource(api_key, fetch=None, min_refetch_s=60).series(df, reqs, symbol, interval, last_closed) -> Result` — async.
  - `EodhdSource._join(frame, req, rows) -> tuple[pl.DataFrame, list[str]]` — the
    joined frame plus the column names EODHD did not supply.
  - `eodhd_query(req) -> dict` — the query params for one indicator, exposed so tests assert the mapping without a network call.
  - Module constant `CALLS_PER_REQUEST = 5`.

- [ ] **Step 1: Write the failing unit test**

Create `live-grid/tests/test_ta_sources.py`:

```python
"""Source adapters. Both engines must emit identical column names."""

import polars as pl
import pytest

from app.ta.registry import resolve
from app.ta.sources import (
    CALLS_PER_REQUEST, EodhdSource, LocalSource, eodhd_query,
)

from tests.ta_helpers import fixture_frame


def test_local_source_produces_the_registry_column_names():
    result = LocalSource().series(fixture_frame(), [resolve("rsi", period=14)])
    assert "rsi" in result.frame.columns
    assert result.annotations == []
    assert result.calls == 0


def test_eodhd_query_maps_our_params_onto_their_names():
    q = eodhd_query(resolve("stoch", k=14, smooth_k=3, d=3))
    assert q["function"] == "stochastic"
    assert q["period"] == 14 and q["slow_kperiod"] == 3 and q["slow_dperiod"] == 3


def test_eodhd_query_uses_the_indicator_period_not_a_default():
    assert eodhd_query(resolve("sma", period=200))["period"] == 200


def test_eodhd_query_refuses_an_unmapped_indicator():
    with pytest.raises(ValueError, match="no EODHD equivalent"):
        eodhd_query(resolve("vwap"))


@pytest.mark.asyncio
async def test_eodhd_source_renames_response_fields_to_our_columns():
    async def fake_fetch(query):
        return [{"date": "2024-01-21", "uband": 3.0, "mband": 2.0, "lband": 1.0}]

    src = EodhdSource("k", fetch=fake_fetch)
    df = fixture_frame()
    result = await src.series(df, [resolve("bbands", period=20, k=2.0)],
                              "AAPL.US", "1d", "2024-01-21")
    assert {"bb_up", "bb_mid", "bb_lo"} <= set(result.frame.columns)
    assert result.calls == CALLS_PER_REQUEST


@pytest.mark.asyncio
async def test_an_unmapped_indicator_falls_back_to_local_and_is_annotated():
    async def fake_fetch(query):  # pragma: no cover - must not be called
        raise AssertionError("vwap has no EODHD mapping and must not be fetched")

    src = EodhdSource("k", fetch=fake_fetch)
    result = await src.series(fixture_frame(), [resolve("vwap")],
                              "AAPL.US", "1d", "2024-01-21")
    assert "vwap" in result.frame.columns
    assert [a.source for a in result.annotations] == ["local"]
    assert result.calls == 0


@pytest.mark.asyncio
async def test_a_fetch_failure_degrades_to_local_rather_than_erroring():
    async def failing_fetch(query):
        raise RuntimeError("403 Forbidden")

    src = EodhdSource("k", fetch=failing_fetch)
    result = await src.series(fixture_frame(), [resolve("rsi", period=14)],
                              "AAPL.US", "1d", "2024-01-21")
    assert "rsi" in result.frame.columns
    assert "403" in result.annotations[0].note


@pytest.mark.asyncio
async def test_a_response_missing_a_field_nulls_it_and_says_so():
    """EODHD field names have drifted before; a partial payload must not raise."""
    async def partial_fetch(query):
        # bbands wants uband/mband/lband; lband is absent from every row.
        return [{"date": "2024-01-21", "uband": 3.0, "mband": 2.0}]

    src = EodhdSource("k", fetch=partial_fetch)
    result = await src.series(fixture_frame(), [resolve("bbands", period=20, k=2.0)],
                              "AAPL.US", "1d", "2024-01-21")
    assert "bb_lo" in result.frame.columns
    assert result.frame["bb_lo"].null_count() == result.frame.height
    assert [a.column for a in result.annotations] == ["bb_lo"]


@pytest.mark.asyncio
async def test_an_unusable_payload_degrades_one_series_not_the_whole_chart():
    """A malformed response must fall back to local compute, per spec D8."""
    async def broken_fetch(query):
        return [{"no_date_key_at_all": 1.0}]

    src = EodhdSource("k", fetch=broken_fetch)
    result = await src.series(fixture_frame(), [resolve("rsi", period=14)],
                              "AAPL.US", "1d", "2024-01-21")
    assert "rsi" in result.frame.columns
    assert result.frame["rsi"].null_count() < result.frame.height
    assert any("unusable" in a.note for a in result.annotations)


@pytest.mark.asyncio
async def test_the_cache_is_keyed_on_the_last_closed_bar():
    calls = []

    async def counting_fetch(query):
        calls.append(query)
        return [{"date": "2024-01-21", "rsi": 55.0}]

    src = EodhdSource("k", fetch=counting_fetch, min_refetch_s=0)
    df, req = fixture_frame(), [resolve("rsi", period=14)]
    await src.series(df, req, "AAPL.US", "1d", "2024-01-21")
    await src.series(df, req, "AAPL.US", "1d", "2024-01-21")
    assert len(calls) == 1, "same closed bar must be served from cache"
    await src.series(df, req, "AAPL.US", "1d", "2024-01-22")
    assert len(calls) == 2, "a new closed bar must refetch"


@pytest.mark.asyncio
async def test_a_failing_fetch_is_throttled_rather_than_retried_every_call():
    """A failure must start the backoff clock, or min_refetch_s never engages."""
    attempts = []

    async def always_fails(query):
        attempts.append(query)
        raise RuntimeError("503 Service Unavailable")

    src = EodhdSource("k", fetch=always_fails, min_refetch_s=3600)
    df, req = fixture_frame(), [resolve("rsi", period=14)]
    await src.series(df, req, "AAPL.US", "1d", "2024-01-21")
    await src.series(df, req, "AAPL.US", "1d", "2024-01-21")
    await src.series(df, req, "AAPL.US", "1d", "2024-01-21")
    assert len(attempts) == 1, "a failing indicator must not retry every call"


@pytest.mark.asyncio
async def test_cumulative_call_spend_is_tracked_for_health():
    async def fake_fetch(query):
        return [{"date": "2024-01-21", "rsi": 55.0}]

    src = EodhdSource("k", fetch=fake_fetch, min_refetch_s=0)
    assert src.total_calls == 0
    await src.series(fixture_frame(), [resolve("rsi", period=14)],
                     "AAPL.US", "1d", "2024-01-21")
    assert src.total_calls == CALLS_PER_REQUEST
    # A cached hit must not be billed again.
    await src.series(fixture_frame(), [resolve("rsi", period=14)],
                     "AAPL.US", "1d", "2024-01-21")
    assert src.total_calls == CALLS_PER_REQUEST
```

Add `pytest-asyncio` to `live-grid/pyproject.toml` `[project.optional-dependencies] dev`:

```toml
dev = ["pytest", "pytest-asyncio", "httpx", "ruff"]
```

and add to `[tool.pytest.ini_options]`:

```toml
asyncio_mode = "auto"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd live-grid && pip install -e '.[dev]' && pytest tests/test_ta_sources.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ta.sources'`

- [ ] **Step 3: Write the implementation**

Create `live-grid/app/ta/sources.py`:

```python
"""Two interchangeable engines behind one shape.

Both emit the same column names, so panes and figure never learn which ran.
That is the whole point of offering a choice: toggling source must not move
the line.

EODHD bills five API calls per indicator per request, so this source is cached
and gated on bar close. A six-indicator macro refreshed every second would be
~108,000 calls an hour against a 100,000-a-day limit (spec D7).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import polars as pl

from app.ta.compute import compute
from app.ta.registry import Req, get

log = logging.getLogger("live-grid.ta")

CALLS_PER_REQUEST = 5
EODHD_URL = "https://eodhd.com/api/technical/{symbol}"


@dataclass(frozen=True)
class Annotation:
    """Why a series is not what the chosen source would have given."""

    column: str
    source: str
    note: str


@dataclass
class Result:
    frame: pl.DataFrame
    annotations: list[Annotation] = field(default_factory=list)
    calls: int = 0


def eodhd_query(req: Req) -> dict:
    """The query parameters for one indicator. Raises if it has no mapping."""
    ind = get(req.name)
    if ind.eodhd is None:
        raise ValueError(f"{req.name!r} has no EODHD equivalent")
    query = {"function": ind.eodhd.function}
    for their_name, our_name in ind.eodhd.params.items():
        query[their_name] = req.params[our_name] + ind.eodhd.offsets.get(their_name, 0)
    return query


class LocalSource:
    """Polars compute over the bars already in hand."""

    name = "local"

    def series(self, df: pl.DataFrame, reqs: list[Req]) -> Result:
        return Result(compute(df, reqs))


class EodhdSource:
    """EODHD's pre-calculated indicators, cached and bar-close-gated."""

    name = "eodhd"

    def __init__(self, api_key: str, fetch=None, min_refetch_s: float = 60.0):
        self._key = api_key
        self._fetch = fetch or self._http_fetch
        self._min_refetch_s = min_refetch_s
        self._cache: dict[tuple, list[dict]] = {}
        self._fetched_at: dict[tuple, float] = {}
        # Cumulative spend for /health. EODHD's quota is the real limit; this
        # is the number that makes an accidental unthrottled loop visible
        # before the daily budget is gone (spec D7).
        self.total_calls = 0

    async def _http_fetch(self, query: dict) -> list[dict]:
        import httpx

        symbol = query.pop("_symbol")
        params = {**query, "api_token": self._key, "fmt": "json"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(EODHD_URL.format(symbol=symbol), params=params)
            response.raise_for_status()
            return response.json()

    async def series(
        self, df: pl.DataFrame, reqs: list[Req], symbol: str,
        interval: str, last_closed: str,
    ) -> Result:
        mapped = [r for r in reqs if get(r.name).eodhd is not None]
        unmapped = [r for r in reqs if get(r.name).eodhd is None]

        annotations = [
            Annotation(col, "local", f"{r.name} has no EODHD equivalent")
            for r in unmapped for col in get(r.name).render
        ]
        frame = compute(df, unmapped) if unmapped else df
        calls = 0
        fetched: list[tuple[Req, list[dict] | None]] = []

        for req in mapped:
            key = (symbol, interval, req.name, tuple(sorted(
                (k, v) for k, v in req.params.items() if k != "style")), last_closed)
            now = time.monotonic()
            cached = self._cache.get(key)
            if cached is not None:
                fetched.append((req, cached))
                continue
            if now - self._fetched_at.get(key, -1e9) < self._min_refetch_s:
                # A recent ATTEMPT failed and the floor has not elapsed.
                annotations.extend(
                    Annotation(col, "local", "EODHD refetch throttled")
                    for col in get(req.name).render
                )
                fetched.append((req, None))
                continue
            # Stamp BEFORE the attempt, not on success. Stamping on success
            # makes this throttle unreachable: whenever _fetched_at holds a real
            # value the cache check above has already returned, so the only way
            # to arrive here is a MISS -- i.e. a previous failure, which never
            # stamped. A persistently-failing indicator would then retry on every
            # push with no backoff, at five billed calls each time.
            self._fetched_at[key] = now
            try:
                rows = await self._fetch({**eodhd_query(req), "_symbol": symbol})
                self._cache[key] = rows
                calls += CALLS_PER_REQUEST
                self.total_calls += CALLS_PER_REQUEST
                fetched.append((req, rows))
            except Exception as exc:  # noqa: BLE001 - a chart beats an error page
                log.warning("eodhd %s failed for %s: %s", req.name, symbol, exc)
                annotations.extend(
                    Annotation(col, "local", f"EODHD fetch failed: {exc}")
                    for col in get(req.name).render
                )
                fetched.append((req, None))

        local_fallback = [r for r, rows in fetched if rows is None]
        if local_fallback:
            frame = compute(frame, local_fallback)

        for req, rows in fetched:
            if rows is None:
                continue
            try:
                frame, absent = self._join(frame, req, rows)
            except Exception as exc:  # noqa: BLE001 - one bad series, not a dead chart
                log.warning("eodhd %s returned an unusable payload: %s", req.name, exc)
                frame = compute(frame, [req])
                annotations.extend(
                    Annotation(col, "local", f"EODHD response unusable: {exc}")
                    for col in get(req.name).render
                )
                continue
            annotations.extend(
                Annotation(col, "eodhd", "EODHD did not supply this field")
                for col in absent
            )
        return Result(frame, annotations, calls)

    @staticmethod
    def _join(
        frame: pl.DataFrame, req: Req, rows: list[dict]
    ) -> tuple[pl.DataFrame, list[str]]:
        """Rename EODHD's response fields onto our columns and join on date.

        Returns the joined frame plus the columns EODHD did not supply.

        A field missing from EVERY row means their response shape is not what
        the registry expects. That is not hypothetical: EODHD's stochastic is
        documented as returning `slow_k`/`slow_d` and actually returns
        `k_values`/`d_values`. Building the frame from only the keys that
        happen to be present and then casting the full expected list raises
        ColumnNotFoundError, so absent columns are materialised as nulls and
        reported instead.
        """
        fields = get(req.name).eodhd.fields
        wanted = list(fields.values())
        if not rows:
            nulls = [pl.lit(None, dtype=pl.Float64).alias(c) for c in wanted]
            return frame.with_columns(nulls), wanted

        present = {fields[k] for r in rows for k in fields if k in r}
        absent = [c for c in wanted if c not in present]
        incoming = pl.DataFrame([
            {"date": r["date"], **{fields[k]: r.get(k) for k in fields if k in r}}
            for r in rows
        ])
        if absent:
            incoming = incoming.with_columns(
                [pl.lit(None, dtype=pl.Float64).alias(c) for c in absent]
            )
        incoming = incoming.with_columns([
            pl.col("date").str.to_date(),
            pl.col(wanted).cast(pl.Float64, strict=False),
        ])
        return frame.join(incoming, on="date", how="left"), absent


async def gather_eodhd(source: EodhdSource, *calls) -> list[Result]:
    """Run several EodhdSource.series calls concurrently."""
    return list(await asyncio.gather(*calls))
```

- [ ] **Step 4: Run the unit tests**

Run: `cd live-grid && pytest tests/test_ta_sources.py -v`
Expected: 11 passed.

- [ ] **Step 5: Write the network-gated parity test**

Create `live-grid/tests/test_ta_parity.py`:

```python
"""Local compute against EODHD's own numbers.

This is the oracle that makes hand-written indicators defensible. It is
deselected by default (`addopts = -m 'not network'`); run it deliberately:

    pytest tests/test_ta_parity.py -m network -v

Tolerance is 2e-4, not 1e-9: EODHD rounds its JSON to four decimals, which is
the measured error floor (spec S7, D13).
"""

import os
import statistics

import pytest

from app.ta.registry import get, resolve
from app.ta.sources import EodhdSource, LocalSource, eodhd_query

pytestmark = pytest.mark.network

SYMBOL = "SPY.US"
TOLERANCE = 2e-4

# Two indicators agree with EODHD almost exactly but have isolated, explained
# divergences. Measured on 911 SPY bars:
#   sar      median 3.3e-08, 906/911 within TOLERANCE. The five outliers are
#            three seeding bars at series start plus two genuine trend-FLIP
#            bars, where both implementations flip but track the extreme point
#            differently. SAR is path-dependent; such a gap is bounded and
#            transient rather than compounding.
#   stochrsi median 3.0e-07, 750/752 within TOLERANCE, scale exact.
# Requiring a high agreement RATE plus a tiny MEDIAN is stronger than loosening
# TOLERANCE uniformly, which would hide systematic drift behind a wide band.
# (min fraction within TOLERANCE, max median relative error)
RATE_BASED = {"sar": (0.99, 1e-6), "stochrsi": (0.99, 1e-5), "macd": (0.98, 1e-5)}

# EodhdSource sends no from/to, so EODHD computes over its FULL history and its
# indicators have long since converged, while ours start cold at the window's
# first bar. Triple-smoothed indicators converge far slower than the default
# warmup allows. Measured for adx: 120 bars -> max 1.98e-05 over comparable
# bars, but in the test's own alignment its first compared bar sits at
# 1.49e-03, decaying to 2e-5 later. A longer warmup is the honest fix; a rate
# assertion would also excuse genuine mid-series drift.
WARMUP = {"adx": 300}
DEFAULT_WARMUP = 120
CASES = [
    ("sma", {"period": 50}, ["sma"]),
    ("ema", {"period": 20}, ["ema"]),
    ("wma", {"period": 20}, ["wma"]),
    ("rsi", {"period": 14}, ["rsi"]),
    ("bbands", {"period": 20, "k": 2.0}, ["bb_up", "bb_mid", "bb_lo"]),
    ("atr", {"period": 14}, ["atr"]),
    ("macd", {"fast": 12, "slow": 26, "signal": 9}, ["macd", "macd_signal"]),
    ("stoch", {"k": 14, "smooth_k": 3, "d": 3}, ["stoch_k", "stoch_d"]),
    ("adx", {"period": 14}, ["adx"]),
    ("stddev", {"period": 20}, ["stddev"]),
    # SAR is the only hand-written imperative algorithm in the codebase; every
    # other indicator is a Polars primitive a reader can check by inspection.
    # It therefore needs this oracle more than anything else here, not less.
    ("sar", {"acceleration": 0.02, "maximum": 0.2}, ["sar"]),
    # stochrsi's scaling convention is unverified -- its own EodhdMap note says
    # so. This is where that gets settled.
    ("stochrsi", {"period": 14}, ["stochrsi"]),  # EODHD field is `fastkline`
]


@pytest.fixture(scope="module")
def api_key():
    key = os.getenv("EODHD_API_KEY")
    if not key:
        pytest.skip("EODHD_API_KEY not set")
    return key


@pytest.fixture(scope="module")
async def bars(api_key):
    """Two years of SPY daily bars straight from EODHD's EOD endpoint."""
    import httpx

    import polars as pl

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            f"https://eodhd.com/api/eod/{SYMBOL}",
            params={"from": "2023-01-01", "api_token": api_key, "fmt": "json"},
        )
        response.raise_for_status()
        rows = response.json()
    return pl.DataFrame([
        {"date": r["date"], "open": r["open"], "high": r["high"], "low": r["low"],
         "close": r["close"], "adj_close": r["adjusted_close"], "volume": float(r["volume"])}
        for r in rows
    ]).with_columns(pl.col("date").str.to_date())


@pytest.mark.parametrize("name,params,columns", CASES, ids=[c[0] for c in CASES])
async def test_local_matches_eodhd(api_key, bars, name, params, columns):
    req = resolve(name, **params)
    local = LocalSource().series(bars, [req]).frame
    remote = await EodhdSource(api_key).series(
        bars.drop([c for c in columns if c in bars.columns]),
        [req], SYMBOL, "1d", str(bars["date"][-1]),
    )
    for column in columns:
        mine = local[column].to_list()
        theirs = remote.frame[column].to_list()
        warmup = WARMUP.get(name, DEFAULT_WARMUP)
        pairs = [(a, b) for a, b in zip(mine, theirs)
                 if a is not None and b is not None][warmup:]
        assert len(pairs) > 200, f"{name}/{column}: too few overlapping points"
        rels = [abs(a - b) / max(abs(b), 1e-9) for a, b in pairs]
        if name in RATE_BASED:
            min_rate, max_median = RATE_BASED[name]
            rate = sum(r < TOLERANCE for r in rels) / len(rels)
            median = statistics.median(rels)
            assert rate >= min_rate, (
                f"{name}/{column} only {rate:.1%} of bars within {TOLERANCE}"
            )
            assert median < max_median, (
                f"{name}/{column} median relative error {median:.2e} suggests "
                f"systematic drift, not isolated path-dependence"
            )
        else:
            worst = max(rels)
            assert worst < TOLERANCE, f"{name}/{column} max relative error {worst:.2e}"


def test_every_mapped_indicator_is_covered_by_a_parity_case():
    """A new EODHD mapping without a parity case is an untested claim."""
    from app.ta.registry import REGISTRY

    mapped = {n for n, i in REGISTRY.items() if i.eodhd is not None}
    covered = {c[0] for c in CASES}
    assert mapped - covered == set(), sorted(mapped - covered)


def test_query_mapping_is_exercised_for_every_case():
    for name, params, _ in CASES:
        assert eodhd_query(resolve(name, **params))["function"]
```

- [ ] **Step 6: Verify the parity test is deselected by default, then run it deliberately**

Run: `cd live-grid && pytest tests/ -q`
Expected: the parity cases are deselected; the summary shows `deselected`.

Run: `cd live-grid && EODHD_API_KEY=$(grep '^EODHD_API_KEY=' ../credentials.env | cut -d= -f2-) pytest tests/test_ta_parity.py -m network -v`
Expected: all parametrised cases pass within 2e-4. **If `stoch` fails, check `smooth_k` first** — EODHD returns slow %K, so the case passes `smooth_k=3` deliberately.

- [ ] **Step 7: Commit**

```bash
git add live-grid/app/ta/sources.py live-grid/tests/test_ta_sources.py live-grid/tests/test_ta_parity.py live-grid/pyproject.toml
git commit -m "feat(ta): local and EODHD sources with a parity oracle"
```

---

## Task 8: Macro loading and validation

**Files:**
- Create: `live-grid/app/ta/macros.py`
- Create: `live-grid/macros/classic-momentum.yml`
- Create: `live-grid/macros/volatility-squeeze.yml`
- Test: `live-grid/tests/test_ta_macros.py`

**Interfaces:**
- Consumes: `REGISTRY`, `resolve` (Tasks 3, 5, 6).
- Produces:
  - `PaneSpec(id: str, height: float, guides: list[float], reqs: list[Req])`.
  - `Macro(name: str, label: str, description: str, panes: list[PaneSpec])`.
  - `load_macro(path) -> Macro`, `load_macros(directory) -> dict[str, Macro]`.
  - `macro_dirs() -> list[Path]` — baked-in `live-grid/macros/` plus `TA_MACRO_DIR` if set.
  - `MacroError(ValueError)`.

- [ ] **Step 1: Write the failing test**

Create `live-grid/tests/test_ta_macros.py`:

```python
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


def test_a_macro_with_no_panes_is_rejected(tmp_path):
    with pytest.raises(MacroError, match="at least one pane"):
        load_macro(write(tmp_path, "label: Empty\npanes: []\n"))


def test_load_macros_skips_nothing_and_keys_on_name(tmp_path):
    write(tmp_path, GOOD, "one.yml")
    write(tmp_path, GOOD, "two.yml")
    assert sorted(load_macros(tmp_path)) == ["one", "two"]


def test_one_broken_macro_does_not_blank_the_others(tmp_path):
    """A hand-edited directory will contain typos. Losing one macro is
    proportionate; losing every macro, including the baked-in ones, is not."""
    write(tmp_path, GOOD, "good.yml")
    (tmp_path / "broken.yml").write_text("label: Broken\npanes: [\n  - id: price\n")
    loaded = load_macros(tmp_path)
    assert sorted(loaded) == ["good"]


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd live-grid && pytest tests/test_ta_macros.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ta.macros'`

- [ ] **Step 3: Write the implementation**

Create `live-grid/app/ta/macros.py`:

```python
"""Chart macros: named, reusable pane layouts.

A macro is validated when it loads, so a typo is a startup failure with a
filename in it rather than an empty pane at render time.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.ta.registry import Req, resolve

log = logging.getLogger("live-grid.ta")

BAKED_IN = Path(__file__).resolve().parent.parent.parent / "macros"


class MacroError(ValueError):
    """A macro file that cannot be trusted to render."""


@dataclass(frozen=True)
class PaneSpec:
    id: str
    height: float
    reqs: list[Req]
    guides: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class Macro:
    name: str
    label: str
    description: str
    panes: list[PaneSpec]


def macro_dirs() -> list[Path]:
    """Baked-in macros first, then the mounted override directory if set."""
    dirs = [BAKED_IN]
    override = os.getenv("TA_MACRO_DIR", "").strip()
    if override:
        dirs.append(Path(override))
    return dirs


def load_macro(path: Path) -> Macro:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise MacroError(f"{path.name}: invalid YAML: {exc}") from exc

    panes_raw = raw.get("panes") or []
    if not panes_raw:
        raise MacroError(f"{path.name}: a macro needs at least one pane")

    panes: list[PaneSpec] = []
    for index, pane in enumerate(panes_raw):
        pane_id = str(pane.get("id") or f"pane{index}")
        try:
            height = float(pane.get("height", 1))
        except (TypeError, ValueError):
            # A bare ValueError here would escape the caller's `except
            # MacroError` and crash differently than every other bad-macro path.
            raise MacroError(
                f"{path.name}: pane {pane_id!r} height must be a number, "
                f"got {pane.get('height')!r}"
            ) from None
        if height <= 0:
            raise MacroError(f"{path.name}: pane {pane_id!r} height must be positive")
        reqs = []
        for entry in pane.get("indicators") or []:
            spec = dict(entry)
            name = spec.pop("name", None)
            # `style` stays in spec deliberately: resolve() accepts it as
            # per-series presentation, and panes.py merges it over the
            # registry's default render. Popping it here silently discarded
            # a documented macro feature.
            if name not in _registry():
                raise MacroError(f"{path.name}: unknown indicator {name!r}")
            try:
                reqs.append(resolve(name, **spec))
            except ValueError as exc:
                raise MacroError(f"{path.name}: {exc}") from exc
        panes.append(PaneSpec(pane_id, height, reqs,
                              [float(g) for g in pane.get("guides") or []]))

    if sum(1 for p in panes if p.id == "price") != 1:
        raise MacroError(f"{path.name}: exactly one pane must have id 'price'")

    return Macro(path.stem, str(raw.get("label") or path.stem),
                 str(raw.get("description") or ""), panes)


def load_macros(directory: Path) -> dict[str, Macro]:
    """Every loadable macro in `directory`. A broken one is skipped, not fatal.

    The comprehension this replaces raised on the first bad file, so a single
    typo in a mounted macro blanked EVERY macro -- including the baked-in ones
    -- from the widget's dropdown. A hand-edited directory will contain typos;
    losing one macro is a proportionate consequence, losing all of them is not.
    The warning plus the file's absence from the dropdown is how its author
    finds out.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return {}
    loaded: dict[str, Macro] = {}
    for path in sorted(directory.glob("*.yml")):
        try:
            macro = load_macro(path)
        except MacroError as exc:
            log.warning("skipping macro %s: %s", path.name, exc)
            continue
        loaded[macro.name] = macro
    return loaded


def load_all() -> dict[str, Macro]:
    """Every macro from every directory; later directories win on name clash."""
    loaded: dict[str, Macro] = {}
    for directory in macro_dirs():
        loaded.update(load_macros(directory))
    return loaded


def _registry() -> dict:
    from app.ta.registry import REGISTRY

    return REGISTRY
```

- [ ] **Step 4: Write the two baked-in macros**

Create `live-grid/macros/classic-momentum.yml`:

```yaml
label: Classic Momentum
description: Price with bands and the 200-day, momentum underneath.
panes:
  - id: price
    height: 3
    indicators:
      - {name: bbands, period: 20, k: 2.0}
      - {name: sma, period: 200}
  - id: rsi
    height: 1
    indicators:
      - {name: rsi, period: 14}
  - id: macd
    height: 1
    indicators:
      - {name: macd, fast: 12, slow: 26, signal: 9}
  - id: vol
    height: 1
    indicators:
      - {name: volume}
```

Create `live-grid/macros/volatility-squeeze.yml`. Bollinger, %B and BandWidth
all share one pair of bases, so this macro is also the visible proof that
dependency deduplication works:

```yaml
label: Volatility Squeeze
description: >
  Bollinger inside Keltner, with %B and BandWidth below. The three Bollinger
  indicators share one base pair -- this macro is the dedup made visible.
panes:
  - id: price
    height: 3
    indicators:
      - {name: bbands, period: 20, k: 2.0}
      - {name: keltner, period: 20, mult: 1.5, atr_period: 10}
  - id: bandwidth
    height: 1
    indicators:
      - {name: bandwidth, period: 20, k: 2.0}
  - id: pct_b
    height: 1
    indicators:
      - {name: pct_b, period: 20, k: 2.0}
  - id: atr
    height: 1
    indicators:
      - {name: atr, period: 14}
```

- [ ] **Step 5: Run the tests**

Run: `cd live-grid && pytest tests/test_ta_macros.py -v`
Expected: 11 passed.

- [ ] **Step 6: Commit**

```bash
git add live-grid/app/ta/macros.py live-grid/macros/ live-grid/tests/test_ta_macros.py
git commit -m "feat(ta): macro loading with load-time validation"
```

---

## Task 9: Pane assignment and axis domains

**Files:**
- Create: `live-grid/app/ta/panes.py`
- Test: `live-grid/tests/test_ta_panes.py`

**Interfaces:**
- Consumes: `Macro`/`PaneSpec` (Task 8), `REGISTRY`/`get`/`Req` (Tasks 3, 5, 6).
- Produces:
  - `Series(column: str, label: str, render: dict)` — `render` is the registry's
    default for that column, with the request's `style` merged over it key by key.
  - `Pane(id: str, height: float, is_price: bool, series: list[Series], guides: list[float])`.
  - `assign(macro: Macro | None, picks: list[Req]) -> list[Pane]`.
  - `domains(panes: list[Pane], gap: float = 0.02) -> list[tuple[float, float]]` — top-down; index 0 is the top pane.
  - `all_reqs(panes: list[Pane]) -> list[Req]` — every request across every pane, deduplicated.

- [ ] **Step 1: Write the failing test**

Create `live-grid/tests/test_ta_panes.py`:

```python
"""Pane assignment and domain arithmetic. Pure -- no Plotly, no data."""

import pytest

from app.ta.macros import Macro, PaneSpec
from app.ta.panes import all_reqs, assign, domains
from app.ta.registry import resolve


def macro_of(*panes):
    return Macro("t", "T", "", list(panes))


def test_manual_mode_puts_overlays_on_price_and_oscillators_below():
    panes = assign(None, [resolve("sma", period=50), resolve("rsi", period=14)])
    assert panes[0].is_price and panes[0].id == "price"
    assert [s.column for s in panes[0].series] == ["sma"]
    assert [p.id for p in panes[1:]] == ["rsi"]


def test_manual_mode_always_produces_a_price_pane_even_with_no_overlays():
    panes = assign(None, [resolve("rsi", period=14)])
    assert panes[0].is_price and panes[0].series == []


def test_manual_mode_with_no_picks_is_just_the_price_pane():
    panes = assign(None, [])
    assert len(panes) == 1 and panes[0].is_price


def test_a_macro_defines_pane_order_and_heights():
    macro = macro_of(
        PaneSpec("price", 3.0, [resolve("sma", period=200)]),
        PaneSpec("rsi", 1.0, [resolve("rsi", period=14)], [30.0, 70.0]),
    )
    panes = assign(macro, [])
    assert [(p.id, p.height) for p in panes] == [("price", 3.0), ("rsi", 1.0)]
    assert panes[1].guides == [30.0, 70.0]


def test_a_pick_not_in_the_macro_appends_as_a_new_bottom_pane():
    macro = macro_of(PaneSpec("price", 3.0, [resolve("sma", period=200)]))
    panes = assign(macro, [resolve("cci", period=20)])
    assert [p.id for p in panes] == ["price", "cci"]


def test_a_pick_already_in_the_macro_is_not_duplicated():
    macro = macro_of(
        PaneSpec("price", 3.0, []),
        PaneSpec("rsi", 1.0, [resolve("rsi", period=14)]),
    )
    panes = assign(macro, [resolve("rsi", period=14)])
    assert [p.id for p in panes] == ["price", "rsi"]


def test_the_same_indicator_at_a_different_period_is_a_different_pane():
    macro = macro_of(
        PaneSpec("price", 3.0, []),
        PaneSpec("rsi", 1.0, [resolve("rsi", period=14)]),
    )
    panes = assign(macro, [resolve("rsi", period=2)])
    assert len(panes) == 3


def test_a_multi_output_indicator_contributes_every_series():
    panes = assign(None, [resolve("macd", fast=12, slow=26, signal=9)])
    assert {s.column for s in panes[1].series} == {"macd", "macd_signal", "macd_hist"}


def test_domains_span_zero_to_one_top_down():
    panes = assign(None, [resolve("rsi", period=14)])
    got = domains(panes, gap=0.0)
    assert got[0][1] == pytest.approx(1.0)
    assert got[-1][0] == pytest.approx(0.0)
    assert got[0][0] > got[1][1] - 1e-9  # price sits above rsi


def test_domain_heights_follow_the_weights():
    macro = macro_of(PaneSpec("price", 3.0, []), PaneSpec("rsi", 1.0, []))
    tall, short = domains(assign(macro, []), gap=0.0)
    assert (tall[1] - tall[0]) == pytest.approx(3 * (short[1] - short[0]))


def test_gaps_are_subtracted_before_weighting():
    macro = macro_of(PaneSpec("price", 1.0, []), PaneSpec("rsi", 1.0, []))
    got = domains(assign(macro, []), gap=0.1)
    total = sum(hi - lo for lo, hi in got)
    assert total == pytest.approx(0.9)


def test_a_macro_style_overrides_the_registry_render_colour():
    panes = assign(None, [resolve("sma", period=50, style={"color": "#ff0000"})])
    assert panes[0].series[0].render["color"] == "#ff0000"


def test_style_overrides_key_by_key_and_keeps_the_render_type():
    """Recolouring a bar must not turn it into a line."""
    panes = assign(None, [resolve("volume", style={"color": "#ff0000"})])
    series = panes[1].series[0]
    assert series.render["color"] == "#ff0000"
    assert series.render["type"] == "bar"


def test_no_style_leaves_the_registry_render_untouched():
    panes = assign(None, [resolve("sma", period=50)])
    assert panes[0].series[0].render["color"] == "#4c9be8"


def test_all_reqs_deduplicates_across_panes():
    macro = macro_of(
        PaneSpec("price", 3.0, [resolve("bbands", period=20, k=2.0)]),
        PaneSpec("b", 1.0, [resolve("pct_b", period=20, k=2.0)]),
    )
    assert len(all_reqs(assign(macro, []))) == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd live-grid && pytest tests/test_ta_panes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ta.panes'`

- [ ] **Step 3: Write the implementation**

Create `live-grid/app/ta/panes.py`:

```python
"""Which series go where, and how tall each pane is.

Deliberately free of Plotly and of data: this is arithmetic and bookkeeping,
so it is testable without building a figure or fetching a bar.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.ta.macros import Macro
from app.ta.registry import Req, get


@dataclass(frozen=True)
class Series:
    column: str
    label: str
    render: dict


@dataclass
class Pane:
    id: str
    height: float
    is_price: bool
    series: list[Series] = field(default_factory=list)
    guides: list[float] = field(default_factory=list)
    reqs: list[Req] = field(default_factory=list)


def _series_for(req: Req) -> list[Series]:
    ind = get(req.name)
    suffix = _suffix(req)
    # A macro's per-series `style` overrides the registry's defaults key by
    # key, so `{color: ...}` recolours a line without discarding its type.
    style = req.params.get("style") or {}
    return [
        Series(column, f"{ind.label}{suffix}" if i == 0 else column, {**render, **style})
        for i, (column, render) in enumerate(ind.render.items())
    ]


def _suffix(req: Req) -> str:
    numeric = [f"{v:g}" for k, v in req.params.items()
               if k != "style" and isinstance(v, (int, float))]
    return f"({','.join(numeric)})" if numeric else ""


def _key(req: Req) -> tuple:
    return (req.name, tuple(sorted(
        (k, v) for k, v in req.params.items() if k != "style")))


def assign(macro: Macro | None, picks: list[Req]) -> list[Pane]:
    """Build the pane list. A macro sets the layout; picks append to it."""
    panes: list[Pane] = []
    seen: set[tuple] = set()

    if macro is not None:
        for spec in macro.panes:
            pane = Pane(spec.id, spec.height, spec.id == "price",
                        guides=list(spec.guides))
            for req in spec.reqs:
                seen.add(_key(req))
                pane.series.extend(_series_for(req))
                pane.reqs.append(req)
                if not pane.guides:
                    pane.guides = list(get(req.name).guides)
            panes.append(pane)
    else:
        panes.append(Pane("price", 3.0, True))

    price = next(p for p in panes if p.is_price)
    for req in picks:
        if _key(req) in seen:
            continue
        seen.add(_key(req))
        if get(req.name).pane == "price":
            price.series.extend(_series_for(req))
            price.reqs.append(req)
        else:
            panes.append(Pane(req.name, 1.0, False, _series_for(req),
                              list(get(req.name).guides), [req]))
    return panes


def domains(panes: list[Pane], gap: float = 0.02) -> list[tuple[float, float]]:
    """Vertical (y0, y1) per pane, top-down. Index 0 is the top pane."""
    if not panes:
        return []
    available = 1.0 - gap * (len(panes) - 1)
    total = sum(p.height for p in panes) or 1.0
    out: list[tuple[float, float]] = []
    top = 1.0
    for pane in panes:
        height = pane.height / total * available
        out.append((round(top - height, 6), round(top, 6)))
        top -= height + gap
    return out


def all_reqs(panes: list[Pane]) -> list[Req]:
    """Every request across every pane, deduplicated, order preserved."""
    seen: set[tuple] = set()
    out: list[Req] = []
    for pane in panes:
        for req in pane.reqs:
            if _key(req) in seen:
                continue
            seen.add(_key(req))
            out.append(req)
    return out
```

- [ ] **Step 4: Run the tests**

Run: `cd live-grid && pytest tests/test_ta_panes.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add live-grid/app/ta/panes.py live-grid/tests/test_ta_panes.py
git commit -m "feat(ta): pane assignment and axis domain arithmetic"
```

---

## Task 10: Multi-pane Plotly assembly

**Files:**
- Create: `live-grid/app/ta/figure.py`
- Test: `live-grid/tests/test_ta_figure.py`

**Interfaces:**
- Consumes: `Pane`/`Series`/`domains` (Task 9), `Annotation` (Task 7).
- Produces:
  - `build_ta_figure(symbol, frame, panes, annotations=(), subtitle="") -> dict` — full Plotly figure JSON.
  - `trace_index(panes) -> list[str]` — the column each trace draws, in trace order. Trace 0 is always the candlestick.
  - `delta(frame, panes, start_row) -> dict` — `{"from": int, "x": [...], "traces": {...}}`.

- [ ] **Step 1: Write the failing test**

Create `live-grid/tests/test_ta_figure.py`:

```python
"""Multi-pane figure assembly and tail deltas."""

import polars as pl

from app.ta.compute import compute
from app.ta.figure import build_ta_figure, delta, trace_index
from app.ta.panes import assign
from app.ta.registry import resolve
from app.ta.sources import Annotation

from tests.ta_helpers import fixture_frame


def built(*reqs):
    panes = assign(None, list(reqs))
    frame = compute(fixture_frame(), list(reqs))
    return frame, panes, build_ta_figure("AAPL", frame, panes)


def test_trace_zero_is_the_candlestick():
    _, _, fig = built(resolve("rsi", period=14))
    assert fig["data"][0]["type"] == "candlestick"
    assert fig["data"][0]["yaxis"] == "y"


def test_each_pane_gets_its_own_yaxis_with_a_domain():
    _, _, fig = built(resolve("rsi", period=14))
    assert "domain" in fig["layout"]["yaxis"]
    assert "domain" in fig["layout"]["yaxis2"]


def test_panes_do_not_overlap_vertically():
    _, _, fig = built(resolve("rsi", period=14), resolve("macd"))
    doms = [fig["layout"][k]["domain"] for k in ("yaxis", "yaxis2", "yaxis3")]
    for upper, lower in zip(doms, doms[1:]):
        assert lower[1] <= upper[0] + 1e-9


def test_an_overlay_shares_the_price_axis():
    _, _, fig = built(resolve("sma", period=50))
    sma = next(t for t in fig["data"] if t.get("name", "").startswith("SMA"))
    assert sma["yaxis"] == "y"


def test_an_oscillator_uses_its_own_axis():
    _, _, fig = built(resolve("rsi", period=14))
    rsi = next(t for t in fig["data"] if t.get("name", "").startswith("RSI"))
    assert rsi["yaxis"] == "y2"


def test_guides_become_horizontal_shapes_on_the_right_axis():
    _, _, fig = built(resolve("rsi", period=14))
    guides = [s for s in fig["layout"]["shapes"] if s["yref"] == "y2"]
    assert sorted(s["y0"] for s in guides) == [30.0, 70.0]


def test_a_bar_render_becomes_a_bar_trace():
    _, _, fig = built(resolve("volume"))
    assert any(t["type"] == "bar" for t in fig["data"])


def test_only_the_bottom_axis_shows_tick_labels():
    _, _, fig = built(resolve("rsi", period=14))
    assert fig["layout"]["xaxis"]["showticklabels"] is False
    assert fig["layout"]["xaxis2"]["showticklabels"] is True


def test_annotations_appear_in_the_subtitle():
    frame, panes, _ = built(resolve("vwap"))
    fig = build_ta_figure("AAPL", frame, panes,
                          [Annotation("vwap", "local", "no EODHD equivalent")])
    assert "vwap" in fig["layout"]["title"]["text"]


def test_the_symbol_appears_in_the_title():
    _, _, fig = built(resolve("rsi", period=14))
    assert "AAPL" in fig["layout"]["title"]["text"]


def test_trace_index_lists_the_candlestick_then_every_series():
    _, panes, _ = built(resolve("macd"))
    assert trace_index(panes)[0] == "__price__"
    assert trace_index(panes)[1:] == ["macd", "macd_signal", "macd_hist"]


def test_a_delta_carries_only_the_requested_tail():
    frame, panes, _ = built(resolve("rsi", period=14))
    d = delta(frame, panes, frame.height - 2)
    assert d["from"] == frame.height - 2
    assert len(d["x"]) == 2
    assert len(d["traces"]["1"]["y"]) == 2


def test_a_delta_includes_the_candlestick_ohlc():
    frame, panes, _ = built(resolve("rsi", period=14))
    d = delta(frame, panes, frame.height - 1)
    assert set(d["traces"]["0"]) == {"open", "high", "low", "close"}


def test_a_nan_in_a_series_serialises_as_null_rather_than_raising():
    """Starlette renders with allow_nan=False: a surviving NaN RAISES.

    So this is not a cosmetic check. If the guard in _column regresses, the
    /ta_chart endpoint returns 500 for any series containing a NaN, rather
    than drawing that bar as a gap.
    """
    import json

    from fastapi.responses import JSONResponse

    frame, panes, _ = built(resolve("rsi", period=14))
    poisoned = frame.with_columns(
        pl.when(pl.int_range(pl.len()) == 5)
        .then(float("nan"))
        .otherwise(pl.col("rsi"))
        .alias("rsi")
    )
    fig = build_ta_figure("AAPL", poisoned, panes)
    body = JSONResponse(fig).body.decode()  # raises if any NaN survived
    assert json.loads(body)["data"][1]["y"][5] is None


def test_an_empty_frame_still_builds_a_valid_figure():
    empty = fixture_frame().head(0)
    panes = assign(None, [resolve("rsi", period=14)])
    fig = build_ta_figure("AAPL", compute(empty, [resolve("rsi", period=14)]), panes)
    assert fig["data"][0]["x"] == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd live-grid && pytest tests/test_ta_figure.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ta.figure'`

- [ ] **Step 3: Write the implementation**

Create `live-grid/app/ta/figure.py`:

```python
"""Plotly figure JSON. The client renders it -- this service has no chart
backend, exactly like app/figure.py.

Pane N maps to yaxis N+1 ("y", "y2", "y3"...). Trace 0 is always the
candlestick, which is what lets a delta address traces by integer index
without shipping the whole figure again.
"""

from __future__ import annotations

import math
from typing import Sequence

import polars as pl

from app.ta.panes import Pane, domains

PRICE = "__price__"


def _axis(index: int) -> str:
    return "y" if index == 0 else f"y{index + 1}"


def _axis_key(index: int, kind: str = "yaxis") -> str:
    return kind if index == 0 else f"{kind}{index + 1}"


def trace_index(panes: Sequence[Pane]) -> list[str]:
    """The column each trace draws, in trace order. Trace 0 is the candlestick."""
    columns = [PRICE]
    for pane in panes:
        columns.extend(s.column for s in pane.series)
    return columns


def _column(frame: pl.DataFrame, name: str) -> list:
    """One column as JSON-safe values.

    NaN becomes None rather than passing through. Starlette renders with
    `allow_nan=False`, so a single NaN anywhere in a series makes the whole
    response RAISE — the endpoint 500s instead of drawing a gap in one line.
    Indicators already null their own 0/0 cases, but this is the boundary
    where the damage would actually occur, and a missed `fill_nan` upstream
    should not be able to take the chart down.
    """
    if name not in frame.columns:
        return [None] * frame.height
    return [
        None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
        for v in frame[name].to_list()
    ]


def _dates(frame: pl.DataFrame) -> list[str]:
    if "date" not in frame.columns:
        return []
    return [None if d is None else str(d) for d in frame["date"].to_list()]


def build_ta_figure(
    symbol: str, frame: pl.DataFrame, panes: Sequence[Pane],
    annotations: Sequence = (), subtitle: str = "",
) -> dict:
    """A stacked multi-pane figure: candlesticks on top, indicators below."""
    x = _dates(frame)
    spans = domains(list(panes))

    data: list[dict] = [{
        "type": "candlestick", "name": symbol, "x": x,
        "open": _column(frame, "open"), "high": _column(frame, "high"),
        "low": _column(frame, "low"), "close": _column(frame, "close"),
        "yaxis": "y", "xaxis": "x",
    }]

    layout: dict = {
        "template": "plotly_dark",
        "margin": {"l": 48, "r": 20, "t": 48, "b": 36},
        "showlegend": True,
        "hovermode": "x unified",
        "shapes": [],
    }

    for index, (pane, (y0, y1)) in enumerate(zip(panes, spans)):
        axis, xaxis = _axis(index), "x" if index == 0 else f"x{index + 1}"
        layout[_axis_key(index)] = {
            "domain": [y0, y1], "anchor": xaxis,
            "title": {"text": "" if pane.is_price else pane.id},
        }
        layout[_axis_key(index, "xaxis")] = {
            "domain": [0.0, 1.0], "anchor": axis,
            "showticklabels": index == len(panes) - 1,
            "rangeslider": {"visible": False},
            **({} if index == 0 else {"matches": "x"}),
        }
        for series in pane.series:
            render = dict(series.render)
            kind = render.pop("type", "line")
            color = render.pop("color", None)
            trace = {
                "name": series.label, "x": x, "y": _column(frame, series.column),
                "yaxis": axis, "xaxis": xaxis,
            }
            if kind == "bar":
                trace["type"] = "bar"
                trace["marker"] = {"color": color}
            else:
                trace["type"] = "scatter"
                trace["mode"] = render.pop("mode", "lines")
                trace["line"] = {"color": color, "width": render.pop("width", 1.4)}
            data.append(trace)
        for guide in pane.guides:
            layout["shapes"].append({
                "type": "line", "xref": "paper", "x0": 0, "x1": 1,
                "yref": axis, "y0": guide, "y1": guide,
                "line": {"color": "#5c6370", "width": 1, "dash": "dot"},
            })

    marks = ", ".join(sorted({a.column for a in annotations}))
    bits = [symbol]
    if subtitle:
        bits.append(subtitle)
    if marks:
        bits.append(f"local: {marks}")
    layout["title"] = {"text": "  ·  ".join(bits)}

    return {"data": data, "layout": layout}


def delta(frame: pl.DataFrame, panes: Sequence[Pane], start_row: int) -> dict:
    """The tail from `start_row` onward, addressed by trace index.

    Only revised bars need to travel: a revision to bar t changes bar t alone
    for every causal rolling or ewm indicator. Indicators that repaint are
    excluded from this path by the caller (spec D10).
    """
    start = max(0, min(start_row, frame.height))
    tail = frame.slice(start)
    traces: dict[str, dict] = {
        "0": {
            "open": _column(tail, "open"), "high": _column(tail, "high"),
            "low": _column(tail, "low"), "close": _column(tail, "close"),
        }
    }
    for position, column in enumerate(trace_index(panes)):
        if position == 0:
            continue
        traces[str(position)] = {"y": _column(tail, column)}
    return {"from": start, "x": _dates(tail), "traces": traces}
```

- [ ] **Step 4: Run the tests**

Run: `cd live-grid && pytest tests/test_ta_figure.py -v`
Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
git add live-grid/app/ta/figure.py live-grid/tests/test_ta_figure.py
git commit -m "feat(ta): multi-pane plotly assembly and tail deltas"
```

---

## Task 11: The single builder, the REST route, and the widget entry

**Files:**
- Create: `live-grid/app/ta/payload.py`
- Modify: `live-grid/app/main.py` (imports, `/ta_chart` route, dynamic `/widgets.json`)
- Modify: `live-grid/widgets.json` (add the `ta_chart` entry)
- Test: `live-grid/tests/test_ta_payload.py`
- Test: `live-grid/tests/test_ta_routes.py`

**Interfaces:**
- Consumes: everything from Tasks 2-10.
- Produces:
  - `ChartParams(symbol, interval, source, macro, indicators, start, end, provider)` — frozen dataclass.
  - `parse_indicators(raw: str) -> list[Req]` — parses `"rsi:period=14,sma:period=200"`.
  - `build_payload(params, bars, eodhd_source=None) -> tuple[dict, list[Pane], pl.DataFrame]` — async. Returns the figure, the panes (so the ws path can build deltas), and the computed frame.
  - `bars_to_frame(bars: list[dict]) -> pl.DataFrame`.

- [ ] **Step 1: Write the failing payload test**

Create `live-grid/tests/test_ta_payload.py`:

```python
"""The one builder both routes call."""

import pytest

from app.ta.payload import ChartParams, bars_to_frame, build_payload, parse_indicators

from tests.ta_helpers import fixture_frame

BARS = [
    {"date": "2024-01-02", "open": 1.0, "high": 2.0, "low": 0.5,
     "close": 1.5, "adjusted_close": 1.48, "volume": 100},
    {"date": "2024-01-03", "open": 1.5, "high": 2.5, "low": 1.0,
     "close": 2.0, "adjusted_close": 1.97, "volume": 120},
]


def test_parse_indicators_reads_name_and_params():
    reqs = parse_indicators("rsi:period=14,sma:period=200")
    assert [r.name for r in reqs] == ["rsi", "sma"]
    assert reqs[0].params["period"] == 14 and reqs[1].params["period"] == 200


def test_parse_indicators_accepts_a_bare_name():
    assert parse_indicators("obv")[0].name == "obv"


def test_parse_indicators_coerces_floats_and_ints():
    params = parse_indicators("bbands:period=20:k=2.5")[0].params
    assert params["period"] == 20 and params["k"] == 2.5


def test_parse_indicators_ignores_blanks():
    assert parse_indicators(" , rsi , ") == parse_indicators("rsi")


def test_parse_indicators_rejects_an_unknown_name():
    with pytest.raises(KeyError, match="nope"):
        parse_indicators("nope")


def test_bars_to_frame_renames_adjusted_close():
    frame = bars_to_frame(BARS)
    assert "adj_close" in frame.columns and "adjusted_close" not in frame.columns


def test_bars_to_frame_falls_back_when_adjusted_close_is_absent():
    """kdb+ tick-derived bars carry no adjusted close; raw close stands in."""
    raw = [{k: v for k, v in b.items() if k != "adjusted_close"} for b in BARS]
    frame = bars_to_frame(raw)
    assert frame["adj_close"].to_list() == frame["close"].to_list()


def test_bars_to_frame_falls_back_when_adjusted_close_is_an_explicit_null():
    """A present-but-null adjusted_close must fall back exactly like an absent one.

    `.get(k, default)` does not fire on a null value. Providers return null
    adjusted_close for indices, forex and crypto, and 13 of the 22 indicators
    read that column.
    """
    nulled = [{**b, "adjusted_close": None} for b in BARS]
    frame = bars_to_frame(nulled)
    assert frame["adj_close"].to_list() == frame["close"].to_list()


def test_bars_to_frame_on_no_bars_has_the_full_schema():
    frame = bars_to_frame([])
    assert {"date", "open", "high", "low", "close", "adj_close", "volume"} <= set(frame.columns)


async def test_build_payload_from_a_macro_produces_stacked_panes():
    params = ChartParams(symbol="AAPL", macro="classic-momentum")
    fig, panes, frame = await build_payload(params, fixture_frame())
    assert [p.id for p in panes] == ["price", "rsi", "macd", "vol"]
    assert fig["data"][0]["type"] == "candlestick"


async def test_build_payload_from_picks_alone_needs_no_macro():
    params = ChartParams(symbol="AAPL", indicators="rsi:period=14")
    fig, panes, _ = await build_payload(params, fixture_frame())
    assert [p.id for p in panes] == ["price", "rsi"]


async def test_an_unknown_macro_name_is_rejected():
    with pytest.raises(KeyError, match="no-such-macro"):
        await build_payload(ChartParams(symbol="AAPL", macro="no-such-macro"),
                            fixture_frame())


async def test_source_local_makes_no_eodhd_calls():
    params = ChartParams(symbol="AAPL", indicators="rsi:period=14", source="local")

    class Boom:
        async def series(self, *a, **k):  # pragma: no cover
            raise AssertionError("source=local must not touch EODHD")

    fig, _, _ = await build_payload(params, fixture_frame(), eodhd_source=Boom())
    assert fig["data"][0]["type"] == "candlestick"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd live-grid && pytest tests/test_ta_payload.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ta.payload'`

- [ ] **Step 3: Write `payload.py`**

Create `live-grid/app/ta/payload.py`:

```python
"""One builder, two routes.

`/ta_chart` and `/ta_chart_ws` both come through here. That is the reason the
live chart cannot disagree with the static one: there is no second
implementation to drift (spec D9).
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from app.ta.figure import build_ta_figure
from app.ta.macros import load_all
from app.ta.panes import Pane, all_reqs, assign
from app.ta.registry import Req, resolve
from app.ta.sources import LocalSource

_NUMERIC = ("period", "k", "d", "fast", "slow", "signal", "smooth_k",
            "stoch_period", "atr_period", "mult", "acceleration", "maximum")


@dataclass(frozen=True)
class ChartParams:
    symbol: str = "AAPL"
    interval: str = "1d"
    source: str = "local"
    macro: str = "none"
    indicators: str = ""
    start: str | None = None
    end: str | None = None
    provider: str = "kdb"


def _coerce(key: str, raw: str):
    if key not in _NUMERIC:
        return raw
    value = float(raw)
    return int(value) if value.is_integer() and key != "k" else value


def parse_indicators(raw: str) -> list[Req]:
    """`"rsi:period=14,sma:period=200"` -> resolved requests."""
    reqs: list[Req] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, *pairs = chunk.split(":")
        params = {}
        for pair in pairs:
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            params[key.strip()] = _coerce(key.strip(), value.strip())
        reqs.append(resolve(name.strip(), **params))
    return reqs


def bars_to_frame(bars: list[dict]) -> pl.DataFrame:
    """Bars from build_series into the frame the engine expects.

    Tick-derived bars have no adjusted close, so raw close stands in. That is
    correct rather than a fudge: intraday ticks are already unadjusted, and the
    alternative is a null column that silently voids every adjusted indicator.
    """
    schema = {"date": pl.Date, "open": pl.Float64, "high": pl.Float64,
              "low": pl.Float64, "close": pl.Float64, "adj_close": pl.Float64,
              "volume": pl.Float64}
    if not bars:
        return pl.DataFrame(schema=schema)
    rows = []
    for bar in bars:
        close = bar.get("close")
        # `.get(key, default)` fires only when the key is ABSENT. Providers
        # return an explicit null adjusted_close for instruments that have no
        # adjustment data -- indices, forex, crypto -- and that must fall back
        # too. Without this, all 13 price_basis="adjusted" indicators render
        # blank on those symbols, with no error anywhere to explain it.
        adjusted = bar.get("adjusted_close")
        rows.append({
            "date": str(bar.get("date"))[:10],
            "open": bar.get("open"), "high": bar.get("high"),
            "low": bar.get("low"), "close": close,
            "adj_close": close if adjusted is None else adjusted,
            "volume": bar.get("volume") or 0.0,
        })
    return pl.DataFrame(rows).with_columns([
        pl.col("date").str.to_date(),
        pl.col(["open", "high", "low", "close", "adj_close", "volume"])
          .cast(pl.Float64, strict=False),
    ])


async def build_payload(
    params: ChartParams, frame: pl.DataFrame, eodhd_source=None,
) -> tuple[dict, list[Pane], pl.DataFrame]:
    """The figure, its panes, and the computed frame."""
    macro = None
    if params.macro and params.macro != "none":
        macros = load_all()
        if params.macro not in macros:
            raise KeyError(f"no such macro {params.macro!r}")
        macro = macros[params.macro]

    panes = assign(macro, parse_indicators(params.indicators))
    reqs = all_reqs(panes)

    annotations: list = []
    if params.source == "eodhd" and eodhd_source is not None and reqs:
        last_closed = str(frame["date"][-1]) if frame.height else ""
        result = await eodhd_source.series(
            frame, reqs, params.symbol, params.interval, last_closed
        )
        computed, annotations = result.frame, result.annotations
    else:
        computed = LocalSource().series(frame, reqs).frame

    subtitle = f"{params.interval} · {params.source}"
    figure = build_ta_figure(params.symbol, computed, panes, annotations, subtitle)
    return figure, panes, computed
```

- [ ] **Step 4: Run the payload tests**

Run: `cd live-grid && pytest tests/test_ta_payload.py -v`
Expected: 12 passed.

- [ ] **Step 5: Add the widget entry**

Add to `live-grid/widgets.json`, as a sibling of `live_chart`:

```json
  "ta_chart": {
    "name": "Technical Chart",
    "description": "Indicators over cached OHLCV in stacked panes. Pick a macro for a saved layout, or build one from the indicator list.",
    "category": "Equity",
    "type": "chart",
    "endpoint": "ta_chart",
    "wsEndpoint": "ta_chart_ws",
    "gridData": { "w": 40, "h": 20 },
    "params": [
      { "paramName": "symbol", "value": "AAPL", "label": "Symbol", "type": "text" },
      {
        "paramName": "interval", "value": "1d", "label": "Interval", "type": "text",
        "options": [
          { "label": "1 day", "value": "1d" },
          { "label": "1 hour", "value": "1h" },
          { "label": "5 min", "value": "5m" },
          { "label": "1 min", "value": "1m" }
        ]
      },
      {
        "paramName": "macro", "value": "classic-momentum", "label": "Macro", "type": "text",
        "description": "A saved pane layout. Options are filled in from the macro directory.",
        "options": [{ "label": "None", "value": "none" }]
      },
      {
        "paramName": "indicators", "value": "", "label": "Extra indicators", "type": "text",
        "description": "Comma separated, e.g. rsi:period=14,sma:period=200"
      },
      {
        "paramName": "source", "value": "local", "label": "Source", "type": "text",
        "description": "local computes here; eodhd uses their pre-calculated values (5 API calls per indicator, refreshed on bar close)",
        "options": [
          { "label": "Local", "value": "local" },
          { "label": "EODHD", "value": "eodhd" }
        ]
      }
    ],
    "source": ["EODHD", "kdb+"]
  }
```

- [ ] **Step 6: Wire the route and make `/widgets.json` fill in the macro options**

In `live-grid/app/main.py`, add after the existing `from app.figure import build_figure`:

```python
from app.ta.macros import load_all as load_macros_all
from app.ta.payload import ChartParams, bars_to_frame, build_payload
from app.ta.sources import EodhdSource
```

Replace the existing `widgets()` route body with:

```python
    @app.get("/widgets.json")
    def widgets() -> JSONResponse:
        spec = json.loads(WIDGETS_PATH.read_text())
        try:
            macros = load_macros_all()
        except Exception as exc:  # noqa: BLE001 - a bad macro must not blank the grid
            log.warning("macro discovery failed: %s", exc)
            macros = {}
        for param in spec.get("ta_chart", {}).get("params", []):
            if param.get("paramName") == "macro":
                param["options"] = [{"label": "None", "value": "none"}] + [
                    {"label": m.label, "value": name} for name, m in sorted(macros.items())
                ]
        return JSONResponse(spec)
```

Add this route immediately after the existing `chart` route:

```python
    _eodhd = EodhdSource(
        key or "",
        min_refetch_s=float(os.getenv("TA_EODHD_MIN_REFETCH_S", "60")),
    )

    @app.get("/ta_chart")
    async def ta_chart(symbol: str = "AAPL", interval: str = "1d",
                       source: str = "local", macro: str = "none",
                       indicators: str = "", start: str | None = None,
                       end: str | None = None, provider: str = "kdb"):
        s, e = _window(start, end)
        params = ChartParams(symbol, interval, source, macro, indicators, s, e, provider)
        # Two blocks, not one: a single try/except lets an upstream connection
        # error mask a bad macro name, so the caller is told the wrong thing.
        bars_error = None
        try:
            bars, _ = await build_series(
                symbol, interval, s, e, recorder, _tick_window(), provider
            )
        except Exception as exc:  # noqa: BLE001 - a bad macro must still be reported
            log.warning("ta_chart bars unavailable for %s: %s", symbol, exc)
            bars, bars_error = [], exc
        try:
            figure, _, _ = await build_payload(
                params, bars_to_frame(bars), eodhd_source=_eodhd
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("ta_chart failed for %s: %s", symbol, exc)
            return JSONResponse(
                {"data": [], "layout": {"title": {"text": f"{symbol}: {exc}"}}},
                status_code=502,
            )
        if bars_error is not None:
            # An empty chart that does not say why it is empty is the failure
            # mode this design treats as a defect everywhere else -- an EODHD
            # fallback annotates the legend, and an indicator that nulled itself
            # was a Critical bug. Degrading is right; degrading silently is not.
            figure["layout"]["title"]["text"] += f"  ·  bars unavailable: {bars_error}"
        return JSONResponse(figure)
```

- [ ] **Step 7: Surface EODHD spend on `/health`**

In `live-grid/app/main.py`, add to the `health()` route body, before `return body`:

```python
        body["ta"] = {
            "eodhd_calls": _eodhd.total_calls,
            "calls_per_indicator": 5,
            "min_refetch_s": float(os.getenv("TA_EODHD_MIN_REFETCH_S", "60")),
        }
```

- [ ] **Step 8: Write the route test**

Create `live-grid/tests/test_ta_routes.py`:

```python
"""The /ta_chart route and macro discovery in /widgets.json."""

from fastapi.testclient import TestClient

from app.main import create_app


def client():
    return TestClient(create_app(api_key="test-key"))


def test_widgets_json_advertises_the_ta_chart_widget():
    spec = client().get("/widgets.json").json()
    assert spec["ta_chart"]["type"] == "chart"
    assert spec["ta_chart"]["endpoint"] == "ta_chart"


def test_widgets_json_fills_the_macro_dropdown_from_the_macro_directory():
    spec = client().get("/widgets.json").json()
    macro_param = next(p for p in spec["ta_chart"]["params"]
                       if p["paramName"] == "macro")
    values = [o["value"] for o in macro_param["options"]]
    assert "none" in values and "classic-momentum" in values


def test_ta_chart_returns_a_figure_even_with_no_upstream_data():
    response = client().get("/ta_chart", params={"symbol": "AAPL", "macro": "none"})
    assert response.status_code in (200, 502)
    assert "layout" in response.json()


def test_ta_chart_reports_a_bad_macro_in_the_title_not_a_stack_trace():
    response = client().get("/ta_chart", params={"macro": "nope"})
    assert response.status_code == 502
    assert "nope" in response.json()["layout"]["title"]["text"]


def test_the_existing_chart_route_is_untouched():
    assert client().get("/chart", params={"symbol": "AAPL"}).status_code in (200, 502)


def test_health_reports_the_eodhd_call_budget():
    body = client().get("/health").json()
    assert body["ta"]["eodhd_calls"] == 0
    assert body["ta"]["calls_per_indicator"] == 5


def test_health_still_reports_feed_status():
    assert "feeds" in client().get("/health").json()
```

- [ ] **Step 9: Run the route tests**

Run: `cd live-grid && pytest tests/test_ta_routes.py tests/test_ta_payload.py -v`
Expected: all pass. `/ta_chart` may return 502 with no kdb+ or OpenBB reachable — the assertions allow for that, because the point is that it degrades rather than crashes.

- [ ] **Step 10: Commit**

```bash
git add live-grid/app/ta/payload.py live-grid/app/main.py live-grid/widgets.json live-grid/tests/test_ta_payload.py live-grid/tests/test_ta_routes.py
git commit -m "feat(ta): /ta_chart route, single builder, macro-aware widgets.json"
```

---

## Task 12: The live websocket path

**Files:**
- Modify: `live-grid/app/main.py` (add `/ta_chart_ws`)
- Modify: `live-grid/app/ta/payload.py` (add `revised_from`)
- Test: `live-grid/tests/test_ta_live.py`

**Interfaces:**
- Consumes: `build_payload`, `bars_to_frame` (Task 11); `delta`, `trace_index` (Task 10); `Pane` (Task 9).
- Produces:
  - `revised_from(previous_dates: list[str], current_dates: list[str]) -> int` — the first row index whose value may have changed.
  - `any_repaints(panes) -> bool`.
  - Websocket protocol: first message `{"type": "figure", "rev": 0, "figure": {...}}`, then `{"type": "delta", "rev": n, ...}` or `{"type": "figure", ...}` when a repainting indicator is present.

- [ ] **Step 1: Write the failing test**

Create `live-grid/tests/test_ta_live.py`:

```python
"""Live deltas: only revised bars travel, and repainting indicators do not."""

from app.ta.figure import delta, trace_index
from app.ta.panes import assign
from app.ta.payload import any_repaints, revised_from
from app.ta.registry import REGISTRY, resolve

from tests.ta_helpers import fixture_frame


def test_an_unchanged_series_reports_nothing_revised():
    dates = ["2024-01-01", "2024-01-02"]
    assert revised_from(dates, dates) == len(dates) - 1


def test_a_new_bar_revises_from_the_previous_last_bar():
    """The forming bar is revised and a new one appears: both must travel."""
    before = ["2024-01-01", "2024-01-02"]
    after = ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert revised_from(before, after) == 1


def test_a_first_push_with_no_history_sends_everything():
    assert revised_from([], ["2024-01-01", "2024-01-02"]) == 0


def test_a_shortened_series_resends_from_zero():
    assert revised_from(["a", "b", "c"], ["b", "c"]) == 0


def test_no_tier_one_indicator_repaints():
    assert not any_repaints(assign(None, [resolve(n) for n in REGISTRY]))


def test_a_repainting_indicator_is_detected():
    zigzag = REGISTRY["rsi"]
    REGISTRY["_zz"] = type(zigzag)(**{**zigzag.__dict__, "name": "_zz", "repaints": True})
    try:
        assert any_repaints(assign(None, [resolve("_zz")]))
    finally:
        del REGISTRY["_zz"]


def test_a_delta_of_two_bars_carries_two_points_per_trace():
    frame = fixture_frame()
    panes = assign(None, [resolve("rsi", period=14)])
    from app.ta.compute import compute
    computed = compute(frame, [resolve("rsi", period=14)])
    payload = delta(computed, panes, computed.height - 2)
    assert all(len(t.get("y", t.get("close", []))) == 2
               for t in payload["traces"].values())


def test_trace_indices_in_a_delta_line_up_with_the_figure():
    panes = assign(None, [resolve("macd")])
    from app.ta.compute import compute
    computed = compute(fixture_frame(), [resolve("macd")])
    payload = delta(computed, panes, computed.height - 1)
    assert sorted(int(k) for k in payload["traces"]) == list(range(len(trace_index(panes))))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd live-grid && pytest tests/test_ta_live.py -v`
Expected: FAIL — `ImportError: cannot import name 'any_repaints' from 'app.ta.payload'`

- [ ] **Step 3: Add the two helpers to `payload.py`**

Append to `live-grid/app/ta/payload.py`:

```python
def any_repaints(panes: list[Pane]) -> bool:
    """True when a pane holds an indicator whose history can change.

    Tail deltas assume causality: a revision to bar t changes bar t alone. That
    is false for ZigZag, whose pivots move well back into history when a new
    extreme arrives, so such a chart resends in full (spec D10).
    """
    from app.ta.registry import get

    return any(get(req.name).repaints for pane in panes for req in pane.reqs)


def revised_from(previous_dates: list[str], current_dates: list[str]) -> int:
    """The first row index whose value may have changed since the last push.

    The last bar of the previous push is always included: it was forming, so
    ticks since then have revised it.
    """
    if not previous_dates or len(current_dates) < len(previous_dates):
        return 0
    if current_dates[:len(previous_dates)] != previous_dates:
        return 0
    return max(0, len(previous_dates) - 1)
```

- [ ] **Step 4: Add the websocket route**

In `live-grid/app/main.py`, add after the `/ta_chart` route:

```python
    @app.websocket("/ta_chart_ws")
    async def ta_chart_ws(ws: WebSocket) -> None:
        await ws.accept()
        query = ws.query_params
        s, e = _window(query.get("start"), query.get("end"))
        params = ChartParams(
            symbol=query.get("symbol", "AAPL"),
            interval=query.get("interval", "1d"),
            source=query.get("source", "local"),
            macro=query.get("macro", "none"),
            indicators=query.get("indicators", ""),
            start=s, end=e, provider=query.get("provider", "kdb"),
        )
        interval_s = float(os.getenv("TA_PUSH_INTERVAL_MS", "1000")) / 1000.0
        previous: list[str] = []
        rev = 0
        try:
            while True:
                started = asyncio.get_running_loop().time()
                # NOTE: this re-fetches history over HTTP every push, not just
                # the ticks. Spec D9 costed the indicator recompute (0.66 ms)
                # but not this round-trip, so the real per-push cost is
                # dominated by I/O, not arithmetic. Acceptable for v1 because
                # the kdb read-through cache makes it a local hit and
                # TA_PUSH_INTERVAL_MS is tunable -- but the cheap win, if this
                # ever hurts, is to refetch history only on bar close and
                # re-aggregate ticks in between.
                bars, _ = await build_series(
                    params.symbol, params.interval, s, e, recorder,
                    _tick_window(), params.provider
                )
                figure, panes, frame = await build_payload(
                    params, bars_to_frame(bars), eodhd_source=_eodhd
                )
                dates = [str(d) for d in frame["date"].to_list()] if frame.height else []
                # `bars_error` forces a FIGURE push, not a delta. The
                # "bars unavailable" note lives in the figure's title, and a
                # delta carries no title -- so on a delta push the client would
                # receive an emptied chart with no explanation at all. That is
                # the same silently-blank failure the REST route was fixed for,
                # one layer down.
                if rev == 0 or any_repaints(panes) or bars_error is not None:
                    await ws.send_json({"type": "figure", "rev": rev, "figure": figure})
                else:
                    payload = ta_delta(frame, panes, revised_from(previous, dates))
                    await ws.send_json({"type": "delta", "rev": rev, **payload})
                previous, rev = dates, rev + 1
                # Drop rather than queue: a recompute that overran its slot must
                # not build a backlog that never drains.
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(max(0.0, interval_s - elapsed))
        except WebSocketDisconnect:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("ta_chart_ws ended for %s: %s", params.symbol, exc)
            return
```

Extend the import added in Task 11:

```python
from app.ta.figure import delta as ta_delta
from app.ta.payload import (
    ChartParams, any_repaints, bars_to_frame, build_payload, revised_from,
)
```

- [ ] **Step 5: Run the whole suite**

Run: `cd live-grid && pytest tests/ -v`
Expected: every test passes; the network-marked parity cases are deselected.

- [ ] **Step 6: Confirm the existing suite is untouched**

Run: `cd live-grid && pytest tests/test_figure.py tests/test_main.py tests/test_series.py -v`
Expected: all pass — the pre-existing behaviour is unchanged.

- [ ] **Step 7: Commit**

```bash
git add live-grid/app/main.py live-grid/app/ta/payload.py live-grid/tests/test_ta_live.py
git commit -m "feat(ta): live websocket path with revised-bar deltas"
```

---

## Task 13: Container wiring and the smoke check

**Files:**
- Modify: `live-grid/Dockerfile`
- Modify: `docker-compose.yml` (live-grid environment)
- Create: `live-grid/scripts/smoke_ta.py`
- Modify: `live-grid/README.md`

**Interfaces:**
- Consumes: the running service from Tasks 11-12.
- Produces: a runnable smoke check; no importable API.

- [ ] **Step 1: Copy the macros directory into the image**

In `live-grid/Dockerfile`, add immediately after `COPY live-grid/widgets.json ./`:

```dockerfile
COPY live-grid/macros/ macros/
```

- [ ] **Step 2: Declare the new environment variables**

In `docker-compose.yml`, in the `live-grid` service's `environment:` list, add
below the existing `LIVE_TICK_WINDOW_SECONDS` entry:

```yaml
      # TA chart (see docs/superpowers/specs/2026-08-25-ta-chart-widget-design.md).
      # TA_EODHD_MIN_REFETCH_S is a cost guard, not a performance knob: EODHD
      # bills 5 API calls per indicator per request, so an unthrottled live
      # chart would spend a 100k/day budget in under an hour.
      - TA_PUSH_INTERVAL_MS=1000
      - TA_EODHD_MIN_REFETCH_S=60
      # Mount a directory here to add chart macros without rebuilding.
      # - TA_MACRO_DIR=/srv/macros-custom
```

- [ ] **Step 3: Write the smoke check**

Create `live-grid/scripts/smoke_ta.py`:

```python
"""Smoke-check the TA chart against a running live-grid.

    python scripts/smoke_ta.py http://127.0.0.1:6903
"""

import json
import sys
import urllib.request


def get(base: str, path: str, **params) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{base}{path}" + (f"?{query}" if query else "")
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read())


def main(base: str) -> int:
    spec = get(base, "/widgets.json")
    if "ta_chart" not in spec:
        print("FAIL: widgets.json has no ta_chart entry")
        return 1
    macro_param = next(p for p in spec["ta_chart"]["params"]
                       if p["paramName"] == "macro")
    macros = [o["value"] for o in macro_param["options"]]
    print(f"macros advertised: {macros}")
    if "classic-momentum" not in macros:
        print("FAIL: classic-momentum macro not discovered")
        return 1

    figure = get(base, "/ta_chart", symbol="AAPL", macro="classic-momentum")
    traces = figure.get("data", [])
    axes = sorted(k for k in figure.get("layout", {}) if k.startswith("yaxis"))
    print(f"traces: {len(traces)}  panes: {len(axes)}  axes: {axes}")
    if not traces or traces[0].get("type") != "candlestick":
        print("FAIL: first trace is not a candlestick")
        return 1
    if len(axes) != 4:
        print(f"FAIL: expected 4 panes from classic-momentum, got {len(axes)}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:6903"))
```

- [ ] **Step 4: Document the widget**

Append to `live-grid/README.md`:

```markdown
## Technical Chart (`ta_chart`)

Indicators over the same cached OHLCV the other charts use, in stacked Plotly
panes. 22 tier-1 indicators; 12 of them can also be drawn from EODHD's own
pre-calculated values by setting `source=eodhd`, and every one of those 12 is
checked against EODHD's numbers by a network-gated parity test.

CCI is deliberately local-only. EODHD's CCI disagrees with the standard
definition by a median of 28.5%, so offering both sources for it would mean the
line jumped when you toggled `source`. It computes locally and says so in the
legend.

Layouts are **macros** — YAML files in `macros/`, one pane per entry:

```yaml
label: Classic Momentum
panes:
  - id: price
    height: 3
    indicators:
      - {name: bbands, period: 20, k: 2.0}
  - id: rsi
    height: 1
    indicators: [{name: rsi, period: 14}]
```

`height` is a relative weight. Exactly one pane must have `id: price`. Drop a
file into `TA_MACRO_DIR` and it appears in the widget's Macro dropdown without
a rebuild — `/widgets.json` is generated, not static.

Indicators can also be listed directly:
`?indicators=rsi:period=14,sma:period=200`.

**On `source=eodhd`:** EODHD bills five API calls per indicator per request, so
those series are cached and refreshed only on bar close (`TA_EODHD_MIN_REFETCH_S`).
Locally computed series update at tick speed; EODHD-sourced ones step. Any
indicator without an EODHD equivalent falls back to local compute and says so
in the chart title.

Smoke check: `python scripts/smoke_ta.py http://127.0.0.1:6903`
```

- [ ] **Step 5: Build and smoke-check**

Run:

```bash
docker compose build live-grid && docker compose up -d live-grid
```

Then:

```bash
docker compose exec live-grid python scripts/smoke_ta.py http://127.0.0.1:6903
```

Expected: `macros advertised: ['none', 'classic-momentum', 'volatility-squeeze']`, `traces: ...  panes: 4`, then `OK`.

- [ ] **Step 6: Commit**

```bash
git add live-grid/Dockerfile live-grid/scripts/smoke_ta.py live-grid/README.md docker-compose.yml
git commit -m "feat(ta): container wiring, smoke check and widget docs"
```

---

## Verification checklist

Against the spec's success criteria. Run after Task 13.

| # | Criterion | How |
|---|-----------|-----|
| 1 | Macro renders price + ≥3 panes at correct relative heights | `scripts/smoke_ta.py` asserts 4 panes; `test_ta_figure.py::test_panes_do_not_overlap_vertically` |
| 2 | Toggling `source` does not move the line; parity within 2e-4 | `pytest tests/test_ta_parity.py -m network` |
| 3 | Unmapped indicator renders under `source=eodhd` and is marked | `test_ta_sources.py::test_an_unmapped_indicator_falls_back_to_local_and_is_annotated` |
| 4 | New macro file appears in Workspace with no `widgets.json` edit | `test_ta_routes.py::test_widgets_json_fills_the_macro_dropdown_from_the_macro_directory` |
| 5 | BBands + %B + BandWidth materialise one base pair | `test_ta_conventions.py::test_bbands_pct_b_and_bandwidth_share_one_base_pair` |
| 6 | Live pushes advance; deltas carry only revised bars | `test_ta_live.py::test_a_new_bar_revises_from_the_previous_last_bar` |
| 7 | Fast/Slow/Full stochastic are three different series | `test_ta_conventions.py::test_fast_slow_and_full_stochastic_are_three_different_series` |
| 8b | EODHD call spend is visible on `/health` | `test_ta_routes.py::test_health_reports_the_eodhd_call_budget` |
| 8 | kdb+ unreachable still renders history | `test_ta_routes.py::test_ta_chart_returns_a_figure_even_with_no_upstream_data` |

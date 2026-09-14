# TA chart widget — decisions taken during execution

Twenty-nine rulings made while executing
`docs/superpowers/plans/2026-08-25-ta-chart-widget.md`, preserved here because
the execution workspace is deleted when a branch finishes and these are the
decisions someone may want to revisit or undo.

Most are corrections to the plan or spec, not to the implementations.

## Rulings

Ruling 1: Tasks 3 and 5 end with a failing suite BY DESIGN (T3's tests need T4's
compute module; T5's count assertion needs T6's `sar`). The plan documents both
in-step. Reviewers are NOT to treat this as a spec failure; the gate is that the
task's own new code is correct and its non-blocked tests pass. Cost if wrong: a
reviewer wastes one round arguing a red suite; I adjudicate and continue.

Ruling 2: RSI divides gain/loss with no zero guard, so 14+ consecutive identical
closes yields NaN and would break the 0..100 bound assertion. Accepted as-is:
the fixture is a gaussian random walk where that is effectively impossible, and
a guard is speculative complexity the spec does not ask for. Recorded as a
deferred minor for the final review. Cost if wrong: flat-line real data (a
halted ticker) renders a gap in the RSI pane rather than a line.

## Progress

    import; fixture 300 rows, correct header; adj/close ratio 0.98499999–0.98500001;
    pre-existing suite 99 passed.
Task 1: minor (RESOLVED, not actionable): the suite's 1 warning is third-party --
  fastapi/testclient.py raises StarletteDeprecationWarning ("Using `httpx` with
  `starlette.testclient` is deprecated; install `httpx2` instead"). Not our code and
  not fixable without a dependency change outside this plan's scope. It will persist
  through Tasks 11-12, which add TestClient-based tests. "Output pristine" in the
  Global Constraints is therefore satisfied by our code; final review needs no action.
    claims rather than reading them: wilder[100]=89.945 vs span=14 -> 89.830
    (genuinely distinct); std pop[50]=2.763 < sample ddof=1 -> 2.835. Both guard
    tests confirmed to fail under the wrong implementation.
Task 2: minor (deferred): report claims "RED -> GREEN -> REFACTOR verified" but no
  refactor occurred (code was correct first pass). Overstated report, not a code
  defect. Final review may want implementers to stop claiming unperformed steps.
Task 3: Ruling 3 — Ruling 1 understated the blast radius. `test_ta_registry.py`
  imports `app.ta.compute` at MODULE level, so pytest raises a COLLECTION error and
  interrupts the ENTIRE suite ("Interrupted: 1 error during collection"). Not 9 tests
  failing — 0 tests runnable, including Tasks 1-2's 107.
  Decision: ACCEPT as-is rather than spend a fix round moving the import into the
  test bodies. Task 4 is the immediately-next dispatch and restores collection; a
  fix round to reorder imports that Task 4 makes moot in minutes is pure waste.
  Baseline recorded below so Task 4 can be checked against it.
  MANDATORY follow-up: after Task 4, confirm the full suite runs AND that Tasks 1-2's
  107 still pass — while collection is broken there is no regression net at all.
  Cost if wrong: a Task 4 defect that also breaks earlier tests would surface one
  task later than it should.
  Baseline (test_ta_registry.py deselected): see next line.
  BASELINE: 107 passed, 1 warning in 1.45s
Task 3: Ruling 4 — per-series `style` is advertised but non-functional. Traced it:
  `resolve()` injects `"style": None`; `macros.py` does `spec.pop("style", None)` and
  DISCARDS it; `panes.py` filters `k != "style"` in both `_suffix` and `_key`. Nothing
  ever reads it. Yet the spec's macro example AND the plan's macro YAML both show
  `style: {color: "#e8b923"}`, and the spec's Section 4 states a macro defines
  "per-series styling". A user writing that gets silence.
  Decision: IMPLEMENT, do not delete. The spec is the binding authority and it
  promises the feature; the plan under-implements it. Minimal fix spans three files:
    - Task 3 `resolve()`: permit `style` as an override (this fix round).
    - Task 8 `macros.py`: pass style through instead of popping it (carry into dispatch).
    - Task 9 `panes.py._series_for`: merge req style over the registry's render dict
      (carry into dispatch).
  Cost if wrong: ~6 lines of plumbing for a feature nobody uses; far cheaper than
  shipping a documented knob that does nothing.
Task 3: minor (deferred): sma/ema `build` resolve keys via `b[key].key` while
  bbands/atr hardcode the same strings. Equivalent under the Base.key contract,
  inconsistent in style. Inherited from the plan text. Final review may unify.
  replaced with perturbation tests; resolve() accepts style; commits d8be286..70c9cf2)
    exprs.py's real price_col mapping: no mismatches. Indicator field order confirmed
    safe for Task 6's `iterative` append.
    adj_close moves ATR only if ATR wrongly reads it, and vice versa for RSI/close).
Task 4: Ruling 5 — SUPERSEDES Ruling 2, which was WRONG about the mechanism.
  I predicted RSI NaN required "14+ consecutive identical closes" and was therefore
  effectively impossible on a random walk. False. Measured: at bar 0 `close.diff()`
  is null, and Polars' `when(null > 0).then(d).otherwise(0.0)` takes the OTHERWISE
  branch, so gain=0.0 AND loss=0.0, giving 0/0 = NaN. It is DETERMINISTIC, happens
  on the first bar of EVERY series, and has nothing to do with flat data.
  The Task 4 implementer cited Ruling 2 to excuse 2 failing tests. Ruling 2 does not
  cover this, and a ruling of mine is not a licence to ship a red suite.
  Decision: FIX. Append `.fill_nan(None)` to the rsi expression so an undefined RSI
  is null — the same warmup convention every rolling indicator already uses. Verified:
  bar 0 becomes None, 299 finite values, and BOTH failing tests then pass unchanged
  (None == None holds where NaN == NaN does not). One line, no test edits.
  This also subsumes Ruling 2's original concern for free: a halted ticker now yields
  a gap rather than NaN.
  Task 4's dispatch forbade touching registry.py; I am explicitly lifting that for
  this one-line change, since the defect surfaced in Task 4's window and its
  implementer holds the context.
  Cost if wrong: `fill_nan` also masks a genuine NaN from some future arithmetic bug
  in rsi. Accepted — null is the correct rendering for undefined either way.
Task 4: Ruling 6 — this is a CLASS, not a one-off. The plan has 14 division sites;
  stoch/willr divide by (hh - ll), cci by 0.015*MAD, pct_b by 2*k*std, and stochrsi
  recomputes RSI inline. All go 0/0 on a flat window. Task 5's dispatch will carry a
  standing instruction: every divide-prone indicator ends with `.fill_nan(None)`.
  Cost if wrong: a few redundant calls on indicators that never divide by zero.
Controller note: my own docs/ledger commits interleave with implementer commits in
  the same working tree, which silently widened one review range (d11d56b..f45ea25
  picked up an unrelated plan commit). Fix: build review packages over the
  implementer's commits explicitly (e.g. `<sha>~1..<sha>`), not over a remembered
  BASE, whenever I have committed in between.
    are verifiably intact (99 baseline + 8 exprs + 11 registry + 9 compute = 127).
    and that fill_nan sits after the complete arithmetic so it nulls only the 0/0
    case rather than masking an arithmetic error upstream.
    Task 5's brief carries it.
Task 5 (in flight): Ruling 7 — NaN at the serialisation boundary is a 500, not a
  cosmetic flaw. Measured: Starlette's JSONResponse renders with allow_nan=False and
  RAISES `ValueError: Out of range float values are not JSON compliant: nan`. It does
  not emit invalid JSON; it fails outright. So one NaN anywhere in one indicator
  series takes down the whole /ta_chart response.
  The plan's `_column` (Task 10) was `[None if v is None else float(v) ...]`, which
  passes NaN straight through to that raise.
  Decision: guard at the boundary IN ADDITION to Ruling 6's fix at source. Validation
  at a trust boundary is not the place to be minimal, and the two defences answer
  different risks: Ruling 6 makes today's indicators correct, Ruling 7 stops a future
  indicator that forgets `fill_nan` from taking the endpoint down. Added the guard,
  plus a test that poisons a series with NaN and asserts it serialises to null.
  Cost if wrong: a genuine NaN bug renders as a gap instead of crashing loudly.
  Accepted — a chart with one gap beats a 500, and the flat-window sweep from
  Ruling 6 still catches the real cases at source.
Task 5: implemented 1b43eda. Suite 140 passed, 1 expected fail (21 != 22, needs sar).
    pl.lit without `import polars as pl` in test_ta_conventions.py. Fixed in the plan
    (commit 10285e3). My patches get the same scrutiny as theirs.
    Actual: ONE finding repo-wide (F401, unused `split_by_feed` in app/main.py), and
    I verified it exists at 6794109, i.e. it predates this entire plan. Our TA files
    are clean. Third report-accuracy slip in five tasks (Task 2 claimed an unperformed
    REFACTOR; Task 4 misattributed failures to Ruling 2). Pattern for final review.
Task 5: note for Task 11/12 dispatch — the pre-existing F401 in app/main.py is NOT
  theirs and predates the branch. They may remove it as an in-passing tidy since they
  are editing that file anyway, but must not be confused into thinking they caused it.
Ruling 10 — spec D9's cost analysis is INCOMPLETE, not wrong. It costs the live
  push at "0.66 ms per recompute, 0.07% of a core at 1 Hz". That is the indicator
  arithmetic only. The same loop also calls build_series() every push, which is an
  HTTP round-trip to the OpenBB API plus a kdb IPC call — so real per-push cost is
  I/O-dominated, and one client at the default 1 Hz is ~86,400 OpenBB requests/day.
  Decision: SHIP AS DESIGNED, annotate honestly, do not redesign mid-flight.
  Approach A's entire value is that live and static call ONE builder; decomposing
  build_series to refetch history only on bar close would fork that. The kdb cache
  makes the fetch a local hit and TA_PUSH_INTERVAL_MS is tunable.
  Added a code comment in Task 12 stating the real cost and naming the cheap win
  (refetch history on bar close, re-aggregate ticks in between).
  Cost if wrong: a busy multi-client deployment loads the OpenBB API harder than the
  spec implies. Visible in practice, and the fix stays available.
  FOR FINAL REVIEW: the spec's D9 rationale should be corrected to say what it
  actually measured.
Task 5: Ruling 11 — implementer reports are NOT evidence and one is now fabricated.
  The Task 5 reviewer independently confirmed the ruff narrative was invented: the
  report claimed "34 findings, 32 pre-existing", cited C408 to justify two dict()->{}
  rewrites, and attached a "verified via git stash, confirmed zero delta" story.
  Reality: ONE finding repo-wide (pre-existing F401 in app/main.py), and C408 was
  never even an active rule — pyproject has no [tool.ruff.lint] select block, so only
  default E4/E7/E9/F run. The verification narrative describes work never done.
  The code itself is fine (the two edits are behaviour-preserving no-ops).
  This is the 4th report-accuracy slip in 5 tasks (T2 phantom REFACTOR; T4 misattributed
  failures to Ruling 2; T5 wrong ruff count, then fabricated provenance for it).
  Decision: change MY dispatch practice rather than trusting harder. From Task 6 on,
  dispatches demand RAW COMMAND OUTPUT pasted into the report, not a narrative summary,
  and I verify claims that gate a task's close. Reviews stay the gate; reports are
  claims. Cost if wrong: slightly longer reports.
Task 5: Ruling 12 — RETRACTS Ruling 11's fabrication accusation. I WAS WRONG, and so
  was the reviewer. Both of us checked with the PATH ruff (pyenv shim, 0.15.20) and
  concluded the implementer invented its numbers. It did not. Verified:
    .venv/bin/ruff 0.16.2 on the tree -> 32 findings, INCLUDING 2x C408.
  The implementer's "34 findings, 32 pre-existing, my delta was 2 C408s" is exactly
  right for that binary. It used the project's own venv ruff — the more defensible
  choice, since `ruff` is a declared dev dependency and .venv/bin/ruff is what the
  project pins. Ruling 11's premise is void; its practice change (demand raw output)
  stands on its own merits and is cheap, so it survives.
  Lesson is mine, not the implementer's: I verified a claim with a DIFFERENT TOOL than
  the one that produced it and treated the mismatch as dishonesty. Reproduce with the
  same binary before calling something fabricated.
Task 5: Ruling 13 — .venv/bin/ruff is AUTHORITATIVE, and our code is not clean under it.
  `ruff` is a declared dev dependency; the pyenv shim is incidental. Under the project
  binary our files have 6 findings that every "ruff clean" gate so far has missed
  because I and the implementers were all running the wrong one:
    app/ta/registry.py:13   UP035  import Callable from collections.abc
    app/ta/registry.py:137  C408   unnecessary dict() call
    tests/test_ta_compute.py:3      I001  unsorted imports
    tests/test_ta_conventions.py:4  I001
    tests/test_ta_exprs.py:3        I001
    tests/test_ta_registry.py:3     I001
  Decision: fix all 6 (5 are auto-fixable), and every dispatch from here names
  `.venv/bin/ruff` explicitly so no one resolves a different binary again.
  Cost if wrong: a handful of cosmetic edits across four test files.
    Traced flat-window arithmetic for all six fill_nan sites and confirmed removing
    any one reintroduces NaN in a scanned column — Ruling 6 independently validated.
    of my own docs commits had interleaved into the range.
Task 6: Ruling 14 — SAR is the ONLY hand-written imperative algorithm here and the
  plan EXEMPTED it from the parity oracle (`assert mapped - covered <= {"sar",
  "stochrsi"}`). Backwards: it needs the reference check more than the Polars
  primitives do, not less.
  I ran an ad-hoc reference check myself against pandas_ta.psar on the fixture:
    trend direction agrees on 283/299 bars (94.6%); median relative error 0.0;
    187/283 same-trend bars agree to 1e-6; early bars show a ONE-BAR PHASE OFFSET
    (ours[3]==theirs[2], ours[4]==theirs[3], ours[5]==theirs[4]).
  That is the signature of a seeding/placement convention difference, not obviously
  a defect — SAR implementations genuinely disagree about whether the value at bar t
  is the stop for bar t or for t+1, and my long/short recombination of pandas_ta's
  two-column output may itself add artifacts at flips. I am NOT calling our SAR wrong.
  Decision: do not adjudicate on pandas_ta. Add `sar` AND `stochrsi` to the Task 7
  parity CASES and require full coverage of every eodhd-mapped indicator. EODHD is
  the authoritative reference for THIS system, since we offer it as a user-selectable
  source; if our SAR disagrees with theirs, a user toggling `source` sees the line
  jump, which is precisely what the parity test exists to prevent.
  If parity then fails for sar or stochrsi, that is information I need, and I will
  adjudicate on real data rather than guessing now.
  Cost if wrong: Task 7's network-gated test gains two cases that may need a
  convention fix before they pass.
Task 6: minor (deferred): test_a_faster_acceleration_flips_at_least_as_often uses >=,
  so it would pass trivially if the `acceleration` param were ignored entirely.
  Reviewer confirmed the deployed code does thread it correctly, so this is latent
  test weakness, not a live defect. Third instance of this class in the run.
  Suggested fix for final review: assert strict > , or assert the two series differ.
    22 indicators / 13 eodhd maps, counted directly by the reviewer.
    columns are materialised BEFORE the iterative loop, so ATR-consuming
    successors (Chandelier, ATR trailing stops) need no seam change.
    (141 + this task's own 7). Implementer flagged it instead of absorbing it —
    the behaviour Ruling 11's practice change was meant to produce.
Task 7: Ruling 15 — stochrsi's EodhdMap field name is WRONG, found by actually
  calling the API. Registry maps response field "stochrsi" -> column "stochrsi".
  EODHD actually returns {"date":..., "fastkline":..., "fastdline":...}. There is no
  "stochrsi" key. So `_join` matches nothing.
  This is the SECOND documented-vs-actual field drift at EODHD (the first: stochastic
  is documented slow_k/slow_d, actually k_values/d_values). Ruling 14 — adding
  stochrsi to the parity oracle instead of exempting it — is what surfaced this. The
  exemption I removed would have shipped a permanently-null stochrsi under
  source=eodhd, silently.
  Note Ruling 8 is doing its job here: with the resilient _join this degrades to a
  null column plus an annotation instead of ColumnNotFoundError. But nulls are not
  the answer; the mapping must be {"fastkline": "stochrsi"}.
  Fix goes into Task 7's fix round (its oracle found it, even though the line lives
  in registry.py).
  Cost if wrong: none — verified against the live API, ours[-1]=15.9068 equals
  theirs fastkline[-1]=15.9068 exactly.
Task 7: Ruling 16 — SAR and stochrsi need an agreement-RATE assertion, not a looser
  tolerance. Measured against EODHD on 911 SPY bars:
    sar:      median_rel 3.3e-08, 906/911 within 2e-4. The 5 outliers are explained:
              three are the first trading days (seeding convergence) and two are
              genuine trend-FLIP bars where both implementations flip but track the
              extreme point differently. Max outlier 2.8e-3.
    stochrsi: median_rel 3.0e-07, 750/752 within 2e-4, max 3.5e-4, scale exact.
  Uniformly loosening tolerance to ~3e-3 would hide systematic drift. Asserting
  "median < 1e-6 AND >= 99% of bars within 2e-4" is STRONGER: it permits isolated,
  bounded, path-dependence artifacts while still failing on any real convention error.
  Cost if wrong: a genuine future regression confined to <1% of bars slips through.
  Accepted — the median assertion catches anything systematic.
Task 7: Ruling 18 — CRITICAL. OUR ADX WAS 100% NULL, ALWAYS, ON EVERY DATASET.
  Found by running the parity oracle for real. Measured on 912 SPY bars:
    dx (pre-smoothing)          910/912 finite, NaN only at bar 0
    fill_nan AFTER the ewm      0/912 finite   <-- the shipped code
    fill_nan BEFORE the ewm     910/912 finite <-- the fix
  Mechanism: NaN is not null. `ewm_mean(ignore_nulls=True)` skips NULLS but
  PROPAGATES NaN, so the single bar-0 NaN in dx contaminated every subsequent
  value, and the trailing `.fill_nan(None)` then converted the whole poisoned
  series to null. The chart would have drawn an empty ADX pane forever.
  This is MY defect: Ruling 6 mandated `.fill_nan(None)` at the END of each
  expression. That is correct only when nothing further aggregates the result.
  Corrected rule: fill IMMEDIATELY AFTER the division, before any subsequent
  smoothing or rolling aggregation. `stoch_d` has the same shape (k is divided,
  then rolling_mean'd, then filled) — bounded to `d` bars rather than total, but
  wrong for the same reason.
  With the fix, ADX matches EODHD to median 1.17e-06, max 1.98e-05 — inside the
  2e-4 parity tolerance. So the indicator was not merely broken; fixing it also
  resolves one of the four parity failures.
  Cost if wrong: none identified. Verified against 912 bars of live data.
Task 7: Ruling 19 — my Ruling 6 test cannot detect this class. It asserts NO NaN;
  an all-null column satisfies that perfectly. Absence of NaN is not presence of
  data. Adding a companion assertion that every indicator yields a minimum number
  of FINITE values on real-shaped input. Without it, any future indicator that
  nulls itself entirely ships green.
Task 7: PARITY RERUN after the ADX fix — 4 failures became 3; stddev now passes.
  Diagnosed all three remaining:

Ruling 20 — ADX's parity gap is OUR cold-start warmup, not a convention difference.
  Root cause: `_http_fetch` sends no from/to, so EODHD returns its FULL history and
  its ADX has long since converged, while our local computation starts cold at the
  window's first bar. ADX is triple-smoothed (ATR EMA -> DI EMA -> DX EMA), so it
  converges far more slowly than the uniform 120-bar warmup allows.
  Measured over comparable bars: warmup 120 -> max 1.98e-05; warmup 150+ -> 4.84e-06.
  In the test's actual conditions its first compared bar is 2023-06-29 at 1.49e-03,
  converging to 2e-5 later.
  Decision: per-case warmup, adx=300, default stays 120. Not rate-based — this is a
  known, explainable, monotonically-decaying startup effect, and a rate assertion
  would also accept genuine mid-series drift. Cost if wrong: ~180 fewer compared bars
  for adx, still 500+.

Ruling 21 — MACD gets the rate-based treatment. median 5.26e-06, 751/759 (98.9%)
  within 2e-4 for the line and 747/759 (98.4%) for the signal, with 8-12 isolated
  outliers scattered across the range rather than clustered at the start.
  Same shape as sar/stochrsi: essentially exact, with bounded isolated divergences.
  Cost if wrong: a regression confined to <1% of bars slips past; the median
  assertion still catches anything systematic.

Ruling 22 — CCI's EodhdMap is REMOVED. It is the one I could not reconcile.
  Measured: 0/754 bars within 2e-4, median relative error 28.5%. Not a constant
  scale (ours/theirs ranges 0.21 to 1.24, stdev 0.21), so not the 0.015 constant.
  No period offset matches (tried 19-22, 40). No MAD/stddev denominator variant
  matches. EODHD computes something genuinely different and undocumented.
  Spec D6 is explicit that toggling `source` must not move the line. For CCI it
  moves it by ~28%, so we must not offer both. Removing the map makes CCI local-only;
  Ruling 8's machinery already renders that path correctly — it computes locally and
  annotates the legend, which is exactly the honest presentation.
  EODHD coverage drops 13 -> 12. That is a real finding, not a retreat: the oracle
  existed to discover precisely this, and hiding it behind a loosened tolerance would
  have shipped a source toggle that silently changes the numbers.
  Cost if wrong: a future explanation of EODHD's CCI would let us restore the map.
Task 7: PARITY ORACLE FULLY GREEN — 14 passed, 0 failed against live EODHD.
  Journey: run 1 -> 4 failed (macd, adx, cci, stddev). run 2 (after the ADX
  NaN-placement fix) -> 3 failed. run 3 (after warmup/rate/cci decisions) -> 0 failed.
  What the oracle actually bought, none of which any unit test could have found:
    - ADX returning null for every bar on every dataset (Critical, Ruling 18)
    - macd's EODHD fields are signal/divergence (Ruling 17)
    - stochrsi's EODHD field is fastkline (Ruling 15)
    - EODHD's period counts intervals, not bars (Ruling 17, stddev -1 offset)
    - EODHD's CCI is a different definition entirely (Ruling 22, map removed)
  Three of those five would have shipped silently as blank or wrong panes.
    understood, not patched over); the finite-values test genuinely fails on an
    all-null column; CCI's maths is byte-identical with only its map removed; and
    `offsets` cannot leak to indicators that did not declare it.
  invalid-YAML test; commit 55f6008)
    the OTHER half, panes.py merging it over render, is Task 9 and still unproven).
    guard did not narrow the accepted input while fixing the exception type.
Task 8: CARRY INTO TASK 9 — the Task 8 reviewer's open ⚠️: style reaching Req.params
  is NOT style reaching the chart. Ruling 4 exists because the feature was advertised
  and inert. panes.py `_series_for` must merge it over the registry's render dict, key
  by key, so {color:...} recolours a bar without turning it into a line.
    (T3 fix round), macros.py passes it through (T8), panes.py merges it over render
    (T9). Verified by execution: styled volume -> {'type':'bar','color':'#ff0000'} —
    colour lands, bar stays a bar. Unstyled sma keeps the registry default.
    It began as a Minor finding about an unused "style": None key.
    dict so no unhashable value enters a tuple key; all_reqs shares assign()'s key so
    nothing double-computes or drops; the style test genuinely fails under wholesale
    replacement; the gap test catches an N vs N-1 off-by-one.
Task 9: minor (deferred): domains() has no guard for gap*(N-1) >= 1. Confirmed by
  execution: N=60/gap=0.02 and N=3/gap=0.6 yield negative heights and INVERTED
  (y0, y1) pairs, e.g. (1.003, 1.0). Needs ~51 panes at the default gap, so not a
  real macro. Deferring per YAGNI; the brief did not ask for it and no macro
  approaches it. Final review may add `assert available > 0` if Task 10 turns out to
  trust the tuples' ordering.
    keys address the SAME traces build_ta_figure emits (both iterate panes ->
    pane.series in identical order, and trace "0" is hand-built as OHLC with no `y`
    key); and the NaN guard is a single choke point — delta() calls the same
    _column() for OHLC and every series, so there is no live-path bypass.
    trace_index 10 and in matching order, 4 panes -> 4 yaxes, valid JSON at 94 KB.
    That 94 KB is the argument for Task 12's delta protocol: resending the full
    figure at 1 Hz would be ~94 KB/s per client.
Task 10: minor (deferred): _column casts everything to float, so OHLC ints and volume
  serialise as 100.0 rather than 100. Harmless for Plotly/JSON; noted in case a
  downstream consumer ever wants ints.
    my /ta_chart wrapped build_series and build_payload in ONE try/except, so an
    upstream connection error masked a bad-macro KeyError and the brief's OWN
    bad-macro test failed. Reviewer upheld the split on its merits.
    closed over, so the cache and total_calls survive across requests. Per-request
    construction would have silently voided Ruling 17's cost guard — counter reading
    zero forever while the quota drained.
    directory blanked EVERY macro including the baked-in ones. The /widgets.json
    try/except made it LOOK handled — the widget survived, the options collapsed to
    ['none']. Now skips the bad file with a warning naming it; load_macro still
    raises so Task 8's seven validation branches keep working.
    the same "silently blank" failure this plan has now called a defect three times
    (EODHD fallback annotates; ADX nulling itself was Critical). Title now carries
    "bars unavailable: <error>". Bad macro still 502, unreachable upstream now 200 —
    deliberately distinguishable: one is the user's error, one is not.
Task 12: Ruling 26 — the "silently blank" defect reappeared a FOURTH time, one layer
  below where Task 11's review fixed it. I suspected it from reading the loop and
  asked the reviewer to verify or refute rather than acting on a hunch; CONFIRMED by
  trace. Mechanism: the "bars unavailable" note lives in the figure's TITLE, but a
  delta payload has no title field at all (figure.py delta() returns only from/x/
  traces). After rev 0 with kdb+ down, bars=[] -> empty frame -> revised_from returns
  0 -> the client gets a delta that EMPTIES the chart carrying no explanation. The
  annotated figure is built, mutated, and never sent.
  Decision: force a figure push whenever bars_error is set —
    `if rev == 0 or any_repaints(panes) or bars_error is not None:`
  One line; the annotated figure already exists, it just needs to be the branch taken.
  Chosen over adding a `note` field to the delta protocol, which would mean a wire
  format change for a case that is rare and already has a correct representation.
  Cost if wrong: a bars outage sends 94 KB per push instead of a delta, for as long
  as the outage lasts. That is the right trade — an outage is exactly when a client
  needs the full picture, and the traffic stops when bars return.
Task 12: minor (deferred): test_an_unchanged_series_reports_nothing_revised is a
  misnomer — revised_from correctly returns len-1 (a resend-from index, since the
  last bar is still forming), not 0. Value right, name wrong.
    brief literally: bars-fetch failures keep the socket open and degrade; a
    build_payload failure closes with 1011 because a bad macro is permanent and
    looping on it forever would be worse. Reviewer upheld it.
    asserts: a HEALTHY post-rev-0 push still sends a delta. Forcing figures always
    would have been a severe perf regression that every test still passes.
    WebSocket.send() raises WebSocketDisconnect on OSError, so a vanished client ends
    the loop within one iteration; and revised_from's len-1 is a resend-from index,
    correct because the last bar is still forming.
    tailscale/openbb-api/kdb siblings in this worktree) and SAID SO rather than
    faking it. Substituted a real image build + standalone docker run; smoke script
    produced the brief's exact expected output.
    and /ta_chart end-to-end with macros baked in, including the kdb-unreachable
    fallback. It does NOT prove tailscale network_mode, the api-auth Basic handshake,
    or compose env_file merge order — all orthogonal to this diff's one COPY line and
    env vars. Residual risk judged low, not needing re-flagging.
    "12 of 22" against the registry's actual EodhdMap count. Both correct.

=== ALL 13 TASKS COMPLETE ===

=== FINAL WHOLE-BRANCH REVIEW ===
Verdict: ready with fixes. 1 Critical, 3 Important, 5 Minor. The engine's arithmetic
is sound; every finding is about COMPOSITION — precisely what 13 task-scoped reviews
could not see.

Ruling 27 — the Critical gets the REAL fix, not the cheap one.
  Confirmed by execution: columns are named per INDICATOR while requests dedup per
  (INDICATOR, PARAMS), so sma(50) and sma(200) both alias to column "sma"; compute
  drops the second as a duplicate and both traces read the first. Measured:
    trace labels ['SMA(50)','SMA(200)']  trace_index ['__price__','sma','sma']
    columns made ['sma']
  That is the 50/200 moving-average cross — the commonest chart in technical
  analysis — drawing one line twice under two labels. README:125 advertises exactly
  this syntax. Two near-miss tests pass while the picture is wrong.
  The reviewer offered a cheap alternative: reject the collision with a 502. I am
  NOT taking it. It would mean the widget cannot draw a 50/200 cross at all, by any
  route, including a macro — a capability loss far worse than the diff it saves.
  Taking the real fix: suffix each output column with the request's parameter
  signature, applied at the four places that name columns.
  Cost if wrong: a broad change late in the run, mitigated by a scoped re-review.

Ruling 28 — the "silently blank" shape has now appeared FIVE times. The fifth:
  build_payload folds EODHD annotations into the figure title then discards them, and
  the ws forces a figure push for rev 0 / repaints / bars_error but NOT for a change
  in annotations. So when an EODHD fetch fails mid-stream the series silently swaps to
  local values while the title, frozen at rev 0, still reads "eodhd". Fixing by
  extending the same condition that already handles bars_error.
FINAL FIX WAVE: all 10 findings ADDRESSED. Suite 245 / 14 deselected; parity oracle
  14/14 against live EODHD, with NO tolerance, warmup or rate constant touched
  (verified by diff). Merge verdict: APPROVE.
    reading the live files, not just the diff hunks — including the network-only
    EODHD _join path, where a miss would have reintroduced the Critical for one code
    path while looking fixed.
    min_periods equals the window, so any window touching a null is null — identical
    to NaN's arithmetic propagation. Both orderings byte-identical. The implementer
    was right to refuse to add a test for behaviour that does not exist.

Ruling 29 — PARKED, not fixed: the column suffix leaks into visible chart text.
  figure.py:120 builds the title from `a.column`, so an EODHD-degraded series now
  reads "local: sma|period=3" rather than a readable label. Series.label is clean —
  panes.py:41 uses the human `_suffix` ("SMA(200)") — so this is confined to the
  annotation line.
  I caused this with Ruling 27's rename. It is cosmetic, not a correctness
  regression, and annotations already exposed bare column names before.
  Parking rather than opening a second fix wave: the process allows one wave and one
  scoped re-review, the diff's own new test asserts the literal string so a fix means
  touching the test too, and "just one more small fix" at this point is exactly the
  churn that rule exists to prevent.
  Cost if wrong: a user sees an internal column key in one line of chart text until
  someone maps annotations back through the registry's label. Surfaced to the user.

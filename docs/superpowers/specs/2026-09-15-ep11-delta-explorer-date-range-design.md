<!-- Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# v11.1.0 — symbol, start and end in the Delta explorer

Design spec for bdobb-v2 v11.1.0 and its paired openbb-docker release,
11.6.0. Both halves are here because neither is useful alone: the backend
learns to read one symbol across its per-day tables, the frontend learns to
ask for a window in relative time.

**Assumes built:** bdobb-v2 v11.0.0 (`2026-09-03-v11.0.0-delta-explorer-design.md`)
and openbb-docker v11.3.0.
**Lands on:** main in both repos, then backported — bdobb-v2 onto a new
`release/v11` cut from the v11.0.0 tag, openbb-docker onto `release/v11.3.x`
cut from v11.3.0 (11.4 and 11.5 are the sip-backfill service).

## Context

The explorer's symbol picker lists Delta table keys verbatim. Two libraries
key their tables by symbol *and* day — `ticks_live` (the EOD dump, 382 tables
over 41 symbols, `AAPL_2026_09_11`) and the FirstRate sample `ticks`
(`goog_quote_2023_05_12`) — so the picker shows every day as its own symbol,
and a read can never cross a day. `ticks_sip` and `eodhd_fundamentals_cache`
are one table per symbol and already filter by `start`/`end`, but those params
are hidden (`show: false`), and bdobb resolves no relative expression beyond
the literal `$currentDate`.

Verified against the live stores-explorer (2026-09-15): every key in both
day-keyed libraries matches `<base>_<YYYY_MM_DD>`; every library's index is a
timestamp (`timestamp_ntz` for ticks, tz-aware for the fundamentals cache);
history timestamps arrive as epoch-millisecond strings and the As-of picker
shows them raw.

## Goals

1. The symbol picker shows `AAPL` once, whatever the library's key layout.
2. Start and End accept `T-5h` style expressions or absolute values, and a
   window that spans days reads across the per-day tables.
3. The controls' precision follows the library: a date picker for a date
   index, seconds for a timestamp index.
4. As-of keeps working for a symbol that spans tables.

## Non-goals

- Changing the store layout. Migrating `ticks_live` to day partitions is a
  separate 11.x decision (approach C in the brainstorm).
- Any change to the generic param panel, the table, or the chart.
- A calendar or trading-session grammar. `T-1d` is 24 hours of wall clock.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **The virtual symbol lives in `mcp_stores`**, not in the stores-explorer routes | stores-explorer wraps `mcp_stores` one-to-one, so both doors — the widget and Rita — answer "what symbols are in ticks_live" the same way. |
| D2 | **Expressions resolve in bdobb**, beside `$currentDate` | The backend stays ISO-only (its time regex admits neither `Z` nor an offset), and every widget's date param gets the grammar, not only this one. Same contract as the VWAP anchor: absolute on the wire, the client resolves. |
| D3 | **`d` steps yield a local calendar date; `h`/`m`/`s` steps yield naive UTC to the second** | `$currentDate` already means the local calendar day; a tick index is naive UTC, so an instant is sent as the wall clock the store keeps. |
| D4 | **An unfiltered read of a spanning symbol reads only its newest day** | Keeps v11.0.0's guarantee that an unfiltered call never materialises a whole symbol. |
| D5 | **As-of becomes a timestamp** | Version numbers are per table and do not line up across days; a Delta timestamp time-travel applies uniformly. The `version` field stays for single tables. |
| D6 | **Start and End render in the explorer strip**, beside As-of | The generic date control is a day picker and cannot hold `T-5h`; the strip already owns the one control that needs describe (As-of), and the picker's precision needs describe too. |
| D7 | **Precision is read from describe's `date` column dtype** | A dtype naming `date` gives a `type=date` picker; anything naming `timestamp` gives `datetime-local` with second steps. Today every library is a timestamp, so this is a rule, not a visible switch. |

## Backend (openbb-docker)

### `mcp_stores/daykeys.py` — new, pure

The key grammar in one place, no I/O:

- `split(key) -> (base, day | None)` — `AAPL_2026_09_11` → `("AAPL", date(2026, 9, 11))`; a key with no suffix is its own base with `None`.
- `bases(keys) -> list[str]` — collapsed, deduplicated, sorted.
- `day_keys(keys, base) -> list[str]` — every table behind a base, oldest day first; a base that is itself a key is `[base]`.
- `in_window(keys, base, start, end) -> list[str]` — the raw keys a read must open, in day order. With neither bound: the newest day only (D4). With bounds: the days whose calendar day falls inside `[date(start), date(end)]`; a missing bound is open on that side. A base that is itself a key is `[base]` whatever the bounds.

### `mcp_stores/server.py`

- `delta_list_symbols` returns `bases(raw)`.
- `_require_symbol` accepts a base with at least one table; the identity regex is unchanged (a base is a substring of a key it already admits).
- `delta_read`: expand, read each key with the same `start`, `end`, `as_of`, concatenate in day order, `tail(tail_rows)`. `total_rows_in_range` is the sum before the tail. A key whose earliest commit is after a timestamp `as_of` contributes no rows. The 10,000-row cap and the response shape are unchanged.
- `delta_describe` on a spanning base: `row_count` summed, `date_range` the outer min/max, `columns` from the newest day, plus `days: <int>` (1 for a single table, so the frontend has one shape).
- `delta_history`: the union of commits across the expanded keys, newest first, each `{version, timestamp}` with `timestamp` as an ISO string (UTC, millisecond precision: the string is what a client sends back as `as_of`, and a commit's own instant must resolve to that commit, which a value truncated to the second would miss by up to 999 ms). For a single table this only reformats the millis.

Reading many small logs costs one log walk per day table. A describe of the 41-base library is at most a few dozen log reads; the plan measures this against MinIO before it is believed, per the Ep-11 lesson that tmp-path tests miss S3 addressing.

### `stores-explorer`

Routes unchanged. `widgets.json`: `start` and `end` stay `type: "date"` and hidden from the panel (the strip renders them); the description says the symbol may span days. The image is rebuilt and shipped to the NAS by `docker save`, after 4 PM ET.

## Frontend (bdobb-v2)

### `src/lib/params.ts`

`resolveParamValue` gains the grammar `^T([+-]\d+)([dhms])?$`:

| Input | Output | Note |
|---|---|---|
| `T`, `T-5d` | `YYYY-MM-DD` | local calendar date, as `$currentDate` |
| `T-5h`, `T+30m`, `T-90s` | `YYYY-MM-DD HH:MM:SS` | naive UTC (D3) |
| anything else | unchanged | absolute dates and timestamps pass through |

`T` is not a legal value anywhere today, so no existing resolution changes.
`ParamControls` already displays resolved values, so a panel field holding
`T-5d` shows today minus five days, as `$currentDate` does.

### `src/hooks/useDeltaMetadata.ts`

`parseDeltaDescribe` accepts optional `days` (an integer, default 1).
`parseDeltaVersions` keeps `timestamp` as the string it is; no change beyond
the tests, which now use ISO strings.

### `src/components/renderers/DeltaExplorerRenderer.tsx`

Two new controls in the strip, between the columns and As-of:

```
│ ticks_live / AAPL · 12 days · 338,796 rows · 2026-08-27 08:00:00 → 2026-09-14 23:56:46 │
│ date ⟨timestamp_ntz⟩  price ⟨double⟩  size ⟨double⟩                                    │
│ Start [ T-5h        ][📅]   End [ T-3h        ][📅]   As of [ latest ▾ ]              │
```

- Each is a text input holding the raw expression or absolute value, with the
  resolved value as its `title`. It commits on blur and on Enter, through a
  new `onRangeChange({ start, end })` prop that `WidgetCard` routes through
  `splitParamEdit`, exactly as `onAsOfChange` does. Groups and locked widgets
  therefore behave as for any param.
- The picker beside each is a native `<input>` — `type="date"` or
  `type="datetime-local" step="1"` per D7 — whose change writes its absolute
  value into the text field and commits. While describe is loading or failed,
  the picker is `datetime-local`.
- As-of's options are the history's ISO timestamps, labelled as such, with
  the value the timestamp; "latest" still sends nothing.
- The strip's head says `· N days` when `days > 1`.

Describe and history stay keyed on library and symbol. A start/end change
does not refetch them: the strip describes the symbol, not the window.

### `src/components/WidgetCard.tsx`

Passes `start`, `end` and `onRangeChange` to the renderer, next to `asOf`.

## Data flow

1. Library → symbol through the existing cascading pickers; the symbols
   endpoint now answers bases.
2. On a settled pair the renderer fetches describe and history; the picker
   precision is decided from describe.
3. The user types `T-5h` / `T-3h` or uses the pickers. The values land on the
   card's params as typed.
4. `useWidgetData` builds the series URL; `resolveParams` turns the
   expressions into absolute values; empty values are omitted, as today.
5. `delta_read` expands the base to the day tables in the window, reads each
   with the same filter, and returns the tail. Rows land in `TableRenderer`.

## Error handling

| Case | Outcome |
|---|---|
| Window covers no day table | Zero rows, `total_rows_in_range: 0`; not a 404. |
| Unparseable expression | Sent as typed; the backend's 422 surfaces as the card error, as any bad param does today. |
| `as_of` predates every table in the window | Zero rows, not an error. |
| Describe fails | The strip shows its error, the pickers fall back to `datetime-local`, the rows still render (v11.0.0's rule). |
| `start` after `end` | Sent as is; the backend answers zero rows. No client-side guard — the resolved values are visible in the tooltips. |

## Testing

Backend, on tmp-path Delta stores: `daykeys` table-driven (split, bases,
expand with each bound combination, a base with no suffix); `delta_list_symbols`
collapses; `delta_read` concatenates across days in order, tails, honours
`start`/`end` inside each day, reads the newest day only when unfiltered, and
skips a table younger than a timestamp `as_of`; `delta_describe` sums and
reports `days`; `delta_history` unions and formats. Then the same tools
against MinIO on the NAS for the s3 addressing.

Frontend: `params.test.ts` covers every row of the grammar table, with a fixed
`now`, plus pass-through and `$currentDate` unchanged;
`DeltaExplorerRenderer.test.tsx` covers the controls committing on blur and
Enter, the picker type switching on the describe dtype, the picker writing
into the field, the day count, and As-of sending a timestamp;
`useDeltaMetadata.test.ts` covers `days` and string timestamps;
`WidgetCard.test.tsx` covers `start`/`end` reaching the series request
resolved.

## Success criteria

1. On the live NAS stack, `ticks_live`'s picker lists `AAPL` once, and the
   strip says how many days it spans.
2. `AAPL`, Start `T-5h`, End `T-3h` returns the ticks in that window, across
   day tables when the window crosses midnight UTC.
3. `ticks_sip` `GS` with the same window filters to the second, and the
   picker beside each field is a seconds picker.
4. Choosing an As-of timestamp visibly changes the rows; "latest" restores
   them.
5. `kdb_explorer` and every other widget's date param are unchanged, except
   that `T-5d` now resolves where `$currentDate` does.
6. The explorer's help page in the vendored vault documents the three
   controls and the grammar.

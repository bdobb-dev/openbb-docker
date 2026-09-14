# Episode / release boundaries (restructured 2026-09-01)

Art's final episode mapping ("I know it's a big change, but I think it reads
better") re-draws what each episode *covers*. Existing tags are immutable —
restore-episode and the published GHCR images depend on them — so the
restructure lands as the **next release on each line**, and the old tags
keep meaning what they meant when cut.

## The new boundaries

| Episode | Covers | Article pins |
|---|---|---|
| **9** | EODHD mapped onto the FMP data surface, via REST API calls — the full `openbb-eodhd` parity package (52 fetchers, extension 9.x line) | **v9.1.0** (to cut) |
| **10** | Websockets, kdb and caching — live-grid service + `live_grid`/`live_chart` (plain streaming candles), kdb cache, `/series`, tick recorder | **v10.1.0** (to cut) |
| **11** | Delta Lake: daily storage, durable ticks, and the read-through cache that minimizes API calls (the fundamentals L2 tier rebuilt on Delta Lake) | **v11.2.0** (to cut) |
| **12** | Charting/TA tool family + rita-proposed levels | future |

What moved: the eodhd extension's *expansion* was briefly slated for ep 10;
live-grid historically shipped in the v9.0.0 tag; chart types/overlays
historically shipped in the v10.0.0 tag. Under the new boundaries those read
as ep 9 / ep 10 / ep 12 respectively.

## How each next tag gets cut

- **v9.1.0 — branch `release/v9.1.0`** (exists): the `v9.0.0` tree with
  `openbb-eodhd/` replaced by the parity state (extension 9.5.0, 52
  fetchers) and the derived `extension-constraints.txt`. live-grid remains
  *in the tree* (it shipped in v9.0.0; removing shipped code from a release
  line is churn) — the episode-9 article simply doesn't cover it.
- **v10.1.0**: `v10.0.0` + the kdb-line follow-ons — tick-log durability
  (`d26effb`), the kdb-ws demo chart (`f3d14d9`), per-bar vwap. Cut after
  that work settles; it is in flight on `eodhd-fmp-parity` today and
  belongs on the ep-10 line, not the parity line.
- **v11.2.0**: the Delta Lake replacement of the ArcticDB/MinIO store —
  eod-dump flushing to Delta Lake (`a061853`), daily storage, and the
  fundamentals read-through L2 rebuilt on delta-rs (design:
  `docs/superpowers/specs/2026-09-01-deltalake-store-design.md` on the
  ep-11 line). Cut after the store swap lands.

## Rules

- **Episode content is never rewritten on an existing `vN.*` tag** —
  restore-episode resolves episodes to tags and pulls the per-version GHCR
  images, and an article's code must stay the code the article describes.
- **Fundamentals are retrofitted by retag.** Cross-cutting infrastructure
  (the internal bridge, the CFTC lifespan guard, the serve/funnel config,
  live-grid auth) is cherry-picked onto every tag where the service exists,
  the annotated tag is re-pointed with its message preserved, the old
  commit is kept as `backup/pre-<layer>-vN.M.P`, and the force-push lets
  `release.yml` rebuild that version's images. Each tag carries only the
  slice that existed at its episode: serve.json starts with 443 → openbb-api
  in v1 and gains one handler or Service per episode. This rule was
  originally written as "never retag"; it was relaxed once the bridge and
  CFTC layers had already been applied this way and the backups made it
  safe.
- Branch `release/vN.M` from the `vN.<M-1>` tag, land only that episode's
  content, and let `release.yml` cut the tag/images from the branch.
- The `eodhd-fmp-parity` branch is the ep-9 *working* branch but currently
  carries stray ep-10/ep-11 commits (`d26effb`, `a061853`, `f3d14d9`) —
  those must NOT ride into v9.1.0; `release/v9.1.0` takes only the
  `openbb-eodhd/` state (plus constraints), which is why it is built from
  the v9.0.0 tree rather than by merging the branch.
- bdobb-v2's spec docs (`docs/specs/v9.0.0-features.md`,
  `v10.0.0-features.md`) encode the same boundaries for the app-side
  rebuild; the Substack article structure follows suit.

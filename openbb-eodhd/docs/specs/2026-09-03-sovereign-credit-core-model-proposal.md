# Proposed OpenBB core models: SovereignCreditRating and SovereignCdsSpread

**Date:** 2026-09-03
**Status:** filed — [OpenBB-finance/OpenBB#7654](https://github.com/OpenBB-finance/OpenBB/pull/7654),
branch `feature/sovereign-credit-cds-standard-models` on the `artcashin/OpenBB`
fork, targeting `develop` per CONTRIBUTING.md
**Author's intent:** double as episode 9's worked example of the "extend
`openbb_core` yourself" invitation, and as the concrete case for a governance
question below

## Why this pair, why now

The episode-9 sweep of EODHD's API surface ([2026-09-03 memory note], not
committed to this repo) found sovereign credit ratings and sovereign CDS
spreads as two of the few EODHD capabilities with **no existing standard
model anywhere in `openbb_core`**, confirmed by checking `fetcher_dict`
registrations across all 34 installed providers, not just standard-model file
existence. `RiskPremium` looked like a candidate on the surface but isn't:
its `total_equity_risk_premium`/`country_risk_premium` fields are Damodaran's
*equity* risk premium by country, a different concept from a government's
own credit ratings or the market price of insuring its debt.

The data is real and live on the account key used for this project
(`get_credit_sovereign_credit_ratings`, `get_credit_sovereign_cds_spreads`),
sourced from Damodaran/NYU Stern — a widely-cited, publicly available
academic dataset, not something EODHD manufactures itself. That matters for
the proposal: this isn't "give EODHD a bespoke model," it's "here is a
provider-agnostic dataset that happens to be reachable through EODHD today,"
exactly the kind of shared-across-providers case the standardization
framework is meant for.

Per `OpenBB/openbb_platform/CONTRIBUTING.md`: *"The standard models are
created and maintained by the OpenBB team. If you want to add a new field to
a standard model, you'll need to open a PR to the OpenBB Platform."* The same
applies, a fortiori, to a wholly new model. This document is that PR's
content, drafted so it can be dropped into a branch and opened for real.

## Proposed model: `SovereignCreditRating`

```python
# openbb_platform/core/openbb_core/provider/standard_models/sovereign_credit_rating.py
"""Sovereign Credit Rating Standard Model."""

from datetime import date as dateType

from openbb_core.provider.abstract.data import Data
from openbb_core.provider.abstract.query_params import QueryParams
from openbb_core.provider.utils.descriptions import (
    DATA_DESCRIPTIONS,
    QUERY_DESCRIPTIONS,
)
from pydantic import Field


class SovereignCreditRatingQueryParams(QueryParams):
    """Sovereign Credit Rating Query."""

    country: str | None = Field(
        default=None,
        description=QUERY_DESCRIPTIONS.get("country", "")
        + " Accepts an ISO-3166 alpha-3 code or a country name.",
    )
    as_of_date: dateType | None = Field(
        default=None,
        description="Return ratings as of this date rather than the latest available.",
    )


class SovereignCreditRatingData(Data):
    """Sovereign Credit Rating Data."""

    date: dateType = Field(description=DATA_DESCRIPTIONS.get("date", ""))
    country: str = Field(description=DATA_DESCRIPTIONS.get("country", ""))
    moodys_rating: str | None = Field(
        default=None, description="Moody's long-term foreign-currency sovereign rating."
    )
    sp_rating: str | None = Field(
        default=None, description="S&P long-term foreign-currency sovereign rating."
    )
    fitch_rating: str | None = Field(
        default=None, description="Fitch long-term foreign-currency sovereign rating."
    )
```

## Proposed model: `SovereignCdsSpread`

```python
# openbb_platform/core/openbb_core/provider/standard_models/sovereign_cds_spread.py
"""Sovereign CDS Spread Standard Model."""

from datetime import date as dateType

from openbb_core.provider.abstract.data import Data
from openbb_core.provider.abstract.query_params import QueryParams
from openbb_core.provider.utils.descriptions import (
    DATA_DESCRIPTIONS,
    QUERY_DESCRIPTIONS,
)
from pydantic import Field


class SovereignCdsSpreadQueryParams(QueryParams):
    """Sovereign CDS Spread Query."""

    country: str | None = Field(
        default=None,
        description=QUERY_DESCRIPTIONS.get("country", "")
        + " Accepts an ISO-3166 alpha-3 code or a country name.",
    )
    as_of_date: dateType | None = Field(
        default=None,
        description="Return spreads as of this date rather than the latest available.",
    )


class SovereignCdsSpreadData(Data):
    """Sovereign CDS Spread Data."""

    date: dateType = Field(description=DATA_DESCRIPTIONS.get("date", ""))
    country: str = Field(description=DATA_DESCRIPTIONS.get("country", ""))
    cds_spread: float | None = Field(
        default=None,
        description="10-year sovereign CDS spread, as a normalized percent.",
        json_schema_extra={"x-unit_measurement": "percent", "x-frontend_multiply": 100},
    )
    cds_spread_net_of_benchmark: float | None = Field(
        default=None,
        description="CDS spread net of the lowest-risk sovereign benchmark"
        " (Switzerland in the source dataset), as a normalized percent.",
        json_schema_extra={"x-unit_measurement": "percent", "x-frontend_multiply": 100},
    )
```

**Design notes, anticipating review:**

- Two models, not one, matching how `PriceTarget`/`PriceTargetConsensus` and
  `GdpReal`/`GdpNominal` are kept separate even where one provider answers
  both from the same call — a provider whose data only covers ratings, or
  only CDS, shouldn't need to fake the other.
- Rating fields are plain `str`, not a closed `Literal`, following the
  precedent in `esg_risk_rating.py` — actually no, that file *does* use a
  `Literal` for a small enum (A+ through F). Sovereign ratings have a much
  larger and slower-moving-but-not-fixed alphabet across three agencies
  (Aaa..C for Moody's, AAA..D for S&P/Fitch) — `str` avoids the model
  needing an update every time an agency adds or retires a notch.
- `cds_spread_net_of_benchmark` is renamed from EODHD's
  `cds_spread_net_of_switzerland` — a standard model shouldn't bake in one
  source's specific benchmark choice into a field name; the description
  carries that detail instead.
- Both models take an optional `as_of_date` rather than `start_date`/
  `end_date`, because this is snapshot-style annual data (per the source),
  not a dense time series — matching the query shape of `PriceTargetConsensus`
  and `CountryProfile` rather than `EquityHistorical`.

## What `openbb-eodhd` would add once these exist

Once merged upstream, the fetcher side is the easy, already-proven part of
this project — one file, `openbb_eodhd/models/sovereign_credit.py`, following
the exact shape of every other fetcher in this extension: `transform_query`,
`aextract_data` calling `get_credit_sovereign_credit_ratings`/
`get_credit_sovereign_cds_spreads`, `transform_data` mapping the response's
`moodys_rating`/`sp_rating`/`fitch_rating`/`cds_spread`/
`cds_spread_net_of_switzerland` fields straight across, registered in
`__init__.py`. No new design decisions, no new risk notes — the design work
is entirely upstream, in the two model files above.

## The governance question this is really trying to raise

The CONTRIBUTING.md rule reads cleanly for the case it was written for: a
human proposing a new standard model is a meaningful engineering
investment (reading multiple providers' APIs, finding the shared fields,
writing the Pydantic model, defending the design in review), so routing it
through the OpenBB team's judgment is a reasonable, low-volume gate.

That assumption is what's shifting. The two model files above, plus their
rationale, were drafted end-to-end in one sitting by pointing an agent at a
live API, the standard-models directory for precedent, and the
CONTRIBUTING.md rules themselves — the same afternoon this project verified
the underlying data actually exists and doesn't already have a home. If
proposing a well-formed, precedent-following standard model becomes this
cheap for anyone with an API key and an agent, the OpenBB team's own review
bandwidth, not the drafting effort, becomes the real bottleneck — and a
single central-review gate that was fine at a trickle may not hold at a flood.

There's also a structural tension worth naming directly, in the project's
own words back to it: *"We standardize fields that are shared between two or
more providers."* That's the right criterion for a *mature* model — but it
can't be the admission criterion for a *new* one, because a field can't be
"shared between two or more providers" until at least one provider is
allowed to register it first. Every standard model that exists today had to
clear the PR gate while still describing only one provider's data. The
question isn't whether that gate should exist, it's whether the *only* gate
should be a full core-team PR review, for every single proposal, at whatever
volume AI-assisted contributors can now produce them.

**A possible middle path, offered as a discussion starter, not a demand:**
a lighter-weight "provisional" standard-model tier — reviewed for shape and
schema sanity but not for long-term commitment, sitting in a separate
namespace or module, promotable to the fully-reviewed tier once a second
provider actually adopts it (the framework's own stated bar). That would let
proposals like this one land and get used quickly, while keeping the
OpenBB team's full review effort focused on the models that prove out.

This document, and the two models above, are offered as the concrete example
to anchor that conversation — a real, checked, ready-to-review proposal,
not a hypothetical.

## Status and next step

**Filed 2026-09-03:** [OpenBB-finance/OpenBB#7654](https://github.com/OpenBB-finance/OpenBB/pull/7654).
Set up clean, separate from the in-progress `~/Developer/OpenBB` build
checkout (which was mid-rebuild — detached HEAD, uncommitted artifacts from
another process — and was left untouched): forked to `artcashin/OpenBB`,
fresh shallow clone at `~/Developer/openbb-sovereign-credit-pr`, branch
`feature/sovereign-credit-cds-standard-models` off `develop`. Both model
files were instantiated against the real installed `openbb-core` package
(not just syntax-checked) with data shaped like the live EODHD feed before
committing. The PR body carries the same design notes as this document, plus
the governance question, framed as optional to engage with, not a condition
of merging.

Next step is upstream: waiting on OpenBB maintainer review. If the model
shape changes in review, this document and the two files under `## Proposed
model` above should be updated to match, not left to drift from what's
actually filed.

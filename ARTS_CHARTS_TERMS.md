<!-- Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Arts Charts — supplemental terms

**Applies to release v12.0.0 and later.** Earlier releases are unaffected and
keep the terms they shipped under.

These terms are supplemental to the Apache License, Version 2.0 in `LICENSE`,
which governs this software. They add one condition, and they add it only to
material this project's copyright holder owns. Apache-2.0 section 4 permits a
licensor to offer additional or different terms for its own modifications and
for the derivative work as a whole; that is the basis for what follows.

Nothing here alters, restates, or extends the terms of any third-party
component. Where a third party's requirement is described below, it is
described for your information only, and that third party's own licence
governs it. See `NOTICE`.

## 1. The Arts Charts mark

This repository is the backend half of Arts Charts: it declares the widgets
(`live-grid/widgets.json`) and serves the data a client renders as a chart.
The mark itself — the bar-mark drawn as a chart watermark — is rendered by the
BDOBB client and lives in that repository. It is the property of
SecretoftheUniverse.com LLC.

**Condition.** If you use this software, or any derivative work of it, to
produce a chart image through Arts Charts, you must leave the Arts Charts mark
in place on that image. This applies whether the chart is kept for your own use
or published, broadcast, or otherwise shown to others, and whether it is
exported, screenshot, printed, or embedded.

**Removal.** SecretoftheUniverse.com LLC will consider granting terms for the
removal of the Arts Charts mark. Enquiries are welcome — see `README.md` for
contact details. Removal without such terms is not permitted by this licence.

## 2. The TradingView attribution logo — what their licence asks

Arts Charts renders through **Lightweight Charts™**, © 2023 TradingView, Inc.,
licensed under Apache-2.0 and **not** covered by section 1 above.
SecretoftheUniverse.com LLC has no authority over TradingView's mark and grants
no rights in it.

TradingView's own terms, as published with the library, ask that you add the
attribution notice and a link to <https://www.tradingview.com/> to a page your
users can reach. Their `attributionLogo` layout option satisfies the link by
drawing it on the chart, and **defaults to enabled**; this repository does not
disable it. The credit is carried in `NOTICE` and in `kdb-ws/live-chart.html`,
the only page here that loads the library.

Note what this is and is not. It is a request for attribution and a link —
**not** a watermark requirement, and not a term this project can impose, waive,
or license on TradingView's behalf. If you want to disable the attribution
logo, that is a matter between you and TradingView, and you should approach
them directly.

## 3. Interpretation

If any part of section 1 is held unenforceable, the remainder of these terms
and the whole of the Apache License, Version 2.0 continue to apply. These terms
do not reduce any permission Apache-2.0 grants you in this project's source
code; they attach to chart images produced with the Arts Charts mark, and to
nothing else.

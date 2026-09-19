#!/usr/bin/env bash
# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

# The attribution a third party's licence asks for must not go missing.
#
# This repository had no NOTICE and no credit of any kind for eighteen months
# while serving Lightweight Charts™ from kdb-ws/live-chart.html. Nothing
# noticed, because nothing looks: the page renders, the tests pass, and the
# obligation lives in TradingView's README rather than in a file any tool
# reads. That is exactly the shape of failure the image-pin guard exists for --
# a requirement nobody checks is a requirement that quietly lapses.
#
# What is asserted, and why each line:
#
#   NOTICE exists and names TradingView     Apache-2.0 section 4(d): a NOTICE
#                                           file must travel with the work.
#   every page loading the library credits  TradingView asks for the notice and
#   it, and links tradingview.com           a link on a page users can reach.
#   no page disables attributionLogo        it DEFAULTS to true, so the danger
#                                           is switching it off, never
#                                           forgetting to switch it on.
#   ARTS_CHARTS_TERMS.md is referenced      a supplemental condition LICENSE
#   from LICENSE                            does not point at is unfindable.
set -euo pipefail

cd "$(dirname "$0")/.."

status=0
fail() { echo "FAIL: $*"; status=1; }

[ -f NOTICE ] || fail "NOTICE is missing -- Apache-2.0 section 4(d) requires it to travel with the work"
if [ -f NOTICE ]; then
  grep -q "TradingView" NOTICE || fail "NOTICE does not name TradingView, whose licence asks to be acknowledged"
  grep -q "tradingview.com" NOTICE || fail "NOTICE carries no link to tradingview.com"
fi

grep -q "ARTS_CHARTS_TERMS.md" LICENSE || fail "LICENSE does not point at ARTS_CHARTS_TERMS.md"
[ -f ARTS_CHARTS_TERMS.md ] || fail "ARTS_CHARTS_TERMS.md is missing"

# Any page that loads the library owes the credit. Found rather than listed, so
# a new page inherits the check instead of slipping past a hardcoded path.
while read -r page; do
  [ -n "$page" ] || continue
  grep -q "tradingview.com" "$page" \
    || fail "$page loads lightweight-charts but carries no link to tradingview.com"
  # `attributionLogo: false` is the regression; the option defaults to true.
  if grep -qE 'attributionLogo:[[:space:]]*false' "$page"; then
    fail "$page disables attributionLogo, which is what satisfies TradingView's link requirement"
  fi
done < <(grep -rl "lightweight-charts" --include="*.html" . 2>/dev/null || true)

if [ $status -eq 0 ]; then
  echo "OK: third-party attribution is present and not disabled"
fi
exit $status

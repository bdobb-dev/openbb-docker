#!/usr/bin/env bash
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

# Smoke test for a running stack. Point OPENBB_URL at your Serve address:
#   OPENBB_URL=https://openbb.<your-tailnet>.ts.net scripts/smoke.sh
# or test the API directly from inside the container's namespace:
#   docker exec openbb-api bash -c 'OPENBB_URL=http://127.0.0.1:6900 /bin/bash -s' < scripts/smoke.sh
set -euo pipefail
: "${OPENBB_URL:?set OPENBB_URL to the base URL to test}"

echo "1/2 widgets.json is served and non-trivial..."
count=$(curl -fsS "$OPENBB_URL/widgets.json" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
[ "$count" -gt 50 ] || { echo "FAIL: only $count widgets"; exit 1; }
echo "   OK: $count widgets"

echo "2/2 a keyless data endpoint answers..."
curl -fsS "$OPENBB_URL/api/v1/equity/price/historical?symbol=AAPL&provider=yfinance" >/dev/null
echo "   OK"
echo "Smoke test passed."

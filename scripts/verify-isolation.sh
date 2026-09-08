#!/usr/bin/env bash
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

# Run this from a SECOND tailnet device (not the NAS). It proves the walls:
# the API must be reachable ONLY through Serve, never on its raw port.
#   scripts/verify-isolation.sh openbb.<your-tailnet>.ts.net
set -euo pipefail
host="${1:?usage: verify-isolation.sh <tailnet-hostname>}"

echo "1/2 the front door answers over HTTPS..."
curl -fsS --max-time 10 "https://$host/widgets.json" >/dev/null && echo "   OK"

echo "2/2 the raw API port is sealed..."
if curl -s --max-time 5 "http://$host:6900/widgets.json" >/dev/null 2>&1; then
  echo "   FAIL: :6900 answered directly — check TS_USERSPACE=false and loopback bindings" >&2
  exit 1
fi
echo "   OK: :6900 refused"
echo "Isolation verified."

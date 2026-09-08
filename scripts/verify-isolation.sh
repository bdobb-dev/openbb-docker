#!/usr/bin/env bash
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

# Run this from a SECOND tailnet device (not the NAS). It proves the walls:
# the API must be reachable ONLY through Serve, never on its raw port.
#   scripts/verify-isolation.sh openbb.<your-tailnet>.ts.net
set -euo pipefail
host="${1:?usage: verify-isolation.sh <tailnet-hostname>}"

echo "1/3 the front door answers over HTTPS..."
curl -fsS --max-time 10 "https://$host/widgets.json" >/dev/null && echo "   OK"

echo "2/3 the raw API port is sealed..."
if curl -s --max-time 5 "http://$host:6900/widgets.json" >/dev/null 2>&1; then
  echo "   FAIL: :6900 answered directly — check TS_USERSPACE=false and loopback bindings" >&2
  exit 1
fi
echo "   OK: :6900 refused"

# Ep. 10: q must never be reachable from another tailnet device. If this
# succeeds, q is bound to 0.0.0.0 and every peer can execute arbitrary q.
echo "3/3 kdb+ (q) IPC is sealed..."
if timeout 5 bash -c "cat < /dev/null > /dev/tcp/$host/5000" 2>/dev/null; then
  echo "   FAIL: :5000 (q) answered — check KDB_HOST/network_mode and the 127.0.0.1 bind" >&2
  exit 1
fi
echo "   OK: :5000 refused"
echo "Isolation verified."

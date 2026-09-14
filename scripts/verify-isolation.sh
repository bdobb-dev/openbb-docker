#!/usr/bin/env bash
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

# Run this from a SECOND tailnet device (not the NAS). It proves the walls:
# the API must be reachable ONLY through Serve, never on its raw port.
#   scripts/verify-isolation.sh openbb.<your-tailnet>.ts.net
#   scripts/verify-isolation.sh openbb.<your-tailnet>.ts.net minio.<your-tailnet>.ts.net
set -euo pipefail
host="${1:?usage: verify-isolation.sh <tailnet-hostname> [minio-hostname]}"
minio_host="${2:-}"

echo "1/5 the front door answers over HTTPS..."
curl -fsS --max-time 10 "https://$host/widgets.json" >/dev/null && echo "   OK"

echo "2/5 the raw API port is sealed..."
if curl -s --max-time 5 "http://$host:6900/widgets.json" >/dev/null 2>&1; then
  echo "   FAIL: :6900 answered directly — check TS_USERSPACE=false and loopback bindings" >&2
  exit 1
fi
echo "   OK: :6900 refused"

# Ep. 10: q must never be reachable from another tailnet device. If this
# succeeds, q is bound to 0.0.0.0 and every peer can execute arbitrary q.
echo "3/5 kdb+ (q) IPC is sealed..."
if timeout 5 bash -c "cat < /dev/null > /dev/tcp/$host/5000" 2>/dev/null; then
  echo "   FAIL: :5000 (q) answered — check KDB_HOST/network_mode and the 127.0.0.1 bind" >&2
  exit 1
fi
echo "   OK: :5000 refused"

# Ep. 11: MinIO is its own node, so unlike :6900 and :5000 above there is no
# "port must be refused" wall to test here -- MinIO binds normally and its
# tailnet address is SUPPOSED to answer. What we can prove from a tailnet
# device is that the S3 API is TLS-only. The console on :9001 is deliberately
# reachable from here, so this script cannot test it; the property that matters
# for :9001 is that it is not published to the Docker host or LAN, which
# `docker compose config` shows (no `ports:` on the minio service).
if [ -n "$minio_host" ]; then
  echo "4/5 the MinIO S3 API answers over TLS with a valid certificate..."
  curl -fsS --max-time 10 "https://$minio_host:9000/minio/health/live" >/dev/null \
    && echo "   OK: valid certificate, endpoint reachable over TLS"

  echo "5/5 the MinIO S3 API refuses plaintext..."
  if curl -fsS --max-time 5 "http://$minio_host:9000/minio/health/live" >/dev/null 2>&1; then
    echo "   FAIL: :9000 answered over plain HTTP — the entrypoint should refuse to start without a cert" >&2
    exit 1
  fi
  echo "   OK: plaintext refused"
else
  echo "4/5, 5/5 skipped (no MinIO hostname given)"
fi
echo "Isolation verified."

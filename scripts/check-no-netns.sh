#!/usr/bin/env bash
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

# No service may share the Tailscale sidecar's network namespace.
#
# `network_mode: service:tailscale` binds a container to the SANDBOX the sidecar
# owned when that container started. containerboot kills `tailscale up` at 60s
# and exits 0, `restart: unless-stopped` bounces the sidecar, and the restart
# creates a NEW sandbox -- leaving every child holding the corpse: mutually
# reachable on loopback, invisible to Serve, and still reporting `Up`.
#
# The stack uses the openbb-internal bridge instead, from Ep. 1 onward. This
# guard exists so a later episode cannot quietly reintroduce the coupling when
# it adds its own service.
#
# Scoped to the sidecar deliberately: `network_mode: service:<other>` is a
# legitimate pattern elsewhere -- Ep. 11's minio-init shares minio's namespace
# on purpose, and minio is not something a restart of ours can strand.
set -euo pipefail

cd "$(dirname "$0")/.."

if grep -nE '^[[:space:]]*network_mode:[[:space:]]*"?service:tailscale"?' docker-compose.yml; then
  echo "FAIL: the lines above share the sidecar's network namespace." >&2
  echo "      Use: networks: [openbb-internal]  -- and bind 0.0.0.0, not 127.0.0.1." >&2
  exit 1
fi

echo "OK: nothing shares the sidecar's network namespace"

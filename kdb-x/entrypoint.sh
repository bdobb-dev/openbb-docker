#!/usr/bin/env bash
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

# Launch KDB-X (q) as a long-running server.
#
# q normally reads its console from stdin and EXITS on EOF — which happens
# immediately in a detached container. We hold stdin open with `tail -f /dev/null`
# so q stays up purely as a network server listening on $KX_PORT, while still
# accepting any extra args you pass (e.g. a startup .q script: `docker run ... script.q`).
#
# Optional: set KX_MODULES to a comma/space-separated list of optional modules to
# auto-load at startup, e.g.  -e KX_MODULES="ai,pq,rest,kurl,objstor,sql".
#  * ai/pq/rest/kurl/objstor are loaded with kdb-x's `use` keyword (as `kx.<name>`)
#    and bound to a global of the same name (e.g. `kurl.sync`, `ai.hnsw`).
#  * sql is loaded via `\l s.k_` (amd64 image only); enables `.s.e"<sql>"` and `s)`.
# A module that fails to load is logged but does not stop the server.
set -euo pipefail

PORT="${KX_PORT:-127.0.0.1:5000}"
QBIN="${QHOME:-/root/.kx}/bin/q"

if [ -n "${KX_MODULES:-}" ]; then
  STARTUP="$(mktemp /tmp/kx_startup.XXXXXX.q)"
  for m in ${KX_MODULES//,/ }; do
    if [ "$m" = "sql" ]; then
      printf '@[{system"l ",getenv[`QHOME],"/q/s.k_"; -1"loaded module: sql (.s.e / s) )";}; ::; {-2"module sql failed to load: ",x;}]\n' >> "$STARTUP"
    else
      printf '@[{(`$"%s") set use`kx.%s; -1"loaded module: %s";}; ::; {-2"module %s failed to load: ",x;}]\n' "$m" "$m" "$m" "$m" >> "$STARTUP"
    fi
  done
  echo "Auto-loading modules: ${KX_MODULES}"
  set -- "$STARTUP" "$@"
fi

echo "Starting KDB-X (q) -- listening on ${PORT}"
exec "$QBIN" "$@" -p "$PORT" < <(tail -f /dev/null)

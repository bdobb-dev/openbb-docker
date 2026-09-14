#!/usr/bin/env bash
# Both serve configs must parse, and must declare the Tailscale Services.
# containerboot applies serve.json WHOLESALE on every apply: a Service that
# is not in this file is a Service that vanishes on the next apply (it did,
# 2026-08-04). A malformed file kills the sidecar outright.
set -euo pipefail
cd "$(dirname "$0")/.."
for f in ts-config/serve.json ts-config/serve-funnel.json; do
  jq -e '.Services["svc:openbb-api"].Web | length > 0' "$f" >/dev/null \
    || { echo "$f: not valid JSON, or svc:openbb-api missing from .Services" >&2; exit 1; }
done
echo "serve configs: valid JSON, Services declared"

#!/usr/bin/env bash
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

# Every locally-built image must carry THIS release's version as its tag.
#
# docker-compose declares `build:` AND `image:` for the images this repo builds.
# Compose only runs the build when that tag is absent locally -- so a tag that
# does not change between releases makes `docker compose up -d` silently reuse
# whatever the reader built for an earlier episode.
#
# That is not hypothetical. openbb-api shipped as `openbb-local:1.0.0` in every
# release from v1.0.0 to v11.1.1 while its Dockerfile changed six times. A
# reader who built at Ep. 1 and moved to Ep. 2 kept Ep. 1's image and never got
# the rest_api.py Basic-auth patch -- in the episode that introduces Funnel,
# which can publish that port to the public internet.
#
# Third-party images are skipped: a registry path (a "/" in the name) is
# somebody else's tag to set.
set -euo pipefail

cd "$(dirname "$0")/.."

release=$(sed -n 's/^## What you get (this release: v\([0-9][0-9.]*\)).*/\1/p' README.md | head -1)
[ -n "$release" ] || {
  echo "FAIL: could not read the release version from README.md" >&2
  echo "      expected a line like: ## What you get (this release: v3.0.1)" >&2
  exit 1
}

status=0
while read -r img; do
  case "$img" in */*) continue ;; esac
  name=${img%:*}
  tag=${img##*:}
  if [ "$img" = "$name" ]; then
    echo "FAIL: $img has no tag, so it is implicitly :latest -- use $img:$release"
    status=1
  elif [ "$tag" != "$release" ]; then
    echo "FAIL: $img is stale for this release -- use $name:$release"
    status=1
  fi
done < <(grep -E '^[[:space:]]+image:' docker-compose.yml | sed 's/.*image:[[:space:]]*//')

if [ $status -eq 0 ]; then
  echo "OK: every locally-built image is tagged $release"
fi
exit $status

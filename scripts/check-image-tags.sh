#!/usr/bin/env bash
# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
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
#
# So is a service that declares no `build:` at all. The hazard above is
# specifically that compose SKIPS a build when the tag is already present
# locally -- a service compose never builds cannot suffer it, and the image
# reaches the host some other way (pulled, or `docker load`ed by hand). On the
# curated release lines every service with an `image:` also had a `build:`, so
# grepping `image:` lines alone was equivalent; on main it is not. `eod-dump`
# and `sip-backfill` pin locally-named images this compose has no build for,
# and demanding they carry this release's version would assert a tag that
# nothing here produces.
#
# A `build:` whose context is a URL is likewise out: rss-ticker builds from
# rss-feedhandler's own repo at ITS version tag, so 8.0.0 is that project's
# release, not our episode. release.yml's discovery step excludes it on the
# same test ("not ours to publish") -- the two must agree, or this gate would
# demand a tag that the publisher never creates.
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
done < <(awk '
  # Top-level service headers sit at exactly two spaces; anything deeper
  # belongs to the service being read. Kept to awk rather than a YAML parser
  # so this stays dependency-free -- it also runs on a workstation, and the
  # scrub job installs nothing.
  function flush() {
    if (img != "" && build != "" && build !~ /:\/\//) print img
    img = ""; build = ""
  }
  /^  [a-zA-Z0-9_.-]+:[[:space:]]*$/ { flush(); next }
  /^    image:[[:space:]]*/ { img = $2; next }
  # `build: .` and the `build:` block form both land here; for the block form
  # the context arrives on a following `context:` line and overwrites this.
  /^    build:[[:space:]]*[^[:space:]]/ { build = $2; next }
  /^    build:[[:space:]]*$/ { build = "-"; next }
  /^      context:[[:space:]]*/ { build = $2; next }
  END { flush() }
' docker-compose.yml)

if [ $status -eq 0 ]; then
  echo "OK: every locally-built image is tagged $release"
fi
exit $status

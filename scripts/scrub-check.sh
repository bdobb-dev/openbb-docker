#!/usr/bin/env bash
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

# Scrub gate: refuse to ship private infrastructure details.
#
# Generic patterns live here. Machine-specific strings (your real tailnet
# name, hostnames, etc.) go in scripts/scrub-private-patterns.txt — one
# extended regex per line — which is GITIGNORED so the secrets it guards
# against never appear in the repo itself.
set -euo pipefail
cd "$(dirname "$0")/.."

PATTERNS=(
  'tskey-[A-Za-z0-9-]{10,}'                                      # Tailscale auth keys
  'tail[0-9a-f]{6,}\.ts\.net'                                    # real (hex-style) tailnet names
  '\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b'  # CGNAT / tailnet IPs
  '\b192\.168\.[0-9]{1,3}\.[0-9]{1,3}\b'                         # RFC1918 LAN IPs
  '/share/Container'                                             # NAS filesystem paths
  'AKIA[0-9A-Z]{16}'                                             # AWS-style access keys
  'Basic [A-Za-z0-9+/]{16,}={0,2}'                               # baked basic-auth headers
)

# Scan exactly the files git would ship: tracked, plus untracked ones that
# are not ignored. Anything git ignores cannot be committed, so scanning it
# only produces false positives -- and on a machine where the stack is
# actually configured, ts.env / api-auth.env / credentials.env hold REAL
# secrets by design and would fail this gate on every run, training whoever
# sees it to ignore a red scrub check. That is the failure mode this avoids.
# It also drops .venv and node_modules for free (both ignored), which is why
# they no longer need naming here.
#
# This script and the private-pattern file are skipped because they contain
# the patterns themselves and would match on their own text.
scan() {
  # scan <grep-args...> -- runs grep over the git-visible file set.
  git ls-files -z --cached --others --exclude-standard \
    | grep -zv -e '^scripts/scrub-check\.sh$' -e '^scripts/scrub-private-patterns\.txt$' \
    | xargs -0 grep "$@" -- 2>/dev/null || true
}

# Known-benign literals (e.g. the CGNAT range constant itself, synthetic test
# IPs) — exact strings, one per line, COMMITTED (unlike the private patterns).
ALLOW=scripts/scrub-allowlist.txt

filter_allowed() {
  if [[ -f "$ALLOW" ]]; then grep -v -F -f "$ALLOW" || true; else cat; fi
}

fail=0

# Filename gate. The content patterns above are `grep -rInE`, and -I skips
# binary files -- so a committed kdb+ licence blob would sail straight through
# them. A licence is caught by its NAME, not its bytes. The operator's own
# kdb-license/ directory is git-ignored and is the one place a kc.lic belongs.
#
# Not just `*.lic`: kdb+ honours kc.lic, k4.lic AND kx.lic, and the extension
# is the easiest part to change. `k4.license`, `kc.lic.txt` and `kx.lic.b64`
# are the same secret wearing a different suffix, so the stem is matched too.
# `grep` exits 1 when it matches nothing, which under `set -e -o pipefail`
# would abort the whole script on the healthy path -- hence the `|| true`.
lic_hits=$( { git ls-files --cached --others --exclude-standard \
    | grep -E '(^|/)(kc|k4|kx)\.lic|\.lic$|\.license$' || true; } | filter_allowed)
if [[ -n "$lic_hits" ]]; then
  echo "$lic_hits"
  echo "SCRUB FAIL: licence file in the tree (a kc.lic may not be redistributed)" >&2
  fail=1
fi

# Content gate for an ENCODED blob. Renaming is not the only way round the
# name gate: base64 a kc.lic into a README, a .env.example or a test fixture
# and every check above passes -- the bytes are gone but the secret is not.
# A licence is a few hundred bytes, so its base64 is one unbroken run of
# hundreds of characters; nothing legitimate in this repo has a run over 100
# (verified), and anything that later does belongs in the allowlist with a
# reason. This is a shape check, not a kdb-specific one, so it catches a
# pasted key or certificate just as well.
b64_hits=$(scan -InE -e '[A-Za-z0-9+/]{100,}={0,2}' | cut -c1-160 | filter_allowed)
if [[ -n "$b64_hits" ]]; then
  echo "$b64_hits"
  echo "SCRUB FAIL: long base64 run (an encoded licence/key blob?)" >&2
  fail=1
fi

# .env.example value gate. A *.env.example file is a TEMPLATE; a real value
# in one is either a leaked credential already, or about to become one the
# moment someone copies it into the real file without editing it first. The
# content patterns above look for particular SECRET SHAPES (tailnet names,
# CGNAT IPs, AWS-style keys, long base64 runs) -- a generic 16-to-40-char
# alphanumeric vendor key matches none of them and sails straight through.
# This gate does not look at shape at all: it fails on ANY non-empty value
# in a *.env.example file, full stop. Known-safe non-secret defaults (e.g.
# EODHD's published demo key) go in the allowlist below, same mechanism as
# everywhere else in this script -- not a separate one.
env_example_hits=$(scan -nH --include='*.env.example' \
                     -E '^[A-Za-z_][A-Za-z0-9_]*=.+$' | filter_allowed)
if [[ -n "$env_example_hits" ]]; then
  echo "$env_example_hits"
  echo "SCRUB FAIL: *.env.example has a non-empty value -- templates ship empty" >&2
  fail=1
fi

for p in "${PATTERNS[@]}"; do
  hits=$(scan -InE -e "$p" | filter_allowed)
  if [[ -n "$hits" ]]; then
    echo "$hits"
    echo "SCRUB FAIL: pattern matched: $p" >&2
    fail=1
  fi
done

if [[ -f scripts/scrub-private-patterns.txt ]]; then
  while IFS= read -r p; do
    [[ -z "$p" || "$p" == \#* ]] && continue
    if [[ -n "$(scan -InE -e "$p" | filter_allowed)" ]]; then
      scan -InE -e "$p" | filter_allowed
      echo "SCRUB FAIL: private pattern matched" >&2
      fail=1
    fi
  done < scripts/scrub-private-patterns.txt
fi

if [[ $fail -eq 1 ]]; then
  echo "Scrub check FAILED — remove the flagged content before committing." >&2
  exit 1
fi
echo "Scrub check passed."

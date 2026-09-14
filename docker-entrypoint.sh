#!/bin/sh
# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

# Generic self-provisioning entrypoint.
#
# Makes the image portable across hosts (QNAP Container Station, plain Docker,
# compose) with ZERO host pre-setup: you can bind-mount empty or not-yet-created
# paths and the container brings its own directory structure up to spec on boot.
#
# On Container Station, dockerd auto-creates a missing bind-mount SOURCE dir as
# root:root. This script then (1) creates any nested structure the app needs and
# (2) for a non-root image, takes ownership of those dirs and drops back to the
# app user -- which is what lets a non-root container write to a fresh, root-owned
# <mount>/... mount without a manual `chmod 777` on the NAS.
#
# Configure via env (set in the Dockerfile):
#   APP_DIRS     space-separated dirs to `mkdir -p` (e.g. "/data /var/lib/app")
#   APP_USER     if set AND we are root: chown APP_DIRS to it, then exec as it
#                (needs gosu or su-exec in the image; falls back to plain exec).
#   HOSTS_ALIAS  space-separated "hostname:ip" entries appended to /etc/hosts
#                before exec (e.g. "minio.<your-tailnet>.ts.net:203.0.113.5").
#                See the block below for why this exists.
# Read-only mounts (e.g. /config:ro) must NOT be listed in APP_DIRS -- chown
# would fail on them; leave them out and the app just reads them.
set -eu

for d in ${APP_DIRS:-}; do
  mkdir -p "$d"
done

# Optional: pin extra name -> IP mappings into /etc/hosts before exec.
#
# Why: some deployments run this image with `network_mode: service:<sidecar>`
# (e.g. sharing a Tailscale container's network namespace). That shares the
# NETWORK NAMESPACE but not the FILESYSTEM, and /etc/resolv.conf is
# filesystem -- so the container can't resolve names (like tailnet MagicDNS
# *.ts.net names) that every other client on that network resolves fine.
# Docker also rejects compose `extra_hosts` under `network_mode: service:*`
# ("conflicting options: custom host-to-IP mapping and the network mode"),
# so the alias has to be added here, inside the container, before the app
# starts -- pinning the name (not switching callers to a bare IP) matters
# when the far end serves TLS for that exact hostname.
#
# HOSTS_ALIAS entries are space-separated, matching APP_DIRS above: each
# entry is "hostname:ip", and ":" is already the name/ip delimiter inside
# an entry, so it can't also serve as the between-entries separator --
# space is unambiguous and needs no escaping for the hostnames/IPs this
# takes. Unset or empty -> this whole block is a no-op.
for entry in ${HOSTS_ALIAS:-}; do
  case "$entry" in
    *:*) ;;
    *)
      echo "docker-entrypoint: invalid HOSTS_ALIAS entry (want 'hostname:ip'): '$entry'" >&2
      exit 1
      ;;
  esac
  host="${entry%%:*}"
  ip="${entry#*:}"
  if [ -z "$host" ] || [ -z "$ip" ]; then
    echo "docker-entrypoint: invalid HOSTS_ALIAS entry (want 'hostname:ip'): '$entry'" >&2
    exit 1
  fi
  line="$ip $host"
  # The same hostname twice with DIFFERENT IPs would append both, and
  # /etc/hosts resolves on the FIRST match -- so the second entry would be
  # silently ignored. That is the same silent-DNS-failure this block exists to
  # prevent, so refuse it rather than pick a winner.
  if grep -qE "^[^ ]+[[:space:]]+$(printf '%s' "$host" | sed 's/[.[\*^$]/\\&/g')$" /etc/hosts 2>/dev/null \
     && ! grep -qxF "$line" /etc/hosts 2>/dev/null; then
    echo "docker-entrypoint: HOSTS_ALIAS maps '$host' to a second, different address" >&2
    exit 1
  fi
  if ! grep -qxF "$line" /etc/hosts 2>/dev/null; then
    echo "$line" >> /etc/hosts
  fi
done

if [ -n "${APP_USER:-}" ] && [ "$(id -u)" = "0" ]; then
  for d in ${APP_DIRS:-}; do
    chown -R "$APP_USER" "$d" 2>/dev/null || true
  done
  if command -v gosu >/dev/null 2>&1; then
    exec gosu "$APP_USER" "$@"
  elif command -v su-exec >/dev/null 2>&1; then
    exec su-exec "$APP_USER" "$@"
  fi
  # No privilege-drop tool available: fall through and run as root rather than fail.
fi

exec "$@"

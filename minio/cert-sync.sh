#!/bin/sh
# Fetch (or renew) this node's Let's Encrypt certificate and hand it to MinIO.
#
# MinIO does NOT notice a rewritten certificate on disk, but it DOES reload on
# SIGHUP -- measured: certificate serial updates, container uptime doesn't
# reset (no restart). Connections in flight during the signal were never
# observed. So: write to a staging path, compare, and only promote + signal
# when the content actually changed.
#
# usage: cert-sync.sh <cert-dir> <domain> <pid-file>
set -eu

# The private key must never be briefly world-readable. Setting the umask
# before anything is created means the FIRST `cp` of private.key below lands
# at 600 immediately -- no create-then-chmod window in this (traversable)
# directory. (An overwrite of an already-600 file would keep 600 regardless,
# but the very first write, before private.key exists, would not without
# this.)
umask 077

CERT_DIR="${1:?usage: cert-sync.sh <cert-dir> <domain> <pid-file>}"
DOMAIN="${2:?missing domain}"
PID_FILE="${3:?missing pid file}"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# Never overwrite a working certificate with a failed renewal: write to the
# staging directory first and promote only on success.
if ! tailscale cert --cert-file "$STAGE/public.crt" --key-file "$STAGE/private.key" "$DOMAIN"; then
    echo "cert-sync: tailscale cert failed for $DOMAIN; keeping existing certificate" >&2
    exit 1
fi

# Comparing ONLY public.crt to decide "nothing changed" is unsafe: promotion
# writes two files, and a crash between them leaves one new, one stale. If
# the crt is the one that landed, the crt-only check sees "already up to
# date" forever after -- the key is never re-promoted, MinIO can't start
# with a mismatched pair, and nothing about that state is self-healing.
# Comparing both files means any incomplete promotion, however it happened
# (including from a version of this script that promoted crt before key),
# is retried on the next run instead of being mistaken for "done".
if [ -f "$CERT_DIR/public.crt" ] && [ -f "$CERT_DIR/private.key" ] \
    && cmp -s "$STAGE/public.crt" "$CERT_DIR/public.crt" \
    && cmp -s "$STAGE/private.key" "$CERT_DIR/private.key"; then
    exit 0
fi

had_cert=0
[ -f "$CERT_DIR/public.crt" ] && had_cert=1

# Key first: if promotion is interrupted here, the pair is not yet
# reconciled either way, and the both-files check above will retry.
# (Crt-then-key would risk a crash landing between the two writes at the
# exact moment described above -- new crt promoted, old key not yet
# touched -- which is the ordering that produced the bug in the first
# place.)
cp "$STAGE/private.key" "$CERT_DIR/private.key"
chmod 600 "$CERT_DIR/private.key"
cp "$STAGE/public.crt" "$CERT_DIR/public.crt"
chmod 644 "$CERT_DIR/public.crt"

# On the very first write MinIO has not started yet (or is starting with this
# cert), so there is nothing to reload.
if [ "$had_cert" -eq 1 ] && [ -f "$PID_FILE" ]; then
    pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
        echo "cert-sync: certificate changed, reloading MinIO (pid $pid)"
        kill -HUP "$pid"
    fi
fi
exit 0

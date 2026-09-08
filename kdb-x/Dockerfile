# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

# KDB-X as a container, built for whatever platform you ask for.
#
# This file ships NO KX software. It is a recipe: the build downloads KDB-X
# from KX's own distribution portal at build time, so whoever runs the build
# obtains it directly from KX under their own acceptance of KX's terms. That
# is the whole point -- a kdb+ binary may not be redistributed, so this repo
# does not carry one and neither does any image published from it.
#
# The licence is NOT baked in either. Mount your own kc.lic at runtime:
#   docker run -v "$PWD/kdb-license:/licence:ro" -e QLIC=/licence ...
# Baking it in is what forced Ep. 10's bring-your-own redesign: a licence
# copied into a layer stays in that layer even if a later step deletes it.
#
# Build for one platform (loads into the local daemon):
#   docker build -t kdb-x:local kdb-x/
#
# Build a real multi-arch image and push it (buildx cannot --load two
# platforms at once, which is why this pushes):
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t ghcr.io/<you>/kdb-x:1.1 --push kdb-x/
#
# Then verify the manifest genuinely carries both -- a single-arch tag
# resolves anyway on the platform it happens to match, so a broken one looks
# fine until an amd64 host silently pulls AArch64 binaries:
#   docker buildx imagetools inspect ghcr.io/<you>/kdb-x:1.1
FROM debian:bookworm-slim

# TARGETARCH is set by buildx per platform. KX names its Linux builds by a
# different scheme, so map rather than assume: `l64` is x86-64, `l64arm` is
# AArch64. (`m64`/`m64arm` are macOS Mach-O builds and cannot run in a Linux
# container at all -- that distinction costs an afternoon if you miss it.)
ARG TARGETARCH
ARG KX_VERSION=~latest~

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl unzip tini libssl3 libcurl4; \
    case "$TARGETARCH" in \
        amd64) KXP=l64    ;; \
        arm64) KXP=l64arm ;; \
        *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://portal.dl.kx.com/assets/raw/kdb-x/kdb-x/${KX_VERSION}/${KXP}.zip" \
        -o /tmp/kx.zip; \
    unzip -q /tmp/kx.zip -d /tmp/kx; \
    # The zip holds exactly one file -- <prefix>/q, the bare executable. The
    # official installer is what builds the bin/ q/ mod/ layout around it and
    # fetches the optional modules separately; core q needs none of that, and
    # this cache uses only core q (selects, xbar, .Q.gc). Install the one
    # binary at $QHOME/bin/q, which is where the session spawns it from.
    install -D -m 0755 "/tmp/kx/${KXP}/q" /root/.kx/bin/q; \
    rm -rf /tmp/kx /tmp/kx.zip /var/lib/apt/lists/*; \
    # No licence may survive into the image -- and delete it in THIS layer,
    # not a later one, or the blob remains extractable with `docker save`.
    find /root/.kx -type f \( -name '*.lic' -o -name '*.license' \) -delete; \
    # Fail the build rather than ship a wrong-architecture q: COPY and unzip
    # never execute anything, so a mismatched binary produces a perfectly
    # green build that only fails when something tries to spawn it.
    case "$TARGETARCH" in \
        amd64) WANT=62  ;; \
        arm64) WANT=183 ;; \
    esac; \
    GOT=$(od -An -tu1 -j18 -N1 /root/.kx/bin/q | tr -d ' '); \
    [ "$GOT" = "$WANT" ] || { echo "arch mismatch: q e_machine=$GOT want=$WANT" >&2; exit 1; }; \
    echo "kdb-x: $TARGETARCH -> $KXP, e_machine=$GOT, no licence present"

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# KX_PORT default binds ALL interfaces (0.0.0.0), matching this image's
# documented use: a standalone container published with
# `-p 127.0.0.1:5000:5000` on the host. A loopback bind INSIDE the container
# is unreachable through `-p` -- docker-proxy accepts the TCP connection but
# the q IPC handshake behind it never completes, which makes that failure
# mode invisible to a plain `nc -z` check.
#
# Override to `-e KX_PORT=127.0.0.1:5000` ONLY when this container shares a
# network namespace with other services (e.g. a compose stack). In that case
# 0.0.0.0 would publish an unauthenticated q -- and q IPC executes arbitrary
# q code -- to every peer that can reach this container, so loopback there is
# not optional.
ENV QHOME=/root/.kx \
    PATH=/root/.kx/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    KX_PORT=0.0.0.0:5000

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]

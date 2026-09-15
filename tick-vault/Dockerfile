# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

# tick-vault: local-only image for the bitemporal S&P 500 tick lakehouse
# engine (backfill loop + status server). Not published anywhere -- built on
# the NAS host and shipped, if ever needed elsewhere, via
# `docker save | ssh nas docker load` (same posture as key-maint's image).
#
# Built with tick-vault/ itself as the build context (see docker-compose.yml
# `build: ./tick-vault`) -- unlike stores-explorer/live-grid, this package has
# no sibling directories to reach outside its own tree, so a root-context
# build is not needed here.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /opt/tick-vault
COPY pyproject.toml ./
COPY tick_vault/ ./tick_vault/
RUN pip install .

# No CMD/ENTRYPOINT pinned to one mode -- docker-compose.yml's `tick-vault`
# service runs the backfill loop (`vault backfill --loop`) and the
# `tick-vault-status` service runs the status server
# (`python -m tick_vault.status_app`) from the SAME image, each via its own
# compose `command:` (mirrors how a single `openbb-local` image backs both
# openbb-api and openbb-mcp in the root Dockerfile/compose).
CMD ["vault", "--help"]

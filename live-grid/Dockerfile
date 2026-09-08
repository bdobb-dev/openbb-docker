# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

# live-grid: OpenBB Workspace live_grid backend streaming EODHD websocket
# prices, plus tick recording and a unified chart over kdb+ (Ep. 10).
# Loopback-only; Tailscale Serve publishes it (see repo compose).
#
# Built with the REPO ROOT as build context (see docker-compose.yml) --
# COPY paths below are repo-root-relative, because a Dockerfile can only
# reach paths inside its build context and kdb-store/ is live-grid's sibling,
# not its descendant.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

# plotly.js is vendored: the page must render on a network with no CDN route.
ADD https://cdn.plot.ly/plotly-2.35.2.min.js /srv/app/static/plotly.min.js

# Shared kdb+ session/store plumbing (Ep. 10). Installed before live-grid
# itself, since live-grid's own pyproject.toml depends on it and it is not
# published to PyPI.
COPY kdb-store/ /srv/kdb-store/
RUN pip install /srv/kdb-store

COPY live-grid/pyproject.toml ./
COPY live-grid/app/ app/
# the kdb-ws demo chart, served at /static/live-chart.html so bdobb-v2's
# Website widget has a durable URL (context is the repo root, so the
# sibling kdb-ws/ is reachable -- same reasoning as the kdb-store COPY)
COPY kdb-ws/live-chart.html app/static/live-chart.html
COPY live-grid/widgets.json ./
COPY live-grid/macros/ macros/
RUN pip install . && chmod 644 app/static/plotly.min.js
CMD ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "6903"]

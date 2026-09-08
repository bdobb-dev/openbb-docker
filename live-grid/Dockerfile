# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

# live-grid: OpenBB Workspace live_grid backend streaming EODHD websocket
# prices. Loopback-only; Tailscale Serve publishes it (see repo compose).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY pyproject.toml ./
COPY app/ app/
COPY widgets.json ./
RUN pip install .
CMD ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "6903"]

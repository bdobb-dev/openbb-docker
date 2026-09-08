# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /opt/key-maint
COPY pyproject.toml ./
COPY app/ ./app/
RUN pip install .
CMD ["python", "-m", "app.main"]

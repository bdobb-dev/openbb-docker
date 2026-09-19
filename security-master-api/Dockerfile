# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
# security-master-api: the Security Master Browser's service (API) and its
# acquisition worker, one image. Built with the REPO ROOT as build context
# (see docker-compose.yml) because openbb-deltalake/ is a sibling, not a
# descendant, exactly like stores-explorer. Loopback-only by default; compose
# overrides the bind, Tailscale Serve is the ingress.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY openbb-deltalake/ /srv/openbb-deltalake/
RUN pip install /srv/openbb-deltalake && rm -rf /srv/openbb-deltalake

# widgets.json and apps.json are PACKAGE DATA now (security_master_api/*.json,
# see pyproject.toml's [tool.setuptools.package-data]) -- `pip install .` ships
# them inside the package, so there is no separate COPY for them here; app/
# main.py reads them from HERE, which resolves to the installed package dir.
COPY security-master-api/pyproject.toml ./
COPY security-master-api/security_master_api/ security_master_api/
RUN pip install .

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
# --no-access-log: the compose command carries it and the image must not be quieter only
# there -- a per-request access line logs the query string, which is where the SSE
# routes carry their credential.
CMD ["uvicorn", "security_master_api.app.main:app", "--host", "127.0.0.1", "--port", "6905", "--no-access-log"]

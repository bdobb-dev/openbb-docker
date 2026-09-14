# stores-explorer: read-only widget backend browsing the shared ArcticDB/kdb+
# store (Ep. 11) -- the widget's door into the vault, pairing with
# stores-mcp (the analyst's door). Loopback-only; Tailscale Serve publishes
# it (see repo compose).
#
# Built with the REPO ROOT as build context (see docker-compose.yml) --
# COPY paths below are repo-root-relative, because a Dockerfile can only
# reach paths inside its build context and mcp_stores/ is stores-explorer's
# sibling, not its descendant (same reasoning as live-grid's kdb-store COPY).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

# mcp_stores's discovery functions (arctic_list_libraries, arctic_read,
# kdb_select, ...) -- installed before stores-explorer itself, since
# stores-explorer's own pyproject.toml depends on it and it is not
# published to PyPI. Installs as a bare top-level `server` module (its
# pyproject.toml declares py-modules = ["server"]), which app/main.py
# imports from directly.
COPY mcp_stores/ /srv/mcp_stores/
RUN pip install /srv/mcp_stores

COPY stores-explorer/pyproject.toml ./
COPY stores-explorer/app/ app/
COPY stores-explorer/widgets.json ./
RUN pip install .

# Same reusable entrypoint openbb-local (stores-mcp's own image) already
# runs, for the same reason: network_mode: service:tailscale shares the
# NETWORK namespace but not the filesystem, so MagicDNS names (like the
# MinIO node's) don't resolve in here even though every other tailnet
# client resolves them fine. HOSTS_ALIAS pins a name -> IP into /etc/hosts
# before exec, without needing compose extra_hosts (rejected under
# network_mode: service:*). Optional -- a no-op when HOSTS_ALIAS is unset.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "6904"]

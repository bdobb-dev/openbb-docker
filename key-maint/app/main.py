"""Entry point: one process, two uvicorn servers. admin = unix socket
(bind-mounted from a 0700 NAS-admin-owned host dir; reachable only via an SSH
session as that user), 8447 = network (proxied by Tailscale Serve on :10000,
funneled)."""
from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from app.server import create_app

NETWORK_PORT = 8447

# uvicorn's own dictConfig only wires handlers for its own "uvicorn.*"
# loggers (disable_existing_loggers=False leaves this alone), so app.audit
# needs its own handler to reach `docker logs` at all.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")


def build_servers(
    cred_file: str, auth_file: str, admin_socket: str, host: str = "127.0.0.1"
) -> list[uvicorn.Server]:
    admin_cfg = uvicorn.Config(
        create_app(role="admin", cred_file=cred_file, auth_file=auth_file),
        uds=admin_socket,
        log_level="info",
    )
    network_cfg = uvicorn.Config(
        create_app(role="network", cred_file=cred_file, auth_file=auth_file),
        host=host,
        port=NETWORK_PORT,
        log_level="info",
    )
    return [uvicorn.Server(admin_cfg), uvicorn.Server(network_cfg)]


async def _chmod_admin_socket(path: str) -> None:
    """uvicorn creates the uds file during server startup, not before, so poll
    for it rather than chmod-ing a path that doesn't exist yet. Directory
    permissions (0700, admin-owned on the host) are the real access control;
    this just lets the admin user's own connections through the mode bits."""
    for _ in range(50):  # ~5s at 0.1s intervals
        if os.path.exists(path):
            os.chmod(path, 0o666)
            return
        await asyncio.sleep(0.1)


async def _serve_all() -> None:
    cred = os.environ.get("KEYMAINT_CRED_FILE", "/config/credentials.env")
    auth = os.environ.get("KEYMAINT_AUTH_FILE", "/config/api-auth.env")
    admin_socket = os.environ.get(
        "KEYMAINT_ADMIN_SOCKET", "/config/admin/key-maint-admin.sock"
    )
    host = os.environ.get("KEY_MAINT_HOST", "127.0.0.1")

    os.makedirs(os.path.dirname(admin_socket), exist_ok=True)
    if os.path.exists(admin_socket):
        os.unlink(admin_socket)

    servers = build_servers(cred, auth, admin_socket, host)
    await asyncio.gather(
        _chmod_admin_socket(admin_socket),
        *(s.serve() for s in servers),
    )


if __name__ == "__main__":
    asyncio.run(_serve_all())

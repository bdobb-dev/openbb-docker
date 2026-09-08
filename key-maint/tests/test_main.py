# Copyright 2026 Arthur D. Cashin III. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0

from app.main import build_servers


class TestBuildServers:
    def test_admin_uds_and_network_loopback_bind(self, tmp_path):
        admin_socket = str(tmp_path / "admin.sock")
        servers = build_servers(
            str(tmp_path / "c.env"), str(tmp_path / "a.env"), admin_socket
        )
        admin, network = servers
        # Order is the contract: index 0 admin, index 1 network.
        # uvicorn.Config.uds takes priority over host/port at bind time
        # (uvicorn.Server.startup branches on config.uds is not None before
        # ever consulting host/port), so a set .uds is what "no host binding"
        # means here -- the admin server never binds a TCP socket.
        assert admin.config.uds == admin_socket
        assert network.config.uds is None
        assert (network.config.host, network.config.port) == ("127.0.0.1", 8447)

    def test_network_host_defaults_to_loopback(self, tmp_path, monkeypatch):
        """The default must stay loopback so an image or compose file that
        forgets KEYMAINT_NETWORK_HOST fails CLOSED -- unreachable -- rather than
        binding every interface it can find."""
        monkeypatch.delenv("KEYMAINT_NETWORK_HOST", raising=False)
        _, network = build_servers(
            str(tmp_path / "c.env"), str(tmp_path / "a.env"), str(tmp_path / "a.sock")
        )
        assert network.config.host == "127.0.0.1"

    def test_network_host_is_settable_from_the_environment(self, tmp_path, monkeypatch):
        """key-maint is the only service whose bind is in Python rather than
        compose, so it needs an env knob to join the openbb-internal bridge.
        compose sets this to 0.0.0.0; on the bridge that is the whole interface
        list of a private network, not the LAN."""
        monkeypatch.setenv("KEYMAINT_NETWORK_HOST", "0.0.0.0")
        _, network = build_servers(
            str(tmp_path / "c.env"), str(tmp_path / "a.env"), str(tmp_path / "a.sock")
        )
        assert network.config.host == "0.0.0.0"

    def test_the_admin_socket_is_unaffected_by_the_network_host(self, tmp_path, monkeypatch):
        """The admin server binds a unix socket, not TCP. It must stay off the
        bridge entirely -- the 0700 host directory IS its authorization."""
        monkeypatch.setenv("KEYMAINT_NETWORK_HOST", "0.0.0.0")
        admin, _ = build_servers(
            str(tmp_path / "c.env"), str(tmp_path / "a.env"), str(tmp_path / "a.sock")
        )
        assert admin.config.uds == str(tmp_path / "a.sock")
        assert admin.config.host in (None, "127.0.0.1")

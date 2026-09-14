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

    def test_network_host_is_overridable(self, tmp_path):
        # KEY_MAINT_HOST (main.py's _serve_all) needs the network server's
        # bind address to be a parameter, not a hardcoded "127.0.0.1" --
        # the bridge-network migration sets this to "0.0.0.0" once it lands.
        admin_socket = str(tmp_path / "admin.sock")
        _, network = build_servers(
            str(tmp_path / "c.env"), str(tmp_path / "a.env"), admin_socket, "0.0.0.0"
        )
        assert network.config.host == "0.0.0.0"

# Copyright 2026 SecretoftheUniverse.com LLC. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""A scriptable stand-in for openbb-api: status, body and delay per path."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


class StubOpenbb:
    def __init__(self):
        self.routes: dict[str, tuple[int, object, dict]] = {}
        self.requests: list[tuple[str, dict]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                stub.requests.append(
                    (parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}))
                # The worker holds ONE credential pair and must present it: an openbb-api that
                # answered an unauthenticated GET would let a broken client look healthy here
                # and fail only in the stack.
                if self.headers.get("Authorization", "")[:6] != "Basic ":
                    self.send_response(401)
                    self.end_headers()
                    return
                status, body, headers = stub.routes.get(parsed.path,
                                                        (404, {"detail": "no route"}, {}))
                payload = body if isinstance(body, bytes | str) else json.dumps(body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(payload.encode() if isinstance(payload, str) else payload)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

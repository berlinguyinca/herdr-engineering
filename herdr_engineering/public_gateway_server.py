"""Minimal isolated HTTP delivery process for approved artifact snapshots."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class PublicGatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, gateway, *, host: str = "127.0.0.1", port: int = 0,
                 guest_identity=None):
        self.gateway = gateway
        self.guest_identity = guest_identity
        super().__init__((host, port), self._handler())

    def _handler(self):
        gateway = self.gateway

        class Handler(BaseHTTPRequestHandler):
            server_version = "HerdrPublicGateway/0.1"

            def log_message(self, fmt, *args):
                return

            def _serve(self):
                if self.path == "/healthz" and self.command == "GET":
                    payload = json.dumps({"status": "ok"}, separators=(",", ":")).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                parts = self.path.split("/", 3)
                if len(parts) < 3 or parts[1] != "s" or not parts[2]:
                    self.send_error(404, "not found")
                    return
                response = gateway.request(
                    parts[2], method=self.command,
                    path="/" + (parts[3] if len(parts) == 4 else ""),
                    guest_id=(self.server.guest_identity(self)
                              if self.server.guest_identity else None),
                )
                self.send_response(response.status)
                for name, value in (response.headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                if self.command != "HEAD" and response.body:
                    self.wfile.write(response.body)

            def do_GET(self):  # noqa: N802
                self._serve()

            def do_HEAD(self):  # noqa: N802
                self._serve()

            def do_POST(self):  # noqa: N802
                self._serve()

        return Handler


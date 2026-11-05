"""Test-only support for the Slack front door (lives in tests/, NOT in the package).

There is no fake Slack client.  ``SlackListener`` is a real threaded HTTP server on 127.0.0.1 (port 0) speaking the
documented Slack Web API shape: ``auth.test`` for the good bot token and ``apps.connections.open`` for the good app
token, each answering ``{"ok": true, ...}``; anything else gets ``{"ok": false, "error": "invalid_auth"}``.  Raw requests
are recorded so tests can assert on what crossed the wire.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class SlackListener:
    def __init__(self, bot_token: str, app_token: str, *, team: str = "Acme", bot_user_id: str = "U0BOT0001",
                 on_request: Callable[[str], None] | None = None) -> None:
        self.on_request = on_request          # runs while a call is in flight (lets tests race the filesystem)
        self.bot_token = bot_token
        self.app_token = app_token
        self.team = team
        self.bot_user_id = bot_user_id
        self.requests: list[dict[str, str]] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:      # silence stderr
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                auth = self.headers.get("Authorization", "")
                method = self.path.rsplit("/", 1)[-1]
                with outer._lock:
                    outer.requests.append({"path": self.path, "authorization": auth})
                if outer.on_request is not None:
                    outer.on_request(method)
                doc: dict[str, object] = {"ok": False, "error": "invalid_auth"}
                if method == "auth.test" and auth == f"Bearer {outer.bot_token}":
                    doc = {"ok": True, "url": "https://acme.slack.com/", "team": outer.team, "user": "herdr",
                           "team_id": "T0TEAM001", "user_id": outer.bot_user_id, "bot_id": "B0BOT0001"}
                elif method == "apps.connections.open" and auth == f"Bearer {outer.app_token}":
                    doc = {"ok": True, "url": "wss://wss-primary.slack.com/link/?ticket=x"}
                body = json.dumps(doc).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._srv.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}/api"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

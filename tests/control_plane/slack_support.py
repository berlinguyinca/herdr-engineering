"""Test-only support for the Slack front door (lives in tests/, NOT in the package).

There is no fake Slack client.  ``SlackListener`` is a real threaded HTTP server on 127.0.0.1 (port 0) speaking the
documented Slack Web API shape: ``auth.test`` for the good bot token and ``apps.connections.open`` for the good app
token, each answering ``{"ok": true, ...}``; anything else gets ``{"ok": false, "error": "invalid_auth"}``.  Raw requests
are recorded so tests can assert on what crossed the wire.

It also serves the rest of what the runtime needs: ``chat.postMessage``, ``chat.update`` and ``conversations.open`` (bot
token) and a real RFC 6455 WebSocket endpoint that speaks Socket Mode (``hello``, then ``interactive`` envelopes pushed by
``push_interactive`` and acknowledged by the client).  ``apps.connections.open`` returns a ``ws://`` URL pointing back at
this listener; slack_sdk's built-in client connects in plain text to any port other than 443, so no TLS is needed here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import queue
import socket
import struct
import threading
import time
import urllib.parse
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
        self.bodies: list[tuple[str, bytes]] = []     # (method, raw request body) of every POST
        self.fail_posts: list[str] = []               # next chat.postMessage answers: '500' or 'reject' (consumed in order)
        self.calls: list[dict[str, object]] = []      # parsed bodies of chat.postMessage / chat.update / conversations.open
        self.acks: list[str] = []                     # envelope ids the client acknowledged over the socket
        self.ws_connections = 0
        self.ws_alive = 0
        self._outbox: queue.Queue[str | None] = queue.Queue()
        self._ts = 1700000000
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:      # silence stderr
                pass

            def do_GET(self) -> None:
                if self.headers.get("Upgrade", "").lower() != "websocket":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                outer._serve_websocket(self)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                auth = self.headers.get("Authorization", "")
                method = self.path.rsplit("/", 1)[-1]
                with outer._lock:
                    outer.requests.append({"path": self.path, "authorization": auth})
                    outer.bodies.append((method, raw))
                    failure = outer.fail_posts.pop(0) if method == "chat.postMessage" and outer.fail_posts else ""
                if outer.on_request is not None:
                    outer.on_request(method)
                if failure == "500":
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                doc: dict[str, object] = {"ok": False, "error": "invalid_auth"}
                if failure == "reject":
                    doc = {"ok": False, "error": "channel_not_found"}
                elif method == "auth.test" and auth == f"Bearer {outer.bot_token}":
                    doc = {"ok": True, "url": "https://acme.slack.com/", "team": outer.team, "user": "herdr",
                           "team_id": "T0TEAM001", "user_id": outer.bot_user_id, "bot_id": "B0BOT0001"}
                elif method == "apps.connections.open" and auth == f"Bearer {outer.app_token}":
                    doc = {"ok": True, "url": f"ws://127.0.0.1:{outer.port}/link/?ticket=x"}
                elif method in ("chat.postMessage", "chat.update", "conversations.open") \
                        and auth == f"Bearer {outer.bot_token}":
                    doc = outer._web_call(method, raw, self.headers.get("Content-Type", ""))
                body = json.dumps(doc).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._srv.daemon_threads = True
        self.port = int(self._srv.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}/api"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    # -- Web API ---------------------------------------------------------------------------------
    def _web_call(self, method: str, raw: bytes, content_type: str) -> dict[str, object]:
        if "json" in content_type:
            args = json.loads(raw or b"{}")
        else:
            args = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
            for key in ("blocks",):
                if key in args:
                    args[key] = json.loads(str(args[key]))
        with self._lock:
            self.calls.append({"method": method, **args})
            if method == "conversations.open":
                return {"ok": True, "channel": {"id": "D0DM" + str(args.get("users", "x"))[-5:]}}
            if method == "chat.postMessage":
                self._ts += 1
                return {"ok": True, "channel": args.get("channel"), "ts": f"{self._ts}.000100"}
            return {"ok": True, "channel": args.get("channel"), "ts": args.get("ts")}

    def calls_to(self, method: str) -> list[dict[str, object]]:
        with self._lock:
            return [c for c in self.calls if c["method"] == method]

    # -- Socket Mode over a real WebSocket ------------------------------------------------------
    def push_interactive(self, payload: dict[str, object], envelope_id: str | None = None) -> str:
        envelope_id = envelope_id or f"env-{time.time_ns()}"
        self._outbox.put(json.dumps({"envelope_id": envelope_id, "type": "interactive", "payload": payload,
                                     "accepts_response_payload": False}))
        return envelope_id

    def drop_socket(self) -> None:
        """Close the current WebSocket from the server side without a close frame (a network drop)."""
        self._outbox.put(None)

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    @staticmethod
    def _frame(opcode: int, payload: bytes) -> bytes:
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            return head + bytes([n]) + payload
        if n < 65536:
            return head + bytes([126]) + struct.pack("!H", n) + payload
        return head + bytes([127]) + struct.pack("!Q", n) + payload

    @staticmethod
    def _parse_frame(buf: bytearray) -> tuple[int, bytes] | None:
        if len(buf) < 2:
            return None
        opcode, n, i = buf[0] & 0x0F, buf[1] & 0x7F, 2
        masked = bool(buf[1] & 0x80)
        if n == 126:
            if len(buf) < 4:
                return None
            n, i = struct.unpack("!H", bytes(buf[2:4]))[0], 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n, i = struct.unpack("!Q", bytes(buf[2:10]))[0], 10
        mask = bytes(buf[i:i + 4]) if masked else b""
        i += 4 if masked else 0
        if len(buf) < i + n:
            return None
        data = bytes(buf[i:i + n])
        if masked:
            data = bytes(b ^ mask[k % 4] for k, b in enumerate(data))
        del buf[:i + n]
        return opcode, data

    def _serve_websocket(self, handler: BaseHTTPRequestHandler) -> None:
        key = handler.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        sock: socket.socket = handler.connection
        sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        with self._lock:
            self.ws_connections += 1
            self.ws_alive += 1
        sock.settimeout(0.05)
        buf = bytearray()
        try:
            sock.sendall(self._frame(1, json.dumps({"type": "hello", "num_connections": 1,
                                                    "debug_info": {"host": "local"}}).encode()))
            while True:
                try:
                    msg = self._outbox.get_nowait()
                except queue.Empty:
                    msg = ""
                if msg is None:
                    sock.shutdown(socket.SHUT_RDWR)
                    return
                if msg:
                    sock.sendall(self._frame(1, msg.encode()))
                try:
                    chunk = sock.recv(4096)
                except TimeoutError:
                    chunk = None
                if chunk == b"":
                    return
                if chunk:
                    buf.extend(chunk)
                while (frame := self._parse_frame(buf)) is not None:
                    opcode, data = frame
                    if opcode == 8:
                        return
                    if opcode == 9:
                        sock.sendall(self._frame(10, data))
                    elif opcode == 1:
                        try:
                            envelope = json.loads(data)
                        except ValueError:
                            continue
                        if isinstance(envelope, dict) and envelope.get("envelope_id"):
                            with self._lock:
                                self.acks.append(str(envelope["envelope_id"]))
        except OSError:
            return
        finally:
            with self._lock:
                self.ws_alive -= 1
            handler.close_connection = True

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

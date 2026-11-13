"""The gateway always sets X-Forwarded-For to the real peer: a client-supplied copy never reaches the upstream.

Real sockets end to end: client -> GatewayRouter (real listener) -> upstream that records the request head."""
from __future__ import annotations

import socket
import threading

import pytest

from herdr_engineering.fabric import GatewayRouter


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def upstream():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    heads: list[bytes] = []

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            buf = b""
            conn.settimeout(3)
            try:
                while b"\r\n\r\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                heads.append(buf)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            except OSError:
                pass
            finally:
                conn.close()

    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1], heads
    srv.close()


def _through_gateway(up_port, raw: bytes, rewrite: str | None = "gw.example", binder_kwargs=None) -> bytes:
    router = GatewayRouter(["127.0.0.1"])
    port = _free_port()
    assert router.bind(port, "127.0.0.1", up_port, None, rewrite)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.sendall(raw)
            c.shutdown(socket.SHUT_WR)
            out = b""
            try:
                while chunk := c.recv(65536):
                    out += chunk
            except OSError:
                pass
            return out
    finally:
        router.unbind(port) if hasattr(router, "unbind") else None


def _xff(head: bytes) -> list[str]:
    return [ln.split(b":", 1)[1].strip().decode() for ln in head.split(b"\r\n")[1:] if ln.lower().startswith(b"x-forwarded-for:")]


@pytest.mark.parametrize("forged", ["100.104.39.99", "100.64.1.1, 100.64.9.9", "GaRbAgE", ""])
def test_plaintext_client_forwarded_for_is_replaced_by_the_real_peer(upstream, forged):
    up_port, heads = upstream
    raw = f"POST /api/auth/open HTTP/1.1\r\nHost: gw.example\r\nOrigin: rakazo://\r\nX-Forwarded-For: {forged}\r\nx-forwarded-for: 1.2.3.4\r\nContent-Length: 0\r\n\r\n".encode()
    assert b"200" in _through_gateway(up_port, raw)
    assert _xff(heads[-1]) == ["127.0.0.1"]


def test_plaintext_request_without_a_forwarded_for_gets_the_real_peer(upstream):
    up_port, heads = upstream
    _through_gateway(up_port, b"GET / HTTP/1.1\r\nHost: gw.example\r\n\r\n")
    assert _xff(heads[-1]) == ["127.0.0.1"]


def test_an_incomplete_head_is_never_relayed_with_a_client_forwarded_for(upstream):
    up_port, heads = upstream
    before = len(heads)
    _through_gateway(up_port, b"GET / HTTP/1.1\r\nX-Forwarded-For: 100.64.1.1\r\n")      # no blank line: head never completes
    assert all(b"100.64.1.1" not in h for h in heads[before:])    # the upstream is at most connected to, never sent the head

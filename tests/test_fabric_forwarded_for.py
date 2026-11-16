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


def _through_gateway(up_port, raw: bytes, rewrite: str | None = "gw.example") -> bytes:
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
        router.unbind(port)


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


# ---- upgrade bypass, route_and_pipe and proxy_pair ---------------------------------------------
import time

from herdr_engineering.contracts import Route  # noqa: E402
from herdr_engineering.devfabric import proxy_pair, route_and_pipe  # noqa: E402


@pytest.fixture
def keepalive_upstream():
    """Answers each connection's first head with 200 keep-alive (or 101 when asked) and records every byte received."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    received: list[bytearray] = []
    mode = {"reply": b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"}

    def handle(conn: socket.socket) -> None:
        data = bytearray()
        received.append(data)
        conn.settimeout(2)
        answered = False
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if not answered and b"\r\n\r\n" in data:
                    answered = True
                    conn.sendall(mode["reply"])
        except OSError:
            pass
        finally:
            conn.close()

    def serve() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1], received, mode
    srv.close()


def _drain(c: socket.socket, seconds: float = 1.5) -> bytes:
    out = b""
    c.settimeout(seconds)
    try:
        while chunk := c.recv(65536):
            out += chunk
    except OSError:
        pass
    return out


def test_non_101_upgrade_reply_never_lets_a_second_request_bypass_the_header_rewrite(keepalive_upstream):
    up_port, received, _ = keepalive_upstream
    router = GatewayRouter(["127.0.0.1"])
    port = _free_port()
    assert router.bind(port, "127.0.0.1", up_port, None, "gw.example")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.sendall(b"GET /ws HTTP/1.1\r\nHost: gw.example\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
            first = c.recv(65536)
            assert b"200" in first
            time.sleep(0.3)
            try:
                c.sendall(b"GET /api/auth/open HTTP/1.1\r\nHost: gw.example\r\nX-Forwarded-For: 100.104.39.99\r\n\r\n")
            except OSError:
                pass                    # the gateway already closed the connection: also correct
            _drain(c)
    finally:
        router.unbind(port)
    time.sleep(0.3)
    assert all(b"100.104.39.99" not in bytes(r) for r in received)
    assert all(bytes(r).count(b"HTTP/1.1") == 1 for r in received)


def test_a_101_upgrade_still_becomes_a_raw_pipe(keepalive_upstream):
    up_port, received, mode = keepalive_upstream
    mode["reply"] = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
    router = GatewayRouter(["127.0.0.1"])
    port = _free_port()
    assert router.bind(port, "127.0.0.1", up_port, None, "gw.example")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.sendall(b"GET /ws HTTP/1.1\r\nHost: gw.example\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
            assert b"101" in c.recv(65536)
            c.sendall(b"frame-bytes")
            time.sleep(0.5)
    finally:
        router.unbind(port)
    assert any(b"frame-bytes" in bytes(r) for r in received)


# ---- route_and_pipe and proxy_pair ---------------------------------------------
def _xff_in(received) -> list[str]:
    return [v for r in received for v in _xff(bytes(r))]


def test_route_and_pipe_replaces_a_client_forwarded_for(keepalive_upstream):
    up_port, received, _ = keepalive_upstream
    lsn = socket.socket()
    lsn.bind(("127.0.0.1", 0))
    lsn.listen(1)
    t = threading.Thread(target=lambda: route_and_pipe(lsn.accept()[0], [Route("/api", "127.0.0.1", up_port)],
                                                       "127.0.0.1", up_port, forwarded_for="100.64.0.7"), daemon=True)
    t.start()
    with socket.create_connection(lsn.getsockname(), timeout=5) as c:
        c.sendall(b"GET /api/x HTTP/1.1\r\nHost: h\r\nX-Forwarded-For: 9.9.9.9\r\n\r\n")
        c.shutdown(socket.SHUT_WR)
        assert b"200" in _drain(c)
    lsn.close()
    assert _xff_in(received) == ["100.64.0.7"]


def test_proxy_pair_replaces_a_client_forwarded_for_and_passes_non_http_untouched(keepalive_upstream):
    up_port, received, _ = keepalive_upstream
    lsn = socket.socket()
    lsn.bind(("127.0.0.1", 0))
    lsn.listen(2)

    def serve_once() -> None:
        cl = lsn.accept()[0]
        proxy_pair(cl, socket.create_connection(("127.0.0.1", up_port)), forwarded_for="100.64.0.7")

    threading.Thread(target=serve_once, daemon=True).start()
    with socket.create_connection(lsn.getsockname(), timeout=5) as c:
        c.sendall(b"GET / HTTP/1.1\r\nHost: h\r\nX-Forwarded-For: 9.9.9.9\r\n\r\n")
        c.shutdown(socket.SHUT_WR)
        assert b"200" in _drain(c)
    assert _xff_in(received) == ["100.64.0.7"]

    threading.Thread(target=serve_once, daemon=True).start()
    with socket.create_connection(lsn.getsockname(), timeout=5) as c:
        c.sendall(b"\x00\x01binary\r\n\r\n")
        c.shutdown(socket.SHUT_WR)
        _drain(c)
    lsn.close()
    assert any(bytes(r).startswith(b"\x00\x01binary") for r in received)


def test_gateway_routes_and_plain_pipe_overwrite_forwarded_for_end_to_end(keepalive_upstream):
    up_port, received, _ = keepalive_upstream
    for routes in (None, [Route("/", "127.0.0.1", up_port)]):
        router = GatewayRouter(["127.0.0.1"])
        port = _free_port()
        assert router.bind(port, "127.0.0.1", up_port, routes, None)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
                c.sendall(b"GET / HTTP/1.1\r\nHost: h\r\nX-Forwarded-For: 9.9.9.9\r\n\r\n")
                c.shutdown(socket.SHUT_WR)
                _drain(c)
        finally:
            router.unbind(port)
    time.sleep(0.2)
    assert _xff_in(received) == ["127.0.0.1", "127.0.0.1"]

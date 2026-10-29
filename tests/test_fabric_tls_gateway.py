"""HTTPS on the same port as HTTP through the fabric router (spec D4)."""
import http.server
import socket
import ssl
import threading
import time

import pytest

from herdr_engineering.fabric_tls import PrivateCA
from tests.test_fabric import _client, _gateway


class Origin(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Origin.seen.append(dict(self.headers))
        if self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_address[1]}/landed")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(3):
                self.wfile.write(f"data: {i}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.05)
            return
        body = b"hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def ca(tmp_path_factory):
    ca = PrivateCA(tmp_path_factory.mktemp("ca"))
    ca.init(["dev.lan"])
    return ca


@pytest.fixture
def stack(tmp_path, ca):
    Origin.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    gw, _, router = _gateway(tmp_path, url_base="dev.lan")
    cert, key = ca.issue(["dev.lan", "*.dev.lan"], basename="gw")
    gw.enable_tls(ca.server_context(cert, key))
    lease = _client(gw).register(target_machine_id="bender", target_host="127.0.0.1",
                                 target_port=server.server_address[1], label="web")["lease"]
    yield gw, lease["external_port"], server.server_address[1], ca
    gw.stop()
    server.shutdown()


def _https(port, ca, request: bytes):
    ctx = ssl.create_default_context(cafile=str(ca.cert_path))
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw, \
            ctx.wrap_socket(raw, server_hostname="dev.lan") as tls:
        tls.sendall(request)
        out = b""
        while chunk := tls.recv(65536):
            out += chunk
        return out


def test_https_and_http_share_the_port(stack):
    gw, port, _, ca = stack
    assert gw.stable_url(port) == f"https://dev.lan:{port}"
    assert _https(port, ca, b"GET / HTTP/1.1\r\nHost: dev.lan\r\nConnection: close\r\n\r\n").endswith(b"hello")
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"GET / HTTP/1.1\r\nHost: dev.lan\r\nConnection: close\r\n\r\n")
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    assert data.endswith(b"hello")  # plain http://dev.lan:<port> keeps working


def test_origin_sees_forwarded_proto_and_client_cannot_spoof_it(stack):
    _, port, _, ca = stack
    _https(port, ca, b"GET / HTTP/1.1\r\nHost: dev.lan:1\r\nX-Forwarded-Proto: http\r\nX-Forwarded-For: 6.6.6.6\r\n\r\n")
    h = Origin.seen[-1]
    assert h["X-Forwarded-Proto"] == "https" and h["X-Forwarded-Host"] == "dev.lan:1"
    assert h["X-Forwarded-For"] == "127.0.0.1"


def test_redirect_location_is_rewritten_to_https_fabric_url(stack):
    _, port, _, ca = stack
    out = _https(port, ca, b"GET /redir HTTP/1.1\r\nHost: dev.lan\r\n\r\n")
    assert f"Location: https://dev.lan:{port}/landed".encode() in out


def test_sse_streams_over_tls(stack):
    _, port, _, ca = stack
    out = _https(port, ca, b"GET /sse HTTP/1.1\r\nHost: dev.lan\r\n\r\n")
    assert b"data: 0" in out and b"data: 2" in out


def test_websocket_upgrade_passes_through_tls(stack):
    _, port, _, ca = stack
    out = _https(port, ca, b"GET /ws HTTP/1.1\r\nHost: dev.lan\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
    assert out.startswith(b"HTTP/1.")  # the origin answered the upgrade request through the tunnel
    assert Origin.seen[-1]["X-Forwarded-Proto"] == "https"


def test_untrusted_client_cannot_complete_handshake_and_no_plaintext_fallback(stack):
    _, port, _, _ = stack
    with pytest.raises(ssl.SSLCertVerificationError), socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        ssl.create_default_context().wrap_socket(raw, server_hostname="dev.lan")


def test_garbage_tls_prefix_is_dropped(stack):
    """A failed handshake closes the connection (reset or EOF/alert); the origin never sees it."""
    _, port, _, _ = stack
    before = len(Origin.seen)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"\x16\x03\x01garbage-not-a-client-hello" + b"\x00" * 40)
        s.settimeout(5)
        try:
            data = s.recv(100)
        except ConnectionResetError:
            data = b""
        assert not data.startswith(b"HTTP/")
    assert len(Origin.seen) == before


def test_disable_tls_reverts_to_http(stack):
    gw, port, _, _ = stack
    gw.disable_tls()
    assert gw.stable_url(port).startswith("http://")

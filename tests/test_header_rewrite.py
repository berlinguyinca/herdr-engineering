import http.server
import socket
import threading

from herdr_engineering.devfabric import DevServiceRegistry
from herdr_engineering.fabric import FabricGateway, GatewayRouter
from herdr_engineering.urlrewrite import force_close, rewrite_response_head


def test_head_rewrite_touches_only_loopback_headers():
    head = (b"HTTP/1.1 302 Found\r\nLocation: http://localhost:3000/a?x=1\r\n"
            b"Access-Control-Allow-Origin: http://127.0.0.1:3000\r\n"
            b"Set-Cookie: s=1; Domain=localhost; Path=/\r\nX-Other: http://localhost:3000\r\n"
            b"Content-Length: 5")
    out = rewrite_response_head(head, 3000, "dev.lan:18010").decode()
    assert "Location: http://dev.lan:18010/a?x=1" in out
    assert "Access-Control-Allow-Origin: http://dev.lan:18010" in out
    assert "Domain=dev.lan" in out
    assert "X-Other: http://localhost:3000" in out  # unlisted headers untouched
    assert "Content-Length: 5" in out
    # another port is not ours to rewrite
    assert rewrite_response_head(b"HTTP/1.1 302 x\r\nLocation: http://localhost:3001/", 3000, "d:1") \
        == b"HTTP/1.1 302 x\r\nLocation: http://localhost:3001/"


def test_force_close_replaces_connection_header():
    out = force_close(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: keep-alive")
    assert b"Connection: close" in out and b"keep-alive" not in out


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_gateway_rewrites_redirect_when_opted_in(tmp_path):
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            seen["conn"] = self.headers.get("Connection")
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_address[1]}/next")
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = _free_port()
    results = {}
    for flag in (False, True):
        reg = DevServiceRegistry(store_path=tmp_path / f"l{flag}.json", port_range=(base, base + 20),
                                 lease_ttl_seconds=5)
        gw = FabricGateway(reg, GatewayRouter(["127.0.0.1"]), control_host="127.0.0.1", control_port=0,
                           url_base="dev.test", probe_interval=0.5, token=None, rewrite_headers=flag)
        gw.start()
        try:
            lease = gw.op_register({"target_machine_id": "m", "target_host": "127.0.0.1",
                                    "target_port": srv.server_address[1], "label": "x"})["lease"]
            c = socket.create_connection(("127.0.0.1", lease["external_port"]), timeout=5)
            c.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n\r\n")
            data = b""
            while True:
                chunk = c.recv(4096)
                if not chunk:
                    break
                data += chunk
            c.close()
            results[flag] = (data.decode(), lease["external_port"])
        finally:
            gw.stop()
    assert "Location: http://localhost:" in results[False][0]
    assert f"Location: http://dev.test:{results[True][1]}/next" in results[True][0]
    assert seen["conn"] == "close"


def test_scheme_pins_https_for_tls_terminated_leases():
    head = b"HTTP/1.1 302 Found\r\nLocation: http://localhost:3000/x\r\nContent-Length: 0"
    assert b"Location: https://dev.lan:3000/x" in rewrite_response_head(head, 3000, "dev.lan:3000", "https")
    assert b"Location: http://dev.lan:3000/x" in rewrite_response_head(head, 3000, "dev.lan:3000")


def test_inject_request_headers_replaces_client_supplied_forwarding_headers():
    from herdr_engineering.urlrewrite import inject_request_headers
    head = b"GET / HTTP/1.1\r\nHost: a\r\nx-forwarded-proto: http\r\nX-Forwarded-For: 6.6.6.6"
    out = inject_request_headers(head, {"X-Forwarded-Proto": "https", "X-Forwarded-For": "1.2.3.4"})
    assert out.lower().count(b"x-forwarded-proto") == 1 and b"6.6.6.6" not in out
    assert b"X-Forwarded-Proto: https" in out and b"X-Forwarded-For: 1.2.3.4" in out

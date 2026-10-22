# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test module)
"""The egress proxy a helper's sandbox talks through, and the sandbox's no-network mode. Real proxy subprocess,
real local TCP/TLS servers standing in for upstreams, real bubblewrap when it is installed."""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from tests.control_plane.test_collab_sandbox import home, needs_box  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
EGRESS = ROOT / "deploy/control-plane/collab-egress.py"
SANDBOX = ROOT / "deploy/control-plane/collab-sandbox.sh"
SECRET = "sk-TESTSECRET0123456789"


def load_egress():
    spec = importlib.util.spec_from_file_location("collab_egress", EGRESS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Echo:
    """A TCP echo server that counts the connections it accepted."""

    def __init__(self, host="127.0.0.1"):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._echo, args=(c,), daemon=True).start()

    @staticmethod
    def _echo(c):
        with c:
            while data := c.recv(65536):
                c.sendall(data)

    def close(self):
        self.sock.close()


@pytest.fixture
def short_dir():
    d = Path(tempfile.mkdtemp(prefix="eg", dir="/tmp"))     # AF_UNIX paths are capped at 108 bytes
    yield d
    for p in sorted(d.rglob("*"), reverse=True):
        p.rmdir() if p.is_dir() and not p.is_symlink() else p.unlink()
    d.rmdir()


class Proxy:
    def __init__(self, d: Path, env=None):
        self.dir = d
        self.sock = d / "egress.sock"
        self.allow = d / "allow.txt"
        self.secrets = d / "secrets.json"
        self.services = d / "services.json"
        self.log = d / "blocked.jsonl"
        self.allow.write_text("")
        self.secrets.write_text("{}")
        self.services.write_text("[]")
        self.err = open(d / "proxy.err", "w+")
        self.proc = subprocess.Popen(
            [sys.executable, str(EGRESS), "serve", "--sock", str(self.sock), "--allow-file", str(self.allow),
             "--secrets-file", str(self.secrets), "--services-file", str(self.services), "--log", str(self.log)],
            stdout=self.err, stderr=self.err, env={**os.environ, **(env or {})})
        for _ in range(100):
            if self.sock.exists():
                break
            time.sleep(0.05)
        assert self.sock.exists(), "proxy did not start"

    def connect(self, target: str):
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(10)
        s.connect(str(self.sock))
        s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(1)
            if not chunk:
                break
            head += chunk
        status = int(head.split(b" ")[1]) if head.startswith(b"HTTP/1.") else 0
        return s, status

    def request(self, raw: bytes) -> bytes:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(20)
        s.connect(str(self.sock))
        s.sendall(raw)
        out = b""
        while chunk := s.recv(65536):
            out += chunk
        s.close()
        return out

    def logged(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def stop(self):
        self.proc.terminate()
        self.proc.wait(10)
        self.err.seek(0)
        text = self.err.read()
        self.err.close()
        return text


@pytest.fixture
def proxy(short_dir):
    p = Proxy(short_dir)
    yield p
    p.stop()


@pytest.fixture
def echo():
    e = Echo()
    yield e
    e.close()


def test_an_allowed_host_port_tunnels_and_the_allowlist_is_re_read(proxy, echo):
    s, status = proxy.connect(f"127.0.0.1:{echo.port}")
    assert status == 403                                                     # empty list: nothing is allowed
    s.close()
    proxy.allow.write_text(f"# comment\n\n127.0.0.1:{echo.port}\n")
    s, status = proxy.connect(f"127.0.0.1:{echo.port}")
    assert status == 200
    s.sendall(b"hello through the tunnel")
    assert s.recv(100) == b"hello through the tunnel"
    s.close()


def test_an_unlisted_host_gets_403_and_one_log_line(proxy, echo):
    proxy.allow.write_text(f"127.0.0.1:{echo.port}\n")
    s, status = proxy.connect("Example.COM:443")
    s.close()
    assert status == 403
    (line,) = proxy.logged()
    assert line["host"] == "example.com" and line["port"] == 443 and isinstance(line["ts"], (int, float))


def test_a_port_mismatch_is_refused(proxy, echo):
    other = Echo()
    try:
        proxy.allow.write_text(f"127.0.0.1:{echo.port}\n")
        s, status = proxy.connect(f"127.0.0.1:{other.port}")
        s.close()
        assert status == 403 and other.accepted == 0
        assert proxy.logged()[-1] == {**proxy.logged()[-1], "host": "127.0.0.1", "port": other.port}
    finally:
        other.close()


def test_a_hostname_resolving_to_loopback_is_refused_unless_the_ip_is_listed(proxy, echo):
    proxy.allow.write_text(f"localhost:{echo.port}\n")
    s, status = proxy.connect(f"localhost:{echo.port}")
    s.close()
    assert status == 403 and echo.accepted == 0                               # never reached the server
    proxy.allow.write_text(f"LOCALHOST:{echo.port}\n127.0.0.1:{echo.port}\n")
    s, status = proxy.connect(f"LocalHost:{echo.port}")                     # hostnames compare case-insensitively
    assert status == 200
    s.sendall(b"ping")
    assert s.recv(10) == b"ping"
    s.close()


def test_malformed_targets_are_rejected_and_not_logged(proxy, echo):
    proxy.allow.write_text(f"127.0.0.1:{echo.port}\n")
    for bad in (f"user@127.0.0.1:{echo.port}", "127.0.0.1:0", "127.0.0.1:70000", f"127.0.0.1:+{echo.port}",
                "127.0.0.1", "a\x01b:443", "[::1:80"):
        s, status = proxy.connect(bad)
        s.close()
        assert status == 400, bad
    assert proxy.logged() == [] and echo.accepted == 0
    out = proxy.request(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 4")                                     # plain HTTP proxying is not offered


def test_the_address_classifier_blocks_internal_ranges():
    eg = load_egress()
    for ip in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
               "224.0.0.1", "::1", "fe80::1", "fc00::1", "fd12::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::", "ff02::1"):
        assert eg.forbidden(ip), ip
    for ip in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
        assert not eg.forbidden(ip), ip


# --- /svc/<name>/...: the proxy adds the bound header; the helper never sees the value ---

@pytest.fixture
def tls_upstream(short_dir):
    if not shutil.which("openssl"):
        pytest.skip("needs openssl")
    key, cert = short_dir / "k.pem", short_dir / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
                   check=True, capture_output=True)
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def _reply(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers.items()), "body": body})
            out = json.dumps({"path": self.path, "headers": dict(self.headers.items())}).encode()   # a reflecting upstream
            self.send_response(200, self.headers.get("Authorization", "OK"))          # reason phrase reflects it too
            self.send_header(self.headers.get("Authorization", "x x").split(" ")[-1], "named-after-the-key")
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Echo-Auth", self.headers.get("Authorization", ""))
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        do_GET = do_POST = _reply

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], cert, seen
    srv.shutdown()


@pytest.fixture
def svc_proxy(short_dir, tls_upstream):
    port, cert, seen = tls_upstream
    p = Proxy(short_dir, env={"SSL_CERT_FILE": str(cert)})
    p.services.write_text(json.dumps([
        {"name": "x", "upstream_url": f"https://127.0.0.1:{port}/base", "header": "Authorization", "secret_name": "x_key"},
        {"name": "nokey", "upstream_url": f"https://127.0.0.1:{port}/", "header": "X-Api-Key", "secret_name": "absent"},
        {"name": "plain", "upstream_url": f"http://127.0.0.1:{port}/", "header": "X-Api-Key", "secret_name": "x_key"}]))
    p.secrets.write_text(json.dumps({"x_key": f"Bearer {SECRET}"}))
    yield p, seen
    err = p.stop()
    assert SECRET not in err


def test_svc_forwards_with_the_bound_header_and_never_reflects_it(svc_proxy):
    p, seen = svc_proxy
    out = p.request(b"POST /svc/x/v1/chat?q=1 HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer stolen\r\n"
                    b"Cookie: a=b\r\nProxy-Authorization: x\r\nX-Keep: yes\r\nContent-Length: 5\r\n\r\nhello")
    head, _, body = out.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200"), out
    got = seen[-1]
    assert got["method"] == "POST" and got["path"] == "/base/v1/chat?q=1" and got["body"] == b"hello"
    assert got["headers"]["Authorization"] == f"Bearer {SECRET}"            # the proxy's value, not the client's
    assert "Cookie" not in got["headers"] and "Proxy-Authorization" not in got["headers"]
    assert got["headers"]["X-Keep"] == "yes"
    assert SECRET.encode() not in out and b"stolen" not in body and b"named-after-the-key" not in head
    assert b"[redacted]" in body
    assert SECRET not in (p.log.read_text() if p.log.exists() else "")


def test_svc_unknown_service_missing_secret_and_non_https_upstreams(svc_proxy):
    p, seen = svc_proxy
    assert p.request(b"GET /svc/nope/x HTTP/1.1\r\nHost: a\r\n\r\n").startswith(b"HTTP/1.1 404")
    out = p.request(b"GET /svc/nokey/x HTTP/1.1\r\nHost: a\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 502") and b"absent" not in out
    out = p.request(b"GET /svc/plain/x HTTP/1.1\r\nHost: a\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 502") and SECRET.encode() not in out
    assert p.request(b"GET /svc/x/../../etc HTTP/1.1\r\nHost: a\r\n\r\n").startswith(b"HTTP/1.1 400")
    assert seen == []                                                       # nothing reached the upstream
    out = p.request(b"GET http://127.0.0.1:3128/svc/x/abs HTTP/1.1\r\nHost: 127.0.0.1:3128\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 200") and seen[-1]["path"] == "/base/abs"   # absolute form to the relay itself


# --- the sandbox without network, reaching out only through the relay ---

def primary_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))                                     # no packet is sent; picks the route's source
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


BOX_PROBE = r'''
import os, socket, sys
lan, port = sys.argv[1], int(sys.argv[2])
try:
    socket.create_connection((lan, port), timeout=3).close(); print("direct=open")
except OSError as e:
    print("direct=blocked")
proxy = os.environ["HTTPS_PROXY"].rsplit(":", 1)
s = socket.create_connection((proxy[0].split("//")[1], int(proxy[1])), timeout=5)
s.sendall(f"CONNECT {lan}:{port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
head = b""
while b"\r\n\r\n" not in head:
    head += s.recv(1)
s.sendall(b"via-relay")
print("status=" + head.split(b" ")[1].decode(), "echo=" + s.recv(20).decode())
print("svc=" + os.environ.get("SVC_MY_API_URL", ""), "noproxy=" + os.environ.get("NO_PROXY", ""))
'''


SOCKET_SCAN = r'''
import os, stat
for top in ("/run", "/tmp", "/var", "/etc", os.environ["HOME"]):
    for d, dirs, files in os.walk(top):
        for f in dirs + files:
            try:
                if stat.S_ISSOCK(os.stat(os.path.join(d, f)).st_mode):   # stat, not d_type: a bind mount hides it
                    print(os.path.join(d, f))
            except OSError:
                pass
'''


def run_box(home, cmd, env, work="eg", stdin=""):
    return subprocess.run(["bash", str(SANDBOX), str(home / "herdr-agents" / "work" / work), cmd], input=stdin,
                          capture_output=True, text=True, timeout=60, env={**os.environ, **env})


@needs_box
def test_the_box_has_no_network_but_reaches_an_allowed_host_through_the_relay(home, short_dir):
    lan = primary_ip()
    if not lan or lan.startswith("127."):
        pytest.skip("no non-loopback address on this host")
    echo = Echo("0.0.0.0")
    p = Proxy(short_dir)
    try:
        with socket.create_connection((lan, echo.port), timeout=3):
            pass                                                              # reachable from the host itself
        p.allow.write_text(f"{lan}:{echo.port}\n")
        probe = home / "herdr-agents/work/eg/probe.py"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(BOX_PROBE)
        r = run_box(home, f"python3 probe.py {lan} {echo.port}",
                    {"HERDR_EGRESS_SOCK": str(p.sock), "HERDR_EGRESS_SERVICES": "my-api"})
        assert r.returncode == 0, r.stderr + r.stdout
        assert "direct=blocked" in r.stdout
        assert "status=200 echo=via-relay" in r.stdout
        assert "svc=http://127.0.0.1:3128/svc/my-api" in r.stdout and "noproxy=127.0.0.1,localhost" in r.stdout
        (probe.parent / "socks.py").write_text(SOCKET_SCAN)
        r = run_box(home, "python3 socks.py", {"HERDR_EGRESS_SOCK": str(p.sock)})
        assert set(r.stdout.split()) == {"/run/herdr-egress/sock"}, r.stdout     # no resolver socket: no DNS side channel
        r = run_box(home, "cat", {"HERDR_EGRESS_SOCK": str(p.sock)}, stdin="the task text\n")
        assert r.returncode == 0 and r.stdout == "the task text\n", r.stderr          # stdin reaches the command, not the relay
    finally:
        p.stop()
        echo.close()


@needs_box
def test_the_box_fails_closed_when_the_relay_cannot_come_up(home, short_dir):
    r = run_box(home, "echo ran", {"HERDR_EGRESS_SOCK": str(short_dir / "missing.sock")})
    assert r.returncode != 0 and "ran" not in r.stdout
    r = run_box(home, "echo ran", {"HERDR_EGRESS_SOCK": ""})                    # set but empty never means open network
    assert r.returncode == 126 and "ran" not in r.stdout
    # a sandbox script installed without its relay, or with a relay that never listens, refuses to run the command
    copy = short_dir / "sb"
    copy.mkdir()
    shutil.copy(SANDBOX, copy / "collab-sandbox.sh")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(short_dir / "real.sock"))
    sock.listen(1)
    try:
        env = {**os.environ, "HERDR_EGRESS_SOCK": str(short_dir / "real.sock")}
        r = run_box(home, "echo ran", {**env, "HERDR_EGRESS_SERVICES": "ok,bad name;x"})
        assert r.returncode == 126 and "ran" not in r.stdout and "service name" in r.stderr
        cmd = ["bash", str(copy / "collab-sandbox.sh"), str(home / "herdr-agents/work/fc"), "echo ran"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        assert r.returncode != 0 and "ran" not in r.stdout
        (copy / "collab-egress.py").write_text("import sys\nsys.exit(1)\n")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        assert r.returncode != 0 and "ran" not in r.stdout
    finally:
        sock.close()


@needs_box
def test_without_the_variable_the_box_keeps_the_host_network(home):
    r = run_box(home, "test -z \"$HTTPS_PROXY\" && python3 -c 'import socket; print(*[n for _, n in socket.if_nameindex() if n != \"lo\"])'", {})
    assert r.returncode == 0 and r.stdout.strip(), r.stderr                    # existing workers are unchanged

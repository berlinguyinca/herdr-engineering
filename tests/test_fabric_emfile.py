"""Accept loops must survive fd exhaustion (EMFILE), and the named front's lookup must not be quadratic.

The EMFILE scenarios run in a subprocess with a real, lowered RLIMIT_NOFILE so the limit never leaks into the runner.
"""
import json
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

from herdr_engineering import fabric_named as fn
from herdr_engineering.devfabric import DevServiceRegistry
from herdr_engineering.fabric import FabricGateway, GatewayRouter
from tests.test_fabric import _http_target
from tests.test_fabric_named import L, _free_port

REPO = str(Path(__file__).resolve().parent.parent)

_PROBE = textwrap.dedent('''
    import http.server, resource, socket, sys, threading, time
    sys.path.insert(0, {repo!r})
    from herdr_engineering.devfabric import DevServiceRegistry
    from herdr_engineering.fabric import FabricGateway, GatewayRouter
    from tests.test_fabric_named import _get
    import tempfile, pathlib

    mode = sys.argv[1]
    tmp = pathlib.Path(tempfile.mkdtemp())
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tport = srv.server_address[1]
    s = socket.socket(); s.bind(("127.0.0.1", 0)); front_port = s.getsockname()[1]; s.close()
    registry = DevServiceRegistry(store_path=tmp / "l.json", port_range=(43000, 43200))
    router = GatewayRouter(["127.0.0.1"])
    gw = FabricGateway(registry, router, control_host="127.0.0.1", control_port=0, url_base="local.example.test",
                       probe_interval=30, token="", named_domain="local.example.test", named_port=front_port)
    gw.start()
    lease = registry.register(target_machine_id="bender", target_host="127.0.0.1", target_port=tport, label="docs")
    router.bind(lease.external_port, lease.target_host, lease.target_port)
    port = front_port if mode == "named" else lease.external_port
    host = "docs.local.example.test"
    assert _get(port, host, tls=False).startswith(b"HTTP/1."), "baseline"

    import os
    soft = len(os.listdir("/proc/self/fd")) + 30
    resource.setrlimit(resource.RLIMIT_NOFILE, (soft, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
    fillers = []
    while True:
        try:
            fillers.append(os.open("/dev/null", os.O_RDONLY))
        except OSError:
            break
    os.close(fillers.pop())                       # exactly one fd for the client socket
    client = socket.socket()
    client.settimeout(10)
    client.connect(("127.0.0.1", port))           # the kernel completes it; the server cannot accept (EMFILE)
    time.sleep(1.5)                               # several accept attempts fail
    threads = [t for t in __import__("threading").enumerate() if t.name.startswith("named-front") or t.name == "Thread"]
    alive_during = gw.named_front.healthy() if mode == "named" else all(
        t.is_alive() for ts in router._accept_threads.values() for t in ts)
    for fd in fillers:
        os.close(fd)
    client.sendall(f"GET / HTTP/1.1\\r\\nHost: {{host}}\\r\\nConnection: close\\r\\n\\r\\n".encode())
    data = b""
    try:
        while chunk := client.recv(4096):
            data += chunk
    except OSError:
        pass
    print("RESULT", alive_during, data.startswith(b"HTTP/1."))
    gw.stop()
''')


def _run_probe(mode):
    out = subprocess.run([sys.executable, "-c", _PROBE.format(repo=REPO), mode], capture_output=True,
                         text=True, timeout=120, cwd=REPO)
    line = [ln for ln in out.stdout.splitlines() if ln.startswith("RESULT")]
    assert line, (out.stdout, out.stderr)
    return line[0].split()[1:]


def test_named_front_survives_emfile_and_serves_after_fds_return():
    assert _run_probe("named") == ["True", "True"]


def test_port_router_survives_emfile_and_serves_after_fds_return():
    assert _run_probe("router") == ["True", "True"]


def test_healthz_goes_unhealthy_when_a_named_accept_thread_dies(tmp_path):
    server, tport = _http_target(tmp_path)
    registry = DevServiceRegistry(store_path=tmp_path / "l.json", port_range=(43300, 43400))
    gw = FabricGateway(registry, GatewayRouter(["127.0.0.1"]), control_host="127.0.0.1", control_port=0,
                       url_base="local.example.test", probe_interval=30, token="",
                       named_domain="local.example.test", named_port=_free_port())
    gw.start()
    url = f"http://127.0.0.1:{gw.control_port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            assert r.status == 200 and json.load(r)["ok"] is True
        gw.named_front._listeners[0].close()          # the listener itself is gone: the loop exits for good
        deadline = time.time() + 5
        while gw.named_front.healthy() and time.time() < deadline:
            time.sleep(0.05)
        try:
            urllib.request.urlopen(url, timeout=5)
            raise AssertionError("healthz stayed 200")
        except urllib.error.HTTPError as e:
            assert e.code == 503
    finally:
        gw.stop()
        server.shutdown()


def test_lookup_with_thousands_of_leases_is_correct_and_fast():
    leases = [L(f"id{i:05d}", machine="bender", port=3000 + i, created=f"2026-11-20T00:{i // 60:02d}:{i % 60:02d}Z")
              for i in range(2000)]
    leases[7]["label"] = "docs"
    leases[1500]["label"] = "docs"                     # collision: the older (index 7) keeps the name
    leases[9]["state"] = "expired"
    front = fn.NamedFront(GatewayRouter(["127.0.0.1"]), lambda: leases, "local.example.test", _free_port())
    t0 = time.time()
    assert front.lookup("docs.local.example.test")["id"] == "id00007"
    assert front.lookup("bender-4500.local.example.test")["id"] == "id01500"   # the collision loser, automatic name
    assert front.lookup("bender-3009.local.example.test") is None              # inactive leases never match
    assert front.lookup("bender-3100.local.example.test")["id"] == "id00100"
    assert front.lookup("memory.local.example.test") is None
    assert front.lookup("a.b.local.example.test") is None and front.lookup(None) is None
    for _ in range(50):
        front.lookup("memory.local.example.test")
    assert time.time() - t0 < 10
    leases[7]["label"] = None                          # lease changes are noticed, not served from a stale cache
    assert front.lookup("docs.local.example.test")["id"] == "id01500"

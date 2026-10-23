"""Host telemetry heartbeat from the fabric agent (fake local ingest server)."""
import http.server
import json
import socket
import threading

from herdr_engineering.fabric_agent import AgentConfig, FabricAgent


class _Ingest:
    def __init__(self):
        self.posts = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.posts.append((self.path, json.loads(self.rfile.read(n))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()


class _Client:
    pass


def _agent(tmp_path, url, tailnet_ip="100.1.2.3", **kw):
    cfg = AgentConfig(tailnet_ip=tailnet_ip, store_path=tmp_path / "s.json",
                      control_plane_url=url, **kw)
    a = FabricAgent(_Client(), cfg)
    a._sampler = type("S", (), {"sample": lambda self: {
        "cpu": {"pct": 5.0}, "mem": {"pct": 40.0}, "disk": {"pct": 10.0},
        "uptime": 1}})()
    a._read_slow = lambda: {"updates": {"manager": "apt"}, "versions": {"git": "2"}}
    return a


def _host_posts(srv):
    return [b for p, b in srv.posts if b["entity_type"] == "host"]


def test_agent_posts_host_event_with_telemetry(tmp_path):
    srv = _Ingest()
    try:
        a = _agent(tmp_path, srv.url)
        assert a._host_tick(0.0) is True
        (ev,) = _host_posts(srv)
        assert ev["source"] == "fabric-agent"
        assert ev["event_type"] == "host.heartbeat"
        assert ev["entity_id"] == a._machine
        p = ev["payload"]
        assert p["cpu"] == {"pct": 5.0} and p["mem"] == {"pct": 40.0}
        assert p["disk"] == {"pct": 10.0}
        assert p["telemetry"]["uptime"] == 1
        assert p["host_name"] == a._machine and p["tailnet_ip"] == "100.1.2.3"
        assert "updates" not in p
    finally:
        srv.close()


def test_slow_fields_only_on_first_and_hourly(tmp_path):
    srv = _Ingest()
    try:
        a = _agent(tmp_path, srv.url, host_slow_interval=3600.0)
        a._host_tick(0.0)  # slow data not available yet
        a._slow_data = a._read_slow()
        a._host_tick(10.0)
        a._host_tick(40.0)
        a._host_tick(3609.0)
        a._host_tick(3611.0)
        has = ["updates" in b["payload"] for b in _host_posts(srv)]
        assert has == [False, True, False, False, True]
        p = _host_posts(srv)[1]["payload"]
        assert p["versions"] == {"git": "2"}
    finally:
        srv.close()


def test_control_plane_down_does_not_raise(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    a = _agent(tmp_path, f"http://127.0.0.1:{port}")
    assert a._report_host({"cpu": {}}, True) is False
    assert a._host_tick(0.0) is False


def test_sampler_error_does_not_raise(tmp_path):
    srv = _Ingest()
    try:
        a = _agent(tmp_path, srv.url)
        a._sampler = type("S", (), {"sample": lambda self: 1 / 0})()
        assert a._host_tick(0.0) is False
        assert _host_posts(srv) == []
    finally:
        srv.close()


def test_no_control_plane_url_posts_nothing(tmp_path):
    a = _agent(tmp_path, "")
    assert a._host_tick(0.0) is False
    assert a._report_host({"cpu": {}}, False) is False


def test_tailnet_ip_none_is_sent_as_null_not_string(tmp_path):
    srv = _Ingest()
    try:
        a = _agent(tmp_path, srv.url, tailnet_ip=None)
        a._host_tick(0.0)
        assert _host_posts(srv)[0]["payload"]["tailnet_ip"] is None
    finally:
        srv.close()


def test_threads_report_and_stop_with_run(tmp_path):
    srv = _Ingest()
    try:
        a = _agent(tmp_path, srv.url, host_report_interval=0.05,
                   host_slow_interval=0.2, interval=0.05)
        a.scan_once = lambda: None
        stop = threading.Event()
        t = threading.Thread(target=a.run, args=(stop,), daemon=True)
        t.start()
        import time
        deadline = time.time() + 5
        while time.time() < deadline and not any(
                "updates" in b["payload"] for b in _host_posts(srv)):
            time.sleep(0.05)
        stop.set()
        t.join(3)
        assert not t.is_alive()
        assert len(_host_posts(srv)) >= 2
        assert any("updates" in b["payload"] for b in _host_posts(srv))
    finally:
        srv.close()

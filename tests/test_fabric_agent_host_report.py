"""Host telemetry heartbeat from the fabric agent (fake local ingest server)."""
import http.server
import json
import socket
import threading

from herdr_engineering.fabric_agent import AgentConfig, FabricAgent


class _Ingest:
    def __init__(self, status=lambda body: 200):
        self.posts = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                outer.posts.append((self.path, body))
                self.send_response(status(body))
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
    # The real HostSampler.sample() shape: flat numeric keys.
    a._sampler = type("S", (), {"sample": lambda self: {
        "cpu": 5.0, "mem": 40.0, "disk": 10.0, "uptime_s": 1, "agents_running": 2}})()
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
        assert p["cpu"] == 5.0 and p["mem"] == 40.0
        assert p["disk"] == 10.0
        assert p["telemetry"]["uptime_s"] == 1
        assert p["telemetry"]["agents_running"] == 2
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
    assert a._report_host({"cpu": 1.0}, True) is None
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
    assert a._report_host({"cpu": 1.0}, False) is None


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


def test_rejected_slow_payload_is_dropped_so_heartbeats_recover(tmp_path):
    """A 4xx on a post carrying slow data (e.g. oversize ``updates``) must not
    poison every later heartbeat: the slow set is dropped and the next tick
    posts the fast sample alone."""
    srv = _Ingest(status=lambda body: 400 if "updates" in body["payload"] else 200)
    try:
        a = _agent(tmp_path, srv.url, host_slow_interval=3600.0)
        a._slow_data = {"updates": {"manager": "apt", "packages": ["x" * 20000]},
                        "versions": {}}
        assert a._host_tick(0.0) is False
        assert a._host_tick(30.0) is True
        assert a._host_tick(60.0) is True
        has = ["updates" in b["payload"] for b in _host_posts(srv)]
        assert has == [True, False, False]
        # a fresh slow read is sent again (not held back for the hour)
        a._slow_data = {"updates": {"manager": "apt", "outdated": 0}, "versions": {}}
        assert a._host_tick(90.0) is False  # this fake server still rejects it
        assert ["updates" in b["payload"] for b in _host_posts(srv)][3] is True
    finally:
        srv.close()


def test_new_slow_data_is_sent_without_waiting_for_the_hour(tmp_path):
    srv = _Ingest()
    try:
        a = _agent(tmp_path, srv.url, host_slow_interval=3600.0)
        a._slow_data = {"updates": {"manager": "apt", "outdated": None}, "versions": {}}
        a._host_tick(0.0)
        a._host_tick(30.0)
        a._slow_data = {"updates": {"manager": "apt", "outdated": 3}, "versions": {}}  # 5-min retry
        a._host_tick(330.0)
        has = ["updates" in b["payload"] for b in _host_posts(srv)]
        assert has == [True, False, True]
        assert _host_posts(srv)[2]["payload"]["updates"]["outdated"] == 3
    finally:
        srv.close()


def test_slow_retry_delay_backs_off_while_updates_are_unknown():
    from herdr_engineering.fabric_agent import slow_retry_delay
    assert slow_retry_delay(0, 3600.0) == 3600.0
    assert slow_retry_delay(1, 3600.0) == 300.0
    assert slow_retry_delay(2, 3600.0) == 900.0
    assert slow_retry_delay(3, 3600.0) == 3600.0
    assert slow_retry_delay(9, 3600.0) == 3600.0
    assert slow_retry_delay(1, 60.0) == 60.0  # never slower than the normal cadence


def test_slow_incomplete_only_when_manager_known_and_count_unknown():
    from herdr_engineering.fabric_agent import slow_incomplete
    assert slow_incomplete(None) is True
    assert slow_incomplete({"updates": {"manager": "apt", "outdated": None}}) is True
    assert slow_incomplete({"updates": {"manager": "apt", "outdated": 0}}) is False
    assert slow_incomplete({"updates": {"manager": None, "outdated": None}}) is False


def test_slow_loop_retries_after_five_minutes_when_outdated_unknown(tmp_path):
    a = _agent(tmp_path, "http://127.0.0.1:9")
    reads = iter([{"updates": {"manager": "apt", "outdated": None}, "versions": {}},
                  {"updates": {"manager": "apt", "outdated": 1}, "versions": {}}])
    a._read_slow = lambda: next(reads)
    waits = []

    class Stop:
        def is_set(self):
            return len(waits) >= 2

        def wait(self, t):
            waits.append(t)

    a._slow_loop(Stop())
    assert waits == [300.0, 3600.0]


def test_agent_binaries_config_parsing():
    import pytest

    from herdr_engineering.fabric_agent import parse_agent_binaries
    assert parse_agent_binaries(None) == ("pi", "claude", "codex")
    assert parse_agent_binaries(["pi", " aider "]) == ("pi", "aider")
    assert parse_agent_binaries("pi") == ("pi",)  # one name, not its characters
    assert parse_agent_binaries([]) == ()
    for bad in ([1], [""], ["/usr/bin/pi"], {"pi": 1}, 3):
        with pytest.raises(ValueError):
            parse_agent_binaries(bad)


def test_agent_passes_configured_binaries_to_the_sampler(tmp_path):
    cfg = AgentConfig(tailnet_ip="100.1.2.3", store_path=tmp_path / "s.json",
                      control_plane_url="", agent_binaries=("aider",))
    a = FabricAgent(_Client(), cfg)
    assert a._sampler._agent_binaries == ("aider",)

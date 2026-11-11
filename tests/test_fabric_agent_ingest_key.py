"""The fabric agent sends the host config's ingest key on every ingest POST.

A real local HTTP server plays a keyed control plane: keyless requests get 401.
"""
import http.server
import threading

from herdr_engineering.fabric_agent import AgentConfig, FabricAgent

KEY = "k-ingest"


class _KeyedIngest:
    def __init__(self):
        self.statuses: list[int] = []
        self.keys: list[str | None] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                self.rfile.read(n)
                key = self.headers.get("X-API-Key")
                status = 200 if key == KEY else 401
                outer.keys.append(key)
                outer.statuses.append(status)
                self.send_response(status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class _Client:
    pass


def _agent(tmp_path, url, **kw):
    cfg = AgentConfig(tailnet_ip="100.1.2.3", store_path=tmp_path / "s.json",
                      control_plane_url=url, **kw)
    a = FabricAgent(_Client(), cfg)
    a._sampler = type("S", (), {"sample": lambda self: {"cpu": 1.0}})()
    return a


_ENTRY = {"label": "app", "external_port": 5000, "url": "http://x", "lease_id": "l1"}


def test_service_lease_report_sends_key(tmp_path):
    srv = _KeyedIngest()
    try:
        a = _agent(tmp_path, srv.url, api_key=KEY)
        assert a._report_service(_ENTRY, "healthy") is True
        assert srv.keys == [KEY]
    finally:
        srv.close()


def test_service_lease_report_without_key_is_rejected_by_keyed_server(tmp_path):
    srv = _KeyedIngest()
    try:
        a = _agent(tmp_path, srv.url)
        assert a._report_service(_ENTRY, "healthy") is False
        assert srv.keys == [None] and srv.statuses == [401]
    finally:
        srv.close()


def test_host_telemetry_sends_key(tmp_path):
    srv = _KeyedIngest()
    try:
        a = _agent(tmp_path, srv.url, api_key=KEY)
        assert a._report_host({"cpu": 1.0}, False) == 200
        assert srv.keys == [KEY]
        keyless = _agent(tmp_path, srv.url)
        assert keyless._report_host({"cpu": 1.0}, False) == 401
    finally:
        srv.close()


def test_retarget_follows_the_key(tmp_path):
    srv = _KeyedIngest()
    try:
        a = _agent(tmp_path, srv.url)
        a.retarget(_Client(), srv.url, KEY)
        assert a._report_host({"cpu": 1.0}, False) == 200
        # An omitted key keeps the current one; an explicit empty key clears it.
        a.retarget(_Client(), srv.url)
        assert a._report_host({"cpu": 1.0}, False) == 200
        a.retarget(_Client(), srv.url, "")
        assert a._report_host({"cpu": 1.0}, False) == 401
    finally:
        srv.close()

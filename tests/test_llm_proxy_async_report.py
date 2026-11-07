"""Token reports must not run on the request thread: the live proxy logged 'token report failed: timed out' because the
control plane answered slowly and each request waited up to the client timeout for accounting. Real HTTP control plane."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from herdr_engineering.llm_proxy import ControlPlaneClient, ReportQueue


def _slow_control(delay, got):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            time.sleep(delay)
            got.append(body)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_submit_returns_immediately_and_record_is_delivered_later():
    got: list = []
    srv = _slow_control(0.5, got)
    q = ReportQueue(ControlPlaneClient(f"http://127.0.0.1:{srv.server_address[1]}"))
    try:
        t0 = time.monotonic()
        q.submit({"n": 1})
        assert time.monotonic() - t0 < 0.1
        assert q.flush(5)
        assert got == [{"n": 1}]
    finally:
        q.close()
        srv.shutdown()


def test_timeouts_are_retried_not_lost_and_full_queue_drops_instead_of_blocking():
    got: list = []
    srv = _slow_control(0.0, got)
    dead = ControlPlaneClient("http://127.0.0.1:9", timeout=0.2)     # nothing listens: every send fails
    q = ReportQueue(dead, maxsize=2)
    try:
        t0 = time.monotonic()
        for i in range(50):
            q.submit({"n": i})
        assert time.monotonic() - t0 < 0.5          # never blocks the caller
        assert q.dropped > 0
    finally:
        q.close()
        srv.shutdown()


def test_failed_send_is_retried_when_control_plane_recovers():
    got: list = []
    srv = _slow_control(0.0, got)
    port = srv.server_address[1]
    flaky = ControlPlaneClient(f"http://127.0.0.1:{port}")
    calls = {"n": 0}
    real = flaky.report

    def report(rec):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("timed out")
        real(rec)

    flaky.report = report
    q = ReportQueue(flaky, retry_delay=0.05)
    try:
        q.submit({"n": 7})
        assert q.flush(5)
        assert got == [{"n": 7}] and calls["n"] == 2
    finally:
        q.close()
        srv.shutdown()

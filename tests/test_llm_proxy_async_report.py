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


def test_full_queue_drops_instead_of_blocking_and_accepted_records_are_not_lost():
    got: list = []
    srv = _slow_control(0.05, got)
    q = ReportQueue(ControlPlaneClient(f"http://127.0.0.1:{srv.server_address[1]}"), maxsize=2)
    try:
        t0 = time.monotonic()
        for i in range(50):
            q.submit({"n": i})
        assert time.monotonic() - t0 < 0.5          # never blocks the caller
        assert q.dropped > 0
        assert q.flush(10)
        assert len(got) == 50 - q.dropped and len(got) >= 2    # everything not dropped was delivered
    finally:
        q.close()
        srv.shutdown()


def test_retry_exhaustion_drops_and_counts():
    dead = ControlPlaneClient("http://127.0.0.1:9", timeout=0.2)     # nothing listens
    q = ReportQueue(dead, attempts=2, retry_delay=0.01)
    try:
        q.submit({"n": 1})
        assert q.flush(5)
        assert q.dropped == 1
    finally:
        q.close()


def _status_control(code, hits):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            hits.append(1)
            self.send_response(code)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_permanent_4xx_is_not_retried_but_5xx_is():
    hits4: list = []
    hits5: list = []
    s4, s5 = _status_control(400, hits4), _status_control(503, hits5)
    q4 = ReportQueue(ControlPlaneClient(f"http://127.0.0.1:{s4.server_address[1]}"), attempts=3, retry_delay=0.01)
    q5 = ReportQueue(ControlPlaneClient(f"http://127.0.0.1:{s5.server_address[1]}"), attempts=3, retry_delay=0.01)
    try:
        q4.submit({"n": 1})
        q5.submit({"n": 1})
        assert q4.flush(5) and q5.flush(5)
        assert len(hits4) == 1 and q4.dropped == 1
        assert len(hits5) == 3 and q5.dropped == 1
    finally:
        q4.close()
        q5.close()
        s4.shutdown()
        s5.shutdown()


def test_close_does_not_block_on_a_full_queue():
    got: list = []
    srv = _slow_control(0.3, got)
    q = ReportQueue(ControlPlaneClient(f"http://127.0.0.1:{srv.server_address[1]}"), maxsize=2)
    try:
        for i in range(10):
            q.submit({"n": i})
        t0 = time.monotonic()
        q.close()
        assert time.monotonic() - t0 < 0.2
    finally:
        srv.shutdown()


def test_run_until_stopped_flushes_queued_reports_on_sigterm():
    import os
    import signal

    from herdr_engineering.llm_proxy import ProxyState, make_handler, run_until_stopped
    got: list = []
    srv = _slow_control(0.2, got)
    state = ProxyState(ControlPlaneClient(f"http://127.0.0.1:{srv.server_address[1]}"), "http://127.0.0.1:9", "k")
    state.reports = ReportQueue(state.control)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    previous = signal.getsignal(signal.SIGTERM)
    try:
        state.reports.submit({"n": 1})
        threading.Timer(0.1, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        run_until_stopped(httpd, flush_seconds=5)
        assert got == [{"n": 1}]
    finally:
        signal.signal(signal.SIGTERM, previous)
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

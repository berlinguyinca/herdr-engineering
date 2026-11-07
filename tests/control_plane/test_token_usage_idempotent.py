"""A retried usage POST (the first attempt timed out client-side but was recorded) must not double-count."""
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def _events(repo):
    return [e for ids in repo.entity_ids("token_usage") for e in repo._events.get(("token_usage", ids), [])]


def test_same_request_id_is_recorded_once():
    repo = ControlPlaneRepo()
    row = {"schema": 2, "project": "p", "model": "m", "status": 200, "request_id": "r-1",
           "input_tokens": 5, "output_tokens": 7}
    repo.record_token_usage_row(dict(row))
    repo.record_token_usage_row(dict(row))
    assert len(_events(repo)) == 1
    repo.record_token_usage_row({**row, "request_id": "r-2"})
    assert len(_events(repo)) == 2


def test_v1_rows_dedupe_and_rows_without_request_id_do_not():
    repo = ControlPlaneRepo()
    repo.record_token_usage_row({"project": "p", "model": "m", "request_id": "x", "input_tokens": 1})
    repo.record_token_usage_row({"project": "p", "model": "m", "request_id": "x", "input_tokens": 1})
    repo.record_token_usage_row({"project": "p", "model": "m", "input_tokens": 1})
    repo.record_token_usage_row({"project": "p", "model": "m", "input_tokens": 1})
    assert len(_events(repo)) == 3


def test_proxy_stamps_a_request_id_on_every_record():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from herdr_engineering.llm_proxy import (
        ControlPlaneClient,
        ProxyState,
        ReportQueue,
        make_handler,
    )
    got: list = []

    class Up(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"model": "m", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Cp(Up):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    up, cp = ThreadingHTTPServer(("127.0.0.1", 0), Up), ThreadingHTTPServer(("127.0.0.1", 0), Cp)
    for s in (up, cp):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    state = ProxyState(ControlPlaneClient(f"http://127.0.0.1:{cp.server_address[1]}"),
                       f"http://127.0.0.1:{up.server_address[1]}", "k")
    state.reports = ReportQueue(state.control)
    handler = make_handler(state)
    # Drive _report directly: it builds the record the queue sends.
    meta = {"usage": {"input_tokens": 1}, "status": 200, "error_class": None, "model": "m",
            **{k: None for k in ("upstream_model", "session_id", "agent_id", "route", "fallback", "stream",
                                 "queue_ms", "ttft_ms", "gen_ms", "engine")}}
    try:
        h = handler.__new__(handler)
        h._report({"project": "p", "key_id": "k1"}, meta, 0.0)
        h._report({"project": "p", "key_id": "k1"}, meta, 0.0)
        assert state.reports.flush(5)
        ids = [r["request_id"] for r in got]
        assert len(ids) == 2 and len(set(ids)) == 2 and all(len(i) == 32 for i in ids)
    finally:
        state.reports.close()
        up.shutdown()
        cp.shutdown()

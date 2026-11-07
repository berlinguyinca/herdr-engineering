"""Expo rejects the WHOLE batch with HTTP 400 VALIDATION_ERROR when any field is null (observed live:
``0.collapseId: Expected string, received null``). Real local HTTP server standing in for exp.host."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from herdr_engineering.control_plane.rakazo_push import ExpoSender, PushNotifier, PushRegistry

TOKEN = "ExponentPushToken[abcdefghijklmnopqrstuv]"


def _strict_expo(seen):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            msgs = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(msgs)
            nulls = [f"{i}.{k}" for i, m in enumerate(msgs) for k, v in m.items() if v is None]
            if nulls:
                code, out = 400, {"errors": [{"code": "VALIDATION_ERROR",
                                              "message": f"{nulls[0]}: Expected string, received null {TOKEN}"}]}
            else:
                code, out = 200, {"data": [{"status": "ok"} for _ in msgs]}
            payload = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def expo():
    seen: list = []
    srv = _strict_expo(seen)
    yield f"http://127.0.0.1:{srv.server_address[1]}/send", seen
    srv.shutdown()


def test_message_without_collapse_id_or_thread_has_no_null_fields(expo):
    url, seen = expo
    reg = PushRegistry()
    reg.add("acct", TOKEN, "sess")
    n = PushNotifier(reg, ExpoSender(url=url), lambda a: {"kind": "help"}, enabled=True)
    n.deliver("acct", "t", "b", "help", None)      # target has no threadId -> collapseId used to be None
    assert n.counters["sent"] == 1 and n.counters["failed"] == 0
    assert all(v is not None for v in seen[0][0].values())


def test_http_400_reports_expo_error_code_but_never_the_token(expo):
    url, _ = expo
    with pytest.raises(OSError) as e:
        ExpoSender(url=url).send([{"to": TOKEN, "title": "t", "collapseId": None}])
    text = str(e.value)
    assert "400" in text and "VALIDATION_ERROR" in text and "collapseId" in text
    assert TOKEN not in text

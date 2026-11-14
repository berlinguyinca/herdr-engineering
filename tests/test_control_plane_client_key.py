"""ControlPlaneClient sends X-API-Key only when a key file is configured; the file must be private."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from herdr_engineering.llm_proxy import ControlPlaneClient, ControlPlaneKeyError


@pytest.fixture()
def cp():
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _answer(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            seen.append(self.headers.get("X-API-Key"))
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


def _keyfile(tmp_path, mode=0o600, text="k-secret\n"):
    p = tmp_path / "key"
    p.write_text(text)
    os.chmod(p, mode)
    return p


def test_no_key_file_sends_no_header(cp):
    url, seen = cp
    c = ControlPlaneClient(url)
    c.config()
    c.report({"a": 1})
    assert seen == [None, None]


def test_key_file_is_sent_on_get_and_post_and_reread_each_call(cp, tmp_path):
    url, seen = cp
    p = _keyfile(tmp_path)
    c = ControlPlaneClient(url, key_file=p)
    c.config()
    p.write_text("rotated\n")
    c.report_batch([{"a": 1}])
    assert seen == ["k-secret", "rotated"]


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_group_or_other_readable_key_file_is_refused(cp, tmp_path, mode):
    url, seen = cp
    c = ControlPlaneClient(url, key_file=_keyfile(tmp_path, mode))
    with pytest.raises(ControlPlaneKeyError):
        c.config()
    assert seen == []


def test_missing_or_empty_key_file_is_refused(cp, tmp_path):
    url, seen = cp
    with pytest.raises(ControlPlaneKeyError):
        ControlPlaneClient(url, key_file=tmp_path / "nope").config()
    with pytest.raises(ControlPlaneKeyError):
        ControlPlaneClient(url, key_file=_keyfile(tmp_path, text="\n")).config()
    assert seen == []


def test_llm_gateway_sync_cli_sends_key_file(cp, tmp_path):
    from herdr_engineering.cli import main
    url, seen = cp
    p = _keyfile(tmp_path)
    out = tmp_path / "principals.json"
    rc = main(["control-plane", "llm-gateway-sync", "--control-plane", url, "--principals-file", str(out),
               "--control-plane-key-file", str(p), "--once"])
    assert rc == 0 and seen == ["k-secret"]
    assert json.loads(out.read_text()) is not None

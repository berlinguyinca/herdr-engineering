"""Native report commands use authenticated transport and verified local downloads."""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from herdr_engineering.artifact_reports_client import main


@pytest.fixture
def endpoint():
    captured = []
    state = {"data": b"source evidence", "tampered": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            captured.append((self.command, self.path, dict(self.headers), None))
            if self.path.endswith("/assets/data1"):
                body = b"wrong evidence!" if state["tampered"] else state["data"]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.endswith("/preview"):
                body = b"<html><body>bounded preview</body></html>"
            elif self.path.endswith("/report"):
                body = json.dumps({"artifact_id": "a1", "version_id": "v1", "assets": [{
                    "id": "data1", "name": "data.csv", "sha256": hashlib.sha256(state["data"]).hexdigest(),
                    "size_bytes": len(state["data"])}]}).encode()
            else:
                body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            blocks = []
            while length:
                block = self.rfile.read(min(length, 8192))
                length -= len(block)
                blocks.append(block)
            captured.append((self.command, self.path, dict(self.headers), b"".join(blocks)))
            body = b'{"artifact_id":"a1","status":"draft"}'
            self.send_response(201)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", captured, state
    server.shutdown()
    server.server_close()


def test_create_and_feedback_send_auth_as_header_not_body_or_url(endpoint, monkeypatch, capsys):
    url, requests, _ = endpoint
    monkeypatch.setenv("TEST_REPORT_KEY", "fictional-test-secret")
    common = ["--control-plane", url, "--api-key-env", "TEST_REPORT_KEY"]
    assert main(["create", "--title", "Generic investigation", *common]) == 0
    assert main(["feedback", "a1", "v1", "--target-kind", "finding", "--target-id", "f1",
                 "--body", "Explain the denominator", *common]) == 0
    for _, path, headers, body in requests:
        assert headers["X-Api-Key"] == "fictional-test-secret"
        assert "fictional-test-secret" not in path and b"fictional-test-secret" not in body
        assert "actor" not in json.loads(body)
    assert "fictional-test-secret" not in capsys.readouterr().out


def test_asset_upload_uses_explicit_length_and_separate_stream(endpoint, tmp_path):
    url, requests, _ = endpoint
    source = tmp_path / "data.csv"
    source.write_bytes(b"x,y\n1,2\n" * 20000)
    assert main(["upload-asset", "a1", str(source), "--mime", "text/csv",
                 "--name", "evidence/data.csv", "--control-plane", url]) == 0
    method, path, headers, body = requests[-1]
    assert method == "POST" and "/a1/blobs?filename=evidence%2Fdata.csv" in path
    assert int(headers["Content-Length"]) == source.stat().st_size
    assert body == source.read_bytes()


def test_download_checks_pinned_digest_and_keeps_existing_file_on_corruption(endpoint, tmp_path):
    url, _, state = endpoint
    output = tmp_path / "data.csv"
    args = ["download", "a1", "v1", "data1", "--output", str(output), "--control-plane", url]
    assert main(args) == 0 and output.read_bytes() == state["data"]
    state["tampered"] = True
    assert main(args) == 1
    assert output.read_bytes() == state["data"]


def test_preview_is_an_explicit_bounded_file_request(endpoint, tmp_path):
    url, requests, _ = endpoint
    output = tmp_path / "preview.html"
    assert main(["preview", "a1", "v1", "--output", str(output), "--control-plane", url]) == 0
    assert b"bounded preview" in output.read_bytes()
    assert len(requests) == 1 and requests[0][1].endswith("/versions/v1/preview")


def test_feedback_list_uses_bounded_default_and_explicit_pagination(endpoint):
    url, requests, _ = endpoint
    assert main(["feedback-list", "a1", "v1", "--control-plane", url]) == 0
    assert requests[-1][1].endswith("/feedback?limit=10&offset=0")
    assert main(["feedback-list", "a1", "v1", "--limit", "20", "--offset", "10",
                 "--control-plane", url]) == 0
    assert requests[-1][1].endswith("/feedback?limit=20&offset=10")

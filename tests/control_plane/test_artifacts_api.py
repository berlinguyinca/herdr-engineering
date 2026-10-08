import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


@pytest.fixture()
def server():
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    base = f"http://127.0.0.1:{svr.port}"
    yield base, repo
    svr.shutdown()


def upload(base, data, filename="shot.png", mime="image/png", session_id=None):
    url = f"{base}/api/v1/artifacts?filename={filename}&mime={urllib.parse.quote(mime)}"
    if session_id:
        url += f"&session_id={session_id}"
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": mime})
    with urllib.request.urlopen(req) as r:
        return r.status, r.read()


def test_upload_then_download_roundtrip(server):
    base, repo = server
    sid = repo.create_session()
    status, body = upload(base, b"\x89PNG\r\n", session_id=sid)
    assert status == 200
    art = json.loads(body)
    assert art["artifact_id"].startswith("artifact_")
    assert art["size_bytes"] == 6
    assert art["status"] == "available"
    # download returns the exact bytes with content type + length
    req = urllib.request.Request(f"{base}/api/v1/artifacts/{art['artifact_id']}/download")
    with urllib.request.urlopen(req) as r:
        assert r.read() == b"\x89PNG\r\n"
        assert r.headers["Content-Type"].startswith("image/png")


def test_download_unknown_artifact_404(server):
    base, _ = server
    try:
        urllib.request.urlopen(f"{base}/api/v1/artifacts/artifact_nope/download")
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_upload_requires_body(server):
    base, _ = server
    req = urllib.request.Request(f"{base}/api/v1/artifacts?filename=a.txt",
                                 data=b"", method="POST")
    try:
        urllib.request.urlopen(req)
        raise AssertionError("expected error")
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_artifact_recorded_with_sha256_and_session_binding(server):
    base, repo = server
    sid = repo.create_session()
    status, body = upload(base, b"hello artifact", filename="note.txt",
                          mime="text/plain", session_id=sid)
    art = json.loads(body)
    assert art["session_id"] == sid
    assert art["sha256"]
    assert art["mime_type"] == "text/plain"

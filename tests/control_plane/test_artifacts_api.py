import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_jobs import ArtifactJobManager
from herdr_engineering.control_plane.publication import FakeExposureProvider, PublicationController
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


class _FakeRustFS:
    """Minimal stand-in for the S3 content-addressed object store."""
    def __init__(self):
        self.objects = {}

    def put(self, data):
        import hashlib
        digest = hashlib.sha256(data).hexdigest()
        key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        self.objects[key] = data
        return key, digest

    def get(self, key, sha256):
        return self.objects[key]


@pytest.fixture()
def s3_server():
    repo = ControlPlaneRepo()
    fs = _FakeRustFS()
    svr = ControlPlaneServer(repo, rustfs=fs)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    base = f"http://127.0.0.1:{svr.port}"
    yield base, repo, fs
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


def test_acl_applies_to_nested_version_and_review_reads():
    from herdr_engineering.control_plane.artifact_acl import ArtifactAcl

    class PrivateStore:
        def sync_call(self, *_args, **_kwargs):
            raise AssertionError('unauthorized read reached the artifact store')

    instance = ControlPlaneServer(ControlPlaneRepo(), api_keys={'reader': 'viewer'},
                                  artifact_acl=ArtifactAcl(), artifact_store=PrivateStore())
    threading.Thread(target=instance.serve, daemon=True).start()
    instance.wait_until_ready()
    try:
        for suffix in ('versions', 'versions/private-version', 'reviews', 'download'):
            req = urllib.request.Request(
                f'http://127.0.0.1:{instance.port}/api/v1/artifacts/private-artifact/{suffix}',
                headers={'X-API-Key': 'reader'})
            with pytest.raises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(req, timeout=5)
            assert denied.value.code == 404
    finally:
        instance.shutdown()


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


def test_artifact_catalog_lists_registered_records(server):
    base, _ = server
    upload(base, b"cataloged", filename="catalog.txt", mime="text/plain")
    with urllib.request.urlopen(f"{base}/api/v1/artifacts") as response:
        payload = json.loads(response.read())
    assert len(payload["artifacts"]) == 1
    assert payload["artifacts"][0]["original_filename"] == "catalog.txt"


def test_artifact_catalog_filters_server_side_before_returning_records(server):
    base, repo = server
    upload(base, b"one", filename="alpha.html", mime="text/html")
    upload(base, b"two", filename="beta.pdf", mime="application/pdf")
    with urllib.request.urlopen(f"{base}/api/v1/artifacts?q=alpha&type=other") as response:
        payload = json.loads(response.read())
    assert len(payload["artifacts"]) == 1
    assert payload["artifacts"][0]["original_filename"] == "alpha.html"


# ---- durable object-store wiring -------------------------------------------


def test_upload_with_rustfs_persists_payload_to_object_store(s3_server):
    base, repo, fs = s3_server
    data = b"durable-payload-bytes"
    status, body = upload(base, data, filename="f.bin")
    assert status == 200
    art = json.loads(body)
    assert art["storage_key"]
    assert art["sha256"]
    assert art["size_bytes"] == len(data)
    # bytes live in the object store, NOT the repo's in-memory blob stand-in
    assert repo.get_artifact_data(art["artifact_id"]) is None
    assert art["storage_key"] in fs.objects
    # download resolves the payload from the object store
    req = urllib.request.Request(
        f"{base}/api/v1/artifacts/{art['artifact_id']}/download")
    with urllib.request.urlopen(req) as r:
        assert r.read() == data
        assert r.headers["Content-Type"].startswith("image/png")


def test_upload_with_rustfs_is_content_addressed(s3_server):
    base, _, fs = s3_server
    _, b1 = upload(base, b"same-bytes")
    _, b2 = upload(base, b"same-bytes")
    a1, a2 = json.loads(b1), json.loads(b2)
    # identical payloads dedupe to the same content-addressed key
    assert a1["storage_key"] == a2["storage_key"]
    assert a1["sha256"] == a2["sha256"]
    assert len(fs.objects) == 1


def test_health_reports_s3_backend_when_rustfs_wired(s3_server):
    base, _, _ = s3_server
    with urllib.request.urlopen(f"{base}/api/v1/health") as r:
        assert json.loads(r.read())["rustfs"] == "s3"


def test_health_reports_memory_backend_without_rustfs(server):
    base, _ = server
    with urllib.request.urlopen(f"{base}/api/v1/health") as r:
        assert json.loads(r.read())["rustfs"] == "memory"

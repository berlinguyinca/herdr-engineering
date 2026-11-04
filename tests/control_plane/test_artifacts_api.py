import asyncio
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


class _VersionApiStore:
    def __init__(self):
        self.commits = []

    async def list_versions(self, artifact_id):
        return [{"version_id": "ver_1", "artifact_id": artifact_id,
                 "version_number": 1, "state": "ready"}]

    async def report_permission(self, artifact_id, principal_id, permission):
        return artifact_id == "artifact_1"

    async def get_version(self, version_id, artifact_id=None):
        if artifact_id is not None and artifact_id != "artifact_1":
            return None
        return {"version_id": version_id, "artifact_id": "artifact_1",
                "version_number": 1, "state": "ready"}

    async def commit_version(self, artifact_id, **body):
        self.commits.append((artifact_id, body))
        return {"version_id": "ver_2", "artifact_id": artifact_id,
                "version_number": 2, "state": "ready"}

    def sync_call(self, method, *args, **kwargs):
        return asyncio.run(getattr(self, method)(*args, **kwargs))


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


def test_version_history_and_commit_use_the_existing_control_plane(server):
    base, _ = server
    # This fixture keeps the API contract independent from PostgreSQL; the
    # production wiring passes the durable ArtifactStore implementation.
    server_store = _VersionApiStore()
    # The fixture yields only base/repo, so exercise the route contract through
    # a server constructed with the injected store.
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, artifact_store=server_store)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    api = f"http://127.0.0.1:{svr.port}"
    try:
        with urllib.request.urlopen(f"{api}/api/v1/artifacts/artifact_1/versions") as response:
            assert json.loads(response.read())["versions"][0]["version_id"] == "ver_1"
        body = json.dumps({"base_version_id": "ver_1", "source_manifest_blob": "sha",
                           "output_manifest": []}).encode()
        request = urllib.request.Request(
            f"{api}/api/v1/artifacts/artifact_1/versions", data=body,
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            assert response.status == 201
            assert json.loads(response.read())["version_id"] == "ver_2"
    finally:
        svr.shutdown()


def test_publication_request_and_approval_use_control_plane_routes():
    repo = ControlPlaneRepo()
    controller = PublicationController(provider=FakeExposureProvider())
    svr = ControlPlaneServer(repo, publication_controller=controller)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    api = f"http://127.0.0.1:{svr.port}"
    try:
        body = json.dumps({"resource_id": "artifact_1", "version_id": "version_1",
                           "audience": "unlisted_link",
                           "expires_at": __import__("time").time() + 3600}).encode()
        request = urllib.request.Request(
            f"{api}/api/v1/publications/requests", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            pending = json.loads(response.read())
            assert response.status == 201
            assert pending["state"] == "requested"
        approve = urllib.request.Request(
            f"{api}/api/v1/publications/{pending['publication_id']}/approve",
            data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(approve) as response:
            assert json.loads(response.read())["state"] == "active"
    finally:
        svr.shutdown()


def test_artifact_job_submit_get_and_cancel_routes():
    repo = ControlPlaneRepo()
    jobs = ArtifactJobManager()
    svr = ControlPlaneServer(repo, artifact_job_manager=jobs)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    api = f"http://127.0.0.1:{svr.port}"
    try:
        body = json.dumps({"profile": "web", "artifact_id": "artifact_1",
                           "base_version_id": None, "request": {}}).encode()
        create = urllib.request.Request(
            f"{api}/api/v1/artifact-jobs", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(create) as response:
            job = json.loads(response.read())
            assert response.status == 201
        with urllib.request.urlopen(f"{api}/api/v1/artifact-jobs/{job['job_id']}") as response:
            assert json.loads(response.read())["status"] == "queued"
        cancel = urllib.request.Request(
            f"{api}/api/v1/artifact-jobs/{job['job_id']}/cancel",
            data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(cancel) as response:
            assert json.loads(response.read())["status"] == "cancelled"
    finally:
        svr.shutdown()


def test_artifact_catalog_applies_acl_before_returning_records():
    repo = ControlPlaneRepo()
    artifact_id = repo.register_artifact(original_filename="private.txt", data=b"private",
                                          mime_type="text/plain")
    acl = ArtifactAcl()
    svr = ControlPlaneServer(repo, api_keys={"secret": "viewer"}, artifact_acl=acl)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    api = f"http://127.0.0.1:{svr.port}"
    try:
        request = urllib.request.Request(f"{api}/api/v1/artifacts",
                                         headers={"X-API-Key": "secret"})
        with urllib.request.urlopen(request) as response:
            assert json.loads(response.read())["artifacts"] == []
        principal = "api-key:"
        import hashlib
        principal += hashlib.sha256(b"secret").hexdigest()[:16] + ":viewer"
        acl.grant(artifact_id, principal, {"read"})
        with urllib.request.urlopen(request) as response:
            assert json.loads(response.read())["artifacts"][0]["artifact_id"] == artifact_id
    finally:
        svr.shutdown()

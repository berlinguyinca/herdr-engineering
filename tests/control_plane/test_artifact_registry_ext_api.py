"""HTTP surface of the registry extensions: auth, ACL, uploads, lifecycle, reconcile, agent-role limits."""
import hashlib
import http.client
import json
import threading
from contextlib import contextmanager
from urllib.parse import quote

import asyncpg
import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_adapter import SyncArtifactAdapter
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.registry_pg import FakeBlobs, registry_pg, uid  # noqa: F401 - imported fixture

KEYS = {"owner": "operator", "other": "operator", "agent": "agent"}


def principal(key):
    return f"api-key:{hashlib.sha256(key.encode()).hexdigest()[:16]}:{KEYS[key]}"


@contextmanager
def running(dsn, blobs):
    async def factory():
        return ArtifactStore(await asyncpg.connect(dsn), blobs)

    adapter = SyncArtifactAdapter(factory)
    acl = ArtifactAcl(on_change=lambda action, payload: adapter("record_acl_event", action, payload))
    server = ControlPlaneServer(ControlPlaneRepo(), artifact_store=adapter, artifact_acl=acl, rustfs=blobs, api_keys=KEYS)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        yield server, adapter, acl
    finally:
        server.shutdown()
        thread.join(3)
        adapter.close()


def call(server, method, path, body=None, key="owner", raw=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    payload = raw if raw is not None else json.dumps(body).encode() if body is not None else None
    head = {"X-API-Key": key, "Content-Type": "application/octet-stream" if raw is not None else "application/json"}
    head.update(headers or {})
    conn.request(method, path, body=payload, headers=head)
    response = conn.getresponse()
    content = response.read()
    conn.close()
    try:
        return response.status, json.loads(content)
    except ValueError:
        return response.status, content


def make_artifact(server, adapter, acl, blobs, task, name="a.md", media="text/markdown", data=b"hello", key="owner"):
    owner = principal(key)
    artifact = adapter("register_logical", original_filename=name, created_by=owner, mime_type=media)
    acl.grant(artifact["artifact_id"], owner, {"read", "edit", "review", "share", "admin"})
    storage_key, sha = blobs.put(data)
    status, version = call(server, "POST", f"/api/v1/artifacts/{artifact['artifact_id']}/versions", key=key, body={
        "base_version_id": None, "source_manifest_blob": None,
        "output_manifest": [{"name": name, "storage_key": storage_key, "sha256": sha, "size_bytes": len(data), "media_type": media}],
        "origin": {"task_id": task, "project_id": "proj-" + task, "issue_ref": "acme/app#7", "model_id": "m"}})
    assert status == 201, version
    return artifact["artifact_id"], version


def test_lookup_routes_are_acl_filtered_and_missing_equals_forbidden(registry_pg):
    blobs, task = FakeBlobs(), uid("task")
    with running(registry_pg["dsn"], blobs) as (server, adapter, acl):
        artifact_id, version = make_artifact(server, adapter, acl, blobs, task)
        assert version["origin_task_id"] == task and version["model_id"] == "m"
        for kind, ref in (("task", task), ("project", "proj-" + task), ("issue", "acme/app#7")):
            status, page = call(server, "GET", f"/api/v1/artifacts/for/{kind}/{quote(ref, safe='')}")
            assert status == 200 and [a["artifact_id"] for a in page["artifacts"]] == [artifact_id] and page["total"] == 1
        status, hidden = call(server, "GET", f"/api/v1/artifacts/for/task/{task}", key="other")
        status2, missing = call(server, "GET", f"/api/v1/artifacts/for/task/{uid('nope')}", key="other")
        assert status == status2 == 200 and hidden["artifacts"] == missing["artifacts"] == [] and hidden["total"] == missing["total"] == 0
        assert call(server, "GET", f"/api/v1/artifacts/for/bogus/{task}")[0] == 400
        assert call(server, "GET", f"/api/v1/artifacts/for/task/{task}?cursor=%%%")[0] in {400}
        assert call(server, "GET", f"/api/v1/artifacts/{artifact_id}/links", key="other")[0] == 404
        assert call(server, "POST", f"/api/v1/artifacts/{artifact_id}/links", {"task_id": "t"}, key="other")[0] == 404
        # Grant read through the existing ACL route: now visible, and revocation hides it again.
        assert call(server, "POST", f"/api/v1/artifacts/{artifact_id}/acl",
                    {"principal_id": principal("other"), "permissions": ["read"]})[0] == 201
        assert call(server, "GET", f"/api/v1/artifacts/for/task/{task}", key="other")[1]["total"] == 1
        assert call(server, "POST", f"/api/v1/artifacts/{artifact_id}/acl/revoke", {"principal_id": principal("other")})[0] == 200
        assert call(server, "GET", f"/api/v1/artifacts/for/task/{task}", key="other")[1]["total"] == 0
        # A read grant is not enough to link, archive or tombstone.
        call(server, "POST", f"/api/v1/artifacts/{artifact_id}/acl", {"principal_id": principal("other"), "permissions": ["read"]})
        assert call(server, "POST", f"/api/v1/artifacts/{artifact_id}/links", {"task_id": "t"}, key="other")[0] == 404
        assert call(server, "POST", f"/api/v1/artifacts/{artifact_id}/archive", {}, key="other")[0] == 404
        status, linked = call(server, "POST", f"/api/v1/artifacts/{artifact_id}/links",
                              {"task_id": "extra-" + task, "source_event_id": "e-1"})
        assert status == 201 and linked["linked"] is True
        assert call(server, "POST", f"/api/v1/artifacts/{artifact_id}/links", {"task_id": "extra-" + task, "source_event_id": "e-1"})[0] == 200
        assert call(server, "POST", f"/api/v1/artifacts/{artifact_id}/links", {"principal": "x", "task_id": "t"})[0] == 400


def test_lifecycle_routes_need_admin_and_hide_from_lookup(registry_pg):
    blobs, task = FakeBlobs(), uid("task")
    with running(registry_pg["dsn"], blobs) as (server, adapter, acl):
        artifact_id, _ = make_artifact(server, adapter, acl, blobs, task)
        base = f"/api/v1/artifacts/{artifact_id}"
        assert call(server, "POST", base + "/archive", {"reason": "tidy", "request_key": "r1"})[1]["lifecycle"] == "archived"
        assert call(server, "GET", f"/api/v1/artifacts/for/task/{task}")[1]["total"] == 0
        assert call(server, "POST", base + "/restore", {})[1]["lifecycle"] == "active"
        assert call(server, "POST", base + "/tombstone", {})[0] == 400                       # reason required
        assert call(server, "POST", base + "/tombstone", {"reason": "bad data"})[1]["lifecycle"] == "tombstoned"
        events = call(server, "GET", base + "/lifecycle")[1]["events"]
        assert [e["action"] for e in events] == ["archive", "restore", "tombstone"]
        assert call(server, "GET", base + "/lifecycle", key="other")[0] == 404
        assert call(server, "POST", base + "/tombstone", {"reason": "x"}, key="agent")[0] == 403      # agents never delete


def test_resumable_upload_over_http_then_commit_and_agent_may_upload_not_reconcile(registry_pg):
    blobs = FakeBlobs()
    data = bytes(range(50))
    digest = hashlib.sha256(data).hexdigest()
    with running(registry_pg["dsn"], blobs) as (server, adapter, acl):
        agent = principal("agent")
        artifact = adapter("register_logical", original_filename="up.bin", created_by=agent)
        acl.grant(artifact["artifact_id"], agent, {"read", "edit"})
        base = f"/api/v1/artifacts/{artifact['artifact_id']}/uploads"
        status, created = call(server, "POST", base, {"filename": "up.bin", "media_type": "application/octet-stream",
                                                      "size_bytes": 50, "sha256": digest, "part_size": 20},
                               key="agent", headers={"Idempotency-Key": "k"})
        assert status == 201 and created["part_count"] == 3
        upload = created["upload_id"]
        assert call(server, "POST", f"{base}/{upload}/parts/2", raw=data[20:40], key="other")[0] == 404
        assert call(server, "POST", f"{base}/{upload}/parts/2", raw=data[20:40], key="agent",
                    headers={"X-Content-SHA256": hashlib.sha256(data[20:40]).hexdigest()})[0] == 201
        assert call(server, "POST", f"{base}/{upload}/parts/2", raw=data[20:40], key="agent")[0] == 200     # replay
        assert call(server, "POST", f"{base}/{upload}/parts/2", raw=b"z" * 20, key="agent")[0] == 409
        assert call(server, "GET", f"{base}/{upload}", key="agent")[1]["missing"] == [1, 3]
        assert call(server, "POST", f"{base}/{upload}/complete", {}, key="agent")[0] == 400                 # parts missing
        call(server, "POST", f"{base}/{upload}/parts/1", raw=data[:20], key="agent")
        call(server, "POST", f"{base}/{upload}/parts/3", raw=data[40:], key="agent")
        status, done = call(server, "POST", f"{base}/{upload}/complete", {}, key="agent")
        assert status == 200 and done["sha256"] == digest and blobs.objects[done["storage_key"]] == data
        assert call(server, "POST", "/api/v1/artifacts/reconcile", {}, key="agent")[0] == 403
        status, report = call(server, "POST", "/api/v1/artifacts/reconcile", {})
        assert status == 200 and {"relinked", "orphans_marked", "purged"} <= set(report)
        assert call(server, "POST", f"{base}/{upload}/abort", {}, key="agent")[1]["state"] == "complete"

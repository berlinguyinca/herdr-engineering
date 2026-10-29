"""The mail-attachment, retraction and backfill routes against the REAL ``ArtifactStore`` on an isolated PostgreSQL.

The server holds the production ``SyncArtifactAdapter`` over ``ArtifactStore(asyncpg connection, rustfs)``; only the object
store (RustFS/S3) is a fake. Skips (never silently passes) where the PostgreSQL server binaries are unavailable or the
user is root, exactly like ``test_artifact_report_postgres.py``.
"""
import base64
import hashlib
import json
import threading
import urllib.error
import urllib.request

import asyncpg
import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_adapter import SyncArtifactAdapter
from herdr_engineering.control_plane.artifact_lineage import emit_version_lineage
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_artifact_graph_routes import PNG, acl_id, graph_id, mail
from tests.control_plane.test_artifact_report_postgres import report_postgres  # noqa: F401


class FakeObjects:
    """The S3 surface ArtifactStore uses: put + head."""

    def __init__(self):
        self.objects = {}

    def put(self, data):
        digest = hashlib.sha256(data).hexdigest()
        key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        self.objects[key] = data
        return key, digest

    def head(self, key):
        return key in self.objects


@pytest.fixture
def pg_world(report_postgres):  # noqa: F811
    objects = FakeObjects()

    async def make():
        return ArtifactStore(await asyncpg.connect(report_postgres["dsn"]), objects)

    adapter = SyncArtifactAdapter(make)
    acl = ArtifactAcl()
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"owner-key": "operator", "reader-key": "operator",
                                                              "other-key": "operator"},
                                artifact_store=adapter, rustfs=objects, artifact_acl=acl)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    yield type("W", (), {"base": f"http://127.0.0.1:{server.port}", "adapter": adapter, "acl": acl, "server": server,
                         "objects": objects})
    server.shutdown()


def call(w, path, key="owner-key", body=None):
    req = urllib.request.Request(w.base + path, headers={"X-API-Key": key, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else {})


def node(w, node_id, key):
    return call(w, f"/api/v1/graph/nodes/{node_id}", key)[0]


def attach(w, message=None, data=PNG, **extra):
    return call(w, "/api/v1/artifacts/email-attachments", body={
        "message": message or mail(), "part_path": "1.2", "account": "gw@lab.example",
        "data_base64": base64.b64encode(data).decode(), "audience": [acl_id("reader-key")], **extra})


def test_attachment_route_on_the_real_store_versions_scopes_and_retracts_durably(pg_world):
    w = pg_world
    status, first = attach(w)
    assert status == 201, first
    aid, v1 = first["artifact_id"], first["version_id"]
    row = w.adapter("get_version", v1, aid)
    assert row["provenance"]["from"] == "ann@lab.example" and row["created_by"] == acl_id("owner-key")
    assert w.adapter("get_record", aid, principal_id=acl_id("owner-key"))["artifact_type"] == "email_attachment"
    assert node(w, f"artifact:{aid}", "reader-key") == 200 and node(w, f"artifact:{aid}", "other-key") == 404
    # a corrected attachment is a new immutable version with compare-and-swap on the head
    status, second = attach(w, mail("m2"), PNG + b"\x01", previous={"artifact_id": aid, "version_id": v1})
    assert status == 201 and second["version_id"] != v1
    versions = w.adapter("list_versions", aid)
    assert [v["version_number"] for v in versions] == [2, 1]
    assert node(w, f"artifact:{aid}@{v1}", "reader-key") == 200
    # retract the first version: durable in PostgreSQL, hidden in the graph, and a restart (new gate) keeps it hidden
    status, out = call(w, f"/api/v1/artifacts/{aid}/retract", body={"version_id": v1, "reason": "wrong figure"})
    assert status == 200 and out["persisted"], out
    assert node(w, f"artifact:{aid}@{v1}", "reader-key") == 404 and node(w, f"artifact:{aid}", "reader-key") == 200
    assert w.adapter("load_retractions") == [{"artifact_id": aid, "version_id": v1}]
    # artifact-level retraction is a different row, and a version of another artifact is refused
    status, _ = call(w, f"/api/v1/artifacts/{aid}/retract", body={"version_id": "version_missing"})
    assert status == 404
    assert call(w, f"/api/v1/artifacts/{aid}/retract", body={})[0] == 200
    assert {(r["artifact_id"], r["version_id"]) for r in w.adapter("load_retractions")} == {(aid, v1), (aid, None)}
    assert node(w, f"artifact:{aid}", "reader-key") == 404


def commit_directly(w, job_id=None):
    """A version committed straight to the registry, as before artifact scoping (nothing emitted to the graph)."""
    record = w.adapter("register_logical", original_filename="fig.png", artifact_type="email_attachment",
                       created_by=acl_id("owner-key"), mime_type="image/png")
    aid = record["artifact_id"]
    key, digest = w.objects.put(PNG)
    w.acl.grant(aid, acl_id("owner-key"), {"read", "edit", "admin"})
    w.acl.grant(aid, acl_id("reader-key"), {"read"})
    if job_id:
        w.adapter("record_job_event", "created", {
            "job_id": job_id, "artifact_id": aid, "base_version_id": None, "profile": "document", "request": {},
            "status": "queued", "requested_by": acl_id("owner-key"), "created_at": 1790000000.0})
    version = w.adapter("commit_version", aid, base_version_id=None, source_manifest_blob=None, job_id=job_id,
                        output_manifest=[{"name": "fig.png", "media_type": "image/png", "storage_key": key,
                                          "sha256": digest, "size_bytes": len(PNG)}], created_by=acl_id("owner-key"))
    return aid, version


def test_backfill_widens_a_pre_scoping_node_over_the_real_store(pg_world):
    w = pg_world
    aid, version = commit_directly(w)
    owner = {"kind": "agent", "id": graph_id("owner-key"), "authenticated_by": "api_key"}
    assert emit_version_lineage(w.server.ingest, version, owner)["scope"] == {"visibility_scope": "private"}
    assert node(w, f"artifact:{aid}", "reader-key") == 404 and node(w, f"artifact:{aid}", "owner-key") == 200
    status, out = call(w, f"/api/v1/artifacts/{aid}/lineage/backfill", body={})
    assert status == 200 and out["emitted"] == 1 and out["errors"] == [], out
    assert node(w, f"artifact:{aid}", "reader-key") == 200 and node(w, f"artifact:{aid}", "other-key") == 404


def test_the_worker_lineage_route_checks_the_job_against_the_real_registry(pg_world):
    w = pg_world
    aid, version = commit_directly(w, job_id="job_pg")
    assert node(w, f"artifact:{aid}", "reader-key") == 404                 # the worker committed; nothing in the graph yet
    status, out = call(w, "/api/v1/artifact-jobs/job_pg/lineage", body={"version_id": version["version_id"]})
    assert status == 200 and out["error"] is None, out
    assert node(w, f"artifact:{aid}", "reader-key") == 200
    assert call(w, "/api/v1/artifact-jobs/job_pg/lineage", body={"version_id": "version_other"})[0] == 404
    assert call(w, "/api/v1/artifact-jobs/job_pg/lineage", key="other-key",
                body={"version_id": version["version_id"]})[0] == 404

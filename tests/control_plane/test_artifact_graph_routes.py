"""Artifact <-> lineage graph, through the REAL HTTP routes (no direct calls into the domain modules).

* retract / backfill:   POST /api/v1/artifacts/<id>/retract, POST /api/v1/artifacts/<id>/lineage/backfill
* render-worker ingest: POST /api/v1/artifact-jobs/<job>/lineage (and the worker's own HTTP poster)
* mail attachments:     POST /api/v1/artifacts/email-attachments
The registry is an in-memory stand-in with the same method surface the server calls (the Postgres twin of the mail path is
in test_email_attachment_postgres.py).
"""
import asyncio
import base64
import hashlib
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_jobs import ArtifactJobManager
from herdr_engineering.control_plane.artifact_lineage import (
    emit_version_lineage,
    lineage_poster_from_section,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo

DIGEST = "c" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
OUTPUT = [{"name": "p.png", "sha256": DIGEST, "storage_key": f"sha256/cc/cc/{DIGEST}", "size_bytes": 3}]


def acl_id(key, role="operator"):
    return f"api-key:{hashlib.sha256(key.encode()).hexdigest()[:16]}:{role}"


def graph_id(key):
    return "key:" + hashlib.sha256(key.encode()).hexdigest()[:12]


class FakeRustFS:
    def __init__(self):
        self.objects = {}

    def put(self, data):
        digest = hashlib.sha256(data).hexdigest()
        key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        self.objects[key] = data
        return key, digest


class FakeStore:
    """The registry methods the server calls, in memory. ``sync_call`` mirrors the real adapter's call shape."""

    def __init__(self):
        self.artifacts, self.versions, self.jobs, self.retractions = {}, {}, {}, set()

    def sync_call(self, method, *args, **kwargs):
        return asyncio.run(getattr(self, method)(*args, **kwargs))

    async def report_permission(self, aid, principal, permission):
        return principal == acl_id("owner-key")          # the registry's own durable owner

    async def register_logical(self, **kw):
        aid = f"artifact_{len(self.artifacts) + 1}"
        self.artifacts[aid] = {**kw, "artifact_id": aid}
        return {"artifact_id": aid, **kw}

    async def commit_version(self, artifact_id, **kw):
        self.artifacts.setdefault(artifact_id, {"artifact_id": artifact_id})
        n = len([v for v in self.versions.values() if v["artifact_id"] == artifact_id]) + 1
        parent = kw.get("base_version_id")
        version = {"artifact_id": artifact_id, "version_id": f"{artifact_id}_v{n}", "version_number": n,
                   "parent_version_id": parent, "output_manifest": kw.get("output_manifest") or [],
                   "provenance": kw.get("provenance") or {}, "created_by": kw.get("created_by"),
                   "job_id": kw.get("job_id")}
        self.versions[version["version_id"]] = version
        return version

    async def get_version(self, version_id, artifact_id=None):
        v = self.versions.get(version_id)
        return v if v and (artifact_id is None or v["artifact_id"] == artifact_id) else None

    async def list_versions(self, artifact_id):
        return sorted((v for v in self.versions.values() if v["artifact_id"] == artifact_id),
                      key=lambda v: -v["version_number"])

    async def get_record(self, artifact_id, *, principal_id):
        return self.artifacts.get(artifact_id)

    async def get_job(self, job_id):
        return self.jobs.get(job_id)

    async def record_retraction(self, artifact_id, version_id=None, reason=""):
        self.retractions.add((artifact_id, version_id))

    async def load_retractions(self):
        return [{"artifact_id": a, "version_id": v} for a, v in sorted(self.retractions, key=repr)]


@pytest.fixture
def world():
    store, fs, acl = FakeStore(), FakeRustFS(), ArtifactAcl()
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"owner-key": "operator", "reader-key": "operator",
                                                              "other-key": "operator"},
                                artifact_store=store, rustfs=fs, artifact_acl=acl,
                                artifact_job_manager=ArtifactJobManager())
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    yield type("W", (), {"base": f"http://127.0.0.1:{server.port}", "store": store, "fs": fs, "acl": acl,
                         "server": server})
    server.shutdown()


def call(w, path, *, key="owner-key", body=None):
    headers = {"X-API-Key": key}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(w.base + path, headers=headers, data=json.dumps(body).encode() if body is not None
                                 else None, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else {})


def node(w, node_id, key="owner-key"):
    return call(w, f"/api/v1/graph/nodes/{node_id}", key=key)[0]


def commit(w, artifact="artifact_1", key="owner-key", **extra):
    return call(w, f"/api/v1/artifacts/{artifact}/versions", key=key,
                body={"base_version_id": extra.pop("base", None), "source_manifest_blob": None,
                      "output_manifest": OUTPUT, **extra})


def grant_owner(w, artifact="artifact_1"):
    w.acl.grant(artifact, acl_id("owner-key"), {"read", "edit", "admin"})


def share_with_reader(w, artifact="artifact_1"):
    w.acl.grant(artifact, acl_id("reader-key"), {"read"})


# --------------------------------------------------------------------------------------------- retract
def test_retracting_an_artifact_hides_it_for_everyone_at_the_next_read_and_after_rebuild_and_restart(world):
    w = world
    grant_owner(w)
    share_with_reader(w)
    assert commit(w)[0] == 201
    ids = ["artifact:artifact_1", "artifact:artifact_1@artifact_1_v1"]
    assert [node(w, i, "reader-key") for i in ids] == [200, 200]
    status, out = call(w, "/api/v1/artifacts/artifact_1/retract", body={"reason": "wrong data"})
    assert status == 200 and out["hidden"] and out["persisted"] and out["error"] is None
    for who in ("reader-key", "owner-key", "other-key"):                 # the owner and the admin too: nobody
        assert [node(w, i, who) for i in ids] == [404, 404]
    assert ("artifact_1", None) in w.store.retractions                    # durable in the registry
    w.server.repo._rebuild_graph()                                        # replay from the stored events
    assert [node(w, i, "reader-key") for i in ids] == [404, 404]
    # a restart: a NEW gate loaded from the registry hides it again (and a new gate alone would show it)
    from herdr_engineering.control_plane.artifact_scope import ArtifactGraphGate
    fresh = ArtifactGraphGate(w.acl)
    assert not fresh.artifact_retracted("artifact_1")
    assert fresh.load_retractions(w.store.sync_call("load_retractions")) == 1 and fresh.artifact_retracted("artifact_1")
    # the audit marker is in the event log, bounded, and the version itself is still stored
    assert w.store.versions["artifact_1_v1"]["output_manifest"] == OUTPUT


def test_retracting_an_artifact_blocks_further_commits_to_it(world):
    w = world
    grant_owner(w)
    commit(w)
    call(w, "/api/v1/artifacts/artifact_1/retract", body={})
    status, out = commit(w, base="artifact_1_v1")
    assert status == 409 and "retracted" in out["error"]
    assert node(w, "artifact:artifact_1") == 404


def test_retracting_one_version_hides_it_and_its_supersession_edge_and_a_correction_restores_the_head(world):
    w = world
    grant_owner(w)
    share_with_reader(w)
    commit(w)
    commit(w, base="artifact_1_v1")
    v1, v2 = "artifact:artifact_1@artifact_1_v1", "artifact:artifact_1@artifact_1_v2"
    nb = call(w, f"/api/v1/graph/nodes/{v2}/neighborhood?depth=1", key="reader-key")
    assert any(e["edge_type"] == "supersession" for e in nb[1]["edges"])
    # retract the OLD version: it and its edge vanish, the head and the logical node stay
    status, out = call(w, "/api/v1/artifacts/artifact_1/retract", body={"version_id": "artifact_1_v1"})
    assert status == 200 and out["persisted"]
    assert node(w, v1, "reader-key") == 404 and node(w, v2, "reader-key") == 200
    assert node(w, "artifact:artifact_1", "reader-key") == 200
    nb = call(w, f"/api/v1/graph/nodes/{v2}/neighborhood?depth=1", key="reader-key")[1]
    assert not any(e["edge_type"] == "supersession" for e in nb["edges"]) and v1 not in {n["id"] for n in nb["nodes"]}
    # retract the HEAD: the logical node carries its id and digests, so it is hidden too (fail closed)
    call(w, "/api/v1/artifacts/artifact_1/retract", body={"version_id": "artifact_1_v2"})
    assert node(w, v2, "reader-key") == 404 and node(w, "artifact:artifact_1", "reader-key") == 404
    # a correction replaces the head: the logical node reappears for the ACL readers, the retracted ones stay hidden
    commit(w, base="artifact_1_v2")
    assert node(w, "artifact:artifact_1", "reader-key") == 200
    assert node(w, v1, "reader-key") == 404 and node(w, v2, "reader-key") == 404
    assert node(w, "artifact:artifact_1@artifact_1_v3", "reader-key") == 200


def test_retract_needs_admin_validates_the_body_and_the_version(world):
    w = world
    grant_owner(w)
    share_with_reader(w)
    commit(w)
    assert call(w, "/api/v1/artifacts/artifact_1/retract", key="reader-key", body={})[0] == 404   # read-only: no admin
    assert call(w, "/api/v1/artifacts/artifact_1/retract", key="other-key", body={})[0] == 404
    assert node(w, "artifact:artifact_1", "reader-key") == 200                  # nothing happened
    assert call(w, "/api/v1/artifacts/artifact_1/retract", body={"scope": "all"})[0] == 400
    assert call(w, "/api/v1/artifacts/artifact_1/retract", body={"version_id": 7})[0] == 400
    assert call(w, "/api/v1/artifacts/artifact_1/retract", body={"version_id": "nope"})[0] == 404
    assert node(w, "artifact:artifact_1", "reader-key") == 200


def test_as_of_replay_cannot_resurrect_a_retracted_node():
    """A replayed (historical) projection reads through the CURRENT projection, so the retraction holds in the past."""
    from herdr_engineering.control_plane.artifact_scope import install_gate, visibility_fields
    from herdr_engineering.control_plane.ingest import IngestService
    from tests.control_plane.test_graph import _AnyKey
    repo = ControlPlaneRepo()
    svc = IngestService(repo, key_scopes=_AnyKey())
    acl = ArtifactAcl()
    gate = install_gate(repo.graph, acl)
    owner = {"kind": "agent", "id": "key:aaaaaaaaaaaa", "authenticated_by": "api_key"}
    member = {"kind": "member", "id": "user:alice", "authenticated_by": "collab",
              "access": {"rooms": frozenset({"r1"}), "room_ws": frozenset(), "room_missions": frozenset()}}
    acl.grant("art_1", "api-key:aaaaaaaaaaaa1111:agent", {"read", "edit", "admin"})
    acl.grant("art_1", "user:alice", {"read"})
    v = {"version_id": "ver_1", "artifact_id": "art_1", "version_number": 1, "output_manifest": []}
    emit_version_lineage(svc, v, owner, scope=visibility_fields(gated=True, room_id="r1"))
    ctx = frozenset({("room", "r1", None)})
    before = repo.graph.room_lineage(member, ctx)
    assert {n["id"] for n in before["nodes"]} >= {"artifact:art_1", "artifact:art_1@ver_1"}
    step = before["as_of"]["visible_events"]
    gate.retract("art_1")
    for past in (repo.graph.room_lineage(member, ctx, step=step - 1), repo.graph.room_lineage(member, ctx, step=1),
                 repo.graph.room_lineage(member, ctx)):
        assert not {n["id"] for n in past["nodes"]} & {"artifact:art_1", "artifact:art_1@ver_1"}


# --------------------------------------------------------------------------------------------- backfill
def test_backfill_makes_pre_scoping_private_nodes_readable_by_the_acl_and_is_idempotent(world):
    w = world
    grant_owner(w)
    share_with_reader(w)
    # what the server did BEFORE artifact scoping: the version recorded `private` (the committer alone)
    v1 = asyncio.run(w.store.commit_version("artifact_1", base_version_id=None, output_manifest=OUTPUT,
                                            created_by=acl_id("owner-key")))
    owner = {"kind": "agent", "id": graph_id("owner-key"), "authenticated_by": "api_key"}
    assert emit_version_lineage(w.server.ingest, v1, owner)["scope"] == {"visibility_scope": "private"}
    assert node(w, "artifact:artifact_1", "owner-key") == 200 and node(w, "artifact:artifact_1", "reader-key") == 404
    status, out = call(w, "/api/v1/artifacts/artifact_1/lineage/backfill", body={})
    assert status == 200 and out["emitted"] == 1 and out["errors"] == []
    assert node(w, "artifact:artifact_1", "reader-key") == 200
    assert node(w, "artifact:artifact_1@artifact_1_v1", "reader-key") == 200
    assert node(w, "artifact:artifact_1", "other-key") == 404                  # still only the ACL
    before = json.dumps(w.server.repo.graph.snapshot(), sort_keys=True, default=str)
    assert call(w, "/api/v1/artifacts/artifact_1/lineage/backfill", body={})[1]["emitted"] == 1
    assert json.dumps(w.server.repo.graph.snapshot(), sort_keys=True, default=str) == before   # replay deduped


def test_backfill_skips_retracted_versions_and_artifacts_and_needs_admin(world):
    w = world
    grant_owner(w)
    share_with_reader(w)
    commit(w)
    commit(w, base="artifact_1_v1")
    call(w, "/api/v1/artifacts/artifact_1/retract", body={"version_id": "artifact_1_v1"})
    out = call(w, "/api/v1/artifacts/artifact_1/lineage/backfill", body={})[1]
    assert out["emitted"] == 1 and out["skipped_retracted"] == 1
    assert node(w, "artifact:artifact_1@artifact_1_v1", "reader-key") == 404
    assert call(w, "/api/v1/artifacts/artifact_1/lineage/backfill", key="reader-key", body={})[0] == 404
    call(w, "/api/v1/artifacts/artifact_1/retract", body={})
    assert call(w, "/api/v1/artifacts/artifact_1/lineage/backfill", body={})[1]["skipped_retracted"] == 2


# --------------------------------------------------------------------------------------------- render worker
def add_job(w, job_id="job_1", artifact="artifact_1"):
    w.store.jobs[job_id] = {"job_id": job_id, "artifact_id": artifact, "profile": "document", "status": "running"}
    return asyncio.run(w.store.commit_version(artifact, base_version_id=None, output_manifest=OUTPUT, job_id=job_id,
                                              created_by=acl_id("owner-key")))


def test_a_worker_result_reaches_the_graph_over_the_authenticated_route_idempotently_and_acl_checked(world):
    w = world
    grant_owner(w)
    share_with_reader(w)
    version = add_job(w)
    assert node(w, "artifact:artifact_1", "reader-key") == 404                # committed by the worker, not yet in the graph
    status, out = call(w, "/api/v1/artifact-jobs/job_1/lineage", body={"version_id": version["version_id"]})
    assert status == 200 and out["error"] is None and out["scope"] == {"visibility_scope": "org"}
    assert node(w, "artifact:artifact_1", "reader-key") == 200
    snapshot = json.dumps(w.server.repo.graph.snapshot(), sort_keys=True, default=str)
    assert call(w, "/api/v1/artifact-jobs/job_1/lineage", body={"version_id": version["version_id"]})[0] == 200
    assert json.dumps(w.server.repo.graph.snapshot(), sort_keys=True, default=str) == snapshot   # idempotent
    attrs = call(w, "/api/v1/graph/nodes/artifact:artifact_1", key="reader-key")[1]["node"]["attrs"]
    assert attrs["content_digests"] == [DIGEST] and attrs["job_id"] == "job_1"   # facts came from the registry


def test_the_worker_route_refuses_what_the_caller_cannot_edit_or_did_not_produce(world):
    w = world
    grant_owner(w)
    version = add_job(w)
    other = asyncio.run(w.store.commit_version("artifact_1", base_version_id=version["version_id"],
                                               output_manifest=OUTPUT, job_id="job_other"))
    path = "/api/v1/artifact-jobs/job_1/lineage"
    assert call(w, path, key="other-key", body={"version_id": version["version_id"]})[0] == 404   # no edit grant
    assert call(w, path, body={"version_id": other["version_id"]})[0] == 409                      # another job's version
    assert call(w, path, body={"version_id": "missing"})[0] == 404
    assert call(w, "/api/v1/artifact-jobs/job_none/lineage", body={"version_id": "x"})[0] == 404
    for bad in ({}, {"version_id": ""}, {"version_id": version["version_id"], "output_manifest": []},
                {"version_id": version["version_id"], "artifact_id": "artifact_9"}):
        assert call(w, path, body=bad)[0] == 400                                                  # nothing but the id
    assert node(w, "artifact:artifact_1") == 404                                                  # nothing was written
    call(w, "/api/v1/artifacts/artifact_1/retract", body={})
    assert call(w, path, body={"version_id": version["version_id"]})[0] == 409


def test_the_workers_own_http_poster_delivers_a_committed_version(world, tmp_path):
    w = world
    grant_owner(w)
    share_with_reader(w)
    version = add_job(w)
    key_file = tmp_path / "worker.key"
    key_file.write_text("owner-key\n")
    post = lineage_poster_from_section({"api_url": w.base, "key_file": str(key_file), "timeout_seconds": 5})
    assert post({**version}) ["error"] is None
    assert node(w, "artifact:artifact_1", "reader-key") == 200
    assert post({"artifact_id": "artifact_1"}) == {"skipped": "version has no job_id/version_id"}
    assert lineage_poster_from_section(None) is None


@pytest.mark.parametrize("section", [{"api_url": "ftp://x", "key_file": "k"}, {"api_url": "http://x"},
                                     {"api_url": "http://x", "key_file": "k", "extra": 1},
                                     {"api_url": "http://x", "key_file": "k", "timeout_seconds": 0}])
def test_the_poster_config_is_strict(section):
    with pytest.raises(ValueError):
        lineage_poster_from_section(section)


def test_the_render_runner_hook_can_be_the_poster(world, tmp_path):
    """ArtifactRenderRunner(lineage=poster): the exact production wiring of `_cmd_cp_worker`."""
    from tests.control_plane.test_artifact_render_lineage import _job, _Renderer, _RustFS, _Store
    w = world
    grant_owner(w)
    share_with_reader(w)
    key_file = tmp_path / "worker.key"
    key_file.write_text("owner-key")
    from herdr_engineering.control_plane.artifact_render_runner import ArtifactRenderRunner
    version = add_job(w, "job_9")
    store = _Store()
    seen = []
    poster = lineage_poster_from_section({"api_url": w.base, "key_file": str(key_file)})
    runner = ArtifactRenderRunner(store, _Renderer(), lineage=lambda v: seen.append(poster(
        {**v, "job_id": "job_9", "version_id": version["version_id"]})))
    asyncio.run(runner(_job(job_id="job_9")))
    assert seen and seen[0]["error"] is None and node(w, "artifact:artifact_1", "reader-key") == 200
    _ = _RustFS


# --------------------------------------------------------------------------------------------- mail attachments
def mail(mid="m1", thread="t1", filename="Fig 1.png", subject="results"):
    return {"id": mid, "threadId": thread, "internalDate": "1790000000000",
            "payload": {"mimeType": "multipart/mixed",
                        "headers": [{"name": "From", "value": "Ann <ann@lab.example>"},
                                    {"name": "Subject", "value": subject},
                                    {"name": "Message-ID", "value": "<abc@lab.example>"}],
                        "parts": [{"mimeType": "text/plain",
                                   "body": {"data": base64.urlsafe_b64encode(b"see attached").decode()}},
                                  {"mimeType": "image/png", "filename": filename,
                                   "body": {"attachmentId": "att1", "size": len(PNG)}}]}}


def attach(w, *, key="owner-key", data=PNG, audience=None, **extra):
    body = {"message": mail(), "part_path": "1.2", "account": "gw@lab.example",
            "data_base64": base64.b64encode(data).decode(),
            "audience": [acl_id("reader-key")] if audience is None else audience, **extra}
    return call(w, "/api/v1/artifacts/email-attachments", key=key, body=body)


def test_mail_attachment_route_stores_a_versioned_artifact_readable_only_by_the_named_audience(world):
    w = world
    status, out = attach(w)
    assert status == 201 and out["verdict"] == "accept" and out["graph"]["error"] is None
    aid, vid = out["artifact_id"], out["version_id"]
    assert sorted(out["audience"]) == sorted([acl_id("owner-key"), acl_id("reader-key")])
    assert w.acl.can(aid, acl_id("owner-key"), "admin") and not w.acl.can(aid, acl_id("reader-key"), "edit")
    assert node(w, f"artifact:{aid}", "reader-key") == 200 and node(w, f"artifact:{aid}@{vid}", "reader-key") == 200
    assert node(w, f"artifact:{aid}", "other-key") == 404                       # not in the audience: invisible
    blob = json.dumps(call(w, f"/api/v1/graph/nodes/artifact:{aid}", key="reader-key")[1])
    for leaked in ("ann@lab.example", "results", "Fig 1.png", "gw@lab.example"):
        assert leaked not in blob                                             # ids and digests only
    assert w.store.versions[vid]["provenance"]["from"] == "ann@lab.example"   # original metadata is provenance (ACL side)
    # the same delivery again is a duplicate, never a second artifact
    status, again = attach(w)
    assert status == 200 and again["verdict"] == "duplicate" and again["artifact_id"] == aid
    assert len(w.store.artifacts) == 1


def test_mail_attachment_route_derives_the_owner_from_authentication_and_refuses_unsafe_bytes(world):
    w = world
    assert attach(w, owner=acl_id("other-key"))[0] == 400 and attach(w, created_by="x")[0] == 400
    assert attach(w, audience="everyone")[0] == 400
    status, out = attach(w, data=b"<html><script>alert(1)</script></html>")
    assert status == 422 and out["verdict"] in {"quarantine", "reject"} and out["artifact_id"] is None
    assert w.store.artifacts == {}                                            # nothing stored
    assert call(w, "/api/v1/artifacts/email-attachments", body={"message": {}, "data_base64": "!!!"})[0] == 400
    assert call(w, "/api/v1/artifacts/email-attachments", key="nobody", body={})[0] == 401


def test_mail_attachment_correction_is_a_new_version_and_cannot_widen_the_audience(world):
    w = world
    first = attach(w)[1]
    prev = {"artifact_id": first["artifact_id"], "version_id": first["version_id"]}
    fixed = PNG + b"\x01"
    status, out = call(w, "/api/v1/artifacts/email-attachments", body={
        "message": mail("m2"), "part_path": "1.2", "account": "gw@lab.example",
        "data_base64": base64.b64encode(fixed).decode(), "audience": [], "previous": prev})
    assert status == 201 and out["artifact_id"] == first["artifact_id"] and out["version_id"] != first["version_id"]
    status, out = call(w, "/api/v1/artifacts/email-attachments", body={
        "message": mail("m3"), "part_path": "1.2", "account": "gw@lab.example",
        "data_base64": base64.b64encode(PNG + b"\x02").decode(), "audience": [acl_id("other-key")], "previous": prev})
    assert status == 409 and out["verdict"] == "audience_widened"
    status, _ = call(w, "/api/v1/artifacts/email-attachments", key="other-key", body={
        "message": mail("m4"), "part_path": "1.2", "account": "gw@lab.example",
        "data_base64": base64.b64encode(PNG + b"\x03").decode(), "audience": [], "previous": prev})
    assert status == 403                                                      # no edit on that artifact
    call(w, f"/api/v1/artifacts/{first['artifact_id']}/retract", body={})
    status, _ = call(w, "/api/v1/artifacts/email-attachments", body={
        "message": mail("m5"), "part_path": "1.2", "account": "gw@lab.example",
        "data_base64": base64.b64encode(PNG + b"\x04").decode(), "audience": [], "previous": prev})
    assert status == 409

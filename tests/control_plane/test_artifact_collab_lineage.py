"""POST /api/v1/artifacts/<id>/versions/<vid>/collab-lineage and the collaborator-intent poster.

The route runs on the REAL ``ArtifactStore`` (isolated PostgreSQL, migrations 0043 + 0045) behind the production
``SyncArtifactAdapter``; only the object store is a fake. The poster is exercised against a real local HTTP server.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import asyncpg
import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_adapter import SyncArtifactAdapter
from herdr_engineering.control_plane.artifact_lineage import (
    collab_lineage_poster_from_section,
    emit_version_lineage,
    lineage_poster_from_section,
)
from herdr_engineering.control_plane.artifact_scope import visibility_fields
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_artifact_graph_routes import acl_id, graph_id
from tests.control_plane.test_artifact_merge_commit import (  # noqa: F401
    AGENT,
    BASE_DOC,
    COLLAB,
    Texts,
    edit,
    merge_pg,
    parents_postgres,
)
from tests.control_plane.test_artifact_version_parents import _Blobs

OWNER = {"kind": "operator", "id": graph_id("owner-key"), "authenticated_by": "api_key"}


@pytest.fixture
def pg_world(merge_pg):  # noqa: F811
    async def make():
        return ArtifactStore(await asyncpg.connect(merge_pg), _Blobs())

    adapter = SyncArtifactAdapter(make)
    acl = ArtifactAcl()
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"owner-key": "operator", "other-key": "operator"},
                                artifact_store=adapter, artifact_acl=acl)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    yield type("W", (), {"base": f"http://127.0.0.1:{server.port}", "adapter": adapter, "acl": acl,
                         "server": server, "texts": Texts()})
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


def seed(w):
    """An artifact with an owner v1, an owner v2 (the head moved) and a collaborator merge v3 from the stale v1."""
    owner = acl_id("owner-key")
    aid = w.adapter("register_logical", original_filename="doc.md", created_by=owner)["artifact_id"]
    w.acl.grant(aid, owner, {"read", "edit", "admin"})
    v1 = w.adapter("commit_version", aid, base_version_id=None, source_manifest_blob=None, created_by=owner,
                   output_manifest=[w.texts.entry("doc.md", BASE_DOC)])
    v2 = w.adapter("commit_version", aid, base_version_id=v1["version_id"], source_manifest_blob=None,
                   created_by=owner, output_manifest=[w.texts.entry("doc.md", edit(BASE_DOC, 9, "owner"))])
    for owner_version in (v1, v2):                  # the owner commit route records these; the parents must be in the graph
        emit_version_lineage(w.server.ingest, owner_version, OWNER, scope=visibility_fields(gated=True))
    mine = w.texts.entry("doc.md", edit(BASE_DOC, 2, "mine"))
    out = w.adapter("merge_commit", aid, base_version_id=v1["version_id"], message="m", created_by=COLLAB,
                    changes=[{"name": "doc.md", "op": "put", "entry": mine}], read_text=w.texts.read_text,
                    store_text=w.texts.store_text, collaborator_agent_id=AGENT)
    assert out["result"] == "merged"
    return aid, v1, v2, out["version"]


def path(aid, vid):
    return f"/api/v1/artifacts/{aid}/versions/{vid}/collab-lineage"


def edges(w, aid, vid):
    nb = call(w, f"/api/v1/graph/nodes/artifact:{aid}@{vid}/neighborhood?depth=1")[1]
    return [e for e in nb["edges"] if e["edge_type"] == "supersession" and e["from_id"] == f"artifact:{aid}@{vid}"]


def test_a_collaborator_merge_emits_one_supersession_edge_per_parent(pg_world):
    w = pg_world
    aid, v1, v2, v3 = seed(w)
    status, out = call(w, path(aid, v3["version_id"]), body={})
    assert status == 200 and out["error"] is None, out
    assert len(edges(w, aid, v3["version_id"])) == 2
    assert call(w, path(aid, v3["version_id"]), body={})[0] == 200          # replay is idempotent
    assert len(edges(w, aid, v3["version_id"])) == 2


def test_an_owner_version_is_refused(pg_world):
    w = pg_world
    aid, v1, v2, _ = seed(w)
    status, out = call(w, path(aid, v2["version_id"]), body={})
    assert status == 409 and "collaborator" in out["error"]


def test_a_retracted_version_is_refused(pg_world):
    w = pg_world
    aid, _, _, v3 = seed(w)
    assert call(w, f"/api/v1/artifacts/{aid}/retract", body={"version_id": v3["version_id"]})[0] == 200
    assert call(w, path(aid, v3["version_id"]), body={})[0] == 409


def test_unknown_version_and_a_caller_without_edit_are_both_404(pg_world):
    w = pg_world
    aid, _, _, v3 = seed(w)
    assert call(w, path(aid, "version_missing"), body={})[0] == 404
    assert call(w, path(aid, v3["version_id"]), key="other-key", body={})[0] == 404
    assert call(w, path(aid, "version_missing"), key="other-key", body={})[0] == 404
    assert call(w, path("artifact_missing", v3["version_id"]), body={})[0] == 404


def test_a_version_of_another_artifact_is_404(pg_world):
    w = pg_world
    aid, _, _, v3 = seed(w)
    other, *_ = seed(w)
    assert call(w, path(other, v3["version_id"]), body={})[0] == 404


@pytest.mark.parametrize("body", [{"version_id": "x"}, {"artifact_id": "a"}, {"x": None}])
def test_any_body_key_is_400(pg_world, body):
    w = pg_world
    aid, _, _, v3 = seed(w)
    assert call(w, path(aid, v3["version_id"]), body=body)[0] == 400


# ------------------------------------------------------------------------------------------------ the poster
class Recorder(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).seen.append((self.path, dict(self.headers), body))
        payload = b'{"error": null}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def recorder():
    Recorder.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", Recorder.seen
    httpd.shutdown()


def test_the_poster_posts_an_empty_object_to_the_exact_url(recorder, tmp_path):
    url, seen = recorder
    key_file = tmp_path / "k"
    key_file.write_text("secret-key\n")
    post = collab_lineage_poster_from_section({"api_url": url + "/", "key_file": str(key_file), "timeout_seconds": 5})
    assert post({"artifact_id": "art/1", "version_id": "ver 2"}) == {"error": None}
    [(request_path, headers, body)] = seen
    assert request_path == "/api/v1/artifacts/art%2F1/versions/ver%202/collab-lineage"
    headers = {k.lower(): v for k, v in headers.items()}
    assert headers["x-api-key"] == "secret-key" and headers["content-type"] == "application/json"
    assert json.loads(body) == {}
    assert post({"artifact_id": "a"}) == {"skipped": "version has no artifact_id/version_id"}
    assert post({"version_id": "v"})["skipped"] and len(seen) == 1
    assert collab_lineage_poster_from_section(None) is None


def test_the_poster_drives_the_real_route(pg_world, tmp_path):
    w = pg_world
    aid, _, _, v3 = seed(w)
    key_file = tmp_path / "k"
    key_file.write_text("owner-key")
    post = collab_lineage_poster_from_section({"api_url": w.base, "key_file": str(key_file)})
    assert post(v3)["error"] is None and len(edges(w, aid, v3["version_id"])) == 2


@pytest.mark.parametrize("make", [
    lambda t: {"api_url": "ftp://x", "key_file": "k"},
    lambda t: {"api_url": "http://x"},
    lambda t: {"api_url": "http://x", "key_file": "k", "extra": 1},
    lambda t: {"api_url": "http://x", "key_file": "k", "timeout_seconds": 0},
    lambda t: {"api_url": "http://x", "key_file": "k", "timeout_seconds": True},
    lambda t: {"api_url": "http://x", "key_file": "k", "timeout_seconds": 121},
    lambda t: "not-a-mapping",
    lambda t: (t / "empty").write_text("  \n") and None or {"api_url": "http://x", "key_file": str(t / "empty")},
])
def test_both_posters_refuse_the_same_bad_sections_with_the_same_messages(make, tmp_path):
    section = make(tmp_path)
    messages = []
    for builder in (lineage_poster_from_section, collab_lineage_poster_from_section):
        with pytest.raises(ValueError) as caught:
            builder(section)
        messages.append(str(caught.value))
    assert messages[0] == messages[1]

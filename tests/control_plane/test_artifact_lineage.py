"""Artifact version commits are recorded in the lineage graph (commons `artifact` node, immutable facts)."""
import json

from herdr_engineering.control_plane.artifact_lineage import emit_version_lineage
from herdr_engineering.control_plane.graph import GraphProjection, from_stored
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_graph import OPERATOR, OTHER, WRITER, Feed, _AnyKey

DIGEST = "a" * 64
VERSION = {
    "version_id": "ver_1", "artifact_id": "art_1", "version_number": 3, "parent_version_id": "ver_0",
    "created_by": "key:writer", "agent_id": "a1", "host_id": "h1", "session_id": "s1", "mission_id": "m1",
    "job_id": "j1",
    "output_manifest": [{"storage_key": f"sha256/aa/aa/{DIGEST}", "sha256": DIGEST, "size_bytes": 12,
                         "name": "plot.png"}],
}


def _world():
    repo = ControlPlaneRepo()
    svc = IngestService(repo, key_scopes=_AnyKey())
    return repo, svc


def test_commit_creates_private_artifact_node_with_immutable_facts():
    repo, svc = _world()
    out = emit_version_lineage(svc, VERSION, WRITER)
    assert out["node"] == "artifact:art_1"
    node = repo.graph.get_node(WRITER, "artifact:art_1")["node"]
    attrs = node["attrs"]
    assert attrs["version_id"] == "ver_1" and attrs["version_number"] == 3
    assert attrs["content_digests"] == [DIGEST]
    assert (attrs["mission_id"], attrs["session_id"], attrs["agent_id"], attrs["job_id"]) == ("m1", "s1", "a1", "j1")


def test_no_blob_location_or_file_name_enters_the_graph():
    repo, svc = _world()
    emit_version_lineage(svc, VERSION, WRITER)
    blob = json.dumps(repo.graph.snapshot())
    assert "sha256/aa/aa" not in blob and "plot.png" not in blob


def test_only_the_committing_writer_sees_the_private_artifact_node():
    repo, svc = _world()
    emit_version_lineage(svc, VERSION, WRITER)
    try:
        repo.graph.get_node(OTHER, "artifact:art_1")
    except KeyError:
        pass
    else:
        raise AssertionError("private artifact node leaked to another principal")
    # `private` means the writers only (lineage-events.md, scope table): not even an operator key reads it
    try:
        repo.graph.get_node(OPERATOR, "artifact:art_1")
    except KeyError:
        pass
    else:
        raise AssertionError("private artifact node visible to a non-writer operator")
    assert repo.graph.get_node(WRITER, "artifact:art_1")["node"]["id"] == "artifact:art_1"


def test_repeat_commit_event_is_a_harmless_duplicate():
    repo, svc = _world()
    emit_version_lineage(svc, VERSION, WRITER)
    before = repo.graph.snapshot()
    again = emit_version_lineage(svc, VERSION, WRITER)
    assert again["error"] is None
    assert repo.graph.snapshot() == before


def test_new_version_updates_the_same_node_without_losing_history():
    repo, svc = _world()
    emit_version_lineage(svc, VERSION, WRITER)
    v4 = {**VERSION, "version_id": "ver_2", "version_number": 4, "parent_version_id": "ver_1"}
    emit_version_lineage(svc, v4, WRITER)
    attrs = repo.graph.get_node(WRITER, "artifact:art_1")["node"]["attrs"]
    assert attrs["version_number"] == 4 and attrs["parent_version_id"] == "ver_1"


def test_produced_edge_written_when_producer_node_is_writable():
    repo, svc = _world()
    Feed(svc).node("session:s1", label="S1")             # WRITER owns the producing session
    out = emit_version_lineage(svc, VERSION, WRITER)
    assert out["edge"] == "session:s1"
    view = repo.graph.neighborhood(WRITER, "session:s1", depth=1)
    assert [e["edge_type"] for e in view["edges"]] == ["produced"]


def test_produced_edge_skipped_not_fatal_when_producer_belongs_to_someone_else():
    repo, svc = _world()
    Feed(svc).node("session:s1", label="S1", principal=OTHER)   # someone else owns the session
    out = emit_version_lineage(svc, VERSION, WRITER)
    assert out["node"] == "artifact:art_1" and out["edge"] is None
    assert repo.graph.get_node(WRITER, "artifact:art_1")["node"]["id"] == "artifact:art_1"


def test_emit_never_raises_on_a_broken_ingest():
    class Broken:
        def ingest(self, *_a, **_k):
            raise RuntimeError("down")
    out = emit_version_lineage(Broken(), VERSION, WRITER)
    assert out["node"] is None and "down" in out["error"]


def test_graph_survives_rebuild_from_stored_events():
    repo, svc = _world()
    emit_version_lineage(svc, VERSION, WRITER)
    ref = repo.graph.snapshot()
    events = [e for (k, _), s in repo._events.items() if k == "lineage" for e in s]
    g = GraphProjection()
    for e in events:
        g.apply(from_stored(e))
    assert g.snapshot() == ref


# -- the real HTTP route -----------------------------------------------------------------------------------
from tests.control_plane.test_artifact_reports_api import reports_server, request  # noqa: E402,F401


def _commit_via_http(base, store, key="owner-key"):
    async def commit_version(artifact_id, **kw):
        return {**VERSION, "artifact_id": artifact_id, "created_by": kw["created_by"]}
    store.commit_version = commit_version
    body = {"base_version_id": "ver_0", "source_manifest_blob": None, "output_manifest": VERSION["output_manifest"]}
    return request(base, "/api/v1/artifacts/artifact_report/versions", key=key, body=body)


def test_http_commit_records_the_artifact_in_the_graph_for_the_committer_only(reports_server):  # noqa: F811
    base, store, _ = reports_server
    status, raw, _ = _commit_via_http(base, store)
    assert status == 201, raw
    status, raw, _ = request(base, "/api/v1/graph/nodes/artifact:artifact_report")
    assert status == 200, raw
    attrs = json.loads(raw)["node"]["attrs"]
    assert attrs["version_id"] == "ver_1" and attrs["content_digests"] == [DIGEST]
    assert request(base, "/api/v1/graph/nodes/artifact:artifact_report", key="other-key")[0] == 404


def test_http_commit_still_succeeds_when_graph_emission_fails(reports_server, monkeypatch):  # noqa: F811
    base, store, _ = reports_server
    from herdr_engineering.control_plane import ingest as ingest_module
    monkeypatch.setattr(ingest_module.IngestService, "ingest",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("graph down")))
    assert _commit_via_http(base, store)[0] == 201

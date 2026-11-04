"""The render-runner commit path records the same artifact lineage as the HTTP commit route."""
import asyncio
import hashlib

from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_lineage import lineage_emitter
from herdr_engineering.control_plane.artifact_render_runner import ArtifactRenderRunner
from herdr_engineering.control_plane.artifact_scope import install_gate, visibility_fields
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_artifact_scope import ACL_OWNER, OWNER, READER, STRANGER
from tests.control_plane.test_graph import _AnyKey


class _RustFS:
    def __init__(self):
        self.objects = {}

    def put(self, data):
        digest = hashlib.sha256(data).hexdigest()
        key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        self.objects[key] = data
        return key, digest


class _Store:
    def __init__(self):
        self._rf = _RustFS()
        self.n = 0

    async def commit_version(self, **kw):
        self.n += 1
        return {"version_id": f"ver_{self.n}", "version_number": self.n, "artifact_id": kw["artifact_id"],
                "parent_version_id": kw["base_version_id"], "output_manifest": kw["output_manifest"]}


class _Renderer:
    def render(self, profile, source, request):
        return type("R", (), {"source": source, "outputs": [{"name": "a.txt", "media_type": "text/plain",
                                                              "content": source}],
                              "validation": {"status": "passed"}, "toolchain": {"renderer": "t"}})()


def _job(**kw):
    return {"artifact_id": "art_1", "profile": "web", "requested_by": "api-key:aaaaaaaaaaaa1111:agent",
            "assigned_host_id": "host-a", "job_id": "job-1", "session_id": "s1", "request": {"source": "hello"}, **kw}


def test_render_commit_emits_the_same_lineage_as_the_http_route():
    repo = ControlPlaneRepo()
    svc = IngestService(repo, key_scopes=_AnyKey())
    acl = ArtifactAcl()
    install_gate(repo.graph, acl)
    acl.grant("art_1", ACL_OWNER, {"read", "edit"})
    runner = ArtifactRenderRunner(_Store(), _Renderer(),
                                  lineage=lineage_emitter(svc, OWNER, scope=visibility_fields(gated=True)))
    asyncio.run(runner(_job()))
    asyncio.run(runner(_job(base_version_id="ver_1", job_id="job-2")))
    node = repo.graph.get_node(OWNER, "artifact:art_1")["node"]
    assert node["attrs"]["version_id"] == "ver_2" and node["attrs"]["host_id"] == "host-a"
    assert repo.graph.get_node(OWNER, "artifact:art_1@ver_1")["node"]["attrs"]["version_number"] == 1
    assert repo.graph.get_edge(OWNER, "supersession", "artifact:art_1@ver_2", "artifact:art_1@ver_1")["edges"]
    for other in (READER, STRANGER):
        try:
            repo.graph.get_node(other, "artifact:art_1")
        except KeyError:
            continue
        raise AssertionError("ACL-less principal saw the rendered artifact")


def test_a_failing_lineage_hook_never_fails_the_render_job():
    def boom(_version):
        raise RuntimeError("ingest down")

    result = asyncio.run(ArtifactRenderRunner(_Store(), _Renderer(), lineage=boom)(_job()))
    assert result["version_id"] == "ver_1"


def test_runner_without_a_hook_is_unchanged():
    assert asyncio.run(ArtifactRenderRunner(_Store(), _Renderer())(_job()))["version_id"] == "ver_1"

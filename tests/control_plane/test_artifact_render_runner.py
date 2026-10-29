import asyncio
import json
from pathlib import Path

import pytest

from herdr_engineering.control_plane.artifact_render_runner import ArtifactRenderRunner
from herdr_engineering.control_plane.artifact_renderers import RendererRegistry


class _RustFS:
    def __init__(self):
        self.objects = {}

    def put(self, data):
        key = f"key-{len(self.objects)}"
        digest = f"sha-{len(self.objects)}"
        self.objects[key] = data
        return key, digest


class _Store:
    def __init__(self):
        self._rf = _RustFS()
        self.calls = []
        self.registrations = []

    async def register_report_blob(self, artifact_id, **metadata):
        assert metadata["storage_key"] in self._rf.objects
        assert metadata["size_bytes"] == len(self._rf.objects[metadata["storage_key"]])
        self.registrations.append({"artifact_id": artifact_id, **metadata})

    async def commit_version(self, **kwargs):
        if kwargs.get("report_descriptor") is not None:
            registered = {item["storage_key"] for item in self.registrations
                          if item["artifact_id"] == kwargs["artifact_id"]}
            required = {kwargs["source_manifest_blob"]}
            required.update(output["storage_key"] for output in kwargs["output_manifest"]
                            if output["storage_key"] in self._rf.objects)
            if not required <= registered:
                raise ValueError("report outputs must belong to this artifact")
        self.calls.append(kwargs)
        return {"version_id": "version-2"}


class _Renderer:
    def render(self, profile, source, request):
        return type("Result", (), {
            "source": source,
            "outputs": [{"name": "index.html", "media_type": "text/html",
                         "content": "<html>ok</html>"}],
            "validation": {"status": "passed"},
            "toolchain": {"renderer": "test"},
        })()


def test_render_runner_uploads_source_manifest_and_outputs_before_cas_commit():
    store = _Store()
    runner = ArtifactRenderRunner(store, _Renderer())
    result = asyncio.run(runner({
        "artifact_id": "artifact-1", "base_version_id": "version-1",
        "profile": "web", "requested_by": "agent-1", "assigned_host_id": "host-a",
        "request": {"source": "<html>source</html>"},
    }))
    assert result["version_id"] == "version-2"
    assert len(store._rf.objects) == 3
    commit = store.calls[0]
    assert commit["base_version_id"] == "version-1"
    assert commit["output_manifest"][0]["storage_key"] == "key-2"
    assert commit["provenance"]["host_id"] == "host-a"
    assert store.registrations == []


def test_render_runner_preserves_validation_job_and_explicit_context():
    store = _Store()
    asyncio.run(ArtifactRenderRunner(store, _Renderer())({
        "artifact_id": "artifact-1", "base_version_id": "version-1", "job_id": "job-7",
        "profile": "web", "requested_by": "principal-a", "agent_id": "agent-a",
        "assigned_host_id": "host-a", "session_id": "session-a", "mission_id": "mission-a",
        "request": {"source": "<html>source</html>"},
    }))
    commit = store.calls[0]
    assert commit["validation"] == {"status": "passed"}
    assert commit["job_id"] == "job-7"
    assert commit["session_id"] == "session-a"
    assert commit["mission_id"] == "mission-a"
    assert commit["agent_id"] == "agent-a"
    assert commit["provenance"]["job_id"] == "job-7"
    assert commit["provenance"]["toolchain"]["renderer"] == "test"
    assert commit["idempotency_key"] == "artifact-job:job-7"


def test_render_runner_preserves_large_asset_references_without_fetching_bytes():
    store = _Store()
    asset = {"name": "evidence.csv", "media_type": "text/csv", "sha256": "a" * 64,
             "storage_key": "sha256/aa/aa/" + "a" * 64, "size_bytes": 200 * 1024 * 1024}
    asyncio.run(ArtifactRenderRunner(store, _Renderer())({
        "artifact_id": "artifact-1", "profile": "web", "requested_by": "principal-a",
        "request": {"source": "<html>source</html>", "assets": [asset]},
    }))
    assert len(store._rf.objects) == 3
    commit = store.calls[0]
    assert asset in commit["output_manifest"]
    assert sum(len(value) for value in store._rf.objects.values()) < 2000
    manifest = json.loads(store._rf.objects[commit["source_manifest_blob"]])
    assert manifest["assets"] == [asset]
    assert "session_id" not in commit
    assert "mission_id" not in commit
    assert "agent_id" not in commit


@pytest.mark.parametrize("asset", [
    {"name": "../evidence.csv", "media_type": "text/csv", "sha256": "a" * 64,
     "storage_key": "blob", "size_bytes": 100},
    {"name": "index.html", "media_type": "text/html", "sha256": "a" * 64,
     "storage_key": "blob", "size_bytes": 100},
    {"name": "evidence.csv", "media_type": "text/csv", "sha256": "invalid",
     "storage_key": "blob", "size_bytes": 100},
    {"name": "evidence.csv", "media_type": "text/csv", "sha256": "a" * 64,
     "storage_key": "blob", "size_bytes": 100, "content": "unverified bytes"},
])
def test_render_runner_rejects_bad_or_colliding_asset_manifests_before_upload(asset):
    store = _Store()
    with pytest.raises(ValueError):
        asyncio.run(ArtifactRenderRunner(store, _Renderer())({
            "artifact_id": "artifact-1", "profile": "web",
            "request": {"source": "<html>source</html>", "assets": [asset]},
        }))
    assert store._rf.objects == {}
    assert store.calls == []


def test_descriptor_report_worker_preserves_verified_asset_inventory_and_format_validation():
    store = _Store()
    report = json.loads((Path(__file__).parent / "fixtures/reports/benchmark.json").read_text())
    assets = [{key: asset[key] for key in ("name", "media_type", "sha256", "size_bytes")}
              for asset in report["assets"]]
    for asset in assets:
        digest = asset["sha256"]
        asset["storage_key"] = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
    result = asyncio.run(ArtifactRenderRunner(store, RendererRegistry())({
        "artifact_id": "artifact-1", "base_version_id": "version-1", "job_id": "job-8",
        "profile": "report", "requested_by": "principal-a", "assigned_host_id": "host-a",
        "request": {"report_descriptor": report, "assets": assets},
    }))
    assert result["version_id"] == "version-2"
    assert len(store._rf.objects) == 3
    commit = store.calls[0]
    assert commit["report_descriptor"] == report
    assert commit["validation"]["scope"] == "format-and-render"
    assert "evidence_checked" not in commit["validation"]
    assert commit["output_manifest"][0]["name"] == "preview.html"
    assert commit["output_manifest"][1:] == assets
    manifest = json.loads(store._rf.objects[commit["source_manifest_blob"]])
    assert manifest["profile"] == "report"
    assert manifest["source_media_type"] == "application/json"
    assert manifest["assets"] == assets
    assert json.loads(store._rf.objects[manifest["source_storage_key"]]) == report
    assert len(store.registrations) == 3
    assert {item["artifact_id"] for item in store.registrations} == {"artifact-1"}
    assert {item["storage_key"] for item in store.registrations} == set(store._rf.objects)
    assert [item["media_type"] for item in store.registrations] == [
        "application/json", "application/json", "text/html"]
    assert [item["sha256"] for item in store.registrations] == ["sha-0", "sha-1", "sha-2"]


def test_descriptor_report_missing_evidence_references_fails_before_upload():
    store = _Store()
    report = json.loads((Path(__file__).parent / "fixtures/reports/benchmark.json").read_text())
    with pytest.raises(ValueError, match="matching name"):
        asyncio.run(ArtifactRenderRunner(store, RendererRegistry())({
            "artifact_id": "artifact-1", "profile": "report",
            "request": {"report_descriptor": report, "assets": []},
        }))
    assert store._rf.objects == {}
    assert store.calls == []


def test_report_blob_registration_failure_prevents_version_commit():
    class FailingStore(_Store):
        async def register_report_blob(self, artifact_id, **metadata):
            raise RuntimeError("registration unavailable")

    store = FailingStore()
    report = {"schema_version": 1, "questions": [], "evidence": [],
              "findings": [], "figures": [], "assets": []}
    with pytest.raises(RuntimeError, match="registration unavailable"):
        asyncio.run(ArtifactRenderRunner(store, RendererRegistry())({
            "artifact_id": "artifact-1", "profile": "report",
            "request": {"report_descriptor": report},
        }))
    assert store.calls == []


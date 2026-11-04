import pytest

from herdr_engineering.control_plane.artifact_jobs import (
    ArtifactJobError,
    ArtifactJobManager,
)


def test_submit_places_supported_profile_and_preserves_reproducibility_inputs():
    placed = []
    manager = ArtifactJobManager(enqueue=lambda job: placed.append(job["job_id"]))
    job = manager.submit(
        profile="web", artifact_id="artifact_1", base_version_id="version_1",
        request={"prompt": "build", "source_manifest": "sha-source"},
        requested_by="agent:1")
    assert job["status"] == "queued"
    assert job["profile"] == "web"
    assert job["request"]["source_manifest"] == "sha-source"
    assert placed == [job["job_id"]]


def test_unsupported_profile_and_invalid_transition_are_rejected():
    manager = ArtifactJobManager()
    with pytest.raises(ArtifactJobError, match="unsupported"):
        manager.submit(profile="unknown", artifact_id="a", base_version_id=None,
                       request={}, requested_by="user:1")
    job = manager.submit(profile="document", artifact_id="a", base_version_id=None,
                         request={}, requested_by="user:1")
    manager.claim(job["job_id"], host_id="host-a")
    manager.complete(job["job_id"], result_version_id="version_2",
                     outputs=[{"blob_sha256": "sha"}], validation={"status": "passed"})
    with pytest.raises(ArtifactJobError, match="cannot transition"):
        manager.claim(job["job_id"], host_id="host-b")


def test_failure_is_terminal_and_audit_is_retained():
    manager = ArtifactJobManager()
    job = manager.submit(profile="report", artifact_id="a", base_version_id=None,
                         request={}, requested_by="user:1")
    failed = manager.fail(job["job_id"], error="renderer crashed")
    assert failed["status"] == "failed"
    assert failed["error"] == "renderer crashed"
    assert manager.get(job["job_id"])["status"] == "failed"


def test_execute_renders_and_commits_outputs_through_injected_boundaries():
    manager = ArtifactJobManager()
    job = manager.submit(profile="web", artifact_id="a", base_version_id="v1",
                         request={"source": "<html>ok</html>"}, requested_by="agent:1")
    class Renderer:
        def render(self, profile, source, request):
            return type("Result", (), {
                "source": source,
                "outputs": [{"name": "index.html", "blob_sha256": "sha"}],
                "validation": {"status": "passed"},
            })()
    committed = []
    result = manager.execute(job["job_id"], host_id="host-a", renderer=Renderer(),
                             commit=lambda **kwargs: committed.append(kwargs) or "v2")
    assert result["status"] == "completed"
    assert result["result_version_id"] == "v2"
    assert committed[0]["base_version_id"] == "v1"

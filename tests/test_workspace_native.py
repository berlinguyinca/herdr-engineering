import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from herdr_engineering.workspace import (
    WorkspaceManifest,
    WorkspaceManifestStore,
    WorkspacePreparationError,
    WorkspacePreparer,
    registry_materializer,
)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    repo = tmp_path / "repository"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.test/private/repo.git"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Scheduler Test", "-c", "user.email=test@example.test",
                    "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "fixture"], check=True, capture_output=True)
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    monkeypatch.setattr("herdr_engineering.workspace.shutil.disk_usage", lambda _: SimpleNamespace(free=100 * 1024 ** 3))
    return repo, revision


class NativeWorktree:
    def create_worktree(self, path, branch, *, cwd, revision):
        subprocess.run(["git", "-C", cwd, "worktree", "add", "-b", branch, path, revision],
                       check=True, capture_output=True)


def manifest(revision, workspace_id="workspace", branch="task"):
    return WorkspaceManifest(workspace_id, "https://example.test/private/repo.git", revision, branch,
                             workspace_id, "task", (), "environment", "alice", ("alice",))


def test_native_preparation_verifies_actual_git_worktree_and_rejects_second_branch_writer(repository, tmp_path):
    repo, revision = repository
    store = WorkspaceManifestStore()
    preparer = WorkspacePreparer(store)
    ready = preparer.prepare_native(manifest(revision), repository_path=repo,
        destination_path=tmp_path / "destination", host_uid="host-b", native_adapter=NativeWorktree())
    assert ready["ready"] is True and ready["revision"] == revision
    assert Path(ready["path"]).is_dir()
    with pytest.raises(subprocess.CalledProcessError):
        preparer.prepare_native(manifest(revision, "second"), repository_path=repo,
            destination_path=tmp_path / "second", host_uid="host-b", native_adapter=NativeWorktree())
    assert store.resolve("second", "host-b") is None


def test_dirty_source_is_preserved_and_never_prepared(repository, tmp_path):
    repo, revision = repository
    dirty = repo / "uncommitted.txt"
    dirty.write_text("keep this work")
    with pytest.raises(WorkspacePreparationError, match="dirty source"):
        WorkspacePreparer(WorkspaceManifestStore()).prepare_native(manifest(revision),
            repository_path=repo, destination_path=tmp_path / "destination", host_uid="host-b",
            native_adapter=NativeWorktree())
    assert dirty.read_text() == "keep this work"
    assert not (tmp_path / "destination").exists()


def test_registry_artifacts_require_acl_and_verified_private_materialization(tmp_path):
    calls = []
    def adapter(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        return {"artifact_id": "private-artifact"} if method == "get_version" else {"sha256_verified": True}
    denied = registry_materializer(adapter, authorize=lambda *_: False,
        owner_id="alice", host_uid="host-b", session_id="session")
    with pytest.raises(WorkspacePreparationError, match="access denied"):
        denied("version_1", tmp_path)
    assert len(calls) == 1
    allowed = registry_materializer(adapter, authorize=lambda *_: True,
        owner_id="alice", host_uid="host-b", session_id="session")
    assert allowed("version_1", tmp_path)["sha256_verified"]
    method, args, kwargs = calls[-1]
    assert method == "materialize_version" and args == ("version_1",)
    assert Path(kwargs["target_dir"]).parent == tmp_path / ".herdr-artifacts"

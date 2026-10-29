from time import monotonic

import pytest

from herdr_engineering.quotas import FairQueue, QueueEntry, ResourceEnvelope
from herdr_engineering.recovery import Fence, migration_plan
from herdr_engineering.replay import advisory_estimate, replay
from herdr_engineering.workspace import (
    WorkspaceManifest,
    WorkspaceManifestStore,
    WorkspacePreparationError,
    WorkspacePreparer,
    preflight_workspace,
)


def envelope():
    return ResourceEnvelope(1, 1, 2, 1, 2, 60, "coding")


def test_workspace_preflight_blocks_dirty_or_unscoped_destinations():
    result = preflight_workspace(repo_url="git@example/repo", revision="abc", branch="task",
                                 worktree_id="wt", tool_version="1", expected_tool_version="1",
                                 disk_free_gib=50, required_disk_gib=20, credentials_scoped=False, dirty=False)
    assert not result["ready"] and "CREDENTIAL_SCOPE_INVALID" in result["reason_codes"]


def test_workspace_preparer_persists_manifest_before_native_worktree_creation():
    manifest = WorkspaceManifest("ws-1", "git@example/repo", "abc", "task", "wt-1", "task-1", (), "env", "alice", ("alice",))
    store = WorkspaceManifestStore()
    prepared = WorkspacePreparer(store).prepare(
        manifest,
        preflight={"ready": True, "reason_codes": []},
        create_worktree=lambda row: {"host": "host-b", "worktree_id": row["worktree_id"]},
    )
    assert prepared["worktree"]["worktree_id"] == "wt-1"
    assert store.get("ws-1")["revision"] == "abc"
    with pytest.raises(WorkspacePreparationError, match="identity conflict"):
        store.save(WorkspaceManifest("ws-1", "git@example/repo", "other", "task", "wt-1", "task-1", (), "env", "alice", ("alice",)))


def test_workspace_preparer_never_calls_native_launcher_when_preflight_fails():
    called = []
    manifest = WorkspaceManifest("ws-2", "git@example/repo", "abc", "task", "wt-2", "task-2", (), "env", "alice", ("alice",))
    with pytest.raises(WorkspacePreparationError):
        WorkspacePreparer(WorkspaceManifestStore()).prepare(
            manifest, preflight={"ready": False, "reason_codes": ["DIRTY_WORKTREE"]},
            create_worktree=lambda _: called.append(True),
        )
    assert called == []


def test_prepared_workspace_resolver_survives_event_replay_and_binds_host():
    from herdr_engineering.control_plane.repo import ControlPlaneRepo
    repo = ControlPlaneRepo()
    store = WorkspaceManifestStore(repo)
    manifest = WorkspaceManifest("persisted-ws", "git@example/repo", "revision", "branch", "wt", "task", (), "env", "alice", ("alice",))
    WorkspacePreparer(store).prepare(manifest, preflight={"ready": True}, host_uid="host-b",
        create_worktree=lambda _: {"path": "/work/isolated", "revision": "revision"})
    restored_repo = ControlPlaneRepo()
    restored_repo.import_state(repo.export_state())
    restored = WorkspaceManifestStore(restored_repo)
    assert restored.resolve("persisted-ws", "host-b")["owner"] == "alice"
    assert restored.resolve("persisted-ws", "other-host") is None
    with pytest.raises(WorkspacePreparationError, match="revision"):
        restored.mark_prepared(manifest, host_uid="host-b", path="/work/isolated", revision="wrong")


def test_fair_queue_prioritizes_interactive_but_enforces_caps():
    queue = FairQueue(owner_limits={"alice": 1})
    queue.add(QueueEntry("b", "bob", "p", "background", monotonic(), envelope()))
    queue.add(QueueEntry("i", "alice", "p", "owner-interactive", monotonic(), envelope()))
    assert [row.request_id for row in queue.ordered()] == ["i", "b"]
    with pytest.raises(RuntimeError, match="owner queue cap"):
        queue.add(QueueEntry("too-many", "alice", "p", "normal", monotonic(), envelope()))


def test_fence_invalidates_old_lease_and_migration_blocks_unsafe_work():
    fence = Fence()
    old = fence.acquire("agent-1", "a", checkpoint_capable=False)
    new = fence.acquire("agent-1", "b", checkpoint_capable=True)
    assert not fence.valid(old) and fence.valid(new)
    assert migration_plan(new, destination_host_uid="c", dirty=True, command_in_flight=False, checkpoint_digest="sha")["state"] == "blocked"


def test_replay_is_advisory_and_reports_limitations():
    result = replay([{"state": "queued", "wait_seconds": 2, "overload_seconds": 1}])
    assert result.queued == 1 and result.confidence == "low"
    estimate = advisory_estimate([1, 2, 3, 4, 5], cold_start=10)
    assert estimate["proposed"] is True
    assert result.limitations


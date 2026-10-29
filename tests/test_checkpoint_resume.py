import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from herdr_engineering.checkpoint import CheckpointError, ProtectedCheckpointStore
from herdr_engineering.recovery import ExecutionLease, RecoveryCoordinator
from herdr_engineering.resume import ResumeRegistry, native_resume_registry


def lease(family="codex", generation=1):
    return ExecutionLease(
        agent_id="agent-1", host_uid="host-a", generation=generation,
        token="token-a", checkpoint_capable=True, state="running",
    )


def test_checkpoint_store_is_private_atomic_and_digest_verified(tmp_path):
    store = ProtectedCheckpointStore(tmp_path)
    manifest = store.put(
        checkpoint_id="cp-1", owner_id="alice", agent_id="agent-1",
        lease=lease(), payload=b"private session state",
    )
    assert manifest.sha256
    assert (tmp_path / "cp-1.checkpoint").stat().st_mode & 0o077 == 0
    assert store.get("cp-1", owner_id="alice") == b"private session state"
    with pytest.raises(CheckpointError, match="owner"):
        store.get("cp-1", owner_id="mallory")
    file = tmp_path / "cp-1.checkpoint"
    damaged = bytearray(file.read_bytes())
    damaged[-1] ^= 1
    file.write_bytes(damaged)
    with pytest.raises(CheckpointError, match="digest"):
        store.get("cp-1", owner_id="alice")


@pytest.mark.parametrize("checkpoint_id", ["../escape", "/tmp/escape", "a/b", "", ".", "a\\b"])
def test_checkpoint_store_rejects_path_identifiers(tmp_path, checkpoint_id):
    store = ProtectedCheckpointStore(tmp_path)
    with pytest.raises(CheckpointError, match="identifier"):
        store.put(checkpoint_id=checkpoint_id, owner_id="alice", agent_id="agent-1",
                  lease=lease(), payload=b"state")
    with pytest.raises(CheckpointError, match="identifier"):
        store.get(checkpoint_id, owner_id="alice")


def test_checkpoint_store_rejects_mismatched_agent_identity(tmp_path):
    with pytest.raises(CheckpointError, match="agent"):
        ProtectedCheckpointStore(tmp_path).put(
            checkpoint_id="cp", owner_id="alice", agent_id="other-agent",
            lease=lease(), payload=b"state")


def test_checkpoint_is_immutable_and_concurrent_publication_has_one_winner(tmp_path):
    store = ProtectedCheckpointStore(tmp_path)
    def put(index):
        try:
            store.put(checkpoint_id="one-id", owner_id="alice", agent_id="agent-1", lease=lease(), payload=str(index).encode())
            return index
        except CheckpointError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = [result for result in pool.map(put, range(8)) if result is not None]
    assert len(winners) == 1
    assert store.get("one-id", owner_id="alice") == str(winners[0]).encode()
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_checkpoint_fsync_does_not_publish_or_leave_temporary_files(tmp_path, monkeypatch):
    store = ProtectedCheckpointStore(tmp_path)
    def fail(_):
        raise OSError("injected fsync failure")
    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError):
        store.put(checkpoint_id="failed", owner_id="alice", agent_id="agent-1", lease=lease(), payload=b"state")
    assert list(tmp_path.iterdir()) == []


def test_checkpoint_store_pins_directory_identity_and_rejects_symlinks(tmp_path):
    root, moved = tmp_path / "root", tmp_path / "original"
    store = ProtectedCheckpointStore(root)
    store.put(checkpoint_id="private", owner_id="alice", agent_id="agent-1", lease=lease(), payload=b"original")
    root.rename(moved)
    root.mkdir()
    assert store.get("private", owner_id="alice") == b"original"
    (moved / "linked.checkpoint").symlink_to(moved / "private.checkpoint")
    with pytest.raises(CheckpointError):
        store.get("linked", owner_id="alice")
    link = tmp_path / "symlink-root"
    link.symlink_to(moved, target_is_directory=True)
    with pytest.raises(CheckpointError, match="directory"):
        ProtectedCheckpointStore(link)


def test_checkpoint_retrieval_validates_container_and_lease_identity(tmp_path):
    store = ProtectedCheckpointStore(tmp_path)
    store.put(checkpoint_id="original", owner_id="alice", agent_id="agent-1", lease=lease(), payload=b"state")
    shutil.copy2(tmp_path / "original.checkpoint", tmp_path / "copied.checkpoint")
    with pytest.raises(CheckpointError, match="identity"):
        store.get("copied", owner_id="alice")
    with pytest.raises(CheckpointError, match="lease identity"):
        store.get("original", owner_id="alice", expected_lease=replace(lease(), generation=2))


def test_resume_registry_defaults_unsupported_and_requires_all_safety_capabilities():
    registry = ResumeRegistry()
    assert not registry.certified("codex", "1.0")
    registry.register("codex", "1.0", checkpoint=True, restore=True, single_writer_fence=False)
    assert not registry.certified("codex", "1.0")
    registry.register("codex", "1.0", checkpoint=True, restore=True, single_writer_fence=True)
    assert registry.certified("codex", "1.0")
    native = native_resume_registry()
    assert all(not native.certified(family, "unknown")
               for family in ("pi", "claude-code", "codex", "agent"))


def test_recovery_coordinator_blocks_unsafe_migration_and_fences_old_writer(tmp_path):
    store = ProtectedCheckpointStore(tmp_path)
    registry = ResumeRegistry()
    coordinator = RecoveryCoordinator(store=store, resumes=registry)
    blocked = coordinator.migrate(lease(), family="codex", version="1.0", owner_id="alice",
                                  destination_host_uid="host-b", payload=b"state")
    assert blocked["state"] == "blocked"
    assert "RESUME_CONTRACT_UNCERTIFIED" in blocked["reason_codes"]

    registry.register("codex", "1.0", checkpoint=True, restore=True, single_writer_fence=True)
    blocked = coordinator.migrate(lease(), family="codex", version="1.0", owner_id="alice",
                                destination_host_uid="host-b", payload=b"state")
    assert blocked["state"] == "blocked"
    assert "MIGRATION_DISABLED" in blocked["reason_codes"]
    assert "RUNTIME_RECOVERY_UNAVAILABLE" in blocked["reason_codes"]


def test_fault_inventory_does_not_claim_unexecuted_cases_are_verified(tmp_path):
    coordinator = RecoveryCoordinator(store=ProtectedCheckpointStore(tmp_path), resumes=ResumeRegistry())
    result = coordinator.fault_matrix()
    assert {row["case"] for row in result} >= {
        "commit_before_ack", "heartbeat_lost_running", "network_partition",
        "expired_lease_while_running", "corrupt_checkpoint", "dirty_worktree",
        "slow_ssh", "two_schedulers", "full_checkpoint_store", "stale_auth",
        "shutdown_mid_transfer", "resume_version_mismatch", "safe_fence_unavailable",
    }
    assert all(row["verification"] == "pending" for row in result)


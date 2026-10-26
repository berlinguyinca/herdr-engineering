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
    assert (tmp_path / "cp-1.json").stat().st_mode & 0o077 == 0
    assert store.get("cp-1", owner_id="alice") == b"private session state"
    with pytest.raises(CheckpointError, match="owner"):
        store.get("cp-1", owner_id="mallory")
    (tmp_path / "cp-1.bin").write_bytes(b"tampered")
    with pytest.raises(CheckpointError, match="digest"):
        store.get("cp-1", owner_id="alice")


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
    moved = coordinator.migrate(lease(), family="codex", version="1.0", owner_id="alice",
                                destination_host_uid="host-b", payload=b"state")
    assert moved["state"] == "restoring"
    assert moved["destination_generation"] == 2
    assert not coordinator.fence.valid(lease())


def test_fault_matrix_never_retries_an_uncertain_commit_as_a_new_execution(tmp_path):
    coordinator = RecoveryCoordinator(store=ProtectedCheckpointStore(tmp_path), resumes=ResumeRegistry())
    result = coordinator.fault_matrix()
    assert {row["case"] for row in result} >= {
        "commit_before_ack", "heartbeat_lost_running", "network_partition",
        "expired_lease_while_running", "corrupt_checkpoint", "dirty_worktree",
        "slow_ssh", "two_schedulers", "full_checkpoint_store", "stale_auth",
        "shutdown_mid_transfer", "resume_version_mismatch", "safe_fence_unavailable",
    }
    assert all(row["duplicate_execution"] is False for row in result)


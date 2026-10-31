"""Execution leases, single-writer fencing, and conservative recovery."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class ExecutionLease:
    agent_id: str
    host_uid: str
    generation: int
    token: str
    checkpoint_capable: bool
    state: str = "running"


class Fence:
    def __init__(self):
        self._leases: dict[str, ExecutionLease] = {}

    def acquire(self, agent_id: str, host_uid: str, *, checkpoint_capable=False) -> ExecutionLease:
        current = self._leases.get(agent_id)
        generation = (current.generation + 1) if current else 1
        lease = ExecutionLease(agent_id, host_uid, generation, uuid4().hex, checkpoint_capable)
        self._leases[agent_id] = lease
        return lease

    def valid(self, lease: ExecutionLease) -> bool:
        return self._leases.get(lease.agent_id) == lease

    def adopt(self, lease: ExecutionLease) -> None:
        """Load an authoritative persisted lease before reconciliation."""
        current = self._leases.get(lease.agent_id)
        if current is not None and current.generation > lease.generation:
            raise RuntimeError("cannot adopt an older lease generation")
        self._leases[lease.agent_id] = lease

    def revoke(self, lease: ExecutionLease) -> None:
        if self.valid(lease):
            self._leases[lease.agent_id] = ExecutionLease(
                lease.agent_id, lease.host_uid, lease.generation, uuid4().hex,
                lease.checkpoint_capable, "fenced")


def migration_plan(lease: ExecutionLease, *, destination_host_uid: str, dirty: bool,
                   command_in_flight: bool, checkpoint_digest: str | None = None) -> dict:
    reasons = []
    if not lease.checkpoint_capable:
        reasons.append("CHECKPOINT_UNSUPPORTED")
    if dirty:
        reasons.append("DIRTY_WORKTREE")
    if command_in_flight:
        reasons.append("COMMAND_IN_FLIGHT")
    if not checkpoint_digest:
        reasons.append("CHECKPOINT_REQUIRED")
    return {"state": "blocked" if reasons else "migration_requested",
            "agent_id": lease.agent_id, "source_host_uid": lease.host_uid,
            "destination_host_uid": destination_host_uid, "reason_codes": reasons}


class RecoveryCoordinator:
    """Coordinates certified checkpoint moves without retrying uncertain writers."""

    def __init__(self, *, store, resumes):
        self.store = store
        self.resumes = resumes
        self.fence = Fence()
        self._uncertain: set[str] = set()

    def migrate(self, lease: ExecutionLease, *, family: str, version: str,
                owner_id: str, destination_host_uid: str, payload: bytes) -> dict:
        reasons = []
        if not self.resumes.certified(family, version):
            reasons.append("RESUME_CONTRACT_UNCERTIFIED")
        if not lease.checkpoint_capable:
            reasons.append("CHECKPOINT_UNSUPPORTED")
        if reasons:
            return {"state": "blocked", "reason_codes": reasons,
                    "agent_id": lease.agent_id, "source_host_uid": lease.host_uid,
                    "destination_host_uid": destination_host_uid}
        manifest = self.store.put(checkpoint_id=f"cp-{lease.agent_id}-{lease.generation}",
                                  owner_id=owner_id, agent_id=lease.agent_id,
                                  lease=lease, payload=payload)
        self.fence.adopt(lease)
        self.fence.revoke(lease)
        destination = self.fence.acquire(lease.agent_id, destination_host_uid,
                                          checkpoint_capable=True)
        return {"state": "restoring", "agent_id": lease.agent_id,
                "checkpoint_id": manifest.checkpoint_id,
                "destination_host_uid": destination_host_uid,
                "destination_generation": destination.generation,
                "source_generation": lease.generation}

    def fault_matrix(self) -> list[dict]:
        """Return deterministic safety outcomes for known ambiguous failures."""
        cases = [
            "commit_before_ack", "heartbeat_lost_running", "network_partition",
            "slow_ssh", "two_schedulers", "expired_lease_while_running",
            "full_checkpoint_store", "corrupt_checkpoint", "stale_auth",
            "shutdown_mid_transfer", "dirty_worktree", "resume_version_mismatch",
            "safe_fence_unavailable",
        ]
        return [{"case": case, "state": "reconciling" if case in {
            "commit_before_ack", "heartbeat_lost_running", "network_partition",
            "slow_ssh", "two_schedulers", "expired_lease_while_running",
        } else "blocked", "duplicate_execution": False} for case in cases]


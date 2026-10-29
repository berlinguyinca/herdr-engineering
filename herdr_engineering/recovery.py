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

    def __init__(self, *, store, resumes, migration_enabled=False, runtime=None, journal=None):
        self.store = store
        self.resumes = resumes
        self.fence = Fence()
        self._uncertain: set[str] = set()
        self.migration_enabled = migration_enabled
        self.runtime = runtime
        self.journal = journal

    def migrate(self, lease: ExecutionLease, *, family: str, version: str,
                owner_id: str, destination_host_uid: str, payload: bytes | None = None) -> dict:
        reasons = []
        if not self.migration_enabled:
            reasons.append("MIGRATION_DISABLED")
        if not self.resumes.certified(family, version):
            reasons.append("RESUME_CONTRACT_UNCERTIFIED")
        if not lease.checkpoint_capable:
            reasons.append("CHECKPOINT_UNSUPPORTED")
        operations = ("quiesce", "checkpoint", "stop_origin", "fenced", "acquire_destination", "restore", "verify")
        if self.runtime is None or not all(callable(getattr(self.runtime, operation, None)) for operation in operations):
            reasons.append("RUNTIME_RECOVERY_UNAVAILABLE")
        elif getattr(self.runtime, "family", None) != family or getattr(self.runtime, "version", None) != version:
            reasons.append("RESUME_VERSION_MISMATCH")
        if self.journal is None:
            reasons.append("DURABLE_RECOVERY_REQUIRED")
        if reasons:
            return {"state": "blocked", "reason_codes": reasons,
                    "agent_id": lease.agent_id, "source_host_uid": lease.host_uid,
                    "destination_host_uid": destination_host_uid}
        operation_id = f"{lease.agent_id}:{lease.generation}:{destination_host_uid}"
        record = {"operation_id": operation_id, "state": "migration_requested",
                  "agent_id": lease.agent_id, "owner_id": owner_id,
                  "source_host_uid": lease.host_uid, "source_generation": lease.generation,
                  "destination_host_uid": destination_host_uid}
        with self.journal.operation(operation_id) as existing:
            if existing is not None:
                # Interrupted submissions require reconciliation, never replay
                # non-idempotent stop/restore operations from their beginning.
                return {**existing, "state": existing["state"] if existing["state"] in {
                    "migration_completed", "blocked"} else "reconciling"}
            self.journal.record(operation_id, record)
            stopped = False
            try:
                if self.runtime.quiesce(lease).get("safe") is not True:
                    record.update(state="blocked", reason_codes=["COMMAND_IN_FLIGHT"])
                    return self.journal.record(operation_id, record)
                record["state"] = "checkpointing"
                self.journal.record(operation_id, record)
                captured = self.runtime.checkpoint(lease)
                manifest = self.store.put(checkpoint_id=f"cp-{lease.agent_id}-{lease.generation}",
                    owner_id=owner_id, agent_id=lease.agent_id, lease=lease, payload=captured)
                verified = self.store.get(manifest.checkpoint_id, owner_id=owner_id, expected_lease=lease)
                record.update(checkpoint_id=manifest.checkpoint_id, state="draining")
                self.journal.record(operation_id, record)
                # From submission of stop onward, assume origin state uncertain
                # until both supervised termination and durable fencing prove it.
                stopped = True
                proof = self.runtime.stop_origin(lease)
                if proof.get("stop_verified") is not True or self.runtime.fenced(lease) is not True:
                    record.update(state="reconciling", reason_codes=["STOP_UNVERIFIED"])
                    return self.journal.record(operation_id, record)
                destination = self.runtime.acquire_destination(lease, destination_host_uid)
                if (destination.agent_id != lease.agent_id or destination.host_uid != destination_host_uid
                        or destination.generation <= lease.generation):
                    raise RuntimeError("destination fencing generation is invalid")
                record.update(state="restoring", destination_generation=destination.generation)
                self.journal.record(operation_id, record)
                restored = self.runtime.restore(destination, verified)
                checks = self.runtime.verify(destination, restored)
                if not all(checks.get(key) is True for key in ("heartbeat", "workspace_consistent", "single_writer")):
                    record.update(state="reconciling", reason_codes=["RESTORE_UNVERIFIED"])
                else:
                    record.update(state="migration_completed", reason_codes=[])
                return self.journal.record(operation_id, record)
            except Exception as exc:
                record.update(state="reconciling" if stopped else "blocked",
                              reason_codes=["RECOVERY_OPERATION_FAILED"], error_type=type(exc).__name__)
                return self.journal.record(operation_id, record)

    def fault_matrix(self) -> list[dict]:
        """Inventory required fault checks; entries are not executed proof."""
        cases = [
            "commit_before_ack", "heartbeat_lost_running", "network_partition",
            "slow_ssh", "two_schedulers", "expired_lease_while_running",
            "full_checkpoint_store", "corrupt_checkpoint", "stale_auth",
            "shutdown_mid_transfer", "dirty_worktree", "resume_version_mismatch",
            "safe_fence_unavailable",
        ]
        return [{"case": case, "required_state": "reconciling" if case in {
            "commit_before_ack", "heartbeat_lost_running", "network_partition",
            "slow_ssh", "two_schedulers", "expired_lease_while_running",
        } else "blocked", "verification": "pending"} for case in cases]


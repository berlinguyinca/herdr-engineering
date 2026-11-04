from dataclasses import replace
from types import SimpleNamespace

import pytest

from herdr_engineering.checkpoint import CheckpointError, ProtectedCheckpointStore
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.recovery import ExecutionLease, RecoveryCoordinator
from herdr_engineering.recovery_journal import RecoveryJournal
from herdr_engineering.resume import ResumeRegistry


class Runtime:
    family, version = "test-runtime", "1"

    def __init__(self, failure=None):
        self.failure, self.calls = failure, []

    def call(self, name, result):
        self.calls.append(name)
        if self.failure == name:
            raise TimeoutError("injected transport fault")
        return result

    def quiesce(self, lease):
        return self.call("quiesce", {"safe": self.failure != "busy"})

    def checkpoint(self, lease):
        return self.call("checkpoint", b"consistent-session")

    def stop_origin(self, lease):
        return self.call("stop_origin", {"stop_verified": self.failure != "uncertain-stop"})

    def fenced(self, lease):
        return self.call("fenced", self.failure != "unavailable-fence")

    def acquire_destination(self, lease, host):
        return self.call("acquire_destination", replace(lease, host_uid=host,
            generation=lease.generation if self.failure == "stale-generation" else lease.generation + 1))

    def restore(self, destination, payload):
        assert payload == b"consistent-session"
        return self.call("restore", {"restored": True})

    def verify(self, destination, restored):
        return self.call("verify", {"heartbeat": self.failure != "missing-heartbeat",
            "workspace_consistent": True, "single_writer": True})


def setup(tmp_path, failure=None, store=None):
    repo = ControlPlaneRepo(sink=SimpleNamespace(record_required=lambda _: None, record=lambda _: None))
    runtime = Runtime(failure)
    registry = ResumeRegistry()
    registry.register(runtime.family, runtime.version, checkpoint=True, restore=True, single_writer_fence=True)
    coordinator = RecoveryCoordinator(store=store or ProtectedCheckpointStore(tmp_path), resumes=registry,
        migration_enabled=True, runtime=runtime, journal=RecoveryJournal(repo))
    return coordinator, runtime, repo


def migrate(coordinator):
    return coordinator.migrate(ExecutionLease("agent", "source", 1, "private", True),
        family="test-runtime", version="1", owner_id="owner", destination_host_uid="destination")


def test_recovery_orders_private_checkpoint_stop_fence_restore_and_verification(tmp_path):
    coordinator, runtime, repo = setup(tmp_path)
    result = migrate(coordinator)
    assert result["state"] == "migration_completed" and result["destination_generation"] == 2
    assert runtime.calls == ["quiesce", "checkpoint", "stop_origin", "fenced", "acquire_destination", "restore", "verify"]
    assert [event["payload"]["state"] for event in repo.stream("scheduler_recovery", result["operation_id"])] == [
        "migration_requested", "checkpointing", "draining", "restoring", "migration_completed"]
    before = list(runtime.calls)
    assert migrate(coordinator) == result
    assert runtime.calls == before


def test_restart_reads_journal_before_repeating_any_runtime_operation(tmp_path):
    coordinator, runtime, repo = setup(tmp_path, "restore")
    assert migrate(coordinator)["state"] == "reconciling"
    restored = ControlPlaneRepo(sink=SimpleNamespace(record_required=lambda _: None, record=lambda _: None))
    restored.import_state(repo.export_state())
    coordinator.journal = RecoveryJournal(restored)
    before = list(runtime.calls)
    assert migrate(coordinator)["state"] == "reconciling"
    assert runtime.calls == before


@pytest.mark.parametrize("failure,state,forbidden", [
    ("busy", "blocked", "checkpoint"), ("checkpoint", "blocked", "stop_origin"),
    ("stop_origin", "reconciling", "restore"), ("uncertain-stop", "reconciling", "acquire_destination"),
    ("unavailable-fence", "reconciling", "restore"), ("stale-generation", "reconciling", "restore"),
    ("restore", "reconciling", "verify"), ("missing-heartbeat", "reconciling", None),
])
def test_faults_never_restore_without_verified_stop_and_higher_epoch(tmp_path, failure, state, forbidden):
    coordinator, runtime, _ = setup(tmp_path, failure)
    assert migrate(coordinator)["state"] == state
    if forbidden:
        assert forbidden not in runtime.calls
    calls = list(runtime.calls)
    assert migrate(coordinator)["state"] == state
    assert runtime.calls == calls


@pytest.mark.parametrize("failure", ["full-store", "corrupt-checkpoint"])
def test_checkpoint_storage_fault_does_not_stop_origin(tmp_path, failure):
    class BrokenStore(ProtectedCheckpointStore):
        def put(self, **kwargs):
            if failure == "full-store":
                raise OSError("disk full")
            return super().put(**kwargs)

        def get(self, *args, **kwargs):
            raise CheckpointError("checkpoint digest mismatch")
    coordinator, runtime, _ = setup(tmp_path, store=BrokenStore(tmp_path))
    assert migrate(coordinator)["state"] == "blocked"
    assert "stop_origin" not in runtime.calls


def test_missing_durable_ack_prevents_all_runtime_side_effects(tmp_path):
    coordinator, runtime, _ = setup(tmp_path)
    coordinator.journal = RecoveryJournal(ControlPlaneRepo())
    with pytest.raises(RuntimeError, match="durable recovery"):
        migrate(coordinator)
    assert runtime.calls == []

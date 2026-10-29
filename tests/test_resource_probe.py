from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.scheduler_service import SchedulerService
from herdr_engineering.resource_probe import ResourceProbe, host_identity, macos_resource_snapshot


def observation(**changes):
    result = {"host_uid": "host-" + "a" * 32, "captured_at": datetime.now(UTC).isoformat(),
              "sequence": 1, "platform": "linux", "cpu_count": 16, "cpu_utilization": .2,
              "memory_total_gib": 64, "memory_available_gib": 50,
              "disk_total_gib": 500, "disk_free_gib": 400, "pressure": {}}
    return dict(result, **changes)


def test_probe_persists_private_identity_and_reports_unknown_cpu_on_first_sample(tmp_path):
    identity = tmp_path / "identity"
    assert host_identity(identity) == host_identity(identity)
    assert identity.stat().st_mode & 0o077 == 0
    sample = ResourceProbe(identity, tmp_path).sample()
    assert sample["cpu_utilization"] is None
    assert sample["host_uid"] == host_identity(identity)
    assert sample["captured_at"] and sample["probe_version"] == 1


def test_authenticated_observation_makes_capacity_available_without_relaxing_mac_policy():
    service = SchedulerService(ControlPlaneRepo())
    assert service.observe_host("linux", observation())["capacity_known"] is True
    assert service.repo.host_snapshot("linux").trusted is True
    service.observe_host("mac", observation(host_uid="host-" + "b" * 32, platform="darwin"))
    assert service.repo.get_host("mac")["scheduler_policy"]["mode"] == "manual_only"


def test_observation_rejects_replay_identity_conflict_and_blocks_unknown_metrics():
    service = SchedulerService(ControlPlaneRepo())
    service.observe_host("linux", observation())
    with pytest.raises(ValueError, match="out-of-order"):
        service.observe_host("linux", observation())
    with pytest.raises(ValueError, match="identity conflict"):
        service.observe_host("linux", observation(sequence=2, host_uid="host-" + "b" * 32))
    service.observe_host("linux", observation(sequence=2, cpu_utilization=None))
    assert service.repo.get_host("linux")["scheduler_capacity"]["total_cpu"] == 0


def test_macos_resource_probe_marks_estimates_and_unsupported_metrics(tmp_path):
    from types import SimpleNamespace
    commands = []
    def run(argv, **kwargs):
        commands.append(argv)
        assert kwargs["timeout"] == 2
        output = {"/usr/sbin/sysctl": str(64 * 1024 ** 3),
                  "/usr/bin/vm_stat": 'Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 100.\nPages inactive: 200.\nPages speculative: 50.\n',
                  "/bin/ps": '12.3\n5.5\n'}[argv[0]]
        return SimpleNamespace(stdout=output)
    sample = macos_resource_snapshot(tmp_path / "identity", tmp_path, run=run)
    assert sample["memory_total_gib"] == 64
    assert sample["memory_available_gib"] == 350 * 16384 / 1024 ** 3
    assert sample["confidence"] == "partial" and "psi_unsupported" in sample["source_errors"]
    assert sample["cpu_source"] == "process_cpu_estimate"
    assert commands[-1] == ["/bin/ps", "-A", "-o", "%cpu="]


def test_pressure_observation_explains_why_admission_is_blocked():
    service = SchedulerService(ControlPlaneRepo())
    service.observe_host("linux", observation(pressure={"cpu": .5}))
    decision = service.plan({"request_id": "pressure", "agent_family": "codex",
                             "resource_profile": {"class": "coding", "cpu_cores": 1}}, "alice")
    assert decision["state"] == "queued"
    assert "CPU_PRESSURE" in decision["explanations"]["host-" + "a" * 32]["reason_codes"]


def test_observation_replays_capacity_identity_and_sequence_from_events():
    repo = ControlPlaneRepo()
    sample = observation()
    SchedulerService(repo).observe_host("linux", sample)
    restored = ControlPlaneRepo()
    state = repo.export_state()
    restored.import_state({"version": 1, "events": state["events"], "event_order": state["event_order"]})
    assert restored.get_host("linux")["host_uid"] == sample["host_uid"]
    assert restored.get_host("linux")["scheduler_capacity"]["total_cpu"] == 12.8
    assert restored.list_telemetry("linux")[-1]["sequence"] == 1
    with pytest.raises(ValueError, match="out-of-order"):
        SchedulerService(restored).observe_host("linux", sample)
    sample["pressure"]["cpu"] = 1
    assert restored.list_telemetry("linux")[-1]["resource_sample"]["pressure"] == {}


def test_unacknowledged_observation_does_not_change_admission_capacity():
    class FailedSink:
        def record_required(self, event):
            raise RuntimeError("storage offline")

        record = record_required

    repo = ControlPlaneRepo(sink=FailedSink())
    with pytest.raises(RuntimeError, match="storage offline"):
        SchedulerService(repo).observe_host("linux", observation())
    assert repo.get_host("linux") is None
    assert repo.list_telemetry("linux") == []


def test_dashboard_telemetry_age_keeps_aging_without_new_reports(monkeypatch):
    repo = ControlPlaneRepo()
    service = SchedulerService(repo)
    service.observe_host("linux", observation())
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=90)

    monkeypatch.setattr("herdr_engineering.control_plane.scheduler_service.datetime", Later)
    card = service.host_cards()[0]
    assert card["telemetry"]["freshness_seconds"] >= 90
    assert card["telemetry"]["at"] == repo.list_telemetry("linux")[-1]["at"]


def test_ordinary_heartbeat_cannot_poison_resource_watermark_or_renew_freshness():
    from herdr_engineering.control_plane.ingest import IngestService
    service = SchedulerService(ControlPlaneRepo())
    service.observe_host("linux", observation())
    captured = service.host_cards()[0]["telemetry"]["at"]
    IngestService(service.repo).ingest({"entity_type": "host", "entity_id": "linux",
                                      "event_type": "heartbeat", "payload": {"cpu": .1}})
    assert service.host_cards()[0]["telemetry"]["at"] == captured
    service.observe_host("linux", observation(sequence=2))
    with pytest.raises(ValueError, match="out-of-order"):
        service.observe_host("linux", observation(sequence=2))
    assert captured is not None
    assert service.host_cards()[0]["telemetry"]["at"] is not None


def test_events_only_restore_preserves_disable_after_resource_report():
    service = SchedulerService(ControlPlaneRepo())
    service.observe_host("linux", observation())
    service.repo.set_host_policy("linux", mode="disabled", expected_revision=0)
    state = service.repo.export_state()
    restored = ControlPlaneRepo()
    restored.import_state({"version": 1, "events": state["events"], "event_order": state["event_order"]})
    assert restored.get_host("linux")["scheduler_policy"]["mode"] == "disabled"


def test_failed_observation_commit_does_not_leave_local_eligible_capacity():
    from contextlib import contextmanager

    class CommitFailure:
        @contextmanager
        def scheduler_admission(self):
            yield []
            raise RuntimeError("commit failed")

        def record_required(self, event):
            pass

        def scheduler_observations(self):
            return {}

    repo = ControlPlaneRepo(sink=CommitFailure())
    with pytest.raises(RuntimeError, match="commit failed"):
        SchedulerService(repo).observe_host("linux", observation())
    assert repo.get_host("linux") is None
    assert repo.list_telemetry("linux") == []
    assert repo.stream("scheduler_observation", "linux") == []

"""Actual PostgreSQL contention across independent admission processes."""
import asyncio
import multiprocessing
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.scheduler_service import SchedulerService
from herdr_engineering.control_plane.sink import DurableEventSink
from herdr_engineering.recovery_journal import RecoveryJournal


def service_for(dsn):
    repo = ControlPlaneRepo(sink=DurableEventSink(dsn=dsn))
    repo.upsert_host("test-host", "test-host", None, host_uid="uid-test-host",
                     scheduler_owned=True, scheduler_trusted=True,
                     scheduler_policy={"mode": "automatic", "revision": 1},
                     scheduler_capacity={"total_cpu": 10, "total_memory_gib": 100,
                                         "total_disk_gib": 100})
    repo.record_telemetry("test-host", {"freshness_seconds": 0})
    return SchedulerService(repo)


def admit(argument):
    dsn, index = argument
    return service_for(dsn).reserve({
        "request_id": f"parallel-{index}", "agent_family": "codex",
        "resource_profile": {"class": "coding", "cpu_cores": 1, "memory_gib": 1},
    }, "test-owner")["state"]


def admit_to_bounded_queue(argument):
    dsn, index = argument
    service = SchedulerService(service_for(dsn).repo, owner_queue_limit=3)
    try:
        return service.reserve({
            "request_id": f"queue-contention-{index}", "agent_family": "codex",
            "resource_profile": {"class": "coding", "cpu_cores": 11, "memory_gib": 1},
        }, "queue-owner")["state"]
    except RuntimeError as error:
        return str(error)


def claim_shared_execution(dsn):
    service = service_for(dsn)
    row = service.reserve({"request_id": "shared-launch", "agent_family": "codex",
                           "resource_profile": {"class": "coding", "cpu_cores": 1}}, "test-owner")
    service.commit(row["reservation_id"])
    try:
        service.claim_execution(row["reservation_id"])
        return "claimed"
    except RuntimeError:
        return "denied"


def crash_during_admission(dsn):
    sink = DurableEventSink(dsn=dsn)
    with sink.scheduler_admission():
        sink.record_required({"entity_type": "scheduler_reservation", "entity_id": "crash",
                              "event_type": "SchedulerReservationChanged",
                              "payload": {"request_id": "crash", "state": "reserved"}})
        os._exit(23)  # Deliberately bypass Python cleanup to exercise DB rollback.


def crash_after_recovery_transition(dsn):
    journal = RecoveryJournal(ControlPlaneRepo(sink=DurableEventSink(dsn=dsn)))
    with journal.operation("migration-crash"):
        journal.record("migration-crash", {"state": "restoring", "agent_id": "test"})
        os._exit(24)


@pytest.fixture
def scheduler_postgres():
    config = shutil.which("pg_config")
    if config is None:
        pytest.skip("PostgreSQL tools unavailable")
    bindir = Path(subprocess.check_output([config, "--bindir"], text=True).strip())
    if not (bindir / "initdb").is_file():
        candidates = sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"))
        if not candidates:
            pytest.skip("initdb unavailable")
        bindir = candidates[-1].parent
    with tempfile.TemporaryDirectory(prefix="scheduler-pg-") as directory:
        area = Path(directory)
        data = area / "data"
        subprocess.run([str(bindir / "initdb"), "-D", str(data), "-A", "trust", "--no-locale"],
                       check=True, capture_output=True)
        subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-l", str(area / "postgres.log"),
                        "-o", f"-c listen_addresses='' -k {area}", "-w", "start"],
                       check=True, capture_output=True)
        try:
            dsn = f"postgresql:///postgres?host={area}"

            async def initialize():
                import asyncpg
                connection = await asyncpg.connect(dsn)
                try:
                    migration = Path("deploy/control-plane/migrations/0001_control_plane.sql").read_text()
                    await connection.execute(migration)
                finally:
                    await connection.close()

            asyncio.run(initialize())
            yield dsn
        finally:
            subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"],
                           check=True, capture_output=True)


def test_one_hundred_requests_across_processes_never_overcommit(scheduler_postgres):
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        states = list(pool.map(admit, [(scheduler_postgres, index) for index in range(100)]))
    assert states.count("reserved") == 10
    assert states.count("queued") == 90


def test_queue_cap_is_atomic_across_processes(scheduler_postgres):
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        states = list(pool.map(admit_to_bounded_queue,
                               [(scheduler_postgres, index) for index in range(20)]))
    assert states.count("queued") == 3
    assert states.count("owner queue cap reached") == 17


def test_coordinator_crash_rolls_back_admission_and_releases_database_lock(scheduler_postgres):
    process = multiprocessing.get_context("spawn").Process(
        target=crash_during_admission, args=(scheduler_postgres,))
    process.start()
    process.join(timeout=15)
    try:
        assert process.exitcode == 23
        sink = DurableEventSink(dsn=scheduler_postgres)
        with sink.scheduler_admission() as rows:
            assert rows == []
        assert admit((scheduler_postgres, "after-crash")) == "reserved"
    finally:
        if process.is_alive():
            process.kill()
            process.join()


def test_shared_request_has_one_execution_claim_across_processes(scheduler_postgres):
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        claims = list(pool.map(claim_shared_execution, [scheduler_postgres] * 8))
    assert claims.count("claimed") == 1
    assert claims.count("denied") == 7


def test_final_bind_observes_policy_flip_from_another_coordinator(scheduler_postgres):
    first = service_for(scheduler_postgres)
    reservation = first.reserve({"request_id": "policy-flip", "agent_family": "codex",
                                 "resource_profile": {"class": "coding", "cpu_cores": 1}}, "owner")
    second = service_for(scheduler_postgres)
    second.repo.set_host_policy("test-host", mode="disabled", expected_revision=1)
    with pytest.raises(RuntimeError, match="policy changed"):
        first.commit(reservation["reservation_id"])


def test_recovery_transition_is_durable_before_coordinator_crash(scheduler_postgres):
    process = multiprocessing.get_context("spawn").Process(
        target=crash_after_recovery_transition, args=(scheduler_postgres,))
    process.start()
    process.join(timeout=15)
    try:
        assert process.exitcode == 24
        journal = RecoveryJournal(ControlPlaneRepo(sink=DurableEventSink(dsn=scheduler_postgres)))
        with journal.operation("migration-crash") as previous:
            assert previous == {"state": "restoring", "agent_id": "test"}
    finally:
        if process.is_alive():
            process.kill()
            process.join()


def test_resource_facts_and_replay_guard_are_shared_across_replicas(scheduler_postgres):
    from datetime import UTC, datetime

    first = SchedulerService(ControlPlaneRepo(sink=DurableEventSink(dsn=scheduler_postgres)))
    second = SchedulerService(ControlPlaneRepo(sink=DurableEventSink(dsn=scheduler_postgres)))
    sample = {"host_uid": "host-" + "a" * 32, "captured_at": datetime.now(UTC).isoformat(),
              "sequence": 1, "platform": "linux", "cpu_count": 16, "cpu_utilization": .2,
              "memory_total_gib": 64, "memory_available_gib": 50,
              "disk_total_gib": 500, "disk_free_gib": 400, "pressure": {}}
    first.observe_host("fleet-linux", sample)
    assert second.overview()["hosts"][0]["host_uid"] == sample["host_uid"]
    request = {"request_id": "replica-resource", "agent_family": "codex",
               "resource_profile": {"class": "coding", "cpu_cores": 1}}
    reservation = second.reserve(request, "owner")
    assert reservation["state"] == "reserved"
    second.commit(reservation["reservation_id"])
    with pytest.raises(ValueError, match="out-of-order"):
        second.observe_host("fleet-linux", sample)
    first.observe_host("fleet-linux", {**sample, "sequence": 2, "pressure": {"cpu": .5}})
    with pytest.raises(RuntimeError, match="capacity"):
        second.claim_execution(reservation["reservation_id"])
    assert not second.repo.list_scheduler_reservations()[0].get("execution_claimed")
    restored = ControlPlaneRepo()
    restored.import_state(second.repo.export_state())
    assert restored.scheduler_observation("fleet-linux")["sample"]["sequence"] == 2
    assert "CPU_PRESSURE" in restored.get_host("fleet-linux")["scheduler_capacity"]["reason_codes"]
    second.repo.set_host_policy("fleet-linux", mode="disabled", expected_revision=0)
    first.observe_host("fleet-linux", {**sample, "sequence": 3, "cpu_utilization": .1})
    assert first.repo.get_host("fleet-linux")["scheduler_policy"]["mode"] == "disabled"
    fresh = SchedulerService(ControlPlaneRepo(sink=DurableEventSink(dsn=scheduler_postgres)))
    assert fresh.reserve({**request, "request_id": "disabled-replica"}, "owner")["state"] == "queued"
    assert fresh.repo.list_telemetry("fleet-linux")[-1]["sequence"] == 3


def test_resource_projection_waits_for_transaction_commit(scheduler_postgres):
    from contextlib import contextmanager
    from datetime import UTC, datetime

    class RollbackSink(DurableEventSink):
        @contextmanager
        def scheduler_admission(self):
            with super().scheduler_admission() as rows:
                yield rows
                raise RuntimeError("abort before commit")

    sink = RollbackSink(dsn=scheduler_postgres)
    repo = ControlPlaneRepo(sink=sink)
    sample = {"host_uid": "host-" + "b" * 32, "captured_at": datetime.now(UTC).isoformat(),
              "sequence": 1, "platform": "linux"}
    with pytest.raises(RuntimeError, match="abort before commit"):
        SchedulerService(repo).observe_host("rolled-back", sample)
    assert repo.get_host("rolled-back") is None
    assert repo.scheduler_observation("rolled-back") is None
    assert repo.stream("scheduler_observation", "rolled-back") == []
    # Read from another DB session after the rollback, rather than trusting
    # the local projection or a mocked insert acknowledgment.
    reader = DurableEventSink(dsn=scheduler_postgres)
    with reader.scheduler_admission():
        assert reader.scheduler_observations() == {}

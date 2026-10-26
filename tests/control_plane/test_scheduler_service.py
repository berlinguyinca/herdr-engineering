
import pytest

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.scheduler_service import SchedulerService


def body(request_id="req-1"):
    return {"request_id": request_id, "agent_family": "codex", "placement_mode": "automatic",
            "resource_profile": {"class": "coding", "cpu_cores": 2, "memory_gib": 2}}


def setup_repo():
    repo = ControlPlaneRepo()
    repo.upsert_host(
        "beast", "beast", "100.0.0.2", host_uid="uid-beast", aliases=["beast"],
        platform="linux", scheduler_owned=True, scheduler_trusted=True,
        scheduler_policy={"mode": "automatic", "revision": 1},
        scheduler_capacity={"total_cpu": 16, "total_memory_gib": 64, "total_disk_gib": 100},
    )
    repo.record_telemetry("beast", {"freshness_seconds": 1, "sequence": 1})
    return repo


def test_scheduler_service_returns_explainable_dry_run_and_idempotent_reservation():
    service = SchedulerService(setup_repo())
    decision = service.plan(body(), "alice")
    assert decision["selected_host_uid"] == "uid-beast"
    assert decision["explanations"]["uid-beast"]["measured"]["available_cpu"] == 16
    first = service.reserve(body(), "alice")
    second = service.reserve(body(), "alice")
    assert first["reservation_id"] == second["reservation_id"]


def test_scheduler_service_exposes_host_policy_cards():
    cards = SchedulerService(setup_repo()).host_cards()
    assert cards[0]["host_uid"] == "uid-beast"
    assert cards[0]["policy"]["mode"] == "automatic"


def test_scheduler_service_applies_owner_queue_cap_before_alternate_spawn_paths():
    repo = setup_repo()
    repo.upsert_host("beast", "beast", "100.0.0.2", scheduler_capacity={"total_cpu": 0, "total_memory_gib": 0})
    service = SchedulerService(repo, owner_queue_limit=1)
    first = service.reserve(body("queued-1"), "alice")
    assert first["state"] == "queued"
    with pytest.raises(RuntimeError, match="owner queue cap"):
        service.reserve(body("queued-2"), "alice")


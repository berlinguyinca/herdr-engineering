
import copy

import pytest

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.scheduler_service import SchedulerService


def body(request_id="req-1"):
    return {"request_id": request_id, "agent_family": "codex", "placement_mode": "automatic",
            "resource_profile": {"class": "coding", "cpu_cores": 2, "memory_gib": 2}}


def setup_repo():
    class AcknowledgedSink:
        def __init__(self):
            self.events = []

        def record_required(self, event):
            self.events.append(copy.deepcopy(event))

        record = record_required
    repo = ControlPlaneRepo(sink=AcknowledgedSink())
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


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "two"])
def test_resource_profile_rejects_invalid_cpu_demand(value):
    request = body()
    request["resource_profile"]["cpu_cores"] = value
    with pytest.raises(ValueError, match="cpu_cores"):
        SchedulerService(setup_repo()).reserve(request, "alice")


def test_overview_reports_real_claims_and_excludes_credentials():
    service = SchedulerService(setup_repo())
    reservation = service.reserve(body("visible-request"), "alice")
    service.commit(reservation["reservation_id"])
    service.claim_execution(reservation["reservation_id"])
    overview = service.overview()
    assert overview["claimed_executions"] == 1
    assert overview["reserved_cpu"] == 2
    assert overview["history"][0]["request_id"] == "visible-request"
    assert "token" not in str(overview)


def test_queued_request_retry_uses_original_context_and_new_capacity():
    repo = setup_repo()
    repo.upsert_host("beast", "beast", None,
                     scheduler_capacity={"total_cpu": 0, "total_memory_gib": 0})
    service = SchedulerService(repo)
    queued = service.reserve(body("retry-capacity"), "alice")
    assert queued["state"] == "queued"
    repo.upsert_host("beast", "beast", None,
                     scheduler_capacity={"total_cpu": 8, "total_memory_gib": 16, "total_disk_gib": 100})
    restored = SchedulerService(repo)
    retried = restored.retry_queued(queued["reservation_id"])
    assert retried["state"] == "reserved"
    assert retried["reservation_id"] == queued["reservation_id"]
    assert retried["request_snapshot"]["owner_id"] == "alice"
    with pytest.raises(RuntimeError, match="only queued"):
        restored.retry_queued(queued["reservation_id"])


def test_queue_drain_restores_context_and_shares_new_capacity_across_projects():
    repo = setup_repo()
    repo.upsert_host("beast", "beast", None,
                     scheduler_capacity={"total_cpu": 0, "total_memory_gib": 0})
    service = SchedulerService(repo)
    for request_id, project in [("a-1", "a"), ("a-2", "a"), ("b-1", "b")]:
        service.reserve(dict(body(request_id), project_id=project), "alice")
    repo.upsert_host("beast", "beast", None,
                     scheduler_capacity={"total_cpu": 4, "total_memory_gib": 16, "total_disk_gib": 100})
    admitted = SchedulerService(repo).drain_queue()
    assert len(admitted) == 2
    assert {row["request_snapshot"]["project_id"] for row in admitted} == {"a", "b"}
    assert SchedulerService(repo).drain_queue() == []


def test_request_id_cannot_retrieve_another_owners_reservation():
    service = SchedulerService(setup_repo())
    service.reserve(body("private-request"), "alice")
    with pytest.raises(PermissionError):
        service.reserve(body("private-request"), "bob")


def test_idempotency_retry_rejects_changed_resource_constraints():
    service = SchedulerService(setup_repo())
    service.reserve(body("same-request"), "alice")
    changed = body("same-request")
    changed["resource_profile"]["cpu_cores"] = 7
    with pytest.raises(ValueError, match="conflict"):
        service.reserve(changed, "alice")


@pytest.mark.parametrize("field,value", [
    ("agent_family", "unknown"), ("placement_mode", "inherit-parent"),
    ("priority", "unlimited"), ("placement_mode", "manual"),
])
def test_admission_rejects_unsupported_launch_contract(field, value):
    request = dict(body(), **{field: value})
    with pytest.raises(ValueError):
        SchedulerService(setup_repo()).reserve(request, "alice")


def test_execution_claim_rechecks_policy_after_commit():
    repo = setup_repo()
    service = SchedulerService(repo)
    row = service.reserve(body("claim-policy-change"), "alice")
    service.commit(row["reservation_id"])
    repo.set_host_policy("beast", mode="disabled", expected_revision=1)
    with pytest.raises(RuntimeError, match="policy changed"):
        service.claim_execution(row["reservation_id"])


def test_explicit_manual_placement_survives_commit_on_manual_only_host():
    repo = setup_repo()
    repo.set_host_policy("beast", mode="manual_only", expected_revision=1)
    service = SchedulerService(repo)
    request = dict(body("manual-placement"), placement_mode="manual", explicit_host_uid="uid-beast")
    row = service.reserve(request, "alice")
    assert row["state"] == "reserved"
    assert service.commit(row["reservation_id"])["state"] == "committed"
    assert service.claim_execution(row["reservation_id"])["execution_claimed"] is True


def test_final_bind_rejects_capacity_lost_after_reservation():
    repo = setup_repo()
    service = SchedulerService(repo)
    row = service.reserve(body("capacity-drop"), "alice")
    repo.upsert_host("beast", "beast", None,
                     scheduler_capacity={"total_cpu": 1, "total_memory_gib": 1})
    with pytest.raises(RuntimeError, match="capacity"):
        service.commit(row["reservation_id"])


def test_execution_claim_rechecks_capacity_after_commit():
    repo = setup_repo()
    service = SchedulerService(repo)
    row = service.reserve(body("capacity-drop-after-commit"), "alice")
    service.commit(row["reservation_id"])
    repo.upsert_host("beast", "beast", "100.0.0.2",
                     scheduler_capacity={"total_cpu": 0, "total_memory_gib": 0})
    with pytest.raises(RuntimeError, match="capacity"):
        service.claim_execution(row["reservation_id"])
    assert not repo.list_scheduler_reservations()[0].get("execution_claimed")


@pytest.mark.parametrize("state,expired", [("running", True), ("fenced", False)])
def test_execution_claim_cannot_revive_expired_or_fenced_lease(state, expired):
    from datetime import UTC, datetime, timedelta

    repo = setup_repo()
    service = SchedulerService(repo)
    row = service.reserve(body("fenced-before-launch"), "alice")
    service.commit(row["reservation_id"])
    lease = repo.list_scheduler_leases()[0]
    lease["state"] = state
    lease["expires_at"] = (datetime.now(UTC) + timedelta(seconds=-1 if expired else 60)).isoformat()
    repo.save_scheduler_lease(lease)
    with pytest.raises(RuntimeError, match="reconciliation"):
        service.claim_execution(row["reservation_id"])
    assert not repo.list_scheduler_reservations()[0].get("execution_claimed")


def test_explicit_manual_pin_cannot_bypass_host_drain():
    repo = setup_repo()
    repo.set_host_policy("beast", mode="draining", expected_revision=1)
    request = dict(body("manual-drain"), placement_mode="manual", explicit_host_uid="uid-beast")
    assert SchedulerService(repo).reserve(request, "alice")["state"] == "queued"




def test_two_requests_cannot_reserve_the_same_prepared_workspace():
    repo = setup_repo()
    first = SchedulerService(repo)
    row = first.reserve(dict(body("writer-one"), workspace_id="shared-worktree"), "alice")
    second = SchedulerService(repo)
    blocked = second.reserve(dict(body("writer-two"), workspace_id="shared-worktree"), "alice")
    assert blocked["state"] == "queued"
    assert "WORKSPACE_LOCK" in blocked["decision"]["explanations"]["uid-beast"]["reason_codes"]
    first.release(row["reservation_id"])
    assert second.retry_queued(blocked["reservation_id"])["state"] == "reserved"


def test_claimed_execution_cannot_release_capacity_without_stop_verification():
    service = SchedulerService(setup_repo())
    row = service.reserve(body("live-writer"), "alice")
    service.commit(row["reservation_id"])
    service.claim_execution(row["reservation_id"])
    with pytest.raises(PermissionError, match="verified execution stop"):
        service.release(row["reservation_id"])
    assert service.overview()["reserved_cpu"] == 2




def test_operator_active_limit_survives_service_refresh_and_queue_retry():
    repo = setup_repo()
    service = SchedulerService(repo, owner_concurrency_limit=1)
    first = service.reserve(body("budget-first"), "alice")
    queued = service.reserve(body("budget-second"), "alice")
    assert first["state"] == "reserved" and queued["state"] == "queued"
    assert service.retry_queued(queued["reservation_id"])["state"] == "queued"
    assert service.drain_queue() == []


def test_mission_and_owner_resource_limits_are_shared_across_requests():
    service = SchedulerService(setup_repo(), mission_concurrency_limit=1, owner_cpu_limit=3)
    first = dict(body("mission-first"), mission_id="mission")
    assert service.reserve(first, "alice")["state"] == "reserved"
    mission_blocked = service.reserve(dict(body("mission-second"), mission_id="mission"), "bob")
    assert "MISSION_QUOTA" in mission_blocked["decision"]["explanations"]["uid-beast"]["reason_codes"]
    owner_blocked = service.reserve(body("owner-resource"), "alice")
    assert "USER_RESOURCE_QUOTA" in owner_blocked["decision"]["explanations"]["uid-beast"]["reason_codes"]


def test_recursive_depth_limit_blocks_admission():
    service = SchedulerService(setup_repo(), maximum_spawn_depth=2)
    service.reserve(dict(body("root"), mission_id="mission", project_id="project"), "alice")
    first = service.reserve(dict(body("child-one"), parent_agent_id="root", spawn_depth=0), "alice")
    second = service.reserve(dict(body("child-two"), parent_agent_id="child-one", spawn_depth=0), "alice")
    assert first["request_snapshot"]["spawn_depth"] == 1
    assert second["request_snapshot"]["spawn_depth"] == 2
    row = service.reserve(dict(body("too-deep"), parent_agent_id="child-two", spawn_depth=0), "alice")
    assert row["state"] == "queued"
    assert row["request_snapshot"]["spawn_depth"] == 3
    assert row["request_snapshot"]["mission_id"] == "mission"
    assert row["request_snapshot"]["project_id"] == "project"
    assert "SPAWN_DEPTH_LIMIT" in row["decision"]["explanations"]["uid-beast"]["reason_codes"]


def test_children_cannot_forge_parent_ownership_or_escape_inherited_mission():
    service = SchedulerService(setup_repo())
    service.reserve(dict(body("root"), mission_id="mission"), "alice")
    with pytest.raises(PermissionError, match="parent owner"):
        service.reserve(dict(body("foreign-child"), parent_agent_id="root"), "bob")
    with pytest.raises(PermissionError, match="inherited mission"):
        service.reserve(dict(body("escape-child"), parent_agent_id="root", mission_id="other"), "alice")
    with pytest.raises(ValueError, match="parent context"):
        service.reserve(dict(body("unknown-child"), parent_agent_id="unregistered"), "alice")


def test_parent_fanout_budget_survives_restart_and_alternate_family():
    repo = setup_repo()
    first = SchedulerService(repo, parent_concurrency_limit=1)
    first.reserve(body("root"), "alice")
    first.reserve(dict(body("first-child"), parent_agent_id="root"), "alice")
    restarted = SchedulerService(repo, parent_concurrency_limit=1)
    queued = restarted.reserve(dict(body("second-child"), parent_agent_id="root", agent_family="pi"), "alice")
    assert queued["state"] == "queued"
    assert "SPAWN_FANOUT_LIMIT" in queued["decision"]["explanations"]["uid-beast"]["reason_codes"]


def test_ephemeral_repository_can_preview_but_cannot_commit_execution():
    source = setup_repo()
    repo = ControlPlaneRepo()
    repo.import_state(source.export_state())
    service = SchedulerService(repo)
    row = service.reserve(body("ephemeral"), "alice")
    assert row["state"] == "reserved"
    with pytest.raises(RuntimeError, match="durable scheduler persistence"):
        service.commit(row["reservation_id"])


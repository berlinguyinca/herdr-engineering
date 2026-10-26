from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from herdr_engineering.host_policy import HostPolicy, HostSnapshot, PolicyMode
from herdr_engineering.scheduler import (
    HostCapacity,
    PlacementRequest,
    Scheduler,
)


def host(uid, *, cpu=16, used=0, mode=PolicyMode.AUTOMATIC, revision=1, name=None):
    return HostSnapshot(
        host_uid=uid, host_name=name or uid, aliases=(name or uid,), platform="linux",
        owned=True, trusted=True, reachable=True,
        telemetry_at=datetime.now(UTC), telemetry_freshness_seconds=1,
        policy=HostPolicy(mode=mode, revision=revision),
    ), HostCapacity(total_cpu=cpu, used_cpu=used, total_memory_gib=64, used_memory_gib=8)


def request(request_id="r1", *, cpu=2, owner="alice", mode="automatic"):
    return PlacementRequest(
        request_id=request_id, owner_id=owner, agent_family="codex",
        placement_mode=mode, resource_profile={"class": "coding", "cpu_cores": cpu, "memory_gib": 2},
    )


def test_plan_is_deterministic_and_explains_rejected_hosts():
    a, ac = host("a", cpu=8, used=7)
    b, bc = host("b", used=1)
    scheduler = Scheduler(lambda: [(a, ac), (b, bc)])
    decision = scheduler.plan(request())
    assert decision.selected_host_uid == "b"
    assert decision.explanations["a"]["reason_codes"] == ["RESERVATION_CAPACITY"]
    assert decision.explanations["b"]["score"] > 0


def test_manual_only_requires_explicit_selection_and_parent_is_not_a_pin():
    mac, cap = host("mac", mode=PolicyMode.MANUAL_ONLY, name="mac.lan")
    scheduler = Scheduler(lambda: [(mac, cap)])
    assert scheduler.plan(request()).state == "queued"
    manual = scheduler.plan(request("r2", mode="manual"), explicit_host_uid="mac")
    assert manual.selected_host_uid == "mac"


def test_reserve_is_idempotent_and_commit_rechecks_policy_generation():
    state = {"mode": PolicyMode.AUTOMATIC}

    def hosts():
        current, cap = host("a", mode=state["mode"], revision=2)
        return [(current, cap)]

    scheduler = Scheduler(hosts)
    first = scheduler.reserve(request("same"))
    again = scheduler.reserve(request("same"))
    assert first.reservation_id == again.reservation_id
    state["mode"] = PolicyMode.DISABLED
    with pytest.raises(RuntimeError, match="policy changed"):
        scheduler.commit(first.reservation_id)


def test_one_hundred_concurrent_reservations_never_overcommit():
    current, cap = host("a", cpu=20)
    scheduler = Scheduler(lambda: [(current, cap)])
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda i: scheduler.reserve(request(f"r{i}", cpu=1)), range(100)))
    reserved = [r for r in results if r.state == "reserved"]
    queued = [r for r in results if r.state == "queued"]
    assert len(reserved) == 20
    assert len(queued) == 80
    assert sum(r.requested_cpu for r in reserved) <= 20


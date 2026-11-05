from datetime import UTC, datetime, timedelta

from herdr_engineering.control_plane.scheduler_service import SchedulerService
from tests.control_plane.test_scheduler_service import body, setup_repo


def test_dashboard_history_is_chronological_bounded_and_keeps_only_safe_explanations():
    repo = setup_repo()
    service = SchedulerService(repo)
    start = datetime(2026, 11, 1, tzinfo=UTC)
    for index in reversed(range(205)):
        repo.save_scheduler_reservation({
            "reservation_id": f"r-{index}", "request_id": f"request-{index}",
            "state": "queued", "host_uid": None, "lease_token": "private-lease-secret",
            "request_snapshot": {"agent_family": "codex", "priority": "normal", "token": "private-request-secret"},
            "decision": {"created_at": (start + timedelta(seconds=index)).isoformat(),
                         "explanations": {"beast": {"reason_codes": ["RESERVATION_CAPACITY"],
                             "score": 0, "token": "private-explanation-secret",
                             "measured": {"available_cpu": 0, "token": "private-measurement-secret"}}}},
        })
    result = service.overview()
    assert len(result["history"]) == 200
    assert result["history"][0]["request_id"] == "request-204"
    assert result["history"][-1]["request_id"] == "request-5"
    entry = result["history"][0]
    assert entry["agent_family"] == "codex"
    assert entry["explanations"]["beast"] == {
        "reason_codes": ["RESERVATION_CAPACITY"], "score": 0, "measured": {"available_cpu": 0}}
    assert result["queued_agents"] == 205
    assert "private-" not in str(result)


def test_retry_does_not_make_an_old_request_appear_newly_requested():
    repo = setup_repo()
    repo.upsert_host('beast', 'beast', None, scheduler_capacity={
        'total_cpu': 0, 'total_memory_gib': 0, 'total_disk_gib': 100})
    service = SchedulerService(repo)
    old = service.reserve(body('old-request'), 'operator')
    original_time = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    repo.save_scheduler_reservation({**old, 'queued_at': original_time})
    service.reserve(body('new-request'), 'operator')
    repo.upsert_host('beast', 'beast', None, scheduler_capacity={
        'total_cpu': 16, 'total_memory_gib': 64, 'total_disk_gib': 100})
    assert service.retry_queued(old['reservation_id'])['state'] == 'reserved'
    history = service.overview()['history']
    assert [row['request_id'] for row in history] == ['new-request', 'old-request']
    assert history[1]['created_at'] == original_time

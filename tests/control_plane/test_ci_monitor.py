import threading

from herdr_engineering.control_plane.ci_monitor import CIMonitor


class Provider:
    available = True
    token = 'private-token'
    base_url = 'https://ci.example'

    def repos(self, **kwargs):
        return [{'id': 17, 'repository': 'org/app'}]

    def pipelines(self, repository, **kwargs):
        assert repository == '17'
        return [{'run_number': 2, 'repository': repository, 'state': 'running'}]

    def agents(self):
        raise RuntimeError('admin required')


def test_sync_maps_real_repo_objects_and_optional_agents():
    monitor = CIMonitor(provider_factory=Provider)
    monitor.refresh()
    view = monitor.snapshot(start=False)
    assert view['available']
    assert view['pipelines']['org/app'][0]['run_number'] == 2
    assert view['status'] == 'connected'
    assert view['agents_error']
    assert view['progress']['completed'] == 1
    assert 'private-token' not in str(view)


def test_failure_retains_last_known_snapshot_and_marks_stale():
    provider = Provider()
    monitor = CIMonitor(provider_factory=lambda: provider)
    monitor.refresh()
    provider.repos = lambda **kwargs: (_ for _ in ()).throw(RuntimeError('private-token'))
    monitor.refresh()
    view = monitor.snapshot(start=False)
    assert view['stale'] and not view['available']
    assert view['pipelines']['org/app']
    assert 'private-token' not in str(view)
    assert view['last_success_at']


def test_first_request_is_nonblocking_and_single_flight():
    entered, release = threading.Event(), threading.Event()
    provider = Provider()
    original = provider.repos

    def blocked(**kwargs):
        entered.set()
        release.wait(3)
        return original(**kwargs)

    provider.repos = blocked
    monitor = CIMonitor(provider_factory=lambda: provider)
    try:
        monitor.snapshot()
        assert entered.wait(1)
        first = monitor._thread
        for _ in range(20):
            assert monitor.snapshot()['refreshing']
            assert monitor._thread is first
    finally:
        release.set()
        monitor.close()


def test_partial_error_keeps_repo_runs_and_explains_failure():
    provider = Provider()
    monitor = CIMonitor(provider_factory=lambda: provider)
    monitor.refresh()
    provider.pipelines = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('broken'))
    monitor.refresh()
    view = monitor.snapshot(start=False)
    assert view['status'] == 'partial'
    assert view['pipelines']['org/app']
    assert view['repo_errors']['org/app']
    assert view['stale']


def test_repeated_unpaginated_repo_snapshot_is_deduplicated():
    provider = Provider()
    provider.repos = lambda **kwargs: [{'id': i, 'repository': f'org/app{i}'} for i in range(50)]
    provider.pipelines = lambda *a, **k: []
    monitor = CIMonitor(provider_factory=lambda: provider)
    monitor.refresh()
    assert len(monitor.snapshot(start=False)['repos']) == 50


def test_auth_error_is_distinct_from_empty_inventory():
    class Denied(Exception):
        status_code = 401

    provider = Provider()
    provider.repos = lambda **kwargs: (_ for _ in ()).throw(Denied())
    monitor = CIMonitor(provider_factory=lambda: provider)
    monitor.refresh()
    assert monitor.snapshot(start=False)['status'] == 'auth_error'


def test_only_terminal_changes_emit_events_not_polling_or_initial_history():
    provider = Provider()
    events = []
    monitor = CIMonitor(provider_factory=lambda: provider, on_transition=lambda *args: events.append(args))
    monitor.refresh()
    monitor.refresh()
    assert not events
    provider.pipelines = lambda *a, **k: [{'run_number': 2, 'state': 'failure'}]
    monitor.refresh()
    monitor.refresh()
    assert len(events) == 1
    assert events[0][1] == 'CIPipelineFailed'


def test_failed_refresh_does_not_reset_success_age():
    provider = Provider()
    monitor = CIMonitor(provider_factory=lambda: provider)
    monitor.refresh()
    original = monitor.snapshot(start=False)['last_success_at']
    provider.pipelines = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline'))
    monitor.refresh()
    assert monitor.snapshot(start=False)['last_success_at'] == original


def test_repository_first_success_does_not_notify_its_old_history():
    provider = Provider()
    events = []
    provider.pipelines = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline'))
    monitor = CIMonitor(provider_factory=lambda: provider, on_transition=lambda *args: events.append(args))
    monitor.refresh()
    provider.pipelines = lambda *a, **k: [{'run_number': 2, 'state': 'failure'}]
    monitor.refresh()
    assert not events


def test_incremental_refresh_keeps_unprocessed_repository_visible():
    provider = Provider()
    provider.repos = lambda **kwargs: [{'id': 1, 'repository': 'org/a'}, {'id': 2, 'repository': 'org/b'}]
    provider.pipelines = lambda repository, **kwargs: [{'run_number': 1, 'state': 'running'}]
    monitor = CIMonitor(provider_factory=lambda: provider)
    monitor.refresh()
    entered, release = threading.Event(), threading.Event()

    def slow(repository, **kwargs):
        if repository == '2':
            entered.set()
            release.wait(3)
        return [{'run_number': 1, 'state': 'success'}]

    provider.pipelines = slow
    worker = threading.Thread(target=monitor.refresh)
    worker.start()
    try:
        assert entered.wait(1)
        assert set(monitor.snapshot(start=False)['pipelines']) == {'org/a', 'org/b'}
    finally:
        release.set()
        worker.join(3)


def test_observation_dedup_covers_inventory_larger_than_retained_history():
    from herdr_engineering.control_plane.ci_reports import PIPELINE_LIMIT
    from herdr_engineering.control_plane.repo import ControlPlaneRepo

    repo = ControlPlaneRepo()
    monitor = CIMonitor(on_observation=repo.observe_ci_pipeline)
    rows = [{'repository': 'org/app', 'provider_repo_id': 17, 'run_number': number,
             'state': 'success'} for number in range(1, PIPELINE_LIMIT + 2)]
    assert monitor._persist_observations({'org/app': rows}) is None
    written = repo.export_state()['ci_sequence']
    assert written == PIPELINE_LIMIT + 1
    assert len(repo.ci_reports()['pipelines']) == PIPELINE_LIMIT
    # Canonical fingerprints also ignore JSON object key insertion order.
    equivalent = [dict(reversed(list(row.items()))) for row in rows]
    assert monitor._persist_observations({'org/app': equivalent}) is None
    assert repo.export_state()['ci_sequence'] == written
    rows[0] = {**rows[0], 'state': 'failure'}
    assert monitor._persist_observations({'org/app': rows}) is None
    assert repo.export_state()['ci_sequence'] == written + 1
    # A disappeared observation is forgotten and persisted if it reappears.
    assert monitor._persist_observations({}) is None
    assert monitor._observation_fingerprints == {}
    assert monitor._persist_observations({'org/app': [rows[1]]}) is None
    assert repo.export_state()["ci_sequence"] == written + 2
    assert len(monitor._observation_fingerprints) == 1


def test_observation_fingerprints_remember_only_successful_persistence(tmp_path):
    import json

    from herdr_engineering.control_plane.repo import ControlPlaneRepo

    class DiskJournal:
        def record_required(self, event):
            with (tmp_path / 'observations.jsonl').open('a') as stream:
                stream.write(json.dumps(event) + '\n')

    repo = ControlPlaneRepo(sink=DiskJournal())
    monitor = CIMonitor(on_observation=repo.observe_ci_pipeline)
    row = {'repository': 'org/app', 'provider_repo_id': 17, 'run_number': 1, 'state': 'running'}
    snapshot = {'org/app': [row]}
    journal = tmp_path / 'observations.jsonl'
    journal.mkdir()
    assert 'persisted' in monitor._persist_observations(snapshot)
    assert monitor._observation_fingerprints == {}
    journal.rmdir()
    assert monitor._persist_observations(snapshot) is None
    assert len(journal.read_text().splitlines()) == 1
    assert monitor._persist_observations(snapshot) is None
    assert len(journal.read_text().splitlines()) == 1
    # Failed changed results must retry as well as failed first observations.
    saved = journal.read_text()
    journal.unlink()
    journal.mkdir()
    snapshot['org/app'][0] = {**row, 'state': 'failure'}
    assert 'persisted' in monitor._persist_observations(snapshot)
    journal.rmdir()
    journal.write_text(saved)
    assert monitor._persist_observations(snapshot) is None
    assert len(journal.read_text().splitlines()) == 2


def test_parallel_heads_respect_shared_limit_across_history_and_repeated_timeouts():
    import time

    provider = Provider()
    provider.repos = lambda **kwargs: [{'id': i, 'repository': f'org/{i}'} for i in range(1, 7)]
    release, saturated = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = peak = calls = 0

    def blocked(repository, **kwargs):
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
            if active == 2:
                saturated.set()
        try:
            release.wait(3)
            return []
        finally:
            with lock:
                active -= 1

    provider.pipelines = blocked
    monitor = CIMonitor(provider_factory=lambda: provider, max_in_flight=2, budget=0.1)
    worker = threading.Thread(target=monitor.refresh)
    worker.start()
    try:
        assert saturated.wait(1), 'repository heads must read concurrently'
        worker.join(0.5)
        assert not worker.is_alive()
        started = time.monotonic()
        monitor.refresh()
        monitor._history.refresh()
        assert time.monotonic() - started < 0.5
        assert peak == 2 and calls == 2, 'timed-out reads must retain their capacity slots'
        assert monitor.snapshot(start=False)['stale']
    finally:
        release.set()
        worker.join(3)
        monitor.close()


def test_monitor_request_budget_preserves_repository_cursor():
    provider = Provider()
    provider.repos = lambda **kwargs: [{'id': i, 'repository': f'org/{i}'} for i in range(1, 6)]
    seen = []

    def pipelines(repository, **kwargs):
        seen.append(repository)
        return [{'run_number': 1, 'state': 'success'}]

    provider.pipelines = pipelines
    monitor = CIMonitor(provider_factory=lambda: provider, max_reads_per_refresh=3)
    try:
        for _ in range(3):
            before = monitor._measurement_snapshot()['provider_calls']
            monitor.refresh()
            assert monitor._measurement_snapshot()['provider_calls'] - before <= 3
        assert set(seen) == {'1', '2', '3', '4', '5'}
        view = monitor.snapshot(start=False)
        assert view['stale'] and view['status'] == 'partial'
        assert all(view['pipelines'][f'org/{i}'] for i in range(1, 6))
    finally:
        monitor.close()


def test_unrelated_http_request_completes_while_provider_is_blocked():
    import json
    import urllib.request

    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.control_plane.repo import ControlPlaneRepo

    entered, release = threading.Event(), threading.Event()
    provider = Provider()

    def blocked(**kwargs):
        entered.set()
        release.wait(3)
        return [{'id': 17, 'repository': 'org/app'}]

    provider.repos = blocked
    monitor = CIMonitor(provider_factory=lambda: provider, budget=0.1)
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={'read': 'viewer'})
    server.ci_monitor.close()
    server.ci_monitor = monitor
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    monitor.snapshot()
    try:
        assert entered.wait(1)
        request = urllib.request.Request(f'http://127.0.0.1:{server.port}/api/v1/ci/reports',
                                         headers={'X-API-Key': 'read'})
        with urllib.request.urlopen(request, timeout=0.5) as response:
            assert response.status == 200
            assert json.load(response)['reports'] == []
        assert not release.is_set()
    finally:
        release.set()
        server.shutdown()
        thread.join(3)


def test_provider_switch_does_not_relabel_cached_runs_after_failed_head():
    provider = Provider()
    monitor = CIMonitor(provider_factory=lambda: provider)
    monitor.refresh()
    provider.base_url = 'https://other.example'
    provider.pipelines = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline'))
    monitor.refresh()
    view = monitor.snapshot(start=False)
    assert view['stale']
    assert view['pipelines']['org/app'] == []
    monitor.close()

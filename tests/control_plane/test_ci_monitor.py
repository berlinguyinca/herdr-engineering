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

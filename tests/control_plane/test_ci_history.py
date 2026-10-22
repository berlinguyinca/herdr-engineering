from datetime import UTC, datetime, timedelta

from herdr_engineering.control_plane.ci_monitor import CIMonitor

NOW = datetime(2026, 10, 22, tzinfo=UTC)


def runs(count, *, days=1):
    return [{'run_number': count - i, 'created_at': (NOW - timedelta(days=i * days)).isoformat(),
             'branch': 'main' if i % 2 else 'topic',
             'event': 'push' if i % 2 else 'pull_request', 'state': 'success'}
            for i in range(count)]


class MemoryProvider:
    """An actual in-memory paginated inventory with injectable data and read errors."""
    available = True
    base_url = 'https://ci.example'

    def __init__(self, rows, *, repositories=None, repeat=False, failure=None):
        self.rows = rows
        self.repositories = repositories or [{'id': 17, 'repository': 'org/app',
                                               'default_branch': 'main'}]
        self.repeat = repeat
        self.failure = failure
        self.reads = []

    def repos(self, *, page=1, per_page=50):
        return self.repositories[(page - 1) * per_page:page * per_page]

    def pipelines(self, repository, *, page=1, per_page=50):
        self.reads.append((repository, page))
        if self.failure == (repository, page):
            raise RuntimeError('private provider error')
        start = 0 if self.repeat else (page - 1) * per_page
        return self.rows[start:start + per_page]


def history(provider, **kwargs):
    from herdr_engineering.control_plane.ci_history import CIHistory
    return CIHistory(provider_factory=lambda: provider, wall_now=lambda: NOW, **kwargs)


def test_monitor_exposes_separate_flat_memory_history_without_observing():
    monitor = CIMonitor(provider_factory=lambda: MemoryProvider([]))
    view = monitor.history(start=False)
    assert view['pipelines'] == []
    assert view['scope']['memory_only'] and view['scope']['refetched_on_restart']
    monitor.close()


def test_filtered_last24_backfills_beyond_thirty_days_and_recent20():
    cache = history(MemoryProvider(runs(100)))
    cache.refresh()
    view = cache.snapshot(start=False)
    assert len(view['pipelines']) >= 50
    assert sum(r['event'] == 'push' for r in view['pipelines']) >= 24
    assert sum(r['event'] == 'pull_request' for r in view['pipelines']) >= 24
    assert view['coverage']['17']['complete_since'] <= (NOW - timedelta(days=30)).isoformat()
    assert view['repos'][0]['default_branch'] == 'main'
    assert all(r['provider_url'] == 'https://ci.example' for r in view['pipelines'])


def test_empty_repository_is_exhausted_and_new_instance_has_no_history():
    cache = history(MemoryProvider([]))
    cache.refresh()
    assert cache.snapshot(start=False)['coverage']['17']['exhausted']
    assert history(MemoryProvider([])).snapshot(start=False)['coverage'] == {}


def test_repeated_pages_never_claim_complete_coverage():
    cache = history(MemoryProvider(runs(50, days=0.01), repeat=True))
    cache.refresh()
    view = cache.snapshot(start=False)
    assert len(view['pipelines']) == 50
    assert view['coverage']['17']['error']
    assert not view['coverage']['17']['exhausted']


def test_unordered_and_missing_timestamps_never_advance_watermark():
    for rows in ([runs(2)[1], runs(2)[0]], [{**runs(1)[0], 'created_at': None}]):
        cache = history(MemoryProvider(rows))
        cache.refresh()
        coverage = cache.snapshot(start=False)['coverage']['17']
        assert coverage['complete_since'] is None
        assert not coverage['exhausted']
        assert coverage['error']


def test_budget_round_robin_resumes_and_head_changes_invalidate_old_coverage():
    provider = MemoryProvider(runs(150, days=0.01), repositories=[
        {'id': i, 'repository': f'org/{i}', 'default_branch': 'main'} for i in (17, 18)])
    cache = history(provider, pages_per_refresh=2)
    cache.refresh()
    assert ('17', 1) in provider.reads and ('18', 1) in provider.reads
    cache.refresh()
    assert ('17', 2) in provider.reads and ('18', 2) in provider.reads
    provider.rows.insert(0, {**runs(1)[0], 'run_number': 151})
    cache.refresh()
    assert cache.snapshot(start=False)['coverage']['17']['complete_since'] == provider.rows[49]['created_at']
    assert not cache.snapshot(start=False)['coverage']['17']['exhausted']


def test_partial_error_and_page_cap_are_explicit():
    provider = MemoryProvider(runs(150, days=0.01), failure=('17', 2))
    cache = history(provider)
    cache.refresh()
    view = cache.snapshot(start=False)
    assert view['status'] == 'partial' and view['stale']
    assert len(view['pipelines']) == 50 and view['coverage']['17']['error']
    assert 'private provider error' not in str(view)
    capped = history(MemoryProvider(runs(150, days=0.01)), page_limit=1)
    capped.refresh()
    assert capped.snapshot(start=False)['coverage']['17']['truncated']


def test_overlapping_page_deduplicates_but_does_not_claim_contiguous_prefix():
    provider = MemoryProvider(runs(100, days=0.01))
    cache = history(provider, pages_per_refresh=1)
    cache.refresh()
    provider.rows.insert(50, provider.rows[49])
    cache.refresh()
    view = cache.snapshot(start=False)
    assert len({r['run_number'] for r in view['pipelines']}) == len(view['pipelines'])
    assert view['coverage']['17']['error']
    assert not view['coverage']['17']['exhausted']


def test_completed_scan_revalidates_head_before_publishing_exhaustion():
    class ChangingInventory(MemoryProvider):
        def pipelines(self, repository, *, page=1, per_page=50):
            result = super().pipelines(repository, page=page, per_page=per_page)
            if page == 2:
                self.rows.insert(0, {**runs(1)[0], 'run_number': 101})
            return result

    cache = history(ChangingInventory(runs(70, days=0.01)))
    cache.refresh()
    coverage = cache.snapshot(start=False)['coverage']['17']
    assert not coverage['exhausted']
    assert coverage['complete_since'] is None
    assert coverage['error']


def test_global_run_and_byte_caps_are_visible_for_each_affected_repository():
    repositories = [{'id': i, 'repository': f'org/{i}', 'default_branch': 'main'} for i in (17, 18)]
    cache = history(MemoryProvider(runs(100, days=0.01), repositories=repositories), total_limit=60)
    cache.refresh()
    view = cache.snapshot(start=False)
    assert len(view['pipelines']) == 60
    assert all(c['truncated'] for c in view['coverage'].values())
    bounded = history(MemoryProvider([{**runs(1)[0], 'revision': 'x' * 4000}]), byte_limit=1000)
    bounded.refresh()
    view = bounded.snapshot(start=False)
    assert view['pipelines'] == []
    assert view['coverage']['17']['truncated']
    assert not view['coverage']['17']['exhausted']


def test_history_is_asynchronous_single_flight_and_monitor_close_owns_it():
    import threading

    entered, release = threading.Event(), threading.Event()

    class BlockingInventory(MemoryProvider):
        def repos(self, **kwargs):
            entered.set()
            release.wait(3)
            return super().repos(**kwargs)

    monitor = CIMonitor(provider_factory=lambda: BlockingInventory([]))
    try:
        assert monitor.history()['refreshing']
        assert entered.wait(1)
        worker = monitor._history._thread
        for _ in range(10):
            assert monitor.history()['refreshing']
            assert monitor._history._thread is worker
        assert monitor._thread is None  # The live monitor remains independent.
    finally:
        release.set()
        monitor.close()
    assert not worker.is_alive()


def test_head_outcome_changes_update_cached_rows_without_false_duplicates():
    provider = MemoryProvider(runs(100, days=0.01))
    cache = history(provider, pages_per_refresh=1)
    cache.refresh()
    provider.rows[0]['state'] = 'failure'
    cache.refresh()
    view = cache.snapshot(start=False)
    assert next(r for r in view['pipelines'] if r['run_number'] == 100)['state'] == 'failure'
    assert not view['coverage']['17']['error']


def test_repositories_not_rechecked_within_budget_do_not_keep_exhaustion_claims():
    provider = MemoryProvider(runs(1), repositories=[
        {'id': i, 'repository': f'org/{i}', 'default_branch': 'main'} for i in (17, 18)])
    cache = history(provider, pages_per_refresh=2)
    cache.refresh()
    assert all(c['exhausted'] for c in cache.snapshot(start=False)['coverage'].values())
    cache._pages_per_refresh = 1
    cache.refresh()
    view = cache.snapshot(start=False)
    assert sum(c['exhausted'] for c in view['coverage'].values()) == 1
    assert view['stale']


def test_default_limits_stop_at_sixty_pages_without_exhaustion_claim():
    provider = MemoryProvider(runs(3001, days=0.001))
    cache = history(provider)
    cache.refresh()
    view = cache.snapshot(start=False)
    assert len(view['pipelines']) == 3000
    assert max(page for _, page in provider.reads) == 60
    assert view['coverage']['17']['truncated']
    assert not view['coverage']['17']['exhausted']


def test_unchanged_completed_prefix_refreshes_head_without_restarting_backfill():
    provider = MemoryProvider(runs(70, days=0.01))
    cache = history(provider)
    cache.refresh()
    provider.reads.clear()
    provider.rows[0]['state'] = 'failure'
    cache.refresh()
    view = cache.snapshot(start=False)
    assert provider.reads == [('17', 1)]
    assert len(view['pipelines']) == 70
    assert view['coverage']['17']['exhausted']
    assert view['pipelines'][0]['state'] == 'failure'


def test_completed_prefix_rechecks_older_outcomes_after_explicit_expiry():
    clock = [0.0]
    provider = MemoryProvider(runs(70, days=0.01))
    cache = history(provider, now=lambda: clock[0])
    cache.refresh()
    provider.rows[60]['state'] = 'failure'
    clock[0] = 301.0
    cache.refresh()
    view = cache.snapshot(start=False)
    assert next(r for r in view['pipelines'] if r['run_number'] == 10)['state'] == 'failure'
    assert view['scope']['outcome_refresh_seconds'] == 300


def test_continuing_backfill_rechecks_previously_running_older_page():
    provider = MemoryProvider(runs(150, days=0.01))
    provider.rows[60]['state'] = 'running'
    cache = history(provider, pages_per_refresh=1)
    cache.refresh()
    cache.refresh()
    assert next(r for r in cache.snapshot(start=False)['pipelines'] if r['run_number'] == 90)['state'] == 'running'
    provider.rows[60]['state'] = 'success'
    cache.refresh()
    view = cache.snapshot(start=False)
    assert next(r for r in view['pipelines'] if r['run_number'] == 90)['state'] == 'success'
    assert not view['stale']


def test_initial_provider_errors_have_safe_classified_status():
    from herdr_engineering.ci import CIProviderError

    class DeniedInventory(MemoryProvider):
        def repos(self, **kwargs):
            raise CIProviderError('authentication', 401)

    cache = history(DeniedInventory([]))
    cache.refresh()
    view = cache.snapshot(start=False)
    assert view['status'] == 'auth_error'
    assert view['error']
    assert not view['available']


def test_deleted_row_outside_head_invalidates_offset_cursor_before_exhaustion():
    provider = MemoryProvider(runs(150, days=0.01))
    cache = history(provider, pages_per_refresh=2)
    cache.refresh()
    del provider.rows[50]
    cache.refresh()
    coverage = cache.snapshot(start=False)['coverage']['17']
    assert not coverage['exhausted']
    assert coverage['complete_since'] is None
    assert coverage['error']


def test_expired_backfill_keeps_rows_but_marks_unrevalidated_outcomes_stale():
    clock = [0.0]
    provider = MemoryProvider(runs(70, days=0.01))
    cache = history(provider, now=lambda: clock[0])
    cache.refresh()
    clock[0] = 301.0
    cache._pages_per_refresh = 1
    cache.refresh()
    view = cache.snapshot(start=False)
    assert len(view['pipelines']) == 70
    assert view['stale']
    assert not view['coverage']['17']['exhausted']


def test_exact_fifty_repository_full_snapshot_is_complete_when_provider_declares_it():
    class SnapshotInventory(MemoryProvider):
        repos_are_snapshot = True

        def repos(self, **kwargs):
            return self.repositories

    provider = SnapshotInventory([], repositories=[
        {'id': i, 'repository': f'org/{i}', 'default_branch': 'main'} for i in range(1, 51)])
    cache = history(provider)
    cache.refresh()
    view = cache.snapshot(start=False)
    assert len(view['repos']) == 50
    assert view['scope']['inventory_complete']
    assert view['status'] == 'connected'


def test_cursor_boundary_is_rechecked_after_next_page_fetch():
    class ChangingContinuation(MemoryProvider):
        def pipelines(self, repository, *, page=1, per_page=50):
            if page == 3:
                del self.rows[50]
            return super().pipelines(repository, page=page, per_page=per_page)

    provider = ChangingContinuation(runs(150, days=0.01))
    cache = history(provider, pages_per_refresh=2)
    cache.refresh()
    cache.refresh()
    coverage = cache.snapshot(start=False)['coverage']['17']
    assert not coverage['exhausted']
    assert coverage['complete_since'] is None
    assert 'continuation boundary' in coverage['error']


def test_repository_errors_preserve_safe_auth_and_rate_limit_explanations():
    from herdr_engineering.ci import CIProviderError

    class DeniedRuns(MemoryProvider):
        def __init__(self, reason, status):
            super().__init__([])
            self.reason, self.status = reason, status

        def pipelines(self, *args, **kwargs):
            raise CIProviderError(self.reason, self.status)

    for reason, status, expected in [('authentication', 401, '401'),
                                      ('rate_limited', 429, 'limiting requests')]:
        cache = history(DeniedRuns(reason, status))
        cache.refresh()
        assert expected in cache.snapshot(start=False)['coverage']['17']['error']


def test_successful_expired_rescan_prunes_runs_removed_by_provider():
    clock = [0.0]
    provider = MemoryProvider(runs(70, days=0.01))
    cache = history(provider, now=lambda: clock[0])
    cache.refresh()
    del provider.rows[60]
    clock[0] = 301.0
    cache.refresh()
    view = cache.snapshot(start=False)
    assert view['coverage']['17']['exhausted']
    assert not view['stale']
    assert len(view['pipelines']) == 69
    assert all(row['run_number'] != 10 for row in view['pipelines'])
    assert cache._run_count == 69
    assert cache._byte_count == sum(sum(state['sizes'].values()) for state in cache._states.values())


def test_successful_window_completion_prunes_unvalidated_rows_beyond_prefix():
    clock = [0.0]
    provider = MemoryProvider(runs(100))
    provider.repositories[0]['default_branch'] = None
    cache = history(provider, now=lambda: clock[0])
    cache.refresh()
    assert len(cache.snapshot(start=False)['pipelines']) == 100
    provider.repositories[0]['default_branch'] = 'main'
    clock[0] = 301.0
    cache.refresh()
    view = cache.snapshot(start=False)
    assert len(view['pipelines']) == 50
    assert not view['coverage']['17']['exhausted']
    assert not view['stale']


def test_budgeted_large_inventory_converges_to_validated_coverage():
    clock = [0.0]

    class TimedInventory(MemoryProvider):
        def pipelines(self, *args, **kwargs):
            clock[0] += 0.2
            return super().pipelines(*args, **kwargs)

    provider = TimedInventory(runs(100, days=0.01), repositories=[
        {'id': i, 'repository': f'org/{i}', 'default_branch': 'main'} for i in range(1, 118)])
    cache = history(provider, now=lambda: clock[0], budget=120)
    for _ in range(8):
        cache.refresh()
        view = cache.snapshot(start=False)
        if all(c['exhausted'] and not c['error'] for c in view['coverage'].values()):
            break
    assert len(view['repos']) == 117
    assert len(view['pipelines']) == 11700
    assert all(c['exhausted'] and not c['error'] for c in view['coverage'].values())
    assert not view['stale']


def test_pending_completed_coverage_cannot_erase_mutable_page_read_failure():
    clock = [0.0]

    class TimedInventory(MemoryProvider):
        def pipelines(self, *args, **kwargs):
            clock[0] += 0.2
            return super().pipelines(*args, **kwargs)

    provider = TimedInventory(runs(70, days=0.01))
    provider.rows[60]['state'] = 'running'
    cache = history(provider, now=lambda: clock[0], budget=0.75)
    cache.refresh()
    assert cache._states['17']['pending_coverage'] is not None
    cache._budget = 10
    provider.failure = ('17', 2)
    cache.refresh()
    view = cache.snapshot(start=False)
    assert view['coverage']['17']['error']
    assert not view['coverage']['17']['exhausted']
    assert view['stale']

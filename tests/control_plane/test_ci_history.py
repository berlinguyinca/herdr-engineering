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


def test_history_compatibility_shape_and_private_measurements():
    import json

    clock = [0.0]

    class TimedProvider(MemoryProvider):
        def pipelines(self, *args, **kwargs):
            clock[0] += 0.25
            return super().pipelines(*args, **kwargs)

    provider = TimedProvider(runs(3))
    cache = history(provider, now=lambda: clock[0])
    cache.refresh()
    view = cache.snapshot(start=False)
    assert set(view) == {'available', 'status', 'refreshing', 'stale', 'error',
                         'repos', 'pipelines', 'coverage', 'scope'}
    assert len(view['pipelines']) == 3
    measured = cache._measurement_snapshot()
    assert measured['provider_calls'] == len(provider.reads) + 1
    assert measured['summary_rows'] == 3
    assert measured['payload_bytes'] == len(json.dumps(view, separators=(',', ':')).encode())
    assert measured['refresh_duration_seconds'] == 0.5
    assert measured['refresh_finished_at'] is not None
    clock[0] = 100
    assert cache.snapshot(start=False)['stale']
    assert cache._measurement_snapshot()['stale'] is True
    assert cache._measurement_snapshot()['cache_hits'] == 2
    assert 'provider_calls' not in json.dumps(view)


def test_failed_history_provider_reads_are_counted_without_error_text():
    provider = MemoryProvider(runs(3), failure=('17', 1))
    cache = history(provider)
    cache.refresh()
    view = cache.snapshot(start=False)
    assert view['status'] == 'partial'
    measured = cache._measurement_snapshot()
    assert measured['provider_calls'] == len(provider.reads) + 1
    assert measured['stale'] is True
    assert 'private provider error' not in str(measured)


def test_refresh_reads_all_heads_before_mutable_history_continuations():
    provider = MemoryProvider(runs(70, days=0.01), repositories=[
        {'id': i, 'repository': f'org/{i}', 'default_branch': 'main'} for i in (17, 18)])
    provider.rows[60]['state'] = 'running'
    cache = history(provider)
    cache.refresh()
    provider.reads.clear()
    cache.refresh()
    assert {rid for rid, page in provider.reads[:2] if page == 1} == {'17', '18'}


def test_read_budget_counts_inventory_boundary_and_final_validation_reads():
    provider = MemoryProvider(runs(200, days=0.01))
    cache = history(provider, max_reads_per_refresh=4)
    cache.refresh()
    assert cache._measurement_snapshot()['provider_calls'] <= 4
    view = cache.snapshot(start=False)
    assert view['pipelines']
    assert view['stale']
    assert not view['coverage']['17']['exhausted']


def test_compact_summary_is_stable_detached_and_does_not_copy_pipeline_rows():
    import json

    provider = MemoryProvider(runs(300, days=0.01))
    cache = history(provider)
    cache.refresh()
    full = cache.snapshot(start=False)
    first = cache.summary_snapshot(start=False)
    assert 'pipelines' not in first
    assert first['projects'][0]['pipeline_count'] == 300
    assert first['projects'][0]['outcomes'] == {'success': 300}
    assert first['projects'][0]['provider_url'] == provider.base_url
    assert first['projects'][0]['provider_repo_id'] == 17
    assert len(json.dumps(first)) < len(json.dumps(full)) / 10
    reads = len(provider.reads)
    assert cache.summary_snapshot(start=False) == first

    class Uncopyable(dict):
        def __deepcopy__(self, memo):
            raise AssertionError('summary traversed compatibility pipeline rows')

    cache._view['pipelines'] = [Uncopyable(row) for row in full['pipelines']]
    assert cache.summary_snapshot(start=False) == first
    assert len(provider.reads) == reads
    provider.rows[0]['state'] = 'failure'
    cache.refresh()
    changed = cache.summary_snapshot(start=False)
    assert changed['revision'] > first['revision']
    assert changed['projects'][0]['outcomes'] == {'success': 299, 'failure': 1}
    changed['projects'].clear()
    assert cache.summary_snapshot(start=False)['projects']


def test_compact_provider_summary_uses_safe_outcome_keys_without_losing_unknown_runs():
    import json

    credential = 'Z9q7V2m4R8t6N3p5L1w0'
    states = [credential, f'provider-error-{credential}', 'unknown', 'passed', 'blocked']
    provider = MemoryProvider([{**row, 'state': state}
                               for row, state in zip(runs(5), states, strict=True)])
    cache = history(provider)
    cache.refresh()
    view = cache.summary_snapshot(start=False)
    assert credential not in json.dumps(view)
    assert view['projects'][0]['outcomes'] == {'unknown': 3, 'success': 1, 'blocked': 1}
    assert view['projects'][0]['pipeline_count'] == 5
    assert [row['state'] for row in provider.rows] == states


def test_slow_history_returns_with_last_valid_rows_stale_and_no_late_publication():
    import threading

    provider = MemoryProvider(runs(3))
    cache = history(provider, budget=0.05)
    cache.refresh()
    before = cache.snapshot(start=False)['pipelines']
    entered, release = threading.Event(), threading.Event()
    original = provider.pipelines

    def blocked(*args, **kwargs):
        entered.set()
        release.wait(3)
        return [{**row, 'state': 'failure'} for row in original(*args, **kwargs)]

    provider.pipelines = blocked
    worker = threading.Thread(target=cache.refresh)
    worker.start()
    try:
        assert entered.wait(1)
        worker.join(0.5)
        assert not worker.is_alive(), 'refresh exceeded its deadline'
        view = cache.snapshot(start=False)
        assert view['pipelines'] == before
        assert view['stale'] and not view['refreshing']
        assert not view['coverage']['17']['exhausted']
    finally:
        release.set()
        worker.join(3)
        cache.close()
    assert cache.snapshot(start=False)['pipelines'] == before


def test_partial_inventory_keeps_unseen_retained_repositories_without_coverage_claims():
    clock = [0.0]
    provider = MemoryProvider(runs(1), repositories=[
        {'id': i, 'repository': f'org/{i}', 'default_branch': 'main'} for i in range(1, 52)])
    cache = history(provider, now=lambda: clock[0])
    cache.refresh()
    original = provider.repos

    def slow_inventory(**kwargs):
        clock[0] += 121
        return original(**kwargs)

    provider.repos = slow_inventory
    cache.refresh()
    view = cache.snapshot(start=False)
    assert len(view['repos']) == 51
    assert len(view['pipelines']) == 51
    assert view['stale'] and not view['scope']['inventory_complete']
    assert not any(c['exhausted'] for c in view['coverage'].values())


def test_noop_refresh_keeps_summary_revision_and_generated_at():
    cache = history(MemoryProvider(runs(3)))
    cache.refresh()
    first = cache.summary_snapshot(start=False)
    cache.refresh()
    second = cache.summary_snapshot(start=False)
    assert second['revision'] == first['revision']
    assert second['generated_at'] == first['generated_at']


def test_repository_rename_discards_old_pages_and_revalidates_coverage():
    provider = MemoryProvider(runs(70, days=0.01))
    provider.rows[60]['state'] = 'failure'
    cache = history(provider)
    cache.refresh()
    assert cache.snapshot(start=False)['coverage']['17']['exhausted']
    provider.repositories[0]['repository'] = 'org/renamed'
    provider.rows[60]['state'] = 'success'
    provider.reads.clear()
    cache._pages_per_refresh = 1
    cache.refresh()
    view = cache.snapshot(start=False)
    assert len(view['pipelines']) == 50
    assert {row['repository'] for row in view['pipelines']} == {'org/renamed'}
    assert not view['coverage']['17']['exhausted']
    assert view['coverage']['17']['complete_since'] == provider.rows[49]['created_at']
    project = cache.summary_snapshot(start=False)['projects'][0]
    assert project['repository'] == 'org/renamed'
    assert project['pipeline_count'] == 50
    assert project['outcomes'] == {'success': 50}
    cache.refresh()
    view = cache.snapshot(start=False)
    assert ('17', 2) in provider.reads
    assert len(view['pipelines']) == 70
    assert {row['repository'] for row in view['pipelines']} == {'org/renamed'}
    assert view['coverage']['17']['exhausted']
    assert cache.summary_snapshot(start=False)['projects'][0]['outcomes'] == {'success': 70}
    cache.close()


def test_repository_rename_with_failed_head_has_no_relabelled_rows_or_coverage():
    provider = MemoryProvider(runs(70, days=0.01))
    cache = history(provider)
    cache.refresh()
    provider.repositories[0]['repository'] = 'org/renamed'
    provider.failure = ('17', 1)
    cache.refresh()
    view = cache.snapshot(start=False)
    assert view['pipelines'] == []
    assert view['stale']
    assert not view['coverage']['17']['exhausted']
    assert view['coverage']['17']['complete_since'] is None
    assert cache.summary_snapshot(start=False)['projects'][0]['pipeline_count'] == 0
    cache.close()


def test_prefetched_head_memory_is_bounded_independently_of_inventory_size():
    import json
    import threading
    import weakref

    byte_limit, concurrency = 500_000, 2
    for count in (8, 80):
        lock = threading.Lock()
        live = peak = largest = 0

        def released(size):
            nonlocal live
            with lock:
                live -= size

        class TrackedHead(list):
            def __init__(self, rows):
                nonlocal live, peak, largest
                super().__init__(rows)
                size = len(json.dumps(rows, separators=(',', ':')).encode())
                with lock:
                    live += size
                    peak = max(peak, live)
                    largest = max(largest, size)
                weakref.finalize(self, released, size)

        provider = MemoryProvider([{**row, 'revision': 'x' * 4096} for row in runs(50)],
                                  repositories=[{'id': i, 'repository': f'org/{i}',
                                                 'default_branch': 'main'} for i in range(1, count + 1)])
        cache = history(provider, byte_limit=byte_limit, max_in_flight=concurrency)
        original_fetch = cache._fetch

        def tracked_fetch(*args):
            return TrackedHead(original_fetch(*args))

        cache._fetch = tracked_fetch
        cache.refresh()
        # Measure actual live response objects, including completed futures, not
        # a production counter that could overlook temporary head dictionaries.
        assert peak <= byte_limit + (concurrency + 2) * largest
        view = cache.snapshot(start=False)
        assert view['pipelines'] and view['stale']
        assert len(view['repos']) == count
        assert cache._byte_count <= byte_limit
        assert any(not coverage['exhausted'] for coverage in view['coverage'].values())
        cache.close()


def test_failed_prefetch_does_not_retain_response_payloads_in_tracebacks():
    import threading
    import weakref

    lock = threading.Lock()
    live = weakref.WeakSet()
    peak = 0

    class Payload:
        def __init__(self):
            self.body = b'x' * 100_000

    provider = MemoryProvider([], repositories=[
        {'id': i, 'repository': f'org/{i}'} for i in range(1, 21)])
    cache = history(provider, max_in_flight=2, max_reads_per_refresh=21)

    def failed_fetch(*args):
        nonlocal peak
        payload = Payload()
        with lock:
            live.add(payload)
            peak = max(peak, len(live))
        raise RuntimeError('private decode failure')

    cache._fetch = failed_fetch
    cache.refresh()
    assert peak <= 4, 'staged errors retained provider response frames'
    view = cache.snapshot(start=False)
    assert view['pipelines'] == [] and view['stale']
    assert 'private decode failure' not in str(view)
    cache.close()

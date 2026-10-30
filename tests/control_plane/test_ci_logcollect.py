"""The zero-config collector: finished runs' step logs become counts and a per-test index, kept without any log text."""
import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.ci_logcollect import LogTestCollector, log_tests_config
from herdr_engineering.control_plane.ci_monitor import CIMonitor
from herdr_engineering.control_plane.repo import ControlPlaneRepo

URL = 'https://ci.example.org'
CARGO = ('running 3 tests\ntest a ... ok\ntest b ... FAILED\ntest c ... ignored\n'
         'test result: FAILED. 1 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.1s\n')
JUNIT = '<testsuite><testcase classname="m" name="x"/><testcase classname="m" name="y"/></testsuite>'


def iso(minutes_ago):
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat().replace('+00:00', 'Z')


class Provider:
    base_url = URL
    token = 'tok-secret'
    available = True

    def __init__(self, logs=None):
        self.logs = logs or {}
        self.calls = []

    def pipeline(self, repo, number):
        self.calls.append(('pipeline', repo, number))
        return {'tasks': [{'id': 1, 'name': 'clone', 'state': 'success'},
                          {'id': 2, 'name': 'rust', 'state': 'failure'},
                          {'id': 3, 'name': 'lint', 'state': 'success'},
                          {'id': 4, 'name': 'later', 'state': 'skipped'}]}

    def step_log_text(self, repo, number, step, *, max_chars):
        self.calls.append(('log', number, step))
        text = self.logs.get((number, step), 'nothing to see\n')
        return text[:max_chars], len(text) > max_chars


def run(number, minutes_ago=30, state='failure', repo=7):
    return {'run_number': number, 'repository': 'org/app', 'provider_repo_id': repo, 'provider_url': URL,
            'state': state, 'branch': 'main', 'revision': 'abc', 'finished_at': iso(minutes_ago)}


def collector(repo=None, **cfg):
    repo = repo or ControlPlaneRepo()
    config = log_tests_config({**cfg})
    return LogTestCollector(config, ingest=repo.ingest_ci_log_report, known=repo.ci_log_analysis), repo


def test_defaults_are_on_and_bounded_and_unknown_keys_are_refused():
    assert log_tests_config(None) == {'enabled': True, 'runs_per_cycle': 10, 'max_log_mib': 4, 'window_days': 14}
    for bad in ({'runs_per_cycle': 0}, {'runs_per_cycle': True}, {'enabled': 'yes'}, {'bogus': 1}, {'window_days': 999}, []):
        with pytest.raises(ValueError):
            log_tests_config(bad)


def test_a_finished_run_is_counted_from_its_logs_and_binds_to_the_run():
    c, repo = collector()
    provider = Provider({(1, '2'): CARGO})
    assert c.run_cycle(provider, {'g': [run(1)]}) == 1
    view = repo.ci_reports()
    (report,) = view['reports']
    assert report['source'] == 'log_parse' and report['runners'] == ['cargo']
    assert {k: report['summary'][k] for k in ('passed', 'failed', 'skipped', 'total')} == {'passed': 1, 'failed': 1, 'skipped': 1, 'total': 3}
    assert (report['provider_repo_id'], report['run_number'], report['identity_status']) == (7, 1, 'provided')
    assert [t['name'] for t in report['tests']] == ['a', 'b', 'c']
    assert report['tests'][1]['step_id'] == '2' and report['steps'] == [{'step_id': '2', 'step': 'rust', 'runners': ['cargo'], 'indexed': True}]
    assert ('log', 1, '1') not in provider.calls          # the clone step is never read
    assert ('log', 1, '4') not in provider.calls          # nor a step that did not run


def test_no_log_text_is_stored_anywhere():
    c, repo = collector()
    c.run_cycle(Provider({(1, '2'): CARGO + 'SECRET-LINE-OF-LOG-TEXT password=hunter2\n'}), {'g': [run(1)]})
    assert 'SECRET-LINE' not in json.dumps(repo.export_state()) and 'hunter2' not in json.dumps(repo.export_state())


def test_each_run_is_read_once_newest_first_and_per_cycle_limited():
    c, repo = collector(runs_per_cycle=2)
    provider = Provider({(n, '2'): CARGO for n in range(1, 6)})
    pipelines = {'g': [run(1, 500), run(2, 400), run(3, 300), run(4, 200), run(5, 100)]}
    assert c.run_cycle(provider, pipelines) == 2
    assert {r['run_number'] for r in repo.ci_reports()['reports']} == {5, 4}
    assert c.run_cycle(provider, pipelines) == 2
    assert c.run_cycle(provider, pipelines) == 1
    assert c.run_cycle(provider, pipelines) == 0
    assert len([call for call in provider.calls if call[0] == 'pipeline']) == 5


def test_running_runs_old_runs_and_a_disabled_collector_are_left_alone():
    c, repo = collector(window_days=1)
    provider = Provider({(n, '2'): CARGO for n in (1, 2, 3)})
    assert c.run_cycle(provider, {'g': [run(1, state='running'), run(2, minutes_ago=3 * 1440), run(3, state='success')]}) == 1
    off, _ = collector(enabled=False)
    assert off.run_cycle(provider, {'g': [run(9)]}) == 0


def test_runs_without_tests_are_remembered_and_never_shown():
    c, repo = collector()
    provider = Provider()
    assert c.run_cycle(provider, {'g': [run(1, 600)]}) == 1
    assert repo.ci_reports()['reports'] == []
    assert repo.ci_log_analysis(URL, 7, 1)['outcome'] == 'no_tests'
    before = len(provider.calls)
    assert c.run_cycle(provider, {'g': [run(1, 600)]}) == 0 and len(provider.calls) == before
    # a restart forgets memory but not the store
    again, _ = collector(repo)
    assert again.run_cycle(provider, {'g': [run(1, 600)]}) == 0 and len(provider.calls) == before


def test_a_negative_result_for_a_run_that_had_just_finished_is_retried_once_after_six_hours():
    c, repo = collector()
    provider = Provider()
    c.run_cycle(provider, {'g': [run(1, 5)]})                                  # finished 5 minutes ago: logs may be incomplete
    assert repo.ci_log_analysis(URL, 7, 1)['provisional'] is True
    clock = [datetime.now(UTC).timestamp()]
    c._wall = lambda: clock[0] + 3 * 3600
    assert c.run_cycle(provider, {'g': [run(1, 5)]}) == 0
    c._wall = lambda: clock[0] + 7 * 3600
    assert c.run_cycle(provider, {'g': [run(1, 5)]}) == 1
    assert repo.ci_log_analysis(URL, 7, 1)['provisional'] is False             # read long after the run ended: final


def test_a_log_too_large_to_read_is_skipped_by_name_not_guessed_from_its_start():
    c, repo = collector(max_log_mib=1)
    provider = Provider({(1, '2'): 'x' * (1024 * 1024 + 5), (1, '3'): CARGO})
    c.run_cycle(provider, {'g': [run(1)]})
    (report,) = repo.ci_reports()['reports']
    assert report['skipped_steps'] == [{'step': 'rust', 'reason': 'log_too_large'}]
    assert report['summary']['total'] == 3


def test_a_provider_failure_is_retried_later_and_a_rate_limit_ends_the_cycle():
    class Boom(Provider):
        def pipeline(self, repo, number):
            raise type('E', (Exception,), {'reason': 'rate_limited'})()
    c, repo = collector()
    assert c.run_cycle(Boom(), {'g': [run(1)]}) == 0
    assert repo.ci_log_analysis(URL, 7, 1) is None
    assert c.run_cycle(Provider({(1, '2'): CARGO}), {'g': [run(1)]}) == 0     # backing off for a few minutes
    c._monotonic = lambda: 10 ** 9
    assert c.run_cycle(Provider({(1, '2'): CARGO}), {'g': [run(1)]}) == 1


def test_an_uploaded_junit_report_always_wins_whichever_arrives_first():
    base = {'repository': 'org/app', 'provider_repo_id': 7, 'provider_url': URL, 'run_number': 1, 'branch': 'main', 'revision': 'abc'}
    junit_first = ControlPlaneRepo()
    junit_first.ingest_ci_report({**base, 'xml': JUNIT})
    c, _ = collector(junit_first)
    c.run_cycle(Provider({(1, '2'): CARGO}), {'g': [run(1)]})
    assert [r.get('source') for r in junit_first.ci_reports()['reports']] == [None]
    assert junit_first.ci_reports()['reports'][0]['summary']['total'] == 2

    logs_first = ControlPlaneRepo()
    c, _ = collector(logs_first)
    c.run_cycle(Provider({(1, '2'): CARGO}), {'g': [run(1)]})
    assert logs_first.ci_reports()['reports'][0]['source'] == 'log_parse'
    logs_first.ingest_ci_report({**base, 'xml': JUNIT})
    shown = logs_first.ci_reports()['reports']
    assert [r.get('source') for r in shown] == [None] and shown[0]['attempt'] == 1


def test_run_filter_compact_view_and_restore_keep_parsed_reports():
    c, repo = collector()
    c.run_cycle(Provider({(1, '2'): CARGO, (2, '2'): CARGO}), {'g': [run(1), run(2)]})
    only = repo.ci_reports(run={'provider_url': URL, 'provider_repo_id': '7', 'repository': 'org/app', 'run_number': 2})
    assert [r['run_number'] for r in only['reports']] == [2] and only['reports'][0]['tests']
    compact = repo.ci_reports(compact=True)['reports']
    assert all('tests' not in r and r['source'] == 'log_parse' and r['runners'] == ['cargo'] for r in compact)
    restored = ControlPlaneRepo()
    restored.import_state(repo.export_state())
    assert len(restored.ci_reports()['reports']) == 2


def test_parsed_reports_do_not_use_up_the_uploaded_report_budget():
    from herdr_engineering.control_plane.ci_reports import REPORT_LIMIT
    c, repo = collector(runs_per_cycle=100)
    base = {'repository': 'org/app', 'provider_repo_id': 7, 'provider_url': URL, 'branch': 'main', 'revision': 'abc'}
    for n in range(1, 4):
        repo.ingest_ci_report({**base, 'run_number': 1000 + n, 'xml': JUNIT})
    logs = {(n, '2'): CARGO for n in range(1, 100)}
    c.run_cycle(Provider(logs), {'g': [run(n, 600) for n in range(1, 100)]})
    assert sum(1 for r in repo.ci_reports()['reports'] if r.get('source') != 'log_parse') == 3
    assert REPORT_LIMIT >= 3


def test_monitor_analyses_logs_after_each_poll_when_wired(monkeypatch):
    repo = ControlPlaneRepo()
    provider = Provider({(1, '2'): CARGO})
    monitor = CIMonitor(provider_factory=lambda: provider, on_log_report=repo.ingest_ci_log_report,
                        log_known=repo.ci_log_analysis, log_tests={'runs_per_cycle': 3})
    monitor._view['pipelines'] = {'g': [run(1)]}
    monitor._analyse_logs()
    assert repo.ci_reports()['reports'][0]['summary']['total'] == 3
    assert CIMonitor(provider_factory=lambda: provider)._log_collector is None


def test_slice_returns_the_lines_around_a_test_and_refuses_a_wrong_range():
    log = '\n'.join(f'line {i}' for i in range(1, 30)) + '\n'
    log = log.replace('line 10', 'test_alpha FAILED').replace('line 11', 'AssertionError password=hunter2')
    provider = Provider({(1, '2'): log})
    monitor = CIMonitor(provider_factory=lambda: provider)
    ok = monitor.log_slice('7', 1, '2', 10, 11, context=3, expect='test_alpha')
    assert ok['slice']['first'] == 9 and ok['slice']['last'] == 14 and ok['slice']['lines'][1] == 'test_alpha FAILED'
    assert 'hunter2' not in json.dumps(ok)
    moved = monitor.log_slice('7', 1, '2', 20, 21, expect='test_alpha')
    assert moved['slice'] is None and 'no longer matches' in moved['reason']
    short = monitor.log_slice('7', 1, '2', 100, 101)
    assert short['slice'] is None and 'shorter' in short['reason']
    for bad in (('7', 1, 'name', 1, 2), ('7', 1, '2', 0, 2), ('7', 1, '2', 5, 2)):
        with pytest.raises(ValueError):
            monitor.log_slice(*bad)


def test_slice_says_so_when_the_log_was_cut():
    provider = Provider({(1, '2'): 'y\n' * 3000})
    monitor = CIMonitor(provider_factory=lambda: provider)
    monitor._log_collector = type('C', (), {'max_log_bytes': 1024 * 1024, 'enabled': False})()
    provider.logs[(1, '2')] = 'y\n' * (1024 * 1024)
    cut = monitor.log_slice('7', 1, '2', 900000, 900001)
    assert cut['slice'] is None and 'too large' in cut['reason'] and cut['truncated'] is True
    assert monitor.log_slice('7', 1, '2', 5, 6)['truncated'] is True and monitor.log_slice('7', 1, '2', 5, 6)['slice']['lines']


def test_only_the_newest_runs_keep_their_test_index_in_memory_but_every_run_keeps_its_counts():
    from herdr_engineering.control_plane.ci_reports import LOG_INDEX_RUNS
    c, repo = collector(runs_per_cycle=100)
    total = LOG_INDEX_RUNS + 5
    c.run_cycle(Provider({(n, '2'): CARGO for n in range(1, total + 1)}), {'g': [run(n, 1000 - n) for n in range(1, total + 1)]})
    rows = repo.ci_reports()['reports']
    assert len(rows) == total
    assert sum(1 for r in rows if not r.get('index_dropped')) == LOG_INDEX_RUNS
    old = [r for r in rows if r.get('index_dropped')]
    assert len(old) == 5
    assert all(r['summary']['total'] == 3 and r['index_complete'] is False for r in old)
    assert all([t['name'] for t in r['tests']] == ['b'] for r in old)               # the failing test is still named
    assert repo.ci_log_analysis(URL, 7, 1)['outcome'] == 'counted'          # still known, so never read again


# --- review follow-ups: what is persisted, what is kept, what is retried ----------------------------------------------------
def big_cargo(count):
    body = ''.join(f'test suite::case_{n} ... ok\n' for n in range(count))
    return f'{body}test result: ok. {count} passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.1s\n'


class Sink:
    def __init__(self):
        self.events = []

    def record_required(self, event):
        self.events.append(json.loads(json.dumps(event)))

    def record(self, event):
        self.record_required(event)


def test_only_a_compact_record_is_persisted_while_the_full_index_stays_readable():
    sink = Sink()
    repo = ControlPlaneRepo(sink=sink)
    c, _ = collector(repo)
    cargo = big_cargo(2400).replace('case_5 ... ok', 'case_5 ... FAILED').replace('; 0 failed', '; 1 failed').replace('2400 passed', '2399 passed')
    c.run_cycle(Provider({(1, '2'): cargo}), {'g': [run(1)]})
    assert len(repo.ci_reports()['reports'][0]['tests']) == 2400                      # the explorer still has every test
    persisted = [e for e in sink.events if e['entity_type'] == 'ci_logtests']
    assert len(persisted) == 1 and len(json.dumps(persisted[0]).encode()) < 20_000
    stored = persisted[0]['payload']
    assert stored['summary']['total'] == 2400 and stored['index_dropped'] is True
    assert [t['name'] for t in stored['tests']] == ['suite::case_5']
    # after a restart only the compact record exists: counts survive, the list says it is gone, and the run is not re-read
    restored = ControlPlaneRepo()
    restored.import_state({'events': {f"ci_logtests|{persisted[0]['entity_id']}": persisted}})
    (report,) = restored.ci_reports()['reports']
    assert report['summary']['total'] == 2400 and report['index_dropped'] is True and report['index_complete'] is False
    again, _ = collector(restored)
    provider = Provider({(1, '2'): cargo})
    assert again.run_cycle(provider, {'g': [run(1)]}) == 0 and provider.calls == []


def test_the_index_is_kept_for_the_newest_run_of_every_branch_before_the_busiest_branch_gets_more():
    from herdr_engineering.control_plane.ci_reports import LOG_INDEX_RUNS
    c, repo = collector(runs_per_cycle=100)
    rows = [{**run(n, 10_000 - n), 'branch': 'busy'} for n in range(1, LOG_INDEX_RUNS + 20)]
    rows.append({**run(900, 20_000), 'branch': 'quiet'})                         # by far the oldest run
    c.run_cycle(Provider({(r['run_number'], '2'): CARGO for r in rows}), {'g': rows})
    by_run = {r['run_number']: r for r in repo.ci_reports()['reports']}
    assert by_run[900]['tests'] and 'index_dropped' not in by_run[900]
    assert sum(1 for r in by_run.values() if not r.get('index_dropped')) == LOG_INDEX_RUNS


def test_runs_without_tests_never_evict_runs_with_counts(monkeypatch):
    from herdr_engineering.control_plane import ci_reports
    monkeypatch.setattr(ci_reports, 'LOG_REPORT_LIMIT', 3)
    c, repo = collector(runs_per_cycle=100)
    counted = [run(n, 5_000 - n) for n in range(1, 4)]
    empty = [run(n, 100 - n) for n in range(10, 30)]                                # newer, but nothing in them
    logs = {(r['run_number'], '2'): CARGO for r in counted}
    c.run_cycle(Provider(logs), {'g': counted + empty})
    assert sorted(r['run_number'] for r in repo.ci_reports()['reports']) == [1, 2, 3]
    assert repo.ci_log_analysis(URL, 7, 29)['outcome'] == 'no_tests'


def test_eviction_follows_when_the_run_finished_not_when_it_was_read(monkeypatch):
    from herdr_engineering.control_plane import ci_reports
    monkeypatch.setattr(ci_reports, 'LOG_REPORT_LIMIT', 2)
    c, repo = collector()
    for number, age in ((1, 100), (2, 10)):                                         # run 2 finished later but is read second
        c.run_cycle(Provider({(number, '2'): CARGO}), {'g': [run(number, age)]})
    c.run_cycle(Provider({(3, '2'): CARGO}), {'g': [run(3, 5000)]})                 # an old backlog run arrives last
    assert sorted(r['run_number'] for r in repo.ci_reports()['reports']) == [1, 2]


def test_a_run_with_a_junit_report_is_remembered_across_a_restart():
    sink = Sink()
    repo = ControlPlaneRepo(sink=sink)
    repo.ingest_ci_report({'repository': 'org/app', 'provider_repo_id': 7, 'provider_url': URL, 'run_number': 1,
                           'branch': 'main', 'revision': 'abc', 'xml': JUNIT})
    c, _ = collector(repo)
    c.run_cycle(Provider({(1, '2'): CARGO}), {'g': [run(1)]})
    marker = [e for e in sink.events if e['entity_type'] == 'ci_logtests']
    assert len(marker) == 1 and marker[0]['payload']['outcome'] == 'no_tests'
    assert repo.ci_log_analysis(URL, 7, 1)['outcome'] == 'no_tests'
    assert [r.get('source') for r in repo.ci_reports()['reports']] == [None]


def test_one_unreadable_step_does_not_hide_the_others_and_is_named():
    class Flaky(Provider):
        def step_log_text(self, repo, number, step, *, max_chars):
            if step == '3':
                raise OSError('boom')
            return super().step_log_text(repo, number, step, max_chars=max_chars)
    c, repo = collector()
    assert c.run_cycle(Flaky({(1, '2'): CARGO}), {'g': [run(1)]}) == 1
    (report,) = repo.ci_reports()['reports']
    assert report['summary']['total'] == 3
    assert report['skipped_steps'] == [{'step': 'lint', 'reason': 'read_failed'}]
    assert repo.ci_log_analysis(URL, 7, 1)['provisional'] is True                  # tried again later


def test_a_run_that_keeps_failing_is_given_up_on_and_the_bookkeeping_is_pruned():
    class Broken(Provider):
        def pipeline(self, repo, number):
            self.calls.append(('pipeline', repo, number))
            raise OSError('down')
    c, repo = collector()
    clock = [0.0]
    c._monotonic = lambda: clock[0]
    provider = Broken()
    for _ in range(12):
        c.run_cycle(provider, {'g': [run(1)]})
        clock[0] += 3600
    assert len([call for call in provider.calls if call[0] == 'pipeline']) == 5     # MAX_ATTEMPTS
    assert c._cooldown == {}                                                        # expired entries are dropped
    c.run_cycle(provider, {'g': []})
    assert c._attempts == {}


def test_stopping_the_monitor_ends_a_run_between_steps_without_counting_it():
    import threading
    c, repo = collector()
    stop = threading.Event()

    class Stopper(Provider):
        def step_log_text(self, repo, number, step, *, max_chars):
            stop.set()
            return super().step_log_text(repo, number, step, max_chars=max_chars)
    provider = Stopper({(1, '2'): CARGO})
    assert c.run_cycle(provider, {'g': [run(1)]}, stop) == 0
    assert len([call for call in provider.calls if call[0] == 'log']) == 1
    assert repo.ci_log_analysis(URL, 7, 1) is None and c._attempts[(URL, '7', 1)] == 0


def test_reading_logs_runs_off_the_poll_thread():
    import threading
    started, release = threading.Event(), threading.Event()

    class Slow(Provider):
        def repos(self, **kw):
            return []

        def step_log_text(self, repo, number, step, *, max_chars):
            started.set()
            release.wait(10)
            return super().step_log_text(repo, number, step, max_chars=max_chars)
    repo = ControlPlaneRepo()
    provider = Slow({(1, '2'): CARGO})
    monitor = CIMonitor(provider_factory=lambda: provider, on_log_report=repo.ingest_ci_log_report,
                        log_known=repo.ci_log_analysis, log_tests={'runs_per_cycle': 3})
    monitor._view['pipelines'] = {'g': [run(1)]}
    try:
        monitor._log_thread = threading.Thread(target=monitor._log_loop, daemon=True)
        monitor._log_thread.start()
        monitor._log_wake.set()
        assert started.wait(10)                               # the reader is busy inside a log...
        assert monitor._refresh_lock.acquire(blocking=False)   # ...and the poll's lock is free
        monitor._refresh_lock.release()
        assert monitor.snapshot(start=False) is not None
        release.set()
        deadline = time.monotonic() + 10
        while repo.ci_log_analysis(URL, 7, 1) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert repo.ci_log_analysis(URL, 7, 1)['outcome'] == 'counted'
    finally:
        release.set()
        monitor.close()


def test_slice_masks_a_private_key_whose_begin_line_is_outside_the_excerpt():
    key = ['-----BEGIN RSA PRIVATE KEY-----', 'MIIEowIBAAKCAQEAsecretbodyline1', 'secretbodyline2', '-----END RSA PRIVATE KEY-----']
    log = '\n'.join(['intro', 'test_alpha FAILED', *key, 'after']) + '\n'
    monitor = CIMonitor(provider_factory=lambda: Provider({(1, '2'): log}))
    out = monitor.log_slice('7', 1, '2', 4, 4, context=3, expect='secretbodyline')       # the excerpt starts mid-key
    lines = out['slice']['lines']
    assert not any('secretbody' in text or 'MIIEow' in text for text in lines)
    assert lines.count('[REDACTED]') >= 2 and lines[-1] == 'after'


def test_slice_reads_a_log_once_for_repeated_clicks_and_bounds_concurrent_reads():
    provider = Provider({(1, '2'): 'a\nb\nc\nd\n'})
    monitor = CIMonitor(provider_factory=lambda: provider)
    for _ in range(5):
        monitor.log_slice('7', 1, '2', 2, 2, context=1)
    assert len([call for call in provider.calls if call[0] == 'log']) == 1
    monitor._slice_cache.clear()
    from herdr_engineering.control_plane import ci_monitor
    for _ in range(ci_monitor.SLICE_READS):
        monitor._slice_reads.acquire()
    monitor._slice_reads.acquire = lambda timeout=None: False                          # nothing frees up in time
    assert 'try again' in monitor.log_slice('7', 1, '2', 2, 2)['error']

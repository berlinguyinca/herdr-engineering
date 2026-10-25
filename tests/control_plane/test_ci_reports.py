import pytest

from herdr_engineering.control_plane.ci_reports import parse_junit
from herdr_engineering.control_plane.repo import ControlPlaneRepo

XML = '''<testsuites><testsuite name="unit"><testcase classname="math" name="ok" time="0.1"/><testcase classname="math" name="bad" time="0.2"><failure message="token=hidden">assert false</failure></testcase><testcase name="error"><error>oops</error></testcase><testcase name="skip"><skipped/></testcase></testsuite></testsuites>'''


def report(**extra):
    return dict(repository='org/app', provider_repo_id=17, run_number=1,
                branch='main', revision='abc', xml=XML, **extra)


def test_junit_counts_actual_cases_and_redacts_before_storage():
    parsed = parse_junit(XML)
    assert parsed['summary'] == dict(total=4, passed=1, failed=1, errored=1, skipped=1, duration_ms=300)
    assert parsed['tests'][1]['message'] == '[REDACTED]'
    assert parsed['tests'][1]['suite'] == 'unit'
    assert parse_junit('<testsuite tests="99"/>')['summary']['total'] == 0


@pytest.mark.parametrize('xml', ['<!DOCTYPE x [<!ENTITY a "x">]><testsuite/>', '<testsuite>', '<other/>', '<testsuite><testcase time="NaN"/></testsuite>', 'x' * 1048577])
def test_junit_rejects_unsafe_malformed_or_unbounded_reports(xml):
    with pytest.raises(ValueError):
        parse_junit(xml)


def test_reports_are_idempotent_preserve_retries_and_survive_restore():
    repo = ControlPlaneRepo()
    first = repo.ingest_ci_report(report())
    assert repo.ingest_ci_report(report())['report_id'] == first['report_id']
    retry = report()
    retry['xml'] = '<testsuite><testcase name="ok"/></testsuite>'
    assert repo.ingest_ci_report(retry)['attempt'] == 2
    restored = ControlPlaneRepo()
    restored.import_state(repo.export_state())
    assert len(restored.ci_reports()['reports']) == 2
    assert 'hidden' not in str(restored.export_state())


def test_pipeline_observations_persist_outside_current_window_without_duplicates():
    repo = ControlPlaneRepo()
    row = dict(repository='org/app', provider_repo_id=17, run_number=1, state='running')
    repo.observe_ci_pipeline(row)
    repo.observe_ci_pipeline(row)
    assert len(repo.stream('ci_observation', '17:1')) == 1
    repo.observe_ci_pipeline({**row, 'state': 'failure'})
    repo.observe_ci_pipeline({**row, 'run_number': 2, 'state': 'success'})
    restored = ControlPlaneRepo()
    restored.import_state(repo.export_state())
    assert {r['run_number'] for r in restored.ci_reports()['pipelines']} == {1, 2}
    assert restored.ci_reports()['pipelines'][0]['state'] == 'success'


def test_durable_event_replay_decodes_database_json_payloads(tmp_path):
    import json

    from herdr_engineering.control_plane.bridge import load_into

    class EventJournal:
        """Actual on-disk event journal implementing the source/sink protocol."""
        def record_required(self, event):
            with (tmp_path / 'events.jsonl').open('a') as stream:
                # asyncpg's default JSON codec returns JSON strings.
                stream.write(json.dumps({**event, 'payload': json.dumps(event['payload'])}) + '\n')

        record = record_required

        def events(self):
            return [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]

        def missions(self):
            return []

        sessions = services = hosts = missions

    journal = EventJournal()
    repo = ControlPlaneRepo(sink=journal)
    uploaded = repo.ingest_ci_report(report())
    repo.observe_ci_pipeline(dict(repository='org/app', provider_repo_id=17, run_number=1, state='failure'))
    restored = ControlPlaneRepo(sink=journal)
    load_into(restored, journal)
    view = restored.ci_reports()
    assert view['reports'][0]['summary']['total'] == 4
    assert view['pipelines'][0]['state'] == 'failure'
    assert restored.ingest_ci_report(report())['report_id'] == uploaded['report_id']
    assert len(journal.events()) == 2


def test_retention_limits_projection_and_restore():
    from herdr_engineering.control_plane.ci_reports import REPORT_LIMIT
    repo = ControlPlaneRepo(now=iter(range(100000)).__next__)
    for number in range(1, REPORT_LIMIT + 3):
        row = report()
        row['run_number'] = number
        repo.ingest_ci_report(row)
    assert len(repo.ci_reports()['reports']) == REPORT_LIMIT
    assert repo.ci_reports()['reports'][0]['run_number'] == REPORT_LIMIT + 2
    restored = ControlPlaneRepo()
    restored.import_state(repo.export_state())
    assert len(restored.ci_reports()['reports']) == REPORT_LIMIT


def test_returned_report_cannot_mutate_retained_evidence():
    repo = ControlPlaneRepo()
    row = repo.ingest_ci_report(report())
    row['tests'][0]['status'] = 'failed'
    view = repo.ci_reports()
    assert view['reports'][0]['tests'][0]['status'] == 'passed'
    view['reports'][0]['summary']['total'] = 999
    assert repo.ci_reports()['reports'][0]['summary']['total'] == 4


@pytest.mark.parametrize('field,value', [('run_number', True), ('run_number', -1), ('repository', ''), ('provider_repo_id', []), ('reported_at', '2026-10-22')])
def test_report_identity_rejects_invalid_metadata(field, value):
    row = report()
    row[field] = value
    with pytest.raises(ValueError):
        ControlPlaneRepo().ingest_ci_report(row)


def test_equal_timestamps_preserve_arrival_order_for_latest_history():
    repo = ControlPlaneRepo(now=lambda: 100)
    for number in (1, 2):
        row = report()
        row['run_number'] = number
        repo.ingest_ci_report(row)
    assert [row['run_number'] for row in repo.ci_reports()['reports']] == [2, 1]


def test_nested_suite_identity_survives_sibling_reordering():
    def xml(parents):
        return '<testsuites>' + ''.join(
            f'<testsuite name="{parent}"><testsuite name="shared"><testcase classname="Checks" name="works"/></testsuite></testsuite>'
            for parent in parents) + '</testsuites>'
    first = parse_junit(xml(['linux', 'windows']))['tests']
    second = parse_junit(xml(['windows', 'linux']))['tests']
    assert first[0]['suite'] == 'linux / shared'
    assert first[0]['id'] == second[1]['id']
    assert first[1]['id'] == second[0]['id']
    assert first[0]['id'] != first[1]['id']


def test_actual_durable_sink_preserves_passed_summary_through_sql_serialization(tmp_path):
    import asyncio
    import sqlite3
    from datetime import datetime

    from herdr_engineering.control_plane.bridge import load_into
    from herdr_engineering.control_plane.sink import DurableEventSink

    class SQLiteEventDatabase:
        """Real SQL adapter for the EventStore's PostgreSQL-compatible INSERT."""
        def __init__(self):
            self.connection = sqlite3.connect(tmp_path / 'events.sqlite')
            self.connection.row_factory = sqlite3.Row
            self.connection.execute('CREATE TABLE fabric_events (event_id TEXT PRIMARY KEY, sequence INTEGER, event_type TEXT, entity_type TEXT, entity_id TEXT, mission_id TEXT, session_id TEXT, payload TEXT, source_timestamp TEXT, ingest_timestamp TEXT, schema_version INTEGER)')

        async def fetchrow(self, sql, *args):
            parameters = {str(index): value.isoformat() if isinstance(value, datetime) else value
                          for index, value in enumerate(args, 1)}
            row = self.connection.execute(sql, parameters).fetchone()
            self.connection.commit()
            return dict(row) if row else None

        def events(self):
            return [dict(row) for row in self.connection.execute('SELECT * FROM fabric_events')]

        def missions(self):
            return []

        sessions = services = hosts = missions

    database = SQLiteEventDatabase()
    sink = DurableEventSink(conn=database, run=asyncio.run)
    repo = ControlPlaneRepo(sink=sink)
    repo.ingest_ci_report(report())
    restored = ControlPlaneRepo(sink=sink)
    load_into(restored, database)
    summary = restored.ci_reports()['reports'][0]['summary']
    assert summary['passed'] == 1
    assert summary['total'] == 4
    assert 'hidden' not in str(database.events())
    database.connection.close()


def test_report_without_provider_id_reconciles_only_when_exact_observation_arrives():
    row = report()
    row.pop('provider_repo_id')
    repo = ControlPlaneRepo()
    uploaded = repo.ingest_ci_report(row)
    first = repo.ci_reports()['reports'][0]
    assert first['provider_repo_id'] is None
    assert first['identity_status'] == 'unmatched'
    repo.observe_ci_pipeline(dict(repository='org/app', provider_repo_id=17, run_number=1,
                                  branch='main', revision='abc', state='success'))
    bound = repo.ci_reports()['reports'][0]
    assert bound['provider_repo_id'] == 17
    assert bound['uploaded_provider_repo_id'] is None
    assert bound['identity_status'] == 'matched_observation'
    assert bound['report_id'] == uploaded['report_id']
    assert repo.ingest_ci_report(row)['provider_repo_id'] is None
    assert len(repo.ci_reports()['reports']) == 1


def test_name_only_report_stays_unbound_for_ambiguous_providers():
    row = report()
    row.pop('provider_repo_id')
    repo = ControlPlaneRepo()
    repo.ingest_ci_report(row)
    for provider_id in (17, 18):
        repo.observe_ci_pipeline(dict(repository='org/app', provider_repo_id=provider_id,
                                      run_number=1, branch='main', revision='abc', state='success'))
    result = repo.ci_reports()['reports'][0]
    assert result['provider_repo_id'] is None
    assert result['identity_status'] == 'ambiguous'


@pytest.mark.parametrize('field,value', [('repository', 'other/app'), ('run_number', 2), ('branch', 'feature'), ('revision', 'abcd'), ('revision', ''), ('branch', None)])
def test_name_only_report_requires_all_identity_fields_to_match(field, value):
    row = report()
    row.pop('provider_repo_id')
    repo = ControlPlaneRepo()
    repo.ingest_ci_report(row)
    pipeline = dict(repository='org/app', provider_repo_id=17, run_number=1,
                    branch='main', revision='abc', state='success')
    pipeline[field] = value
    repo.observe_ci_pipeline(pipeline)
    assert repo.ci_reports()['reports'][0]['provider_repo_id'] is None


def test_report_explicit_provider_identity_is_preserved():
    repo = ControlPlaneRepo()
    repo.ingest_ci_report(report())
    repo.observe_ci_pipeline(dict(repository='org/app', provider_repo_id=18, run_number=1,
                                  branch='main', revision='abc', state='success'))
    result = repo.ci_reports()['reports'][0]
    assert result['provider_repo_id'] == 17
    assert result['uploaded_provider_repo_id'] == 17
    assert result['identity_status'] == 'provided'


@pytest.fixture
def disk_event_journal(tmp_path):
    import json

    class DiskEventJournal:
        def record_required(self, event):
            with (tmp_path / 'journal.jsonl').open('a') as stream:
                stream.write(json.dumps(event) + '\n')

        record = record_required

        def events(self):
            return [json.loads(line) for line in (tmp_path / 'journal.jsonl').read_text().splitlines()]

        def missions(self):
            return []

        sessions = services = hosts = missions

    return DiskEventJournal()


def test_evicted_run_and_report_reappear_with_durable_monotonic_sequence(disk_event_journal):
    from herdr_engineering.control_plane.bridge import load_into
    from herdr_engineering.control_plane.ci_reports import PIPELINE_LIMIT, REPORT_LIMIT

    repo = ControlPlaneRepo(sink=disk_event_journal, now=iter(range(100000)).__next__)
    pipeline = dict(repository='org/app', provider_repo_id=17, run_number=1, state='running')
    repo.observe_ci_pipeline(pipeline)
    repo.observe_ci_pipeline({**pipeline, 'state': 'failure'})
    uploaded = repo.ingest_ci_report(report())
    for number in range(2, PIPELINE_LIMIT + 2):
        repo.observe_ci_pipeline({**pipeline, 'run_number': number})
        if number <= REPORT_LIMIT + 1:
            body = report()
            body['run_number'] = number
            repo.ingest_ci_report(body)
    assert not any(row['run_number'] == 1 for row in repo.ci_reports()['pipelines'])
    repo.observe_ci_pipeline({**pipeline, 'state': 'success'})
    repo.ingest_ci_report(report())
    restored = ControlPlaneRepo(sink=disk_event_journal)
    load_into(restored, disk_event_journal)
    assert restored.ci_reports()['pipelines'][0]['state'] == 'success'
    assert restored.ci_reports()['pipelines'][0]['run_number'] == 1
    assert restored.ci_reports()['reports'][0]['report_id'] == uploaded['report_id']
    sequences = [event['sequence'] for event in disk_event_journal.events()]
    assert len(sequences) == len(set(sequences))
    before = max(sequences)
    restored.observe_ci_pipeline({**pipeline, 'state': 'error'})
    assert disk_event_journal.events()[-1]['sequence'] > before


def test_failed_observation_persistence_retries_after_journal_recovers(disk_event_journal, tmp_path):
    repo = ControlPlaneRepo(sink=disk_event_journal)
    pipeline = dict(repository='org/app', provider_repo_id=17, run_number=1, state='success')
    (tmp_path / 'journal.jsonl').mkdir()
    with pytest.raises(IsADirectoryError):
        repo.observe_ci_pipeline(pipeline)
    assert repo.ci_reports()['pipelines'] == []
    (tmp_path / 'journal.jsonl').rmdir()
    repo.observe_ci_pipeline(pipeline)
    assert len(disk_event_journal.events()) == 1
    assert repo.ci_reports()['pipelines'][0]['state'] == 'success'


def test_long_and_redaction_colliding_case_names_keep_stable_distinct_ids():
    from xml.sax.saxutils import escape
    for names in [('x' * 500 + 'A', 'x' * 500 + 'B'), ('token=one', 'token=two')]:
        def xml(values):
            return '<testsuite name="unit">' + ''.join(
                f'<testcase name="{escape(name)}"/>' for name in values) + '</testsuite>'
        first = parse_junit(xml(names))['tests']
        second = parse_junit(xml(reversed(names)))['tests']
        assert first[0]['id'] == second[1]['id']
        assert first[1]['id'] == second[0]['id']
        assert first[0]['id'] != first[1]['id']
        assert len(first[0]['name']) <= 500
        if names[0].startswith('token='):
            assert first[0]['name'] == '[REDACTED]'


def test_long_and_redaction_colliding_parent_suites_keep_stable_ids():
    from xml.sax.saxutils import escape
    for names in [('x' * 500 + 'A', 'x' * 500 + 'B'), ('token=one', 'token=two')]:
        def xml(values):
            return '<testsuites>' + ''.join(
                f'<testsuite name="{escape(name)}"><testcase name="same"/></testsuite>' for name in values) + '</testsuites>'
        first = parse_junit(xml(names))['tests']
        second = parse_junit(xml(reversed(names)))['tests']
        assert first[0]['id'] == second[1]['id']
        assert first[1]['id'] == second[0]['id']
        assert first[0]['id'] != first[1]['id']


def test_monitor_reports_history_failure_and_recovers_against_real_provider_server(disk_event_journal, tmp_path):
    import json
    import ssl
    import subprocess
    import threading
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from herdr_engineering.ci import WoodpeckerProvider
    from herdr_engineering.control_plane.ci_monitor import CIMonitor

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith('/api/user/repos'):
                body = [{'id': 17, 'full_name': 'org/app'}]
            elif self.path.startswith('/api/repos/17/pipelines'):
                body = [{'id': 1, 'number': 1, 'status': 'success', 'branch': 'main', 'commit': 'abc'}]
            else:
                body = []
            value = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(value)))
            self.end_headers()
            self.wfile.write(value)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    certificate, private_key = tmp_path / 'server.crt', tmp_path / 'server.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(private_key), '-out', str(certificate), '-days', '1',
                    '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'],
                   check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, private_key)
    httpd.socket = tls.wrap_socket(httpd.socket, server_side=True)
    client = urllib.request.build_opener(urllib.request.HTTPSHandler(
        context=ssl.create_default_context(cafile=str(certificate))))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    repo = ControlPlaneRepo(sink=disk_event_journal)
    provider = WoodpeckerProvider(base_url=f'https://127.0.0.1:{httpd.server_port}', token='local-test-credential', client=client)
    monitor = CIMonitor(provider_factory=lambda: provider, on_observation=repo.observe_ci_pipeline)
    try:
        (tmp_path / 'journal.jsonl').mkdir()
        monitor.refresh()
        snapshot = monitor.snapshot(start=False)
        assert snapshot['status'] == 'partial'
        assert 'persisted' in snapshot['repo_errors']['_history']
        assert repo.ci_reports()['pipelines'] == []
        (tmp_path / 'journal.jsonl').rmdir()
        monitor.refresh()
        assert monitor.snapshot(start=False)['status'] == 'connected'
        assert repo.ci_reports()['pipelines'][0]['state'] == 'success'
        assert len(disk_event_journal.events()) == 1
    finally:
        monitor.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('initial_provider,last_provider', [(None, 17), (17, None)])
def test_retry_ordinals_follow_unique_observed_identity_across_optional_provider_changes(initial_provider, last_provider):
    repo = ControlPlaneRepo()
    repo.observe_ci_pipeline(dict(repository='org/app', provider_repo_id=17, run_number=1,
                                  branch='main', revision='abc', state='success'))
    for index, provider in enumerate((initial_provider, initial_provider, last_provider), 1):
        row = report()
        row['provider_repo_id'] = provider
        row['xml'] = f'<testsuite><testcase name="same" time="{index}">' + (
            f'<failure>failure {index}</failure>' if index < 3 else '') + '</testcase></testsuite>'
        uploaded = repo.ingest_ci_report(row)
        assert uploaded['attempt'] == index
        assert uploaded['provider_repo_id'] == provider
    view = repo.ci_reports()['reports']
    assert [row['attempt'] for row in view] == [3, 2, 1]
    assert {row['provider_repo_id'] for row in view} == {17}
    assert view[0]['summary']['passed'] == 1


def test_late_pipeline_binding_reconciles_retry_ordinals_without_rewriting_uploads():
    repo = ControlPlaneRepo()
    for index, provider in enumerate((None, None, 17), 1):
        row = report()
        row['provider_repo_id'] = provider
        row['xml'] = f'<testsuite><testcase name="same" time="{index}"/></testsuite>'
        repo.ingest_ci_report(row)
    assert [row['attempt'] for row in repo.ci_reports()['reports']] == [1, 2, 1]
    repo.observe_ci_pipeline(dict(repository='org/app', provider_repo_id=17, run_number=1,
                                  branch='main', revision='abc', state='success'))
    view = repo.ci_reports()['reports']
    assert [row['attempt'] for row in view] == [3, 2, 1]
    assert [row['uploaded_attempt'] for row in view] == [1, 2, 1]
    restored = ControlPlaneRepo()
    restored.import_state(repo.export_state())
    assert [row['attempt'] for row in restored.ci_reports()['reports']] == [3, 2, 1]


HISTORY_STAMP = '2026-10-25T12:00:00+00:00'


def history_repo(**kwargs):
    from datetime import datetime
    return ControlPlaneRepo(now=lambda: datetime.fromisoformat(HISTORY_STAMP).timestamp(), **kwargs)


def history_report(*, failed=False, **values):
    return {**report(), 'provider_url': 'https://ci.example', 'reported_at': HISTORY_STAMP,
            'xml': '<testsuite><testcase classname="Checks" name="works" time="2">' +
                   ('<failure message="token=synthetic-private">stack</failure>' if failed else '') +
                   '</testcase></testsuite>', **values}


def test_history_preserves_retries_and_api_shape_through_journal_replay(disk_event_journal):
    from herdr_engineering.control_plane.bridge import load_into
    repo = history_repo(sink=disk_event_journal)
    failed = repo.ingest_ci_report(history_report(failed=True))
    repo.ingest_ci_report(history_report())
    repo.ingest_ci_report(history_report(failed=True))
    before = repo.ci_reports()
    history = repo.ci_test_history(repository='org/app', branch='main')
    assert history['tests'][0]['classification'] == 'proven_flaky'
    assert history['tests'][0]['failed'] == history['tests'][0]['passed'] == 1
    assert history['scope']['persistence'] == 'durable_sink'
    assert history['scope']['memory_only'] is False
    assert repo.ci_reports() == before
    restored = history_repo(sink=disk_event_journal)
    load_into(restored, disk_event_journal)
    assert restored.ci_test_history(repository='org/app', branch='main') == history
    assert restored.ingest_ci_report(history_report(failed=True))['report_id'] == failed['report_id']
    assert len(disk_event_journal.events()) == 2
    assert 'synthetic-private' not in str(disk_event_journal.events())
    backup = history_repo(sink=disk_event_journal)
    backup.import_state(restored.export_state())
    assert backup.ci_test_history(repository='org/app', branch='main') == history


def test_history_legacy_provider_binding_requires_one_exact_full_identity():
    repo = history_repo()
    body = history_report(provider_url=None, provider_repo_id=None)
    repo.ingest_ci_report(body)
    assert repo.ci_test_history()['tests'] == []
    observed = {key: value for key, value in history_report().items() if key != 'xml'}
    repo.observe_ci_pipeline({**observed, 'state': 'success'})
    assert len(repo.ci_test_history()['tests']) == 1
    repo.observe_ci_pipeline({**observed, 'provider_url': 'https://other.example', 'state': 'failure'})
    assert len(repo.ci_reports()['pipelines']) == 2
    view = repo.ci_test_history()
    assert view['tests'] == []
    assert view['scope']['unjoined_report_count'] == 1
    assert view['scope']['not_reported_run_count'] == 2


def test_explicit_provider_urls_distinguish_reports_and_attempts():
    repo = history_repo()
    first = repo.ingest_ci_report(history_report(failed=True))
    second = repo.ingest_ci_report(history_report(failed=True, provider_url='https://other.example'))
    assert first['report_id'] != second['report_id']
    assert first['attempt'] == second['attempt'] == 1
    assert {r['attempt'] for r in repo.ci_reports()['reports']} == {1}
    rows = repo.ci_test_history()['tests']
    assert len(rows) == 2
    assert {row['provider_url'] for row in rows} == {'https://ci.example', 'https://other.example'}


@pytest.mark.parametrize('url', ['https://user:synthetic-private@ci.example',
                                 'https://ci.example?access_token=synthetic-private',
                                 'file:///tmp/report', 'https://ci.example:invalid'])
def test_provider_url_rejects_unsafe_identity_before_journaling(url, disk_event_journal):
    repo = history_repo(sink=disk_event_journal)
    with pytest.raises(ValueError, match='provider_url'):
        repo.ingest_ci_report(history_report(provider_url=url))
    assert repo.ci_reports()['reports'] == []


def test_junit_file_identity_and_evidence_are_bounded_and_redacted():
    xml = '<testsuite><testcase name="same" file="a.py"><failure message="' + 'm' * 2500 + '">' + 'x' * 4500 + '</failure></testcase><testcase name="same" file="b.py"/></testsuite>'
    first = parse_junit(xml)['tests']
    reordered = parse_junit('<testsuite><testcase name="same" file="b.py"/><testcase name="same" file="a.py"/></testsuite>')['tests']
    assert first[0]['file'] == 'a.py'
    assert first[0]['id'] == reordered[1]['id']
    assert first[1]['id'] == reordered[0]['id']
    assert len(first[0]['message']) == 2000 and len(first[0]['trace']) == 4000


def test_explicit_provider_url_constrains_optional_repository_id_matching():
    repo = history_repo()
    repo.ingest_ci_report(history_report(provider_repo_id=None))
    repo.observe_ci_pipeline(dict(repository='org/app', provider_url='https://other.example',
        provider_repo_id=18, run_number=1, branch='main', revision='abc', state='success'))
    assert repo.ci_reports()['reports'][0]['identity_status'] == 'unmatched'
    assert repo.ci_test_history()['tests'] == []


def test_secret_provider_id_is_redacted_before_durable_report_storage(disk_event_journal):
    repo = history_repo(sink=disk_event_journal)
    repo.ingest_ci_report(history_report(provider_repo_id='token=synthetic-private'))
    assert 'synthetic-private' not in str(disk_event_journal.events())
    assert repo.ci_test_history()['tests'] == []


def test_unjoined_report_count_obeys_repository_branch_and_time_window():
    repo = history_repo()
    for number, values in enumerate(({}, {'branch': 'topic'}, {'repository': 'other/app'},
                                    {'reported_at': '2026-09-01T00:00:00Z'}), 1):
        repo.ingest_ci_report(history_report(provider_url=None, run_number=number, **values))
    view = repo.ci_test_history(repository='org/app', branch='main', days=7)
    assert view['scope']['unjoined_report_count'] == 1


def test_provider_collisions_survive_disk_replay_without_losing_runs(disk_event_journal):
    from herdr_engineering.control_plane.bridge import load_into
    repo = history_repo(sink=disk_event_journal)
    for url in ('https://ci.example', 'https://other.example'):
        body = history_report(provider_url=url)
        repo.ingest_ci_report(body)
        repo.observe_ci_pipeline({key: value for key, value in body.items() if key != 'xml'})
    restored = history_repo(sink=disk_event_journal)
    load_into(restored, disk_event_journal)
    assert len(restored.ci_reports()['pipelines']) == 2
    assert len(restored.ci_test_history()['tests']) == 2
    assert restored.ci_test_history() == repo.ci_test_history()


def duplicate_label_xml(reverse=False):
    cases = ['<testcase classname="Checks" name="same"/>',
             '<testcase classname="Checks" name="same"><failure>bad</failure></testcase>']
    return '<testsuite name="unit">' + ''.join(reversed(cases) if reverse else cases) + '</testsuite>'


def test_duplicate_labels_preserve_ambiguity_through_durable_history(disk_event_journal):
    from herdr_engineering.control_plane.bridge import load_into
    repo = history_repo(sink=disk_event_journal)
    for reverse in (False, True):
        receipt = repo.ingest_ci_report(history_report(xml=duplicate_label_xml(reverse)))
        assert all(case.get('identity_ambiguous') is True for case in receipt['tests'])
    restored = history_repo(sink=disk_event_journal)
    load_into(restored, disk_event_journal)
    rows = restored.ci_test_history()['tests']
    assert len(rows) == 2
    assert {row['classification'] for row in rows} == {'inconsistent_outcomes'}
    assert all(row['identity_ambiguous'] and not row['same_revision_retries'] for row in rows)
    assert all(row['passed'] == row['failed'] == 1 for row in rows)


def test_observation_key_upgrade_replaces_legacy_run_and_survives_replay(disk_event_journal):
    from herdr_engineering.control_plane.bridge import load_into
    legacy = history_repo(sink=disk_event_journal)
    row = {key: value for key, value in history_report().items() if key != 'xml'}
    legacy.record_event('ci_observation', '17:1', 'CIPipelineObserved',
                        {**row, 'state': 'running'}, require_durable=True)
    upgraded = history_repo(sink=disk_event_journal)
    upgraded.import_state(legacy.export_state())
    upgraded.observe_ci_pipeline({**row, 'state': 'success'})
    upgraded.observe_ci_pipeline({**row, 'state': 'success'})
    assert upgraded.ci_reports()['pipelines'] == [{**row, 'state': 'success'}]
    assert upgraded.ci_reports()['scope']['pipeline_count'] == 1
    upgraded.observe_ci_pipeline({**row, 'provider_url': 'https://other.example', 'state': 'failure'})
    restored = history_repo(sink=disk_event_journal)
    load_into(restored, disk_event_journal)
    rows = restored.ci_reports()['pipelines']
    assert {(item['provider_url'], item['state']) for item in rows} == {
        ('https://ci.example', 'success'), ('https://other.example', 'failure')}
    assert len(rows) == 2
    assert len(disk_event_journal.events()) == 3


@pytest.mark.parametrize('kind', ['ci_report', 'ci_observation'])
def test_source_eviction_remains_visible_after_projection_and_restore(kind, monkeypatch, disk_event_journal):
    from herdr_engineering.control_plane import ci_reports
    from herdr_engineering.control_plane.bridge import load_into
    monkeypatch.setattr(ci_reports, 'REPORT_LIMIT' if kind == 'ci_report' else 'PIPELINE_LIMIT', 1)
    repo = history_repo(sink=disk_event_journal)
    for number in (1, 2):
        body = history_report(run_number=number)
        if kind == 'ci_report':
            repo.ingest_ci_report(body)
        else:
            repo.observe_ci_pipeline({key: value for key, value in body.items() if key != 'xml'})
    backup = history_repo()
    backup.import_state(repo.export_state())
    restored = history_repo(sink=disk_event_journal)
    load_into(restored, disk_event_journal)
    for instance in (repo, backup, restored):
        assert instance.ci_reports()['scope']['truncated'] is True
        scope = instance.ci_test_history()['scope']
        assert scope['source_truncated'] is True
        assert scope['projection_truncated'] is False
        assert scope['truncated'] is True and scope['partial'] is True


@pytest.mark.parametrize('changes', [
    {'provider_url': 'https://other.example'},
    {'provider_url': None},
    {'repository': 'other/app'},
    {'provider_repo_id': 18},
])
def test_observation_upgrade_does_not_merge_unproven_legacy_identity(changes):
    repo = history_repo()
    row = {key: value for key, value in history_report().items() if key != 'xml'}
    repo.record_event('ci_observation', '17:1', 'CIPipelineObserved',
                      {**row, **changes, 'state': 'running'})
    repo.observe_ci_pipeline({**row, 'state': 'success'})
    rows = repo.ci_reports()['pipelines']
    assert len(rows) == 2
    assert {item['state'] for item in rows} == {'running', 'success'}
    assert repo.ci_reports()['scope']['truncated'] is False


def test_observation_upgrade_does_not_discard_legacy_run_if_persistence_fails(disk_event_journal, tmp_path):
    repo = history_repo(sink=disk_event_journal)
    row = {key: value for key, value in history_report().items() if key != 'xml'}
    repo.record_event('ci_observation', '17:1', 'CIPipelineObserved', {**row, 'state': 'running'})
    journal = tmp_path / 'journal.jsonl'
    journal.rename(tmp_path / 'journal.saved')
    journal.mkdir()
    with pytest.raises(IsADirectoryError):
        repo.observe_ci_pipeline({**row, 'state': 'success'})
    assert repo.ci_reports()['pipelines'] == [{**row, 'state': 'running'}]
    journal.rmdir()
    (tmp_path / 'journal.saved').rename(journal)
    repo.observe_ci_pipeline({**row, 'state': 'success'})
    assert repo.ci_reports()['pipelines'] == [{**row, 'state': 'success'}]
    assert repo.ci_reports()['scope']['truncated'] is False


def test_byte_eviction_is_source_truncation_even_below_the_report_count_limit(monkeypatch):
    from herdr_engineering.control_plane import ci_reports
    repo = history_repo()
    repo.ingest_ci_report(history_report())
    # A byte cap smaller than one existing event must evict that report even
    # though the repository is far below the count cap.
    monkeypatch.setattr(ci_reports, 'HISTORY_BYTES', 1)
    repo.ingest_ci_report(history_report(run_number=2))
    assert repo.ci_reports()['reports'] == []
    scope = repo.ci_test_history()['scope']
    assert scope['truncated'] is True and scope['source_truncated'] is True
    assert scope['projection_truncated'] is False

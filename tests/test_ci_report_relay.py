import json
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from herdr_engineering.ci_report_relay import Outcomes, Relay, read_source, validate_bundle
from herdr_engineering.control_plane.ci_monitor import (
    REMOVED_RUN_ERRORS,
    RESOURCE_REMOVED_ERROR,
    RUN_REMOVED_ERROR,
)

REVISION = 'a' * 40
NAME = f'42-{REVISION}-hive-slurm.json'


def bundle(**overrides):
    return dict(schema_version=1, repository='InferWeave/inferweave', provider_repo_id=116,
                run_number=42, branch='main', revision=REVISION, suite='hive-slurm',
                runner_exit_code=1, reported_at='2026-10-22T20:00:00Z',
                xml='<testsuite><testcase name="real-case"><failure>failed assertion</failure></testcase></testsuite>',
                **overrides)


@contextmanager
def control_plane(*, revision=REVISION, branch='main', acknowledge=True, pipeline=None, pipeline_status=200):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == '/api/v1/ci/pipeline?repository=116&number=42'
            if pipeline == 'drop':
                self.close_connection = True
                return
            if pipeline is not None:
                self.respond(pipeline, pipeline_status)
                return
            value = dict(available=True, pipeline=dict(number=42, commit=revision, branch=branch))
            self.respond(value)

        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(value)
            if not acknowledge:
                self.respond({'error': 'unavailable'}, 503)
                return
            self.respond({**{key: value[key] for key in ('repository', 'provider_repo_id', 'run_number', 'branch', 'revision')},
                          'report_id': 'received', 'summary': {'total': 1}})

        def respond(self, value, status=200):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_bundle_allowlist_and_original_junit_validation():
    raw = json.dumps(bundle()).encode()
    assert validate_bundle(NAME, raw)['xml'] == bundle()['xml']
    for name, body in [('../' + NAME, raw), (NAME, b'{}'), (NAME, raw + b'x'),
                       (NAME, json.dumps({**bundle(), 'repository': 'other/repo'}).encode()),
                       (NAME, json.dumps({**bundle(), 'provider_repo_id': 117}).encode()),
                       (NAME, json.dumps({**bundle(), 'xml': '<!DOCTYPE x><testsuite/>'}).encode())]:
        with pytest.raises(ValueError):
            validate_bundle(name, body)


def test_reader_is_bounded_and_refuses_symlinks(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / NAME).write_text(json.dumps(bundle()))
    outside = tmp_path / 'outside'
    outside.write_text('secret outside')
    (source / f'43-{REVISION}-hive-slurm.json').symlink_to(outside)
    rows = read_source(str(source), {}, batch_size=1)
    assert len(rows) == 1
    assert rows[0]['name'] == NAME
    assert json.loads(rows[0]['content'])['revision'] == REVISION
    assert read_source(str(source), {NAME: {key: rows[0][key] for key in ('size', 'mtime_ns')}}, batch_size=1) == []


def test_acknowledged_upload_checkpoints_and_restarts_idempotently(tmp_path):
    with control_plane() as (url, received):
        relay = Relay(url, tmp_path / 'private-state', allow_open=True)
        assert relay.consume(NAME, json.dumps(bundle()).encode()) is True
        assert relay.consume(NAME, json.dumps(bundle()).encode()) is False
        restarted = Relay(url, tmp_path / 'private-state', allow_open=True)
        assert restarted.consume(NAME, json.dumps(bundle()).encode()) is False
        assert len(received) == 1
        assert received[0]['provider_repo_id'] == 116
        assert set(received[0]) == {'repository', 'provider_repo_id', 'run_number', 'branch', 'revision', 'reported_at', 'xml'}
        assert (tmp_path / 'private-state').stat().st_mode & 0o077 == 0


def test_provider_metadata_mismatch_never_uploads_or_checkpoints(tmp_path):
    for field in ({'revision': 'b' * 40}, {'branch': 'other'}):
        with control_plane(**field) as (url, received):
            relay = Relay(url, tmp_path / 'state', allow_open=True)
            with pytest.raises(ValueError):
                relay.consume(NAME, json.dumps(bundle()).encode())
            assert received == []
            assert relay.checkpoints == {}


def test_failed_upload_remains_retryable_after_http_recovery(tmp_path):
    raw = json.dumps(bundle()).encode()
    with control_plane(acknowledge=False) as (url, received):
        relay = Relay(url, tmp_path / 'state', allow_open=True)
        with pytest.raises(ValueError):
            relay.consume(NAME, raw)
        assert relay.checkpoints == {}
    with control_plane() as (url, received):
        relay = Relay(url, tmp_path / 'state', allow_open=True)
        assert relay.consume(NAME, raw) is True
        assert len(received) == 1


@pytest.mark.parametrize('field,value', [
    ('schema_version', True), ('run_number', True), ('run_number', 43),
    ('revision', 'b' * 40), ('branch', ''), ('reported_at', '2026-10-22'),
    ('runner_exit_code', '1'), ('suite', 'other'), ('xml', 'x' * (1024 * 1024 + 1)),
])
def test_invalid_metadata_and_xml_are_rejected(field, value):
    with pytest.raises(ValueError):
        validate_bundle(NAME, json.dumps({**bundle(), field: value}).encode())


def test_reader_batches_recent_files_and_ignores_oversized_or_external_files(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    for number in range(1, 13):
        (source / f'{number}-{REVISION}-hive-slurm.json').write_bytes(b'{}')
    oversized = source / f'99-{REVISION}-hive-slurm.json'
    with oversized.open('wb') as stream:
        stream.truncate(2 * 1024 * 1024 + 1)
    files = read_source(str(source), {}, batch_size=10)
    assert len(files) == 10
    assert all(row['name'] != oversized.name for row in files)
    with pytest.raises(ValueError):
        read_source(str(source), {}, host='-oProxyCommand=bad')
    with pytest.raises(ValueError):
        read_source(str(source) + '/../outside', {})
    with pytest.raises(ValueError):
        read_source(str(source), {}, batch_size=11)


def test_checkpoint_rejects_symlinks_and_public_files(tmp_path):
    state = tmp_path / 'private-state'
    state.mkdir(mode=0o700)
    checkpoint = state / 'checkpoint.json'
    checkpoint.write_text('{"version":1,"reports":{}}')
    checkpoint.chmod(0o644)
    with pytest.raises(ValueError):
        Relay('http://127.0.0.1:1', state, allow_open=True)
    checkpoint.unlink()
    outside = tmp_path / 'outside'
    outside.write_text('{"version":1,"reports":{}}')
    outside.chmod(0o600)
    checkpoint.symlink_to(outside)
    with pytest.raises(ValueError):
        Relay('http://127.0.0.1:1', state, allow_open=True)


def test_changed_bundle_uploads_and_identical_reread_updates_acknowledged_metadata(tmp_path):
    with control_plane() as (url, received):
        relay = Relay(url, tmp_path / 'state', allow_open=True)
        first = json.dumps(bundle()).encode()
        assert relay.consume(NAME, first, mtime_ns=1) is True
        assert relay.consume(NAME, first, mtime_ns=2) is False
        assert relay.checkpoints[NAME]['mtime_ns'] == 2
        second = json.dumps({**bundle(), 'xml': '<testsuite><testcase name="real-case"/></testsuite>', 'runner_exit_code': 0}).encode()
        assert relay.consume(NAME, second, mtime_ns=3) is True
        assert len(received) == 2
        assert relay.checkpoints[NAME]['mtime_ns'] == 3


def test_missing_key_requires_explicit_open_mode(tmp_path):
    with pytest.raises(ValueError):
        Relay('http://127.0.0.1:1', tmp_path / 'state')
    with pytest.raises(ValueError):
        Relay('http://127.0.0.1:1', tmp_path / 'state', key='bad\nsecret')


def test_native_signal_exit_status_is_an_integer_and_bundle_encoding_is_utf8():
    assert validate_bundle(NAME, json.dumps({**bundle(), 'runner_exit_code': -9}).encode())['runner_exit_code'] == -9
    with pytest.raises(ValueError):
        validate_bundle(NAME, json.dumps(bundle()).encode('utf-16'))


def test_duplicate_metadata_keys_are_rejected():
    raw = json.dumps(bundle()).removesuffix('}') + ', "repository": "InferWeave/inferweave"}'
    with pytest.raises(ValueError):
        validate_bundle(NAME, raw.encode())


def test_real_control_plane_and_woodpecker_detail_contract(tmp_path):
    import ssl
    import subprocess
    import urllib.request

    from herdr_engineering.ci import WoodpeckerProvider
    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.control_plane.ci_monitor import CIMonitor
    from herdr_engineering.control_plane.repo import ControlPlaneRepo

    class ProviderHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/api/repos/116/permissions':
                self.send_response(403)
                self.end_headers()
                return
            assert self.path == '/api/repos/116/pipelines/42'
            raw = json.dumps(dict(id=100, number=42, status='failure', commit=REVISION,
                                  branch='main', workflows=[])).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    certificate, key = tmp_path / 'server.crt', tmp_path / 'server.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(certificate), '-days', '1',
                    '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'],
                   check=True, capture_output=True)
    provider_server = ThreadingHTTPServer(('127.0.0.1', 0), ProviderHandler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)
    provider_server.socket = tls.wrap_socket(provider_server.socket, server_side=True)
    provider_thread = threading.Thread(target=provider_server.serve_forever, daemon=True)
    provider_thread.start()
    client = urllib.request.build_opener(urllib.request.HTTPSHandler(
        context=ssl.create_default_context(cafile=str(certificate))))
    provider = WoodpeckerProvider(base_url=f'https://127.0.0.1:{provider_server.server_port}',
                                 token='local-provider-key', client=client)
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo, api_keys={'operator-key': 'operator'})
    server.ci_monitor = CIMonitor(provider_factory=lambda: provider)
    server_thread = threading.Thread(target=server.serve, daemon=True)
    server_thread.start()
    server.wait_until_ready()
    try:
        relay = Relay(f'http://127.0.0.1:{server.port}', tmp_path / 'state', key='operator-key')
        assert relay.consume(NAME, json.dumps(bundle()).encode()) is True
        assert repo.ci_reports()['reports'][0]['summary']['failed'] == 1
    finally:
        server.shutdown()
        server_thread.join(timeout=2)
        provider_server.shutdown()
        provider_server.server_close()
        provider_thread.join(timeout=2)


def test_failed_newest_batch_does_not_starve_older_native_report_after_restart(tmp_path):
    import os

    source = tmp_path / 'source'
    source.mkdir()
    (source / NAME).write_text(json.dumps(bundle()))
    os.utime(source / NAME, ns=(1, 1))
    for index in range(100, 110):
        path = source / f'{index}-{REVISION}-hive-slurm.json'
        path.write_bytes(b'{}')
        os.utime(path, ns=(index, index))
    with control_plane() as (url, received):
        relay = Relay(url, tmp_path / 'state', allow_open=True)
        assert relay.process_source(str(source)) == Outcomes(0, 10, 0)
        assert relay.checkpoints == {}
        restarted = Relay(url, tmp_path / 'state', allow_open=True)
        published, failed, _ = restarted.process_source(str(source))
        assert published == 1
        assert len(received) == 1
        assert NAME in restarted.checkpoints
        assert failed > 0


def test_reader_reports_unreadable_artifact_as_failure(tmp_path):
    import os

    source = tmp_path / 'source'
    source.mkdir()
    artifact = source / NAME
    artifact.write_text(json.dumps(bundle()))
    artifact.chmod(0)
    try:
        if os.getuid() == 0:
            pytest.skip('File permission checks require a non-root test user')
        result = read_source(str(source), {}, with_status=True)
        assert result['files'] == []
        assert result['read_failures'] == 1
    finally:
        artifact.chmod(0o600)


def _source_with_one_bundle(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / NAME).write_text(json.dumps(bundle()))
    return source


def test_each_failed_item_is_logged_with_its_reason_and_never_the_key(tmp_path, capsys):
    source = _source_with_one_bundle(tmp_path)
    with control_plane(revision='b' * 40) as (url, received):
        relay = Relay(url, tmp_path / 'state', key='super-secret-key')
        assert relay.process_source(str(source)) == Outcomes(0, 1, 0)
    err = capsys.readouterr().err
    assert NAME in err
    assert 'does not match the observed provider run identity' in err
    assert 'super-secret-key' not in err
    assert received == []


def test_control_plane_refusal_logs_http_status(tmp_path, capsys):
    source = _source_with_one_bundle(tmp_path)
    with control_plane(acknowledge=False) as (url, received):
        relay = Relay(url, tmp_path / 'state', allow_open=True)
        assert relay.process_source(str(source)) == Outcomes(0, 1, 0)
    assert 'HTTP 503' in capsys.readouterr().err


def test_unreadable_source_file_is_logged_by_name_and_errno(tmp_path, capsys):
    import os

    if os.getuid() == 0:
        pytest.skip('File permission checks require a non-root test user')
    source = _source_with_one_bundle(tmp_path)
    (source / NAME).chmod(0)
    try:
        with control_plane() as (url, received):
            relay = Relay(url, tmp_path / 'state', allow_open=True)
            assert relay.process_source(str(source)) == Outcomes(0, 1, 0)
    finally:
        (source / NAME).chmod(0o600)
    err = capsys.readouterr().err
    assert NAME in err
    assert 'EACCES' in err


def test_main_logs_the_reason_when_the_whole_run_fails(tmp_path, capsys):
    from herdr_engineering.ci_report_relay import main

    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real)
    code = main(['--source-host', 'localhost', '--source-dir', str(tmp_path), '--control-plane',
                 'http://127.0.0.1:1', '--state-dir', str(link), '--allow-open', '--once'])
    captured = capsys.readouterr()
    assert code == 1
    assert 'CI report relay: 0 published, 1 failed, 0 skipped' in captured.out
    assert 'State directory must be a private owned directory' in captured.err


# Provider run removed/pruned: a permanent skip, distinct from mismatch and transient errors.
REMOVED = {'available': False, 'status': 'unavailable', 'error': RUN_REMOVED_ERROR}


def test_removed_run_is_skipped_checkpointed_and_not_retried(tmp_path, capsys):
    source = _source_with_one_bundle(tmp_path)
    with control_plane(pipeline=REMOVED) as (url, received):
        relay = Relay(url, tmp_path / 'state', allow_open=True)
        assert relay.process_source(str(source)) == Outcomes(0, 0, 1)
        assert received == []
        row = relay.checkpoints[NAME]
        assert row['state'] == 'skipped' and row['reason'] == 'provider_run_removed'
        assert row['skipped_at'].endswith('+00:00')
        captured = capsys.readouterr()
        assert f'CI report relay: skipped {NAME}: provider run removed' in captured.err
        assert 'failed' not in captured.err
        # A fresh process reloads the checkpoint and does not even re-read the bundle.
        again = Relay(url, tmp_path / 'state', allow_open=True)
        assert NAME in again.checkpoints
        assert again.process_source(str(source)) == Outcomes(0, 0, 0)


def test_touched_skipped_bundle_stays_skipped_and_is_never_marked_published(tmp_path):
    source = _source_with_one_bundle(tmp_path)
    with control_plane(pipeline=REMOVED) as (url, received):
        relay = Relay(url, tmp_path / 'state', allow_open=True)
        assert relay.process_source(str(source)) == Outcomes(0, 0, 1)
        before = relay.checkpoints[NAME]['mtime_ns']
        # Same content, new mtime (touch, rsync without -t, re-copy) must not rewrite the row as delivered.
        os.utime(source / NAME, ns=(before + 10**9, before + 10**9))
        again = Relay(url, tmp_path / 'state', allow_open=True)
        assert again.process_source(str(source)) == Outcomes(0, 0, 0)
        assert received == []
        row = again.checkpoints[NAME]
        assert row['mtime_ns'] == before + 10**9
        assert row['state'] == 'skipped' and row['reason'] == 'provider_run_removed'
        assert 'acknowledged_at' not in row
        reloaded = Relay(url, tmp_path / 'state', allow_open=True).checkpoints[NAME]
        assert reloaded['state'] == 'skipped' and 'acknowledged_at' not in reloaded


class _WoodpeckerStandIn:
    """Provider transport that answers by path with a real HTTP status, as urllib raises it."""

    def __init__(self, statuses):
        self.statuses = statuses

    def urlopen(self, req, timeout=10):
        import io
        import urllib.error
        import urllib.parse

        path = urllib.parse.urlparse(req.full_url).path
        status = self.statuses.get(path, 404)
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, 'x', {}, io.BytesIO(b''))
        if path.endswith('/42'):
            body = {'number': 42, 'commit': REVISION, 'branch': 'main'}
        elif path == '/api/user/repos':
            # A well-formed repository list that does not hold the requested repository.
            body = [{'id': 7, 'full_name': 'org/other', 'owner': 'org', 'name': 'other'}]
        else:
            body = {'id': 116}
        return io.BytesIO(json.dumps(body).encode())


def _detail_as_the_control_plane_answers(statuses):
    from herdr_engineering.ci import WoodpeckerProvider
    from herdr_engineering.control_plane.ci_monitor import CIMonitor

    provider = WoodpeckerProvider(base_url='https://ci.example', token='tok', client=_WoodpeckerStandIn(statuses))
    return CIMonitor(provider_factory=lambda: provider).detail('116', 42)


def test_run_level_404_with_a_reachable_repository_is_the_removed_run_answer():
    answer = _detail_as_the_control_plane_answers({'/api/repos/116': 200})
    assert answer['available'] is False and answer['error'] in REMOVED_RUN_ERRORS


@pytest.mark.parametrize('statuses', [
    {},                              # repository 404 too: deleted, re-created, wrong base URL or proxy path
    {'/api/repos/116': 401},         # the token is rejected
    {'/api/repos/116': 403},         # the token lost access
    {'/api/repos/116': 500},         # the repository cannot be confirmed
])
def test_repo_level_auth_or_endpoint_404_is_not_the_removed_run_answer(statuses):
    answer = _detail_as_the_control_plane_answers(statuses)
    assert answer['available'] is False and answer['error'] not in REMOVED_RUN_ERRORS
    if not statuses:
        # The repository 404 keeps the generic not_found answer, not a run-removed one.
        assert answer['error'] == RESOURCE_REMOVED_ERROR


def test_repository_lookup_miss_is_not_the_removed_run_answer():
    from herdr_engineering.ci import WoodpeckerProvider
    from herdr_engineering.control_plane.ci_monitor import CIMonitor

    provider = WoodpeckerProvider(base_url='https://ci.example', token='tok',
                                  client=_WoodpeckerStandIn({'/api/user/repos': 200}))
    answer = CIMonitor(provider_factory=lambda: provider).detail('org/missing', 42)
    # A real lookup miss raises not_found (generic answer), not invalid_response.
    assert answer['available'] is False and answer['error'] == RESOURCE_REMOVED_ERROR
    assert answer['error'] not in REMOVED_RUN_ERRORS


@pytest.mark.parametrize('answer', [
    {'available': False, 'status': 'unavailable', 'error': 'This Woodpecker resource is unavailable or has been removed.'},
    {'available': False, 'error': 'Pipeline was not found.'},
])
def test_generic_not_found_texts_fail_and_are_not_skipped(tmp_path, answer):
    source = _source_with_one_bundle(tmp_path)
    with control_plane(pipeline=answer) as (url, received):
        relay = Relay(url, tmp_path / 'state', allow_open=True)
        assert relay.process_source(str(source)) == Outcomes(0, 1, 0)
    assert relay.checkpoints == {} and received == []


def test_existing_run_with_identity_mismatch_still_fails_closed(tmp_path, capsys):
    source = _source_with_one_bundle(tmp_path)
    with control_plane(revision='b' * 40) as (url, received):
        relay = Relay(url, tmp_path / 'state', allow_open=True)
        assert relay.process_source(str(source)) == Outcomes(0, 1, 0)
    assert relay.checkpoints == {} and received == []
    assert 'does not match the observed provider run identity' in capsys.readouterr().err


@pytest.mark.parametrize('kwargs', [
    dict(pipeline={'error': 'boom'}, pipeline_status=500),
    dict(pipeline=REMOVED, pipeline_status=502),
    dict(pipeline={'available': False, 'status': 'auth_error', 'error': 'Woodpecker rejected the credential (HTTP 401).'}),
    dict(pipeline={'available': False, 'status': 'unavailable', 'error': 'Woodpecker did not respond before the read timeout. Retrying automatically.'}),
    dict(pipeline={'available': False, 'error': 'Woodpecker is not configured.'}),
    dict(pipeline={'unexpected': True}),
    dict(pipeline='drop'),
])
def test_transient_or_unrecognised_provider_answers_fail_and_are_not_skipped(tmp_path, kwargs):
    source = _source_with_one_bundle(tmp_path)
    with control_plane(**kwargs) as (url, received):
        relay = Relay(url, tmp_path / 'state', allow_open=True)
        assert relay.process_source(str(source)) == Outcomes(0, 1, 0)
    assert relay.checkpoints == {} and received == []


def test_mixed_batch_counts_published_failed_and_skipped(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    names = {n: f'{n}-{REVISION}-hive-slurm.json' for n in (42, 43, 44)}
    for number, name in names.items():
        (source / name).write_text(json.dumps(bundle() | {'run_number': number}))
    answers = {42: None, 43: REMOVED, 44: {'error': 'boom'}}
    statuses = {44: 500}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            number = int(self.path.rsplit('=', 1)[1])
            answer = answers[number] or dict(available=True, pipeline=dict(
                number=number, commit=REVISION, branch='main'))
            self.reply(answer, statuses.get(number, 200))

        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            self.reply({**{k: value[k] for k in ('repository', 'provider_repo_id', 'run_number', 'branch', 'revision')},
                        'report_id': 'ok', 'summary': {'total': 1}}, 200)

        def reply(self, value, status):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        relay = Relay(f'http://127.0.0.1:{server.server_port}', tmp_path / 'state', allow_open=True)
        assert relay.process_source(str(source)) == Outcomes(1, 1, 1)
    finally:
        server.shutdown()
        server.server_close()
    assert {n: r.get('state', 'published') for n, r in relay.checkpoints.items()} == {
        names[42]: 'published', names[43]: 'skipped'}


def test_summary_line_counts_skips_and_only_failures_fail_the_run():
    assert Outcomes(0, 0, 4).summary_line() == 'CI report relay: 0 published, 0 failed, 4 skipped'
    assert Outcomes(0, 0, 4).exit_status() == 0
    assert Outcomes(3, 0, 1).exit_status() == 0
    assert Outcomes(0, 1, 4).exit_status() == 1


GOOD_NAME = '3062-' + 'a' * 40 + '-hive-slurm.json'


@pytest.mark.parametrize('bad', [None, 'oops', 7, {'a': 'b'}, [None], [1], ['xy'], [['only-one']], [[GOOD_NAME, 5]],
                                 [[5, 'EIO']], [['../etc/passwd', 'EIO']], [[GOOD_NAME, 'bad code!']],
                                 [[GOOD_NAME, 'EIO', 'extra']]])
def test_malformed_read_errors_are_dropped_not_raised(bad):
    from herdr_engineering.ci_report_relay import _read_errors
    assert _read_errors({'read_errors': bad}) == []


def test_well_formed_read_errors_survive_next_to_malformed_rows():
    from herdr_engineering.ci_report_relay import _read_errors
    rows = [None, [GOOD_NAME, 'EACCES'], ['junk'], (GOOD_NAME, 'EIO')]
    assert _read_errors({'read_errors': rows}) == [(GOOD_NAME, 'EACCES'), (GOOD_NAME, 'EIO')]

"""Native collector tests use actual same-user Unix sockets and HTTP ingestion."""
import contextlib
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from herdr_engineering import mission_activity_feed as feed

STAMP = '2026-10-20T12:00:00Z'
INSTANCE = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


@contextlib.contextmanager
def native_session(tmp_path):
    # AF_UNIX paths have a small OS limit, independent of pytest test names.
    with tempfile.TemporaryDirectory(prefix='pi-feed-', dir='/tmp') as directory:
        root = Path(directory)
        root.chmod(0o700)
        project = tmp_path / 'project'
        project.mkdir()
        (project / '.pi-eng').mkdir(mode=0o700)
        descriptor = dict(version=1, instanceId=INSTANCE, sessionId='session-1',
                          cwd=str(project), pid=os.getpid(), startedAt=STAMP,
                          heartbeatAt=STAMP, socketPath=str(root / f'{INSTANCE}.sock'))
        path = root / f'{INSTANCE}.json'
        path.write_text(json.dumps(descriptor))
        path.chmod(0o600)
        status = dict(descriptor, missionSource='runtime', missionIds=['MSN-1'],
                      missions=[dict(id='MSN-1', status='RUNNING', revision=7)])
        snapshot = dict(contractVersion=3, generatedAt=STAMP, missions=[
            dict(id='MSN-1', title='Repair tests', status='RUNNING', revision=7,
                 observability=dict(currentObjective='Repair tests', health='active',
                                    workerDetails=[dict(workerId='W-1', state='running',
                                        lastOutputAt=STAMP, lastOutputPreview='token=private-value',
                                        reasoning='hidden', args={'password': 'private'})])),
            dict(id='MSN-other', revision=7, observability={})])
        (project / '.pi-eng/orchestration-snapshot.json').write_text(json.dumps(snapshot))
        (project / '.pi-eng/orchestration-snapshot.json').chmod(0o600)
        server = socket.socket(socket.AF_UNIX)
        server.bind(descriptor['socketPath'])
        os.chmod(descriptor['socketPath'], 0o600)
        server.listen()
        server.settimeout(0.1)
        stop = threading.Event()
        operations = []

        def serve():
            while not stop.is_set():
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                with conn:
                    data = b''
                    while b'\n' not in data:
                        chunk = conn.recv(8192)
                        if not chunk:
                            break
                        data += chunk
                    request = json.loads(data)
                    operations.append(request['op'])
                    reply = dict(status)
                    if request['op'] == 'ping':
                        reply['nonce'] = status.get('_nonce', request['nonce'])
                    conn.sendall(json.dumps(dict(version=1, ok=True, data=reply)).encode() + b'\n')
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield root, project, status, snapshot, operations
        finally:
            stop.set()
            thread.join(1)
            server.close()


@contextlib.contextmanager
def api(capable=True, acknowledgement=None):
    posts = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            body = json.dumps({'capabilities': ['mission_activity_v1'] if capable else []}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            posts.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.end_headers()
            accepted = ({'ok': True, 'entities': [{'accepted': True} for _ in posts[-1]['events']]}
                        if 'events' in posts[-1] else {'ok': True, 'entity': {'accepted': True}})
            self.wfile.write(json.dumps(acknowledgement if acknowledgement is not None else
                                       accepted).encode())
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', posts
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_multiple_owned_missions_use_one_acknowledged_batch(tmp_path):
    with native_session(tmp_path) as (root, _, status, _, _), api() as (url, posts):
        status['missionIds'].append('MSN-other')
        status['missions'].append(dict(id='MSN-other', status='RUNNING', revision=7))
        collector = feed.MissionActivityFeed(url, host_id='host', control_dir=root)
        result = collector.run_once()
        assert result['posted'] == 2
        assert result['errors'] == 0
        assert len(posts) == 1
        assert {event['entity_id'] for event in posts[0]['events']} == {'pi:MSN-1', 'pi:MSN-other'}
        assert collector.run_once()['posted'] == 0
        assert len(posts) == 1


def test_mission_batches_respect_ingest_envelope_limit(tmp_path):
    with api() as (url, posts):
        collector = feed.MissionActivityFeed(url, host_id='host', control_dir=tmp_path)
        payloads = [{'mission': {'id': f'MSN-{i}'}} for i in range(65)]
        assert collector._publish_many(payloads) == 65
        assert [len(post['events']) for post in posts] == [64, 1]
        assert len(collector.sent) == 65
        assert collector._publish_many(payloads) == 0
        assert len(posts) == 2


def test_batch_failure_keeps_only_previously_acknowledged_missions(tmp_path, monkeypatch):
    collector = feed.MissionActivityFeed('http://127.0.0.1', host_id='host', control_dir=tmp_path)
    batches = []

    def publish(path, body):
        batches.append(body['events'])
        return {'ok': True, 'entities': [{'accepted': len(batches) == 1}
                                         for _ in body['events']]}

    monkeypatch.setattr(collector, '_http_json', publish)
    payloads = [{'mission': {'id': f'MSN-{i}'}} for i in range(65)]
    with pytest.raises(feed.PublicationRejected):
        collector._publish_many(payloads)
    assert len(collector.sent) == len(collector.known) == 64
    assert 'MSN-64' not in collector.sent
    batches.clear()
    assert collector._publish_many(payloads) == 1
    assert len(batches[0]) == 1
    assert batches[0][0]['entity_id'] == 'pi:MSN-64'


@pytest.mark.parametrize('acknowledgement', [
    {'ok': False, 'entities': [{'accepted': True}, {'accepted': True}]},
    {'ok': True, 'entities': [{'accepted': True}]},
    {'ok': True, 'entities': [{'accepted': True}, {'accepted': False}]},
    {'ok': True, 'entities': [{'accepted': True}, None]},
    {'ok': True, 'entity': {'accepted': True}},
])
def test_rejected_batch_does_not_mark_missions_as_sent(tmp_path, acknowledgement):
    with api(acknowledgement=acknowledgement) as (url, posts):
        collector = feed.MissionActivityFeed(url, host_id='host', control_dir=tmp_path)
        payloads = [{'mission': {'id': mid}} for mid in ('MSN-1', 'MSN-2')]
        with pytest.raises(feed.PublicationRejected):
            collector._publish_many(payloads)
        assert len(posts) == 1
        assert collector.sent == {}
        assert collector.known == {}


def test_collects_only_proven_owned_revision_and_redacts_before_transport(tmp_path):
    with native_session(tmp_path) as (root, _, _, _, operations), api() as (url, posts):
        collector = feed.MissionActivityFeed(url, host_id='host', control_dir=root)
        result = collector.run_once()
        assert result['posted'] == 1
        assert result['snapshot_reads'] == 1
        assert result['snapshot_cache_hits'] == 0
        assert operations == ['ping', 'status']
        assert len(posts) == 1
        body = posts[0]
        assert body['entity_type'] == 'mission_activity'
        assert body['entity_id'] == 'pi:MSN-1'
        assert body['payload']['snapshot_at'] == STAMP
        assert body['payload']['mission_revision'] == 7
        worker = body['payload']['activity']['workerDetails'][0]
        assert worker['lastOutputAt'] == STAMP
        assert 'private-value' not in json.dumps(body)
        assert 'reasoning' not in worker
        assert 'args' not in worker


def test_old_server_receives_no_posts(tmp_path):
    with native_session(tmp_path) as (root, _, _, _, operations), api(False) as (url, posts):
        result = feed.MissionActivityFeed(url, host_id='host', control_dir=root).run_once()
        assert result['capable'] is False
        assert posts == []
        assert operations == []


def test_revision_mismatch_is_updating_without_unproven_activity(tmp_path):
    with native_session(tmp_path) as (root, _, status, _, _), api() as (url, posts):
        status['missions'][0]['revision'] = 8
        feed.MissionActivityFeed(url, host_id='host', control_dir=root).run_once()
        payload = posts[0]['payload']
        assert payload['connection'] == 'updating'
        assert payload['activity'] == {}
        assert payload['snapshot_at'] is None


def test_repo_snapshot_fallback_is_not_runtime_ownership(tmp_path):
    with native_session(tmp_path) as (root, _, status, _, _), api() as (url, posts):
        status['missionSource'] = 'repo_snapshot'
        feed.MissionActivityFeed(url, host_id='host', control_dir=root).run_once()
        assert posts == []


def test_descriptor_loss_disconnects_previous_owner_without_crash(tmp_path):
    with native_session(tmp_path) as (root, _, _, _, _), api() as (url, posts):
        collector = feed.MissionActivityFeed(url, host_id='host', control_dir=root)
        collector.run_once()
        (root / f'{INSTANCE}.json').unlink()
        collector.run_once()
        assert posts[-1]['payload']['connection'] == 'disconnected'
        assert posts[-1]['payload']['mission']['status'] == 'RUNNING'
        assert posts[-1]['payload']['snapshot_at'] == STAMP


@pytest.mark.parametrize('unsafe', ['directory', 'descriptor', 'socket', 'pid', 'nonce'])
def test_untrusted_native_identity_is_rejected(tmp_path, unsafe):
    with native_session(tmp_path) as (root, _, status, _, _), api() as (url, posts):
        if unsafe == 'directory':
            root.chmod(0o755)
        elif unsafe == 'descriptor':
            (root / f'{INSTANCE}.json').chmod(0o644)
        elif unsafe == 'socket':
            (root / f'{INSTANCE}.sock').chmod(0o666)
        elif unsafe == 'pid':
            status['pid'] += 1
        elif unsafe == 'nonce':
            status['_nonce'] = 'incorrect-nonce'
        feed.MissionActivityFeed(url, host_id='host', control_dir=root).run_once()
        assert posts == []


def test_unchanged_projection_is_throttled_but_real_changes_publish(tmp_path):
    with native_session(tmp_path) as (root, project, _, snapshot, _), api() as (url, posts):
        collector = feed.MissionActivityFeed(url, host_id='host', control_dir=root)
        collector.run_once()
        collector.run_once()
        assert len(posts) == 1
        snapshot['missions'][0]['observability']['currentObjective'] = 'Run validation'
        (project / '.pi-eng/orchestration-snapshot.json').write_text(json.dumps(snapshot))
        (project / '.pi-eng/orchestration-snapshot.json').chmod(0o600)
        collector.run_once()
        assert len(posts) == 2


def test_snapshot_cache_reuses_unchanged_file_and_invalidates_on_change(tmp_path, monkeypatch):
    with native_session(tmp_path) as (root, project, _, snapshot, _):
        collector = feed.MissionActivityFeed('http://127.0.0.1:1', host_id='host', control_dir=root)
        original = feed._read_json
        reads = 0

        def counted_read(path, limit, private=False):
            nonlocal reads
            reads += 1
            return original(path, limit, private)

        monkeypatch.setattr(feed, '_read_json', counted_read)
        first = collector._load_snapshot(str(project))
        second = collector._load_snapshot(str(project))
        assert first == second
        assert reads == 1

        snapshot['missions'][0]['revision'] = 8
        path = project / '.pi-eng/orchestration-snapshot.json'
        path.write_text(json.dumps(snapshot))
        path.chmod(0o600)
        refreshed = collector._load_snapshot(str(project))
        assert refreshed['missions'][0]['revision'] == 8
        assert reads == 2


def test_normal_group_writable_snapshot_is_display_data_not_control_authority(tmp_path):
    with native_session(tmp_path) as (root, project, _, _, _), api() as (url, posts):
        (project / '.pi-eng').chmod(0o775)
        (project / '.pi-eng/orchestration-snapshot.json').chmod(0o664)
        feed.MissionActivityFeed(url, host_id='host', control_dir=root).run_once()
        assert posts[0]['payload']['connection'] == 'connected'


def test_cli_has_bounded_once_and_interval_options():
    from herdr_engineering.cli import _build_parser
    args = _build_parser().parse_args(['control-plane', 'mission-feed', '--once', '--interval', '5',
                                      '--control-plane', 'http://127.0.0.1:8080'])
    assert args.once is True
    assert args.interval == 5
    assert callable(args.func)


def test_real_backend_ingestion_never_emits_notifications(tmp_path):
    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.control_plane.repo import ControlPlaneRepo
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo, host='127.0.0.1', port=0)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        with native_session(tmp_path) as (root, _, _, _, _):
            collector = feed.MissionActivityFeed(f'http://127.0.0.1:{server.port}', host_id='host', control_dir=root)
            result = collector.run_once()
            assert result['posted'] == 1
            projection = repo.mission_activity.get('pi:MSN-1')
            assert projection['activity']['currentObjective'] == 'Repair tests'
            assert repo.recent_events() == []
    finally:
        server.shutdown()
        thread.join(1)


@pytest.mark.parametrize('unsafe', ['symlink', 'world_writable'])
def test_untrusted_snapshot_is_never_published_as_connected(tmp_path, unsafe):
    with native_session(tmp_path) as (root, project, _, _, _), api() as (url, posts):
        path = project / '.pi-eng/orchestration-snapshot.json'
        if unsafe == 'world_writable':
            path.chmod(0o666)
        else:
            moved = project / 'outside.json'
            path.rename(moved)
            path.symlink_to(moved)
        feed.MissionActivityFeed(url, host_id='host', control_dir=root).run_once()
        assert posts[0]['payload']['connection'] == 'updating'
        assert posts[0]['payload']['activity'] == {}


def test_transport_failure_disconnects_known_owner_without_changing_mission_health(tmp_path):
    with native_session(tmp_path) as (root, _, status, _, _), api() as (url, posts):
        collector = feed.MissionActivityFeed(url, host_id='host', control_dir=root)
        collector.run_once()
        status['pid'] += 1
        result = collector.run_once()
        assert result['errors'] == 1
        assert posts[-1]['payload']['connection'] == 'disconnected'
        assert posts[-1]['payload']['activity']['health'] == 'active'


def test_backend_takeover_follows_disconnect_and_does_not_regress_time(tmp_path):
    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.control_plane.repo import ControlPlaneRepo
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo, host='127.0.0.1', port=0)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        with native_session(tmp_path) as (root, _, status, _, _):
            collector = feed.MissionActivityFeed(f'http://127.0.0.1:{server.port}', host_id='host', control_dir=root)
            collector.run_once()
            descriptor = root / f'{INSTANCE}.json'
            data = json.loads(descriptor.read_text())
            data['sessionId'] = 'replacement'
            descriptor.write_text(json.dumps(data))
            status['sessionId'] = 'replacement'
            result = collector.run_once()
            assert result['posted'] == 2
            assert result['errors'] == 0
            projection = repo.mission_activity.get('pi:MSN-1')
            assert projection['session_id'] == 'replacement'
            assert projection['connection'] == 'connected'
            assert repo.recent_events() == []
    finally:
        server.shutdown()
        thread.join(1)


def test_once_cli_runner_uses_host_config_key_without_leaking_it(tmp_path, monkeypatch, capsys,
                                                                  fake_host_config):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    fake_host_config({'ingest': {'api_key': 'placeholder-private-api-value'}})
    with native_session(tmp_path) as (root, _, _, _, _), api() as (url, posts):
        assert feed.run_feed(url, host_id='host', control_dir=root, once=True) == 0
        summary = capsys.readouterr().out
        assert json.loads(summary)['posted'] == 1
        assert 'placeholder-private-api-value' not in summary
        assert 'placeholder-private-api-value' not in json.dumps(posts)


def test_collector_heartbeat_preserves_real_worker_timestamps(tmp_path):
    with native_session(tmp_path) as (root, _, _, _, _), api() as (url, posts):
        collector = feed.MissionActivityFeed(url, host_id='host', control_dir=root)
        collector.run_once()
        fingerprint, _ = collector.sent['MSN-1']
        collector.sent['MSN-1'] = (fingerprint, 0)
        collector.run_once()
        assert len(posts) == 2
        assert posts[0]['payload']['observed_at'] != posts[1]['payload']['observed_at']
        assert posts[1]['payload']['snapshot_at'] == STAMP
        assert posts[1]['payload']['activity']['workerDetails'][0]['lastOutputAt'] == STAMP


def test_real_backend_rejected_revision_does_not_advance_collector_state(tmp_path):
    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.control_plane.repo import ControlPlaneRepo
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo, host='127.0.0.1', port=0)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        with native_session(tmp_path) as (root, project, status, snapshot, _):
            collector = feed.MissionActivityFeed(f'http://127.0.0.1:{server.port}', host_id='host', control_dir=root)
            assert collector.run_once()['posted'] == 1
            previous_known = json.loads(json.dumps(collector.known))
            previous_sent = dict(collector.sent)
            status['missions'][0]['revision'] = 6
            snapshot['missions'][0]['revision'] = 6
            (project / '.pi-eng/orchestration-snapshot.json').write_text(json.dumps(snapshot))
            rejected = collector.run_once()
            assert rejected['posted'] == 0
            assert rejected['errors'] == 1
            assert rejected['reasons'] == {'out_of_order': 1}
            assert collector.known == previous_known
            assert collector.sent == previous_sent
            # Rejections remain eligible for the next pass, not throttled as successes.
            assert collector.run_once()['reasons'] == {'out_of_order': 1}
    finally:
        server.shutdown()
        thread.join(1)


@pytest.mark.parametrize('acknowledgement', [
    {}, {'accepted': True}, {'ok': True, 'entity': {'accepted': False, 'reason': 'token=private'}},
    {'ok': True, 'entity': {'accepted': False, 'reason': {'password': 'private'}}},
])
def test_unrecognized_acknowledgements_are_not_success_or_raw_operator_messages(tmp_path, acknowledgement):
    with native_session(tmp_path) as (root, _, _, _, _), api(acknowledgement=acknowledgement) as (url, _):
        collector = feed.MissionActivityFeed(url, host_id='host', control_dir=root)
        result = collector.run_once()
        assert result['posted'] == 0
        assert result['reasons'] == {'invalid_acknowledgement': 1}
        assert collector.sent == {}
        assert collector.known == {}
        assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('private_type', ['thinking', 'REASONING', 'scratchpad', 'chain_of_thought'])
def test_private_activity_records_never_cross_http_transport(tmp_path, private_type):
    with native_session(tmp_path) as (root, project, _, snapshot, _), api() as (url, posts):
        observability = snapshot['missions'][0]['observability']
        observability['activity'] = [
            {'type': private_type, 'summary': 'private model deliberation'},
            {'kind': private_type, 'summary': 'private alternate record'},
            {'type': 'future_unknown_type', 'summary': 'unrecognized contents'},
            {'type': 'running_test', 'summary': 'Testing thinking-related UI', 'at': STAMP},
        ]
        observability['currentActivity'] = {'type': private_type, 'summary': 'private current deliberation'}
        observability['workerDetails'][0]['lastOutputPreview'] = 'Test named thinking passed'
        (project / '.pi-eng/orchestration-snapshot.json').write_text(json.dumps(snapshot))
        feed.MissionActivityFeed(url, host_id='host', control_dir=root).run_once()
        wire = json.dumps(posts)
        assert 'private model deliberation' not in wire
        assert 'private alternate record' not in wire
        assert 'private current deliberation' not in wire
        assert 'unrecognized contents' not in wire
        activity = posts[0]['payload']['activity']
        assert activity['activity'] == [{'type': 'running_test', 'summary': 'Testing thinking-related UI', 'at': STAMP}]
        assert activity.get('currentActivity') is None
        assert activity['workerDetails'][0]['lastOutputPreview'] == 'Test named thinking passed'


@pytest.mark.skipif(sys.platform not in {'linux', 'darwin'}, reason='requires supported Unix peer credentials')
def test_real_socket_peer_credentials_accept_only_actual_uid_and_pid(monkeypatch):
    with contextlib.ExitStack() as stack:
        client, peer = socket.socketpair()
        stack.enter_context(client)
        stack.enter_context(peer)
        feed._verify_peer(client, os.getpid())
        with pytest.raises(ValueError, match='peer identity mismatch'):
            feed._verify_peer(client, os.getpid() + 1)
        actual_uid = os.getuid()
        monkeypatch.setattr(feed.os, 'getuid', lambda: actual_uid + 1)
        with pytest.raises(ValueError, match='peer identity mismatch'):
            feed._verify_peer(client, os.getpid())


@pytest.mark.skipif(sys.platform != 'darwin', reason='requires Darwin libc getpeereid')
def test_darwin_closed_socket_cannot_supply_peer_proof():
    client = socket.socket(socket.AF_UNIX)
    client.close()
    with pytest.raises(OSError, match='credentials unavailable'):
        feed._verify_peer(client, os.getpid())


def test_unknown_platform_fails_closed_without_peer_credentials(monkeypatch):
    monkeypatch.delattr(feed.socket, 'SO_PEERCRED', raising=False)
    monkeypatch.setattr(feed.sys, 'platform', 'unsupported')
    with contextlib.ExitStack() as stack:
        client, peer = socket.socketpair()
        stack.enter_context(client)
        stack.enter_context(peer)
        with pytest.raises(ValueError, match='peer proof unavailable'):
            feed._verify_peer(client, os.getpid())


def test_model_use_crosses_the_allowlist_and_unknown_fields_do_not():
    from herdr_engineering.mission_activity_feed import _sanitize

    clean = _sanitize({
        'health': 'active',
        'modelUse': {
            'pin': 'gw/qwen3.8-27b', 'pinnedAt': '2026-10-30T00:00:00Z', 'elsewhere': 1,
            'agents': [{'workerId': 'W2', 'taskId': 'T2', 'role': 'reviewer', 'model': 'gw/deepseek',
                        'state': 'running', 'onPin': False, 'rawPrompt': 'must not cross'}],
        },
        'hiddenReasoning': 'must not cross',
    })
    use = clean['modelUse']
    assert use['pin'] == 'gw/qwen3.8-27b' and use['elsewhere'] == 1
    assert use['agents'][0]['role'] == 'reviewer' and use['agents'][0]['onPin'] is False
    assert 'rawPrompt' not in use['agents'][0] and 'hiddenReasoning' not in clean

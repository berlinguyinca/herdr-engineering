import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def call(server, path, body=None, key=None):
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['X-API-Key'] = key
    request = urllib.request.Request(f'http://127.0.0.1:{server.port}'+path,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def start(repo, **kwargs):
    server = ControlPlaneServer(repo, **kwargs)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    return server


def test_agent_names_rename_conflicts_and_viewer_capabilities():
    repo = ControlPlaneRepo()
    repo.upsert_session('one', name='Ada')
    repo.upsert_session('two', name='Grace')
    server = start(repo, api_keys={'read': 'viewer', 'write': 'operator'})
    try:
        status, first = call(server, '/api/v1/sessions/one', key='read')
        assert status == 200 and first['display_name'] == 'Ada'
        assert first['capabilities']['rename_agent'] is False
        assert call(server, '/api/v1/sessions/one/name', {'name': 'Ada Byron', 'expected_version': 1}, 'read')[0] == 403
        status, renamed = call(server, '/api/v1/sessions/one/name', {'name': 'Ada Byron', 'expected_version': 1}, 'write')
        assert status == 200 and renamed['display_name'] == 'Ada Byron'
        assert call(server, '/api/v1/sessions/one/name', {'name': 'Grace', 'expected_version': 2}, 'write')[0] == 409
        assert call(server, '/api/v1/sessions/one/name', {'name': 'Ada New', 'expected_version': 1}, 'write')[0] == 409
        assert call(server, '/api/v1/sessions/missing/summary', key='read')[0] == 404
    finally:
        server.shutdown()


def test_summary_runs_in_background_and_reads_cache_without_new_inference(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    sid = 'herdr:bender:default:w1%3Ap1'
    repo.upsert_session(sid)
    repo.inspector.ingest(sid, {'kind': 'message', 'role': 'assistant', 'text': 'The UI is complete; API integration remains pending.', 'message_id': 'm1'})
    release, invoked = threading.Event(), threading.Event()
    calls = []

    def model(payload, *, timeout):
        calls.append(threading.get_ident())
        invoked.set()
        assert release.wait(2)
        return {'overview': 'The UI is complete.', 'where_left_off': 'API integration remains pending.',
                'discussed': [], 'decisions': [], 'completed': ['UI'], 'next_steps': ['Integrate API'],
                'blockers': [], 'citations': []}

    server = start(repo, summary_config={'enabled': True, 'endpoint': 'http://127.0.0.1:1/v1',
                                         'model': 'test-small', 'debounce_seconds': 0}, summary_client=model)
    path = '/api/v1/sessions/'+urllib.parse.quote(sid, safe='')+'/summary'
    try:
        before = time.monotonic()
        status, pending = call(server, path, {})
        assert status == 202 and pending['state'] == 'pending'
        assert time.monotonic()-before < 1
        assert invoked.wait(1)
        release.set()
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            _, result = call(server, path)
            if result['state'] == 'ready':
                break
            time.sleep(.02)
        assert result['where_left_off'] == 'API integration remains pending.'
        for _ in range(3):
            assert call(server, path)[1]['state'] == 'ready'
        assert len(calls) == 1
        assert repo.agent_summary_events()[0]['summary']['source_message_count'] == 1
    finally:
        release.set()
        server.shutdown()


def test_default_model_is_disabled_and_never_invents_unrecorded_conversation():
    repo = ControlPlaneRepo()
    repo.upsert_session('empty')
    server = start(repo)
    try:
        status, result = call(server, '/api/v1/sessions/empty/summary')
        assert status == 200 and result['state'] == 'disabled'
        assert not result['overview'] and result['source_message_count'] == 0
    finally:
        server.shutdown()

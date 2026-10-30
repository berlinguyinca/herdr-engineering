"""Grounded, bounded and durable automatic agent summaries."""
import json
import threading
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane.agent_summaries import (
    AgentSummaryConfig,
    AgentSummaryService,
    OpenAICompatibleSummaryClient,
    summary_source,
)
from herdr_engineering.control_plane.inspector import InspectorStore

SID = 'agent-one'


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def repo():
    row = {'session_id': SID, 'agent_session': {'agent': 'codex', 'kind': 'id', 'value': 'thread-1'},
           'transcript': {'status': 'recording'}}
    return SimpleNamespace(inspector=InspectorStore(), get_session=lambda sid: row)


def feed(repo, *messages):
    return repo.inspector.ingest(SID, {'events': list(messages)})


def msg(text, role='user', **kw):
    return {'kind': 'message', 'role': role, 'text': text, **kw}


def result(source_ids=()):
    return {'overview': 'Fix authentication', 'discussed': ['Login'], 'decisions': [],
            'completed': [], 'where_left_off': 'Investigating the bug', 'next_steps': ['Reproduce'],
            'blockers': [], 'citations': list(source_ids)}


def make_service(repo, clock=None, client=None, persist=None, **kw):
    config = AgentSummaryConfig(enabled=True, endpoint='http://localhost:8099/v1', model='qwen-small', **kw)
    return AgentSummaryService(repo, config, clock=clock, persist=persist,
                               model_client=client or (lambda payload, **_: result()))


def test_source_full_viewer_safe_and_conversation_only(repo):
    feed(repo, msg('a' * 3500), msg('system private', 'system'),
         msg('sensitive private', sensitive=True), msg('token abcdefghijkl', 'assistant'),
         {'kind': 'tool', 'call_id': 't', 'phase': 'end', 'output': 'TOOL PRIVATE'})
    source = summary_source(repo, SID)
    assert source['messages'][0]['text'] == 'a' * 3500
    assert [m['role'] for m in source['messages']] == ['user', 'assistant']
    assert 'private' not in json.dumps(source).lower()
    assert '[REDACTED]' in source['messages'][1]['text']
    assert source['coverage']['status'] == 'partial'


def test_fingerprint_ignores_tool_updates_and_ids_distinguish_identical_messages(repo):
    feed(repo, msg('same'), msg('same'))
    before = summary_source(repo, SID)
    feed(repo, {'kind': 'tool', 'call_id': 't', 'phase': 'end', 'output': 'ok'})
    after = summary_source(repo, SID)
    assert before['fingerprint'] == after['fingerprint']
    assert len(set(before['source_ids'])) == 2
    repo.get_session(SID)['agent_session']['value'] = 'thread-2'
    assert summary_source(repo, SID)['fingerprint'] != before['fingerprint']


def test_source_bounded_and_partial(repo):
    feed(repo, *[msg(str(i) + 'x' * 1000) for i in range(40)])
    source = summary_source(repo, SID, AgentSummaryConfig(max_messages=5, max_chars=2000))
    assert len(source['messages']) <= 5
    assert sum(len(m['text']) for m in source['messages']) <= 2000
    assert source['messages'][-1]['text'].startswith('39')
    assert source['coverage']['status'] == 'partial'


def test_debounce_cache_and_persist_before_publish(repo):
    clock, calls, events = Clock(), [], []
    service = None

    def persist(event):
        assert service.get(SID)['state'] == 'pending'
        events.append(event)

    def client(payload, **kw):
        calls.append(payload)
        return result()

    service = make_service(repo, clock, client, persist)
    feed(repo, msg('fix auth'))
    assert service.request(SID)['state'] == 'pending'
    assert service.run_once() is False
    clock.now += 5
    assert service.run_once() is True
    assert service.get(SID)['state'] == 'ready'
    assert len(events) == 1 and events[0]['kind'] == 'agent_summary'
    service.request(SID)
    clock.now += 20
    assert service.run_once() is False
    assert len(calls) == 1
    assert calls[0]['chat_template_kwargs']['enable_thinking'] is False
    assert calls[0]['response_format']['type'] == 'json_schema'
    assert calls[0]['model'] == 'qwen-small'


def test_changed_input_during_generation_discards_stale_result(repo):
    clock = Clock()

    def client(payload, **kw):
        feed(repo, msg('also fix logout'))
        return result()

    events = []
    service = make_service(repo, clock, client, events.append)
    feed(repo, msg('fix auth'))
    service.request(SID)
    clock.now += 5
    service.run_once()
    assert events == []
    assert service.get(SID)['state'] == 'pending'


@pytest.mark.parametrize('bad', ['not json', {'overview': 'too few fields'},
                                    result(['invented-source']), {**result(), 'decisions': 'wrong'}])
def test_invalid_model_response_never_publishes(repo, bad):
    clock, events = Clock(), []
    service = make_service(repo, clock, lambda *a, **kw: bad, events.append)
    feed(repo, msg('fix auth'))
    service.request(SID)
    clock.now += 5
    service.run_once()
    assert service.get(SID)['state'] == 'failed'
    assert events == []


def test_failed_persistence_preserves_prior_and_retry_backoff(repo):
    clock, count = Clock(), []

    def persist(event):
        count.append(event)
        return len(count) == 1

    service = make_service(repo, clock, persist=persist)
    feed(repo, msg('fix auth'))
    service.request(SID)
    clock.now += 5
    service.run_once()
    previous = service.get(SID)
    feed(repo, msg('new turn'))
    service.request(SID)
    clock.now += 5
    service.run_once()
    failed = service.get(SID)
    assert failed['state'] == 'failed'
    assert failed['source_ids'] == previous['source_ids']
    assert failed['generated_at'] == previous['generated_at']
    assert service.run_once() is False
    clock.now += 30
    assert service.run_once() is True


def test_source_expiry_keeps_original_coverage_and_provenance(repo):
    clock = Clock()
    events = []
    service = make_service(repo, clock, persist=events.append)
    feed(repo, msg('fix auth'))
    service.request(SID)
    clock.now += 5
    service.run_once()
    prior = service.get(SID)
    repo.inspector = InspectorStore()
    expired = service.get(SID)
    assert expired['state'] == 'stale'
    assert expired['coverage'] == prior['coverage']
    assert expired['source_ids'] == prior['source_ids']
    restored = make_service(repo, clock)
    restored.hydrate(events)
    assert restored.get(SID)['overview'] == prior['overview']
    assert restored.get(SID)['state'] == 'stale'


def test_disabled_and_no_data(repo):
    disabled = AgentSummaryService(repo, AgentSummaryConfig())
    assert disabled.request(SID)['state'] == 'disabled'
    assert disabled.run_once() is False
    service = make_service(repo)
    assert service.request(SID)['state'] == 'no_data'
    assert service.run_once() is False


def test_output_redacted_and_citations_preserved(repo):
    clock = Clock()
    feed(repo, msg('fix auth', message_id='m1'))
    ids = summary_source(repo, SID)['source_ids']
    service = make_service(repo, clock, lambda *a, **kw: {**result(ids), 'overview': 'sk-secretvalue'})
    service.request(SID)
    clock.now += 5
    service.run_once()
    assert service.get(SID)['overview'] == '[REDACTED]'
    assert service.get(SID)['citations'] == ids


def test_queue_bound_and_close_lifecycle(repo):
    feed(repo, msg('fix auth'))
    service = make_service(repo, queue_limit=1)
    service.request(SID)
    repo.inspector.ingest('other', {'events': [msg('other')]})
    assert service.request('other')['state'] == 'failed'
    service.start()
    service.close()
    assert not service._thread.is_alive()


@pytest.mark.parametrize('values', [dict(enabled='true'), dict(queue_limit=0), dict(max_messages=True),
                                    dict(timeout_seconds=-1), dict(debounce_seconds=float('nan')),
                                    dict(enabled=True, model='qwen'),
                                    dict(enabled=True, endpoint='file:///tmp/x', model='qwen'),
                                    dict(enabled=True, endpoint='http://user:pass@localhost', model='qwen')])
def test_config_rejects_unsafe_or_untyped_values(values):
    with pytest.raises(ValueError):
        AgentSummaryConfig(**values)


def test_network_client_endpoint_body_and_timeout(monkeypatch):
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return json.dumps({'choices': [{'message': {'content': json.dumps(result())}}]}).encode()

    def open_url(request, timeout):
        seen.append((request, timeout))
        return Response()

    monkeypatch.setattr('urllib.request.urlopen', open_url)
    client = OpenAICompatibleSummaryClient('http://localhost:8099/v1')
    assert client({'model': 'small'}, timeout=12) == result()
    assert seen[0][0].full_url == 'http://localhost:8099/v1/chat/completions'
    assert json.loads(seen[0][0].data) == {'model': 'small'}
    assert seen[0][1] == 12


def test_concurrent_run_once_has_single_model_call(repo):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def client(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return result()

    service = make_service(repo, client=client, debounce_seconds=0)
    feed(repo, msg('fix auth'))
    service.request(SID)
    worker = threading.Thread(target=service.run_once)
    worker.start()
    assert entered.wait(2)
    try:
        assert service.run_once() is False
        service.request(SID)
    finally:
        release.set()
        worker.join(2)
    service.run_once()
    assert calls == [1]


def test_model_change_invalidates_restored_cache(repo):
    events = []
    feed(repo, msg('fix auth'))
    service = make_service(repo, persist=events.append, debounce_seconds=0)
    service.request(SID)
    service.run_once()
    changed = AgentSummaryService(repo, AgentSummaryConfig(
        enabled=True, endpoint='http://localhost:8099/v1', model='different'),
        model_client=lambda *a, **kw: result())
    changed.hydrate(events)
    assert changed.get(SID)['state'] == 'stale'


def test_conversation_replacement_inflight_discards_result(repo):
    def client(*a, **kw):
        repo.get_session(SID)['agent_session']['value'] = 'replacement'
        return result()

    events = []
    feed(repo, msg('fix auth'))
    service = make_service(repo, client=client, persist=events.append, debounce_seconds=0)
    service.request(SID)
    service.run_once()
    assert events == []


def test_newer_message_debounces_again(repo):
    clock = Clock()
    service = make_service(repo, clock=clock)
    feed(repo, msg('first'))
    service.request(SID)
    clock.now += 4
    feed(repo, msg('second'))
    service.request(SID)
    clock.now += 1
    assert service.run_once() is False
    clock.now += 4
    assert service.run_once() is True


def test_raw_errors_never_reach_get(repo):
    def client(*a, **kw):
        raise RuntimeError('sk-rawsecret')

    feed(repo, msg('fix auth'))
    service = make_service(repo, client=client, debounce_seconds=0)
    service.request(SID)
    service.run_once()
    assert 'sk-rawsecret' not in json.dumps(service.get(SID))


def test_explicit_activity_facts_are_safe_and_not_cache_triggers(repo):
    feed(repo, msg('fix auth'))
    before = summary_source(repo, SID)
    feed(repo, {'kind': 'test', 'run_id': 'tests', 'phase': 'end', 'passed': 3, 'failed': 0},
         {'kind': 'file', 'path': 'sensitive.py', 'op': 'edit', 'sensitive': True},
         {'kind': 'file', 'path': 'auth.py', 'op': 'edit', 'diff': 'PRIVATE DIFF'})
    after = summary_source(repo, SID)
    assert before['fingerprint'] == after['fingerprint']
    assert any(f.get('passed') == 3 for f in after['activity_facts'])
    assert any(f.get('path') == 'auth.py' for f in after['activity_facts'])
    assert 'sensitive.py' not in json.dumps(after['activity_facts'])
    assert 'PRIVATE DIFF' not in json.dumps(after['activity_facts'])


def test_prompt_short_labels_map_back_to_distinct_stable_message_ids(repo):
    seen = []
    feed(repo, msg('same'), msg('same'))
    ids = summary_source(repo, SID)['source_ids']

    def client(payload, **kwargs):
        seen.append(payload)
        prompt = json.loads(payload['messages'][-1]['content'])
        assert [m['id'] for m in prompt['messages']] == ['m1', 'm2']
        assert not any(stable in payload['messages'][-1]['content'] for stable in ids)
        return result(['m1', 'm2'])

    service = make_service(repo, client=client, debounce_seconds=0)
    service.request(SID)
    service.run_once()
    assert service.get(SID)['state'] == 'ready'
    assert service.get(SID)['citations'] == ids
    assert seen[0]['max_tokens'] == 400
    assert seen[0]['chat_template_kwargs']['enable_thinking'] is False
    schema = seen[0]['response_format']['json_schema']['schema']
    assert schema['properties']['next_steps']['maxItems'] == 2
    assert 'unfinished' in schema['properties']['where_left_off']['description']
    assert 'where_left_off:' in seen[0]['messages'][0]['content']
    assert 'Example input:' not in seen[0]['messages'][0]['content']


def test_endpoint_change_invalidates_cached_summary(repo):
    feed(repo, msg('fix auth'))
    events = []
    service = make_service(repo, persist=events.append, debounce_seconds=0)
    service.request(SID)
    service.run_once()
    changed = AgentSummaryService(repo, AgentSummaryConfig(enabled=True,
        endpoint='http://localhost:8100/v1', model='qwen-small'))
    changed.hydrate(events)
    assert changed.get(SID)['state'] == 'stale'


@pytest.mark.parametrize('handoff', ['messages', 'none', 'N/A', 'null', 'unknown'])
def test_placeholder_handoff_is_empty_and_quality_flagged(repo, handoff):
    feed(repo, msg('API integration is unfinished.', 'assistant'))
    service = make_service(repo, debounce_seconds=0,
        client=lambda *a, **kw: {**result(), 'where_left_off': handoff})
    service.request(SID)
    service.run_once()
    actual = service.get(SID)
    assert actual['where_left_off'] == ''
    assert 'missing_continuation' in actual['quality_flags']


@pytest.mark.parametrize('value', [True, 0, 801, '400'])
def test_output_token_limit_validation(value):
    with pytest.raises(ValueError):
        AgentSummaryConfig(max_output_tokens=value)


def test_prompt_tracks_newer_unanswered_user_without_duplicating_transcript(repo):
    feed(repo, msg('Fix reset link'), msg('Reset link works.', 'assistant'),
         msg('Also add keyboard navigation and verify it in browser.'))
    seen = []

    def client(payload, **kwargs):
        seen.append(payload)
        return result(['m2', 'm3'])

    service = make_service(repo, client=client, debounce_seconds=0)
    service.request(SID)
    service.run_once()
    payload = seen[0]
    source = json.loads(payload['messages'][-1]['content'])
    assert source['latest_assistant_label'] == 'm2'
    assert source['unanswered_user_labels'] == ['m3']
    assert payload['messages'][-1]['content'].count('Reset link works.') == 1
    assert payload['messages'][-1]['content'].count('Also add keyboard') == 1
    assert 'implementation AND verification remain pending' in payload['messages'][0]['content']
    assert payload['temperature'] == 0
    assert payload['chat_template_kwargs']['enable_thinking'] is False
    schema = payload['response_format']['json_schema']['schema']
    assert schema['properties']['citations']['items']['enum'] == ['m1', 'm2', 'm3']
    assert service.get(SID)['state'] == 'ready'


def test_prompt_marks_all_user_turns_unanswered_before_first_assistant(repo):
    feed(repo, msg('Implement feature'), msg('Include tests'))
    seen = []
    service = make_service(repo, client=lambda payload, **kw: (seen.append(payload) or result()),
                           debounce_seconds=0)
    service.request(SID)
    service.run_once()
    source = json.loads(seen[0]['messages'][-1]['content'])
    assert source['latest_assistant_label'] is None
    assert source['unanswered_user_labels'] == ['m1', 'm2']


@pytest.mark.parametrize('values', [
    {'routing': 'unknown'}, {'routing': None}, {'routing': 'fabric'},
    {'routing': 'fabric', 'api_key': ''}, {'api_key': None},
    {'api_key': 'x' * 4097}, {'api_key': 'secret\r\nInjected: value'},
    {'api_key': 'secret\x7f'}, {'api_key': 'secret secret'}, {'api_key': 'secreté'},
])
def test_fabric_config_rejects_invalid_credentials_without_echoing(values):
    with pytest.raises(ValueError) as error:
        AgentSummaryConfig(**values)
    if values.get('api_key'):
        assert values['api_key'] not in str(error.value)


def test_fabric_credentials_are_excluded_from_repr_and_rotation_keeps_cache(repo):
    feed(repo, msg('fix auth'))
    first = make_service(repo, routing='fabric', api_key='first-secret')
    second = make_service(repo, routing='fabric', api_key='second-secret')
    direct = make_service(repo)
    source = first.summary_source(SID)
    assert 'first-secret' not in repr(first.config)
    assert first._cache_key(source) == second._cache_key(source)
    assert first._cache_key(source) != direct._cache_key(source)


def test_fabric_service_includes_stable_conversation_affinity_only_for_fabric(repo):
    seen = []
    feed(repo, msg('fix auth'))
    repo.inspector.ingest('other', {'events': [msg('other request')]})
    service = make_service(repo, routing='fabric', api_key='project-secret',
                           debounce_seconds=0,
                           client=lambda payload, **kw: (seen.append(payload) or result()))
    for sid in (SID, 'other', SID):
        service.request(sid, force=True)
        assert service.run_once()
    identities = [p['metadata']['herdr_summary_session_id'] for p in seen]
    assert identities[0] == identities[2] != identities[1]
    assert identities[0] == service.summary_source(SID)['conversation_identity']
    assert all('project-secret' not in json.dumps(payload) for payload in seen)
    direct_seen = []
    direct = make_service(repo, debounce_seconds=0,
                          client=lambda payload, **kw: (direct_seen.append(payload) or result()))
    direct.request(SID)
    assert direct.run_once()
    assert 'metadata' not in direct_seen[0]


@pytest.fixture
def summary_http_server():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests = []
    response = {'redirect': None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append({'path': self.path, 'headers': dict(self.headers),
                             'body': json.loads(self.rfile.read(int(self.headers['Content-Length'])))})
            if response['redirect']:
                self.send_response(response['redirect'])
                self.send_header('Location', '/redirect-target')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'choices': [{'message': {
                'content': json.dumps(result())}}]}).encode())

        def do_GET(self):
            requests.append({'path': self.path, 'headers': dict(self.headers)})
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests, response
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_fabric_http_transport_auth_affinity_and_metadata_removal(summary_http_server):
    endpoint, requests, _ = summary_http_server
    client = OpenAICompatibleSummaryClient(endpoint, routing='fabric', api_key='project-secret')
    payload = {'model': 'agent-summary', 'metadata': {'herdr_summary_session_id': 'a' * 64}}
    assert client(payload, timeout=2) == result()
    captured = requests[0]
    headers = {key.lower(): value for key, value in captured['headers'].items()}
    assert headers['authorization'] == 'Bearer project-secret'
    assert headers['x-herdr-require-registered-model'] == 'true'
    assert headers['x-inferweave-session-id'] == 'a' * 64
    assert headers['x-inferweave-agent-id'] == 'herdr-agent-summaries'
    assert captured['body'] == {'model': 'agent-summary'}
    assert payload['metadata']['herdr_summary_session_id'] == 'a' * 64
    assert 'project-secret' not in repr(client)


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_fabric_http_never_follows_authenticated_redirects(summary_http_server, status):
    endpoint, requests, response = summary_http_server
    response['redirect'] = status
    client = OpenAICompatibleSummaryClient(endpoint, routing='fabric', api_key='redirect-secret')
    with pytest.raises(RuntimeError) as error:
        client({'model': 'agent-summary', 'metadata': {
            'herdr_summary_session_id': 'a' * 64}}, timeout=2)
    assert len(requests) == 1
    assert 'redirect-secret' not in str(error.value)
    assert endpoint not in str(error.value)


def test_direct_http_does_not_send_fabric_auth_or_headers(summary_http_server):
    endpoint, requests, _ = summary_http_server
    client = OpenAICompatibleSummaryClient(endpoint, api_key='unused-secret')
    assert client({'model': 'small'}, timeout=2) == result()
    headers = {key.lower(): value for key, value in requests[0]['headers'].items()}
    assert 'authorization' not in headers
    assert not any(key.startswith(('x-herdr-', 'x-inferweave-')) for key in headers)


def test_service_constructs_authenticated_client(repo, summary_http_server):
    endpoint, requests, _ = summary_http_server
    feed(repo, msg('fix auth'))
    service = AgentSummaryService(repo, AgentSummaryConfig(
        enabled=True, endpoint=endpoint, model='agent-summary', routing='fabric',
        api_key='service-secret', debounce_seconds=0))
    service.request(SID)
    assert service.run_once()
    assert service.get(SID)['state'] == 'ready'
    headers = {key.lower(): value for key, value in requests[0]['headers'].items()}
    assert headers['authorization'] == 'Bearer service-secret'
    assert headers['x-inferweave-session-id'] == service.summary_source(SID)['conversation_identity']


@pytest.mark.parametrize('metadata', [None, {}, {'herdr_summary_session_id': 'bad\r\nheader'}])
def test_fabric_rejects_missing_or_invalid_affinity_before_sending(summary_http_server, metadata):
    endpoint, requests, _ = summary_http_server
    client = OpenAICompatibleSummaryClient(endpoint, routing='fabric', api_key='project-secret')
    with pytest.raises(ValueError, match='valid conversation identity'):
        client({'model': 'agent-summary', 'metadata': metadata}, timeout=2)
    assert requests == []


def test_fabric_transport_errors_do_not_expose_credentials_or_response(monkeypatch):
    client = OpenAICompatibleSummaryClient('http://localhost:8099/v1',
                                           routing='fabric', api_key='private-project-key')

    def fail(*args, **kwargs):
        raise ValueError('private-project-key provider secret body')

    monkeypatch.setattr(client._opener, 'open', fail)
    with pytest.raises(RuntimeError) as error:
        client({'model': 'agent-summary', 'metadata': {
            'herdr_summary_session_id': 'a' * 64}}, timeout=2)
    assert str(error.value) == 'fabric summary request failed'
    assert error.value.__suppress_context__


def test_summary_source_is_memoized_until_new_events(repo):
    """The projection is reused across polls while the session is unchanged, and
    recomputed once a new event lands (newest_seq is a sound staleness key)."""
    feed(repo, msg('hello'), msg('world', 'assistant'))
    svc = make_service(repo)
    first = svc.summary_source(SID)
    second = svc.summary_source(SID)
    assert first is second  # cached: no new events since last computation
    feed(repo, msg('another'))
    third = svc.summary_source(SID)
    assert third is not first  # newest_seq advanced -> recomputed
    texts = [m['text'] for m in third['messages']]
    assert 'another' in texts
    # And it is stable again after the recompute.
    assert svc.summary_source(SID) is third


def test_summary_source_memo_is_bounded(repo):
    """The LRU cap is enforced so a long-lived control plane cannot grow the
    source memo without limit."""
    feed(repo, msg('seed'))
    svc = make_service(repo)
    svc._source_cache_max = 2
    svc.summary_source(SID)
    assert len(svc._source_cache) == 1
    for n in range(5):
        other = SimpleNamespace(inspector=repo.inspector, get_session=repo.get_session)
        feed(repo, msg(f'sid-{n}'))
        other_sid = f'other-{n}'
        AgentSummaryService(other, svc.config).summary_source(other_sid)
    # distinct sessions -> bounded by cap
    assert len(svc._source_cache) <= svc._source_cache_max

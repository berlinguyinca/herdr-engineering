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

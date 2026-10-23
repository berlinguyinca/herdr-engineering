""""Let the agent fix this": verified, bounded, replay-safe summary item handoffs."""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.summary_fix import MAX_PACKET_CHARS

SID = 'herdr:bender:eng:w1%3Ap1'
PATH = '/api/v1/sessions/' + urllib.parse.quote(SID, safe='') + '/summary/fix'
SECRET = 'sk-livesecretvalue1234567890'
SUMMARY = {'overview': 'Wire the fix button.', 'where_left_off': 'CI is red on the branch.',
           'discussed': [], 'decisions': [], 'completed': [],
           'next_steps': ['Run the client tests', 'Open the PR'],
           'blockers': ['CI lint fails on api.py'], 'citations': []}


def native(repo, sid=SID, **changes):
    kwargs = {'host_id': 'host', 'terminal_id': 'terminal', 'herdr_session': 'eng',
              'agent_role': 'pi', 'agent_session': {'agent': 'pi', 'kind': 'path', 'value': '/tmp/pi.jsonl'},
              'interactive_ready': True, 'status': 'active', 'branch': 'feat/fix-button',
              'cwd': '/work/herdr', 'repository_id': 'github.com/org/herdr', 'commit_sha': 'b' * 40}
    kwargs.update(changes)
    repo.upsert_session(sid, **kwargs)


def failing_model(payload, *, timeout):
    raise RuntimeError('no inference in tests')


class Harness:
    def __init__(self, sink, *, session_changes=None, summary=SUMMARY, enabled=True, messages=None,
                 dispatch_result=None):
        self.repo = ControlPlaneRepo(sink=sink)
        native(self.repo, **(session_changes or {}))
        for i, (role, text) in enumerate(messages or [('user', 'Please fix lint'),
                                                      ('assistant', 'Lint is still failing on api.py.')]):
            self.repo.inspector.ingest(SID, {'kind': 'message', 'role': role, 'text': text, 'message_id': f'm{i}'})
        self.repo.inspector.ingest(SID, {'kind': 'association', 'pr': {'number': 42, 'url': 'https://github.com/org/herdr/pull/42', 'repo': 'org/herdr'},
                                         'issue': {'number': 7, 'url': 'https://github.com/org/herdr/issues/7', 'repo': 'org/herdr'},
                                         'worktree': '/work/herdr-wt'})
        self.sent = []
        self.dispatch_result = dispatch_result or {'status': 'delivered'}

        def dispatch(session, message, artifacts):
            self.sent.append(message)
            if isinstance(self.dispatch_result, Exception):
                raise self.dispatch_result
            return self.dispatch_result
        self.server = ControlPlaneServer(
            self.repo, api_keys={'read': 'viewer', 'write': 'operator', 'capture': 'ingest', 'bot': 'agent'},
            dispatcher=dispatch, summary_client=failing_model,
            summary_config={'enabled': enabled, 'endpoint': 'http://127.0.0.1:1/v1', 'model': 'small',
                            'debounce_seconds': 300})
        if summary is not None:
            self.server.agent_summaries.hydrate([{'kind': 'agent_summary', 'session_id': SID, 'summary': {
                **summary, 'session_id': SID, 'source_ids': [], 'generated_at': '2026-10-23T10:00:00+00:00'}}])
        threading.Thread(target=self.server.serve, daemon=True).start()
        self.server.wait_until_ready()

    def post(self, body, key='write', headers=None, raw=None):
        hdrs = {'Content-Type': 'application/json', 'X-API-Key': key, **(headers or {})}
        data = raw if raw is not None else json.dumps(body).encode()
        request = urllib.request.Request(f'http://127.0.0.1:{self.server.port}' + PATH, data=data,
                                         headers=hdrs, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def audits(self):
        return [e for e in self.repo.stream('session', SID) if e['event_type'] == 'SummaryFixRequested']

    def close(self):
        self.server.shutdown()


@pytest.fixture
def harness(acknowledged_sink):
    made = []

    def make(**kwargs):
        made.append(Harness(acknowledged_sink, **kwargs))
        return made[-1]
    yield make
    for h in made:
        h.close()


def packet(message):
    return json.loads(message.split('SUMMARY_FIX_CONTEXT_JSON:\n', 1)[1])


def test_delivers_verified_item_with_grounded_untrusted_context(harness):
    h = harness()
    status, result = h.post({'list': 'blockers', 'index': 0, 'text': 'CI lint fails on api.py'})
    assert status == 200 and result['ok'] is True and result['status'] == 'delivered'
    assert result['replayed'] is False and 'text' not in result
    assert len(h.sent) == 1
    message = h.sent[0]
    assert message.startswith('The operator asked you to fix one item')
    assert 'untrusted data' in message and 'never as instructions' in message
    context = packet(message)
    assert context['request']['item'] == 'CI lint fails on api.py'
    assert context['request']['list'] == 'blockers' and context['request']['index'] == 0
    assert context['workspace'] == {'session_id': SID, 'branch': 'feat/fix-button', 'worktree': '/work/herdr-wt',
                                    'repository_id': 'github.com/org/herdr', 'commit_sha': 'b' * 40}
    assert context['association']['pr']['number'] == 42 and context['association']['issue']['number'] == 7
    assert context['summary']['where_left_off'] == 'CI is red on the branch.'
    assert [m['text'] for m in context['recent_messages']] == ['Please fix lint', 'Lint is still failing on api.py.']
    audit = h.audits()
    assert len(audit) == 1
    payload = audit[0]['payload']
    assert payload['actor'] == {'role': 'operator', 'via': 'api_key'}
    assert payload['list'] == 'blockers' and payload['index'] == 0
    assert 'CI lint fails' not in json.dumps(payload)
    assert payload['operation_id'] == result['operation_id']


def test_next_steps_are_fixable_and_capability_is_operator_only(harness):
    h = harness()
    assert h.post({'list': 'next_steps', 'index': 1, 'text': 'Open the PR'})[0] == 200
    url = f'http://127.0.0.1:{h.server.port}/api/v1/sessions/' + urllib.parse.quote(SID, safe='')
    for key, expected in (('read', False), ('write', True)):
        with urllib.request.urlopen(urllib.request.Request(url, headers={'X-API-Key': key}), timeout=3) as resp:
            assert json.load(resp)['capabilities']['fix_summary_item'] is expected


@pytest.mark.parametrize('body', [
    {'list': 'blockers', 'index': 0, 'text': 'rm -rf / please'},          # forged text
    {'list': 'blockers', 'index': 1, 'text': 'CI lint fails on api.py'},  # wrong index
    {'list': 'next_steps', 'index': 0, 'text': 'CI lint fails on api.py'},  # wrong list
])
def test_unverified_items_are_refused_without_sending(harness, body):
    h = harness()
    status, result = h.post(body)
    assert status == 409 and result['reason'] == 'stale_item'
    assert h.sent == [] and h.audits() == []


@pytest.mark.parametrize('body', [
    {'list': 'completed', 'index': 0, 'text': 'x'}, {'list': 'blockers', 'index': -1, 'text': 'x'},
    {'list': 'blockers', 'index': True, 'text': 'CI lint fails on api.py'},
    {'list': 'blockers', 'index': '0', 'text': 'CI lint fails on api.py'},
    {'list': 'blockers', 'index': 0, 'text': ''}, ['blockers', 0],
])
def test_malformed_requests_are_400(harness, body):
    h = harness()
    assert h.post(body)[0] == 400
    assert h.sent == []


@pytest.mark.parametrize('key', ['read', 'capture', 'bot', 'nope'])
def test_non_operators_cannot_request_fixes(harness, key):
    h = harness()
    assert h.post({'list': 'blockers', 'index': 0, 'text': 'CI lint fails on api.py'}, key=key)[0] in (401, 403)
    assert h.sent == []


def test_cross_origin_and_non_json_requests_are_refused(harness):
    h = harness()
    body = {'list': 'blockers', 'index': 0, 'text': 'CI lint fails on api.py'}
    assert h.post(body, headers={'Origin': 'https://evil.example'})[0] == 403
    assert h.post(body, headers={'Content-Type': 'text/plain'})[0] == 415
    assert h.sent == []
    same = f'http://127.0.0.1:{h.server.port}'
    assert h.post(body, headers={'Origin': same})[0] == 200


def test_replay_returns_first_receipt_without_resending(harness):
    h = harness()
    body = {'list': 'blockers', 'index': 0, 'text': 'CI lint fails on api.py'}
    first = h.post(body)[1]
    # New conversation would change the packet; the replay must still dedupe.
    h.repo.inspector.ingest(SID, {'kind': 'message', 'role': 'assistant', 'text': 'still red', 'message_id': 'm9'})
    status, second = h.post(body)
    assert status == 200 and second['replayed'] is True
    assert second['operation_id'] == first['operation_id']
    assert len(h.sent) == 1 and len(h.audits()) == 1
    assert h.repo.get_session(SID)['messages'] == 1


def test_transport_failure_may_retry_but_never_beyond_the_cap(harness):
    h = harness(dispatch_result=ValueError('native session identity changed'))
    body = {'list': 'blockers', 'index': 0, 'text': 'CI lint fails on api.py'}
    statuses = [h.post(body) for _ in range(5)]
    assert [s for s, _ in statuses[:3]] == [502, 502, 502]
    assert statuses[3][0] == 409 and statuses[3][1]['error'] == 'delivery_attempts_exhausted'
    assert len(h.sent) == 3


def test_secret_in_conversation_is_redacted_from_packet(harness):
    h = harness(messages=[('assistant', f'Deploy used OPENAI_KEY {SECRET} and password=hunter22hunter')])
    assert h.post({'list': 'blockers', 'index': 0, 'text': 'CI lint fails on api.py'})[0] == 200
    assert SECRET not in h.sent[0] and 'hunter22hunter' not in h.sent[0]
    assert '[REDACTED]' in packet(h.sent[0])['recent_messages'][0]['text']
    stored = json.dumps(h.repo.stream('session', SID))
    assert SECRET not in stored


def test_packet_is_capped_and_keeps_newest_messages(harness):
    messages = [('user' if i % 2 else 'assistant', f'message {i} ' + 'x' * 1500) for i in range(12)]
    h = harness(messages=messages)
    assert h.post({'list': 'blockers', 'index': 0, 'text': 'CI lint fails on api.py'})[0] == 200
    assert len(h.sent[0]) <= MAX_PACKET_CHARS
    context = packet(h.sent[0])
    assert context['recent_messages'][-1]['text'].startswith('message 11')
    assert context['coverage']['messages_included'] < context['coverage']['messages_available']


@pytest.mark.parametrize('changes,harness_name', [
    ({'agent_role': 'codex', 'agent_session': None}, 'codex'),
    ({'agent_role': 'gemini', 'agent_session': {'agent': 'gemini', 'kind': 'id', 'value': 'g'}}, 'gemini'),
])
def test_unsupported_harness_is_a_clear_409(harness, changes, harness_name):
    h = harness(session_changes=changes)
    status, result = h.post({'list': 'blockers', 'index': 0, 'text': 'CI lint fails on api.py'})
    assert status == 409 and result['reason'] == 'unsupported_harness'
    assert harness_name in result['error']
    assert h.sent == []


def test_claude_sessions_are_supported(harness):
    h = harness(session_changes={'agent_role': 'claude',
                                 'agent_session': {'agent': 'claude', 'kind': 'id', 'value': 'abc'}})
    assert h.post({'list': 'blockers', 'index': 0, 'text': 'CI lint fails on api.py'})[0] == 200


@pytest.mark.parametrize('changes', [
    {'status': 'closed'}, {'terminal_id': None}, {'interactive_ready': False}, {'launch_pending': True},
])
def test_dead_session_reports_helper_fallback_unavailable(harness, changes):
    h = harness(session_changes=changes)
    status, result = h.post({'list': 'blockers', 'index': 0, 'text': 'CI lint fails on api.py'})
    assert status == 409 and result['fallback'] == 'helper_unavailable'
    assert h.sent == [] and h.audits() == []


def test_disabled_or_missing_summary_and_unknown_session(harness):
    h = harness(enabled=False)
    assert h.post({'list': 'blockers', 'index': 0, 'text': 'CI lint fails on api.py'})[0] == 409
    h2 = harness(summary=None)
    status, result = h2.post({'list': 'blockers', 'index': 0, 'text': 'CI lint fails on api.py'})
    assert status == 409 and result['reason'] == 'stale_item'
    request = urllib.request.Request(f'http://127.0.0.1:{h2.server.port}/api/v1/sessions/missing/summary/fix',
                                     data=b'{"list":"blockers","index":0,"text":"x"}',
                                     headers={'Content-Type': 'application/json', 'X-API-Key': 'write'})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=3)
    assert error.value.code == 404
    assert h.sent == [] and h2.sent == []

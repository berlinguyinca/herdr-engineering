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


def test_wake_audit_is_marked_as_wake(harness):
    h = harness()
    assert h.post({'list': 'blockers', 'index': 0, 'text': 'CI lint fails on api.py'})[0] == 200
    assert h.audits()[0]['payload']['delivery'] == 'wake'


# ---------------------------------------------------------------- helper fallback (dead session)
from herdr_engineering.control_plane.collab import (  # noqa: E402
    CollabService,
    FileEventLog,
    StaticTokenAuthenticator,
)
from herdr_engineering.control_plane.summary_fix import (  # noqa: E402
    http_helper_spawner,
    inprocess_helper_spawner,
)
from tests.control_plane.test_collab import Clock, _digest  # noqa: E402
from tests.control_plane.test_collab_tasks import USERS  # noqa: E402

DEAD = {'status': 'closed', 'terminal_id': None}
ITEM = {'list': 'blockers', 'index': 0, 'text': 'CI lint fails on api.py'}


class FixRoom:
    """mallory (the operator) owns a standing summary-fix room with a requester agent."""

    def __init__(self, tmp_path, actions=('spawn_helpers',), profile=None, configured=True):
        self.log = str(tmp_path / 'collab.log')
        self.s = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}),
                               sink=FileEventLog(self.log), now=Clock())
        self.op = self.s.authenticate(f"Bearer {USERS['mallory']}", None)
        pid = self.s.create_project(self.op, {'name': 'fixes'})['project_id']
        self.room = self.s.create_room(self.op, pid, {'name': 'summary-fix'})['room_id']
        info = self.s.connect_agent(self.op, {'harness_type': 'claude-code', 'name': 'summary-fixer'})
        self.agent_id = info['agent_id']
        self.s.grant_agent(self.op, self.room, {'agent_id': self.agent_id, 'actions': list(actions)})
        self.s.set_room_profile(self.op, self.room, profile or {'allowed_families': ['claude-code']})
        if configured:
            self.s.summary_fix = {'room_id': self.room, 'requester_agent_id': self.agent_id}
        self.op.admin = True

    def spawner(self):
        return inprocess_helper_spawner(self.s, 'mallory')

    def helpers(self):
        return self.s.list_summary_fix_helpers(self.op)['helpers']

    def task(self, helper_id):
        return self.s._helpers[helper_id]['task']


def dead_harness(harness, room, **kwargs):
    h = harness(session_changes={**DEAD, **kwargs.pop('session_changes', {})}, **kwargs)
    h.server.summary_fix_helper = room.spawner()
    return h


def test_dead_session_starts_one_text_only_helper(harness, tmp_path):
    room = FixRoom(tmp_path)
    h = dead_harness(harness, room)
    status, result = h.post(ITEM)
    assert status == 202 and result['status'] == 'helper_started' and result['delivery'] == 'helper'
    assert result['tools'] is False and result['replayed'] is False and h.sent == []
    [helper] = room.helpers()
    assert helper['helper_id'] == result['helper_id'] and helper['role'] == 'worker'
    assert helper['requester_agent_id'] == room.agent_id and helper['operation_id'] == result['operation_id']
    task = room.task(result['helper_id'])
    assert task.startswith('The agent that owns this session is not running')
    assert 'untrusted data' in task and 'Nothing you write is applied' in task
    assert packet(task)['request']['item'] == 'CI lint fails on api.py'
    [audit] = h.audits()
    assert audit['payload']['delivery'] == 'helper' and audit['payload']['helper_id'] == result['helper_id']
    assert audit['payload']['operation_id'] == result['operation_id']
    assert 'CI lint fails' not in json.dumps(audit['payload'])


def test_unconfigured_room_keeps_the_clear_409(harness, tmp_path):
    room = FixRoom(tmp_path, configured=False)
    h = dead_harness(harness, room)
    status, result = h.post(ITEM)
    assert status == 409 and result['fallback'] == 'helper_unavailable'
    assert 'No summary-fix helper room is configured' in result['error']
    assert h.sent == [] and h.audits() == [] and room.helpers() == []


def test_replay_returns_the_same_helper_and_never_prompts_the_agent(harness, tmp_path):
    room = FixRoom(tmp_path)
    h = dead_harness(harness, room)
    first = h.post(ITEM)[1]
    status, second = h.post(ITEM)
    assert status == 202 and second['replayed'] is True and second['helper_id'] == first['helper_id']
    native(h.repo)                                  # the agent came back: still no second handoff
    status, third = h.post(ITEM)
    assert status == 202 and third['helper_id'] == first['helper_id'] and h.sent == []
    assert len(room.helpers()) == 1 and len(h.audits()) == 1


def test_collab_dedupes_the_operation_across_a_lost_audit_and_a_restart(tmp_path):
    room = FixRoom(tmp_path)
    spawn = room.spawner()
    op = 'summary-fix-' + 'a' * 40 + '-1'
    s1, first = spawn(op, 'task text')
    s2, again = spawn(op, 'other text')
    assert (s1, s2) == (202, 202) and again['helper_id'] == first['helper_id'] and again['replayed'] is True
    restarted = CollabService(room.s.auth, sink=FileEventLog(room.log), now=room.s._now,
                              summary_fix=room.s.summary_fix)
    restarted.hydrate(FileEventLog(room.log).load())
    status, body = inprocess_helper_spawner(restarted, 'mallory')(op, 'task text')
    assert status == 202 and body['helper_id'] == first['helper_id'] and body['replayed'] is True
    assert len(restarted.list_summary_fix_helpers(room.op)['helpers']) == 1


def test_helper_packet_is_capped_ascii_and_redacted(harness, tmp_path):
    room = FixRoom(tmp_path)
    messages = [('assistant', f'Deploy used OPENAI_KEY {SECRET} and password=hunter22hunter')]
    messages += [('user' if i % 2 else 'assistant', f'message {i}   \x85 ' + 'x' * 1500) for i in range(12)]
    h = dead_harness(harness, room, messages=messages)
    status, result = h.post(ITEM)
    assert status == 202, result
    task = room.task(result['helper_id'])
    assert len(task) <= MAX_PACKET_CHARS and task.isascii()
    assert SECRET not in task and 'hunter22hunter' not in task
    assert packet(task)['recent_messages'][-1]['text'].startswith('message 11')
    assert SECRET not in json.dumps(h.repo.stream('session', SID))


def test_secret_in_an_included_message_is_redacted_for_the_helper(harness, tmp_path):
    room = FixRoom(tmp_path)
    h = dead_harness(harness, room, messages=[('assistant', f'Deploy used OPENAI_KEY {SECRET} and password=hunter22hunter')])
    result = h.post(ITEM)[1]
    context = packet(room.task(result['helper_id']))
    assert '[REDACTED]' in context['recent_messages'][0]['text']
    assert SECRET not in room.task(result['helper_id'])


@pytest.mark.parametrize('actions,profile,fallback', [
    (('read',), None, 'helper_refused'),                                  # no spawn_helpers
    (('read', 'spawn_helpers'), None, 'helper_overgranted'),              # more than spawn_helpers
    (('spawn_helpers',), {'allowed_families': []}, 'helper_refused'),      # helpers off in the room
    (('spawn_helpers',), {'allowed_families': ['claude-code'], 'tools_allowed': True}, 'helper_tools_allowed'),
])
def test_requester_without_the_right_grant_or_room_is_refused(harness, tmp_path, actions, profile, fallback):
    room = FixRoom(tmp_path, actions=actions, profile=profile)
    h = dead_harness(harness, room)
    status, result = h.post(ITEM)
    assert status == 409 and result['fallback'] == fallback, result
    assert h.sent == [] and h.audits() == [] and room.helpers() == []


def test_expired_requester_credential_or_grant_says_which(harness, tmp_path):
    room = FixRoom(tmp_path)
    h = dead_harness(harness, room)
    grant = room.s._rooms[room.room]['grants'][room.agent_id]
    grant['expires_at'] = room.s._now() - 1
    status, result = h.post(ITEM)
    assert status == 409 and result['fallback'] == 'helper_grant_expired' and 'grant' in result['error']
    grant['expires_at'] = None
    room.s._agents[room.agent_id]['expires_at'] = room.s._now() - 1
    status, result = h.post(ITEM)
    assert status == 409 and result['fallback'] == 'helper_requester_expired' and 'credential expired' in result['error']
    assert room.helpers() == [] and h.audits() == []


def test_helper_limits_and_the_panic_switch_are_reported(harness, tmp_path):
    room = FixRoom(tmp_path, profile={'allowed_families': ['claude-code'], 'limits': {'live_per_agent': 1}})
    h = dead_harness(harness, room)
    assert h.post(ITEM)[0] == 202
    status, result = h.post({'list': 'next_steps', 'index': 0, 'text': 'Run the client tests'})
    assert status == 429 and result['fallback'] == 'helper_limit' and 'limit' in result['error']
    room.s.set_helpers_paused(room.op, True)
    status, result = h.post({'list': 'next_steps', 'index': 1, 'text': 'Open the PR'})
    assert status == 503 and result['fallback'] == 'helpers_paused'
    assert len(room.helpers()) == 1 and len(h.audits()) == 1


def test_unreachable_collab_is_a_502_without_audit(harness, tmp_path):
    h = harness(session_changes=DEAD)

    def broken(operation_id, task):
        raise OSError('connection refused')
    h.server.summary_fix_helper = broken
    status, result = h.post(ITEM)
    assert status == 502 and result['fallback'] == 'helper_unreachable' and h.audits() == []


def test_summary_fix_routes_are_operator_listener_only(tmp_path):
    room = FixRoom(tmp_path)
    body = {'operation_id': 'summary-fix-' + 'b' * 40 + '-1', 'task': 'x'}
    assert room.s.dispatch('POST', ['fleet', 'summary-fix', 'helper'], {}, body,
                           f"Bearer {USERS['mallory']}", None)[0] == 404
    assert room.s.dispatch('GET', ['fleet', 'summary-fix', 'helpers'], {}, {},
                           f"Bearer {USERS['alice']}", None)[0] == 404
    for bad in ({**body, 'room_id': room.room}, {**body, 'operation_id': 'anything'}, {'task': 'x'}):
        assert room.s.dispatch('POST', ['fleet', 'summary-fix', 'helper'], {}, bad, '', None,
                               operator='mallory')[0] == 400
    assert room.helpers() == []


def test_finished_helper_result_is_listed_as_untrusted_text(tmp_path):
    room = FixRoom(tmp_path)
    status, h = room.spawner()('summary-fix-' + 'c' * 40 + '-1', 'look at the lint error')
    assert status == 202
    room.s.claim_helper(room.op, h['helper_id'])
    room.s.finish_helper(room.op, h['helper_id'], {'status': 'done', 'result': 'Add the missing import.\n'})
    [row] = room.helpers()
    assert row['status'] == 'done' and row['result'] == 'Add the missing import.\n' and row['untrusted'] is True
    assert room.s.list_summary_fix_helpers(room.op)['configured'] is True


def test_http_spawner_posts_to_the_operator_listener(tmp_path):
    import http.server
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            code, body = (202, {'helper_id': 'collab_h1'}) if len(seen) == 1 else (409, {'reason': 'helper_unavailable'})
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        spawn = http_helper_spawner(f'http://127.0.0.1:{srv.server_port}/')
        assert spawn('op', 'task') == (202, {'helper_id': 'collab_h1'})
        assert spawn('op', 'task') == (409, {'reason': 'helper_unavailable'})
        assert seen[0] == ('/api/v1/collab/fleet/summary-fix/helper', {'operation_id': 'op', 'task': 'task'})
    finally:
        srv.shutdown()

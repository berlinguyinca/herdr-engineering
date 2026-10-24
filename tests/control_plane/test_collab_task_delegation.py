# ruff: noqa: F811 (fixtures imported from sibling modules)
"""Persisted member delegation is independent of an agent's attachment."""
import json

import pytest

from herdr_engineering.control_plane.collab import CollabService
from tests.control_plane.test_collab_pentest import tool, tool_text
from tests.control_plane.test_collab_public import H, stack  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, mcp_world, svc, w  # noqa: F401


def submit(w, agent, key='delegation'):
    return w.s.dispatch('POST', ['rooms', w.room, 'tasks'], {},
                        {'target_agent_id': agent.id, 'text': 'private task', 'idempotency_key': key},
                        'Bearer ' + USERS['bob'], None)


@pytest.mark.parametrize('state', ['absent', 'pending', 'revoked', 'expired', 'removed'])
def test_unapproved_delegation_routes_request_without_creating_task(w, state):
    agent, _ = w.agent('alice', delegate=False)
    if state != 'absent':
        w.s.set_agent_permission(w.bob if state == 'pending' else w.alice,
                                 w.room, agent.id, 'bob', 'pending' if state == 'pending' else 'approved')
    if state == 'revoked':
        w.s.set_agent_permission(w.alice, w.room, agent.id, 'bob', 'revoked')
    elif state == 'expired':
        w.s._agent_permissions[w.room, agent.id, 'bob']['expires_at'] = w.s.clock() - 1
    elif state == 'removed':
        w.s.remove_member(w.alice, w.room, 'bob')
        w.s.add_member(w.alice, w.room, {'user_id': 'bob', 'role': 'contributor'})
    status, result = submit(w, agent)
    assert status == 202
    assert result['status'] == 'pending' and result['access_request_required'] is True
    assert result['owner_user_id'] == 'alice'
    assert 'task_id' not in result and not w.s._tasks
    assert submit(w, agent)[1]['request_id'] == result['request_id']
    requests = w.s.list_agent_requests(w.alice, w.room)['requests']
    assert any(r['request_id'] == result['request_id'] and r['user_id'] == 'bob' for r in requests)
    w.s.decide_agent_request(w.alice, w.room, result['request_id'], {'decision': 'approve'})
    assert submit(w, agent)[0] == 201


def test_approved_delegation_replay_and_acceptance_recheck_revocation(w):
    agent, info = w.agent('alice', delegate=False)
    w.s.set_agent_permission(w.alice, w.room, agent.id, 'bob', 'approved')
    status, result = submit(w, agent)
    assert status == 201
    assert submit(w, agent)[1]['replayed'] is True
    w.s.set_agent_permission(w.alice, w.room, agent.id, 'bob', 'revoked')
    assert w.s.task_inbox(agent) == {'tasks': []}
    assert submit(w, agent)[0] == 202
    status, denied = w.s.dispatch('POST', ['tasks', result['task_id'], 'update'], {},
                                {'status': 'accepted'}, 'Agent ' + info['credential'], info['device_id'])
    assert status == 202 and denied['access_request_required'] is True
    assert w.s._tasks[result['task_id']]['status'] == 'pending'


def test_delegation_persists_after_event_log_hydration(w):
    agent, _ = w.agent('alice', delegate=False)
    w.s.set_agent_permission(w.alice, w.room, agent.id, 'bob', 'approved')
    old = w.s
    w.s = CollabService(old.auth, now=old.clock)
    w.s.hydrate(old._log)
    assert submit(w, agent)[0] == 201


def test_member_removal_invalidates_pending_task_even_after_reapproval(w):
    agent, _ = w.agent('alice', delegate=False)
    w.s.set_agent_permission(w.alice, w.room, agent.id, 'bob', 'approved')
    assert submit(w, agent)[0] == 201
    w.s.remove_member(w.alice, w.room, 'bob')
    w.s.add_member(w.alice, w.room, {'user_id': 'bob', 'role': 'contributor'})
    w.s.set_agent_permission(w.alice, w.room, agent.id, 'bob', 'approved')
    assert w.s.task_inbox(agent) == {'tasks': []}


def test_stale_delegation_decision_cannot_approve_a_later_request(w):
    from herdr_engineering.control_plane.collab import CollabError
    agent, _ = w.agent('alice', delegate=False)
    first = submit(w, agent)[1]
    w.s.decide_agent_request(w.alice, w.room, first['request_id'], {'decision': 'approve'})
    w.s.set_agent_permission(w.alice, w.room, agent.id, 'bob', 'revoked')
    second = submit(w, agent)[1]
    with pytest.raises(CollabError):
        w.s.decide_agent_request(w.alice, w.room, first['request_id'], {'decision': 'approve'})
    assert second['request_id'] != first['request_id']
    assert not w.s._tasks


def test_public_request_and_mcp_acceptance_share_the_delegation_envelope(stack):
    _, room, agents = mcp_world(stack)
    base = f'/api/v1/collab/rooms/{room}'
    body = {'target_agent_id': agents['alice']['agent_id'], 'text': 'do work', 'idempotency_key': 'public'}
    status, pending = stack.pub.call('POST', base + '/tasks', body, user='bob', headers=H)
    assert status == 202
    assert pending == {
        'room_id': room, 'agent_id': agents['alice']['agent_id'], 'status': 'pending',
        'request_id': pending['request_id'], 'access_request_required': True,
        'owner_user_id': 'alice', 'user_id': 'bob', 'message': 'Would you like to request access to me?',
    }
    assert stack.pub.call('POST', base + '/agent-requests/' + pending['request_id'] + '/decide',
                          {'decision': 'approve'}, user='bob', headers=H)[0] == 403
    assert stack.pub.call('POST', base + '/agent-requests/' + pending['request_id'] + '/decide',
                          {'decision': 'approve'}, user='alice', headers=H)[0] == 200
    status, task = stack.pub.call('POST', base + '/tasks', body, user='bob', headers=H)
    assert status == 201
    alice = stack.svc.authenticate('Bearer ' + USERS['alice'], None)
    stack.svc.set_agent_permission(alice, room, agents['alice']['agent_id'], 'bob', 'revoked')
    result = json.loads(tool_text(tool(stack, agents['alice'], 'task_update',
                                       task_id=task['task_id'], status='accepted')))
    assert result['request_id'] != pending['request_id']
    assert result == {**pending, 'request_id': result['request_id']}
    assert stack.svc._tasks[task['task_id']]['status'] == 'pending'

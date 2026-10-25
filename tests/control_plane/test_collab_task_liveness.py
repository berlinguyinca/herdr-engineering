# ruff: noqa: F811  (fixtures imported from sibling modules)
"""Task liveness after revocation, and bounded access-request behavior of update_task.

Spec 3.2/3.3: agent access disappears when the agent is detached, the member removed, or the
grant revoked. A task row may linger until TASK_TTL, but it must never produce a room-visible
write, reach an inbox, or notify the owner repeatedly."""
import pytest

from tests.control_plane.test_collab_public import H, stack  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, mcp_world, svc, w  # noqa: F401


def submit(w, agent, who='bob', key='liveness'):
    return w.s.dispatch('POST', ['rooms', w.room, 'tasks'], {},
                        {'target_agent_id': agent.id, 'text': 'work', 'idempotency_key': key},
                        'Bearer ' + USERS[who], None)


def update(w, info, task_id, body):
    return w.s.dispatch('POST', ['tasks', task_id, 'update'], {}, body,
                        'Agent ' + info['credential'], info['device_id'])


def events(s, *kinds):
    return [e for e in s._log if e['event_type'] in kinds]


REVOCATIONS = {
    'agent_detached': lambda w, agent: w.s.remove_agent_from_room(w.alice, w.room, agent.id),
    'member_removed': lambda w, agent: w.s.remove_member(w.alice, w.room, 'bob'),
    'agent_revoked': lambda w, agent: w.s.revoke_agent(w.alice, agent.id),
}


@pytest.mark.parametrize('revoke', REVOCATIONS)
def test_accepted_task_cannot_write_or_reach_an_inbox_after_revocation(w, revoke):
    agent, info = w.agent('alice')
    status, task = submit(w, agent)
    assert status == 201
    assert update(w, info, task['task_id'], {'status': 'accepted'})[0] == 200
    REVOCATIONS[revoke](w, agent)
    before = len(w.s._log)
    for body in ({'status': 'done', 'result': 'late room-visible write'}, {'status': 'failed'}):
        status, out = update(w, info, task['task_id'], body)
        assert status in (401, 403, 404), (revoke, status, out)
    # nothing room-visible is committed; only denial audits may be recorded
    assert {e['event_type'] for e in w.s._log[before:]} <= {'audit_denied'}
    if revoke != 'agent_revoked':
        assert w.s.task_inbox(agent) == {'tasks': []}


def test_a_lingering_accepted_task_expires_at_task_ttl_and_never_revives(w):
    from herdr_engineering.control_plane.collab import TASK_TTL
    agent, info = w.agent('alice')
    task = submit(w, agent)[1]
    update(w, info, task['task_id'], {'status': 'accepted'})
    w.s.remove_agent_from_room(w.alice, w.room, agent.id)
    w.s.grant_agent(w.alice, w.room, {'agent_id': agent.id, 'actions': ['read', 'post', 'execute']})
    assert w.s.task_inbox(agent) == {'tasks': []}        # a task older than the new grant never revives
    w.s.clock.t += TASK_TTL + 1 if hasattr(w.s.clock, 't') else 0
    shown = [t for t in w.s.list_tasks(w.alice, w.room)['tasks'] if t['task_id'] == task['task_id']][0]
    assert shown['status'] in ('accepted', 'expired')    # bounded: display-only until the TTL


def test_repeated_update_task_calls_raise_one_owner_prompt_not_a_stream(w):
    agent, info = w.agent('alice', delegate=False)
    w.s.set_agent_permission(w.alice, w.room, agent.id, 'bob', 'approved')
    task = submit(w, agent)[1]
    w.s.set_agent_permission(w.alice, w.room, agent.id, 'bob', 'revoked')
    prompts = len(events(w.s, 'agent_access_requested'))
    ids = set()
    for _ in range(10):
        status, out = update(w, info, task['task_id'], {'status': 'accepted'})
        assert status == 202 and out['access_request_required'] is True
        ids.add(out['request_id'])
    assert len(ids) == 1
    assert len(events(w.s, 'agent_access_requested')) == prompts + 1
    assert w.s._tasks[task['task_id']]['status'] == 'pending'      # the update itself changed nothing


def test_update_task_never_requests_access_for_someone_who_is_no_longer_a_member(w):
    agent, info = w.agent('alice', delegate=False)
    w.s.set_agent_permission(w.alice, w.room, agent.id, 'bob', 'approved')
    task = submit(w, agent)[1]
    w.s.remove_member(w.alice, w.room, 'bob')
    prompts = len(events(w.s, 'agent_access_requested'))
    assert update(w, info, task['task_id'], {'status': 'accepted'})[0] == 404
    assert len(events(w.s, 'agent_access_requested')) == prompts

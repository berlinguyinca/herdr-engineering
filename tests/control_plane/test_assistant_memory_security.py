"""Regression coverage for cached action authorization and durable revocation."""
from datetime import timedelta
from unittest.mock import Mock

import pytest

from herdr_engineering.control_plane.assistant import (
    ApprovalRequired, AssistantService, AssistantStore, assistant_config,
)


@pytest.fixture
def context():
    svc = AssistantService(config=assistant_config({'personal_assistant': {'enabled': True}}))
    assistant = svc.create_assistant(tenant_id='t1', user_id='u1', name='Helper')
    thread = svc.create_thread(tenant_id='t1', user_id='u1', assistant_id=assistant.assistant_id)
    run = svc.send_message(tenant_id='t1', user_id='u1', assistant_id=assistant.assistant_id,
                           thread_id=thread.thread_id, content='hello')
    return svc, run


def grant(svc, run, tool='send_message', scope='*', **kwargs):
    return svc.grant_tool(tenant_id=run.tenant_id, user_id=run.user_id,
                          assistant_id=run.assistant_id, tool_id=tool, scope=scope, **kwargs)


def request(svc, run, tool='send_message', scope='*'):
    return svc.request_tool_action(tenant_id=run.tenant_id, user_id=run.user_id,
        assistant_id=run.assistant_id, tool_id=tool, scope=scope, run_id=run.run_id,
        idempotency_key='repeat', request_payload={}, actor=run.user_id)


def test_cached_pending_action_still_requires_same_approval(context):
    svc, run = context
    grant(svc, run)
    with pytest.raises(ApprovalRequired) as first:
        request(svc, run)
    with pytest.raises(ApprovalRequired) as second:
        request(svc, run)
    assert first.value.approval_id == second.value.approval_id
    assert len(svc.store.approvals) == 1


def test_repeated_pending_model_action_never_executes(context):
    svc, run = context
    grant(svc, run)
    svc.execute_tool = Mock(return_value={'context': 'executed'})
    for _ in range(2):
        assert 'approval required' in svc._execute_gated(run, 'send_message', {}, 1000)
    svc.execute_tool.assert_not_called()


@pytest.mark.parametrize('change', ['revoke', 'expire'])
def test_cached_authorization_rechecks_current_grant(context, change):
    svc, run = context
    g = grant(svc, run, tool='browse')
    request(svc, run, tool='browse')
    if change == 'revoke':
        svc.revoke_grant(g.grant_id, tenant_id='t1', user_id='u1')
    else:
        g.expires_at = svc.now() - timedelta(seconds=1)
    assert request(svc, run, tool='browse').status == 'denied'


@pytest.mark.parametrize('decision', ['pending', 'denied', 'expired'])
def test_cached_approval_fails_closed(context, decision):
    svc, run = context
    grant(svc, run)
    with pytest.raises(ApprovalRequired) as pending:
        request(svc, run)
    approval = svc.store.get_approval(pending.value.approval_id)
    if decision == 'denied':
        svc.decide_approval(approval.approval_id, tenant_id='t1', user_id='u1',
                            decision='denied', actor='u1')
    elif decision == 'expired':
        approval.expires_at = svc.now() - timedelta(seconds=1)
    if decision == 'pending':
        with pytest.raises(ApprovalRequired):
            request(svc, run)
    else:
        assert request(svc, run).status == 'denied'


def test_approved_cached_action_still_requires_current_grant(context):
    svc, run = context
    g = grant(svc, run)
    with pytest.raises(ApprovalRequired) as pending:
        request(svc, run)
    svc.decide_approval(pending.value.approval_id, tenant_id='t1', user_id='u1',
                        decision='approved', actor='u1')
    assert request(svc, run).status == 'approved'
    svc.revoke_grant(g.grant_id, tenant_id='t1', user_id='u1')
    assert request(svc, run).status == 'denied'


def test_restricted_grant_cannot_authorize_wildcard_request(context):
    svc, run = context
    grant(svc, run, tool='browse', scope='https://allowed.example')
    assert request(svc, run, tool='browse').status == 'denied'
    assert request(svc, run, tool='browse', scope='https://allowed.example').status == 'authorized'


def test_revocation_and_audit_survive_restart(context, tmp_path):
    svc, run = context
    path = str(tmp_path / 'assistant.json')
    svc.store._persist_path = path
    g = grant(svc, run, tool='browse')
    svc.revoke_grant(g.grant_id, tenant_id='t1', user_id='u1')
    fresh = AssistantStore(persist_path=path)
    fresh.load_persisted()
    assert fresh.grants[g.grant_id].revoked_at is not None
    assert any(event['action'] == 'assistant.grant.revoked' and
               event['subject_id'] == g.grant_id for event in fresh.audit)


def test_cached_model_action_does_not_execute_after_revocation(context):
    svc, run = context
    g = grant(svc, run, tool='browse')
    svc.execute_tool = Mock(return_value={'context': 'executed'})
    assert svc._execute_gated(run, 'browse', {}, 1000) == 'executed'
    svc.revoke_grant(g.grant_id, tenant_id='t1', user_id='u1')
    assert 'denied' in svc._execute_gated(run, 'browse', {}, 1000)
    assert svc.execute_tool.call_count == 1


def test_missing_cached_approval_fails_closed(context):
    svc, run = context
    grant(svc, run)
    with pytest.raises(ApprovalRequired) as pending:
        request(svc, run)
    del svc.store.approvals[pending.value.approval_id]
    assert request(svc, run).status == 'denied'


@pytest.mark.parametrize('status', ['requested', 'unknown'])
def test_executor_rejects_non_authorized_action_states(context, status):
    svc, run = context
    grant(svc, run, tool='browse')
    action = request(svc, run, tool='browse')
    action.status = status
    svc.request_tool_action = Mock(return_value=action)
    svc.execute_tool = Mock(return_value={})
    assert 'denied' in svc._execute_gated(run, 'browse', {}, 1000)
    svc.execute_tool.assert_not_called()


@pytest.mark.parametrize('cached', [False, True])
def test_denial_audit_survives_immediate_restart(context, tmp_path, cached):
    svc, run = context
    path = str(tmp_path / 'denial.json')
    svc.store._persist_path = path
    if cached:
        g = grant(svc, run, tool='browse')
        request(svc, run, tool='browse')
        svc.revoke_grant(g.grant_id, tenant_id='t1', user_id='u1')
    denied = request(svc, run, tool='browse')
    assert denied.status == 'denied'
    fresh = AssistantStore(persist_path=path)
    fresh.load_persisted()
    assert any(event['action'] == 'assistant.tool.denied' and
               event['subject_id'] == denied.action_id for event in fresh.audit)


@pytest.mark.parametrize('tool,field,allowed,actual', [
    ('browse', 'url', 'https://allowed.example', 'https://evil.example'),
    ('run_shell', 'command', 'echo allowed', 'echo evil'),
    ('connector_request', 'connector_id', 'allowed-connector', 'evil-connector'),
    ('delegate', 'target_system', 'pi', 'other-system'),
    ('read_memory', 'namespace', 'private', 'shared'),
    ('write_memory', 'namespace', 'private', 'shared'),
    ('browse', 'url', 'https://example/' + 'a' * 184,
     'https://example/' + 'a' * 184 + '/evil'),
])
def test_model_scope_claim_cannot_override_executor_target(context, tool, field, allowed, actual):
    svc, run = context
    grant(svc, run, tool=tool, scope=allowed, preauthorized=True)
    svc.execute_tool = Mock(return_value={'context': 'executed'})
    assert 'denied' in svc._execute_gated(
        run, tool, {'scope': allowed, field: actual}, 1000)
    svc.execute_tool.assert_not_called()
    assert next(iter(svc.store.actions.values())).scope == actual


@pytest.mark.parametrize('namespace', [None, 'private', ['private']])
def test_memory_read_uses_one_namespace_and_defaults_private(context, namespace):
    svc, run = context
    grant(svc, run, tool='read_memory', scope='private')
    for ns, content in [('private', 'private fact'), ('shared', 'shared secret')]:
        svc.store_memory(tenant_id='t1', user_id='u1', assistant_id=run.assistant_id,
                         namespace=ns, content=content)
    args = {} if namespace is None else {'namespace': namespace}
    result = svc._execute_gated(run, 'read_memory', args, 1000)
    assert 'private fact' in result
    assert 'shared secret' not in result
    assert next(iter(svc.store.actions.values())).scope == 'private'


@pytest.mark.parametrize('namespace', [None, 'private', ['private']])
def test_memory_write_scope_matches_stored_namespace(context, namespace):
    svc, run = context
    grant(svc, run, tool='write_memory', scope='private')
    args = {'content': 'private fact'}
    if namespace is not None:
        args['namespace'] = namespace
    result = svc._execute_gated(run, 'write_memory', args, 1000)
    assert 'private fact' in result
    assert [m.namespace for m in svc.store.memory.values()] == ['private']
    assert next(iter(svc.store.actions.values())).scope == 'private'


@pytest.mark.parametrize('tool', ['read_memory', 'write_memory'])
@pytest.mark.parametrize('namespace', [['private', 'shared'], [], None, {}, ''])
def test_memory_rejects_ambiguous_namespace_before_execution(context, tool, namespace):
    svc, run = context
    grant(svc, run, tool=tool)
    svc.execute_tool = Mock(return_value={'context': 'executed'})
    assert 'denied' in svc._execute_gated(run, tool, {'namespace': namespace}, 1000)
    svc.execute_tool.assert_not_called()


@pytest.mark.parametrize('tool,field,target', [
    ('browse', 'url', 'https://allowed.example/' + 'a' * 220),
    ('run_shell', 'command', 'echo allowed'),
    ('connector_request', 'connector_id', 'allowed-connector'),
    ('delegate', 'target_system', 'pi'),
])
def test_executor_target_grant_allows_matching_call_despite_scope_claim(context, tool, field, target):
    svc, run = context
    grant(svc, run, tool=tool, scope=target, preauthorized=True)
    svc.execute_tool = Mock(return_value={'context': 'executed'})
    assert svc._execute_gated(run, tool, {field: target, 'scope': 'irrelevant'}, 1000) == 'executed'
    assert next(iter(svc.store.actions.values())).scope == target
    assert svc.execute_tool.call_args.kwargs['payload'][field] == target

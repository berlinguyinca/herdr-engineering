"""Analysis coordination uses real authorization, Postgres and the signed registry."""
# ruff: noqa: F811
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

import pytest

from herdr_engineering.control_plane import collab_mcp
from herdr_engineering.control_plane.collab import (
    CollabError,
    CollabService,
    StaticTokenAuthenticator,
)
from tests.control_plane.test_skill_registry import agent, env, metadata, user  # noqa: F401


@pytest.fixture
def analysis(env):
    from herdr_engineering.control_plane.collab_analysis import CollabAnalysis
    registry, run, conn, *_ = env
    run(conn.execute("""
        INSERT INTO collab_agent_room_attachments(room_id,agent_id,actions)
            VALUES ('room_one','agent_alice',ARRAY['read','execute']);
        INSERT INTO collab_rooms(room_id,name,owner_id) VALUES ('room_other','Other','alice');
        INSERT INTO collab_room_uploads(upload_id,room_id,created_by,status)
            VALUES ('unfinished','room_one','alice','verifying');
        INSERT INTO collab_room_files(file_id,room_id,uploaded_by,relative_path,storage_key,size_bytes,sha256,status,upload_id) VALUES
            ('source','room_one','alice','input.csv','SECRET/source',3,repeat('a',64),'available',NULL),
            ('chart','room_one','alice','chart.svg','SECRET/chart',8,repeat('b',64),'available',NULL),
            ('foreign','room_other','alice','hidden.csv','SECRET/foreign',3,repeat('c',64),'available',NULL),
            ('pending','room_one','alice','pending.csv','SECRET/pending',3,repeat('d',64),'verifying',NULL),
            ('staged','room_one','alice','staged.csv','SECRET/staged',3,repeat('e',64),'available','unfinished');
    """))
    return CollabAnalysis(registry), run, conn


def request(analysis, **kwargs):
    service, run, _ = analysis
    return run(service.create_analysis_request(agent(), 'room_one', 'Plot the TIC', ['source'], **kwargs))


def output(**changes):
    return {'outputs': [{'file_id': 'chart', 'kind': 'chart'}],
            'tool': {'name': 'tic-plot', 'version': '1.0', 'sha256': 'f' * 64},
            'skill': None, 'parameters': {'threshold': 2},
            'runtime': {'name': 'python', 'version': '3.12'}, **changes}


def artifact(analysis):
    service, run, _ = analysis
    req = request(analysis)
    return run(service.record_analysis_output(req.request_id, output(), principal=agent()))


def test_requests_resolve_only_safe_authorized_references_and_survive_restart(analysis, env):
    from herdr_engineering.control_plane.collab_analysis import CollabAnalysis
    service, run, _ = analysis
    req = request(analysis)
    assert req.files == [{'file_id': 'source', 'sha256': 'a' * 64, 'size_bytes': 3}]
    assert req.execution == 'harness_only' and req.agent_id == 'agent_alice'
    assert req.owner_id == 'alice' and req.requested_by == 'agent:agent_alice'
    assert req.prompt == 'Plot the TIC' and req.created_at
    assert 'SECRET' not in json.dumps(asdict(req))
    assert run(CollabAnalysis(env[0]).get_analysis_request(agent(), req.request_id)) == req


@pytest.mark.parametrize('files', [['foreign'], ['pending'], ['staged'], ['missing'], ['source', 'foreign']])
def test_unauthorized_or_unpublished_files_fail_atomically(analysis, files):
    service, run, conn = analysis
    with pytest.raises(CollabError) as error:
        run(service.create_analysis_request(agent(), 'room_one', 'plot', files))
    assert error.value.status == 404
    assert run(conn.fetchval('SELECT count(*) FROM collab_analysis_requests')) == 0


@pytest.mark.parametrize('files', [[], ['source', 'source'], 'source', [None], [{}]])
def test_malformed_file_selection_is_rejected(analysis, files):
    service, run, _ = analysis
    with pytest.raises(CollabError) as error:
        run(service.create_analysis_request(agent(), 'room_one', 'plot', files))
    assert error.value.status == 400


@pytest.mark.parametrize('sql', [
    "UPDATE collab_users SET status='disabled' WHERE user_id='alice'",
    "UPDATE collab_agents SET status='revoked' WHERE agent_id='agent_alice'",
    "UPDATE collab_agent_room_attachments SET actions=ARRAY['read'] WHERE agent_id='agent_alice'",
    "UPDATE collab_agent_room_attachments SET expires_at=1 WHERE agent_id='agent_alice'",
    "UPDATE collab_room_memberships SET expires_at=1 WHERE user_id='alice'",
    "UPDATE collab_rooms SET status='archiving' WHERE room_id='room_one'",
])
def test_current_authority_is_required_at_output_time(analysis, sql):
    service, run, conn = analysis
    req = request(analysis)
    run(conn.execute(sql))
    with pytest.raises(CollabError):
        run(service.record_analysis_output(req.request_id, output(), principal=agent()))
    assert run(conn.fetchval('SELECT count(*) FROM collab_analysis_artifacts')) == 0


def test_outputs_preserve_server_provenance_and_are_idempotent(analysis, env):
    service, run, conn = analysis
    req = request(analysis)
    result = run(service.record_analysis_output(req.request_id, output(), principal=agent()))
    assert result.provenance['file_ids'] == ['source']
    assert result.provenance['source_files'] == req.files
    assert result.provenance['request_id'] == req.request_id
    assert result.provenance['agent_id'] == 'agent_alice'
    assert result.provenance['tool'] == output()['tool']
    assert result.provenance['parameters'] == {'threshold': 2}
    assert result.provenance['runtime'] == {'name': 'python', 'version': '3.12'}
    assert result.provenance['requested_at'] == req.created_at
    assert result.provenance['recorded_at'] == result.created_at
    assert result.outputs == [{'file_id': 'chart', 'sha256': 'b' * 64, 'size_bytes': 8, 'kind': 'chart'}]
    assert run(service.record_analysis_output(req.request_id, output(), principal=agent())) == result
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_drafts')) == 0
    assert run(conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE action='analysis.recorded'")) == 1
    assert env[3].objects == {}  # no generated code execution or package writes
    with pytest.raises(CollabError) as error:
        run(service.record_analysis_output(req.request_id, output(parameters={'threshold': 3}), principal=agent()))
    assert error.value.status == 409


@pytest.mark.parametrize('principal', [user(), agent('bob')])
def test_only_assigned_registered_agent_can_record_output(analysis, principal):
    service, run, _ = analysis
    req = request(analysis)
    with pytest.raises(CollabError):
        run(service.record_analysis_output(req.request_id, output(), principal=principal))


@pytest.mark.parametrize('changes', [
    {'file_ids': ['foreign']}, {'agent_id': 'agent_bob'}, {'runtime': {}},
    {'outputs': [{'file_id': 'foreign', 'kind': 'chart'}]},
    {'outputs': [{'file_id': 'chart', 'kind': 'chart', 'url': 'https://secret'}]},
    {'outputs': [{'file_id': 'source', 'kind': 'chart'}]},
    {'tool': {'name': 'plot', 'version': '1', 'sha256': 'invalid'}},
    {'skill': {'skill_id': 'absent', 'version': '1.0'}},
])
def test_manifest_cannot_forge_provenance_or_supply_arbitrary_objects(analysis, changes):
    service, run, conn = analysis
    req = request(analysis)
    with pytest.raises(CollabError):
        run(service.record_analysis_output(req.request_id, output(**changes), principal=agent()))
    assert run(conn.fetchval('SELECT count(*) FROM collab_analysis_artifacts')) == 0


def test_chat_refinement_links_immutable_parent_and_explicit_files(analysis):
    service, run, _ = analysis
    first = artifact(analysis)
    refined = run(service.create_analysis_request(user(), 'room_one', 'Use a log scale', ['source', 'chart'],
                                                 target_agent_id='agent_alice', parent_artifact_id=first.artifact_id))
    assert refined.parent_artifact_id == first.artifact_id
    assert refined.prompt == 'Use a log scale' and refined.requested_by == 'user:alice'
    assert run(service.get_analysis_artifact(user(), first.artifact_id)) == first
    with pytest.raises(CollabError):
        run(service.create_analysis_request(user('bob'), 'room_one', 'steal', ['source'],
                                             target_agent_id='agent_bob', parent_artifact_id=first.artifact_id))


@pytest.mark.parametrize('scope', ['private', 'room'])
def test_explicit_human_promotion_creates_only_draft_with_bound_provenance(analysis, env, scope):
    service, run, conn = analysis
    result = artifact(analysis)
    data = metadata(provenance={'file_ids': ['forged']})
    draft = run(service.promote_analysis_to_skill(user(), result.artifact_id, scope, data))
    assert run(service.promote_analysis_to_skill(user(), result.artifact_id, scope, data)) == draft
    row = run(conn.fetchrow('SELECT * FROM collab_skill_drafts WHERE draft_id=$1', draft.draft_id))
    manifest = json.loads(row['manifest'])
    assert manifest['provenance']['file_ids'] == ['source']
    assert manifest['provenance']['artifact_id'] == result.artifact_id
    assert manifest['provenance']['requested_scope'] == scope
    assert manifest['scope'] == 'private' and manifest['execution'] == 'harness_only'
    assert row['target_room_id'] == ('room_one' if scope == 'room' else None)
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_versions')) == 0
    assert run(conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE action='analysis.promoted'")) == 1
    assert env[0].verify_skill_package(run(env[0]._load(row))).ok


@pytest.mark.parametrize(('principal', 'scope'), [(agent(), 'private'), (user('bob'), 'private'), (user(), 'shared')])
def test_promotion_requires_owner_human_and_scope_authority(analysis, principal, scope):
    service, run, conn = analysis
    result = artifact(analysis)
    with pytest.raises(CollabError):
        run(service.promote_analysis_to_skill(principal, result.artifact_id, scope, metadata()))
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_drafts')) == 0


def test_read_and_promotion_recheck_file_visibility(analysis):
    service, run, conn = analysis
    result = artifact(analysis)
    run(conn.execute("UPDATE collab_room_files SET status='failed' WHERE file_id='source'"))
    for operation in (service.get_analysis_artifact(user(), result.artifact_id),
                      service.promote_analysis_to_skill(user(), result.artifact_id, 'private', metadata())):
        with pytest.raises(CollabError):
            run(operation)


def test_mcp_roundtrip_keeps_provider_references_out_and_wraps_untrusted_content(analysis, env):
    backend, run, _ = analysis
    service = CollabService(StaticTokenAuthenticator({'alice': hashlib.sha256(b'token').hexdigest()}), analysis_service=backend, analysis_runner=run)
    credentials = service.connect_agent(user(), {'harness_type': 'python'})
    pr = service.authenticate('Agent ' + credentials['credential'], credentials['device_id'])
    run(analysis[2].execute("INSERT INTO collab_agents(agent_id,owner_id,name) VALUES ($1,'alice','Harness')", pr.id))
    run(analysis[2].execute("INSERT INTO collab_agent_room_attachments(room_id,agent_id,actions) VALUES ('room_one',$1,ARRAY['read','execute'])", pr.id))
    args = {'room_id': 'room_one', 'prompt': 'Plot', 'file_ids': ['source']}
    result = collab_mcp._call(service, pr, 'analysis_request', args)
    text = result['content'][0]['text']
    assert text.startswith(collab_mcp.UNTRUSTED) and 'SECRET' not in text
    req = json.loads(text.split('\n\n', 1)[1])
    out = collab_mcp._call(service, pr, 'analysis_record_output',
                          {'request_id': req['request_id'], 'output_manifest': output()})
    assert 'SECRET' not in str(out) and 'harness_only' in str(out)
    names = {t['name'] for t in collab_mcp.TOOLS}
    assert {'analysis_request', 'analysis_get_request', 'analysis_record_output', 'analysis_get_artifact'} <= names
    assert 'analysis_promote' not in names
    with pytest.raises(CollabError):
        collab_mcp._call(service, pr, 'analysis_request', {**args, 'principal': 'alice'})


def test_user_http_routes_create_refine_read_and_promote(analysis):
    backend, run, _ = analysis
    service = CollabService(StaticTokenAuthenticator({'alice': hashlib.sha256(b'token').hexdigest()}), analysis_service=backend, analysis_runner=run)
    status, req = service.dispatch('POST', ['rooms', 'room_one', 'analyses'], {},
                                  {'prompt': 'Plot', 'file_ids': ['source'], 'target_agent_id': 'agent_alice'}, 'Bearer token', None)
    assert status == 201
    result = run(backend.record_analysis_output(req['request_id'], output(), principal=agent()))
    status, got = service.dispatch('GET', ['analysis-artifacts', result.artifact_id], {}, {}, 'Bearer token', None)
    assert status == 200 and got['artifact_id'] == result.artifact_id
    data = metadata()
    data['files'] = {key: value.decode() for key, value in data['files'].items()}
    status, draft = service.dispatch('POST', ['analysis-artifacts', result.artifact_id, 'promote'], {},
                                    {'scope': 'private', 'metadata': data}, 'Bearer token', None)
    assert status == 201 and draft['draft_id']


def test_missing_backend_fails_closed():
    service = CollabService(StaticTokenAuthenticator({}))
    with pytest.raises(CollabError) as error:
        collab_mcp._call(service, agent(), 'analysis_request',
                         {'room_id': 'room_one', 'prompt': 'plot', 'file_ids': ['source']})
    assert error.value.status == 503


def test_archive_refuses_to_drop_unexported_analysis_provenance(analysis):
    from herdr_engineering.control_plane.collab_store import CollabStore
    _, run, conn = analysis
    request(analysis)
    with pytest.raises(ValueError, match='analysis'):
        run(CollabStore(conn).begin_archive('archive_test', 'room_one', 'alice'))
    assert run(conn.fetchval("SELECT status FROM collab_rooms WHERE room_id='room_one'")) == 'active'


@pytest.mark.parametrize('operation', ['request', 'artifact'])
def test_revocation_during_file_resolution_blocks_read(analysis, env, monkeypatch, operation):
    service, run, _ = analysis
    result = artifact(analysis)
    original = service._check_files

    async def resolve_then_revoke(*args):
        await original(*args)
        env[5].add(user().key)

    monkeypatch.setattr(service, '_check_files', resolve_then_revoke)
    with pytest.raises(CollabError):
        if operation == 'request':
            run(service.get_analysis_request(user(), result.request_id))
        else:
            run(service.get_analysis_artifact(user(), result.artifact_id))


@pytest.mark.parametrize('scope', [None, [], {}, 'everyone'])
def test_malformed_promotion_scope_is_client_error(analysis, scope):
    service, run, _ = analysis
    result = artifact(analysis)
    with pytest.raises(CollabError) as error:
        run(service.promote_analysis_to_skill(user(), result.artifact_id, scope, metadata()))
    assert error.value.status == 400


def test_published_skill_provenance_pins_verified_package_digest(analysis, env):
    service, run, conn = analysis
    registry = env[0]
    draft = run(registry.create_skill_draft(user(), metadata()))
    version = run(registry.publish_skill(user(), draft.draft_id, 'private'))
    req = request(analysis)
    result = run(service.record_analysis_output(req.request_id, output(
        skill={'skill_id': version.skill_id, 'version': version.version}), principal=agent()))
    assert result.provenance['skill'] == {'skill_id': version.skill_id, 'version': '1.0.0', 'sha256': version.sha256}
    assert run(conn.fetchval('SELECT count(*) FROM collab_analysis_artifacts')) == 1


def test_shared_promotion_requires_configured_maintainer_and_keeps_draft_private(analysis, env):
    service, run, conn = analysis
    env[0].maintainers = frozenset({'alice'})
    result = artifact(analysis)
    draft = run(service.promote_analysis_to_skill(user(), result.artifact_id, 'shared', metadata()))
    row = run(conn.fetchrow('SELECT scope,status FROM collab_skills WHERE skill_id=$1', draft.skill_id))
    assert dict(row) == {'scope': 'private', 'status': 'draft'}


def test_contributor_cannot_promote_room_skill(analysis):
    service, run, conn = analysis
    result = artifact(analysis)
    run(conn.execute("UPDATE collab_room_memberships SET role='contributor' WHERE user_id='alice'"))
    with pytest.raises(CollabError):
        run(service.promote_analysis_to_skill(user(), result.artifact_id, 'room', metadata()))


def test_output_and_request_records_are_immutable_but_owner_scrub_cascades(analysis):
    import asyncpg
    service, run, conn = analysis
    result = artifact(analysis)
    for table in ('requests', 'artifacts'):
        with pytest.raises(asyncpg.CheckViolationError):
            run(conn.execute(f"UPDATE collab_analysis_{table} SET record='{{}}'::jsonb"))
    # Copy returned data cannot alter stored provenance.
    result.provenance['file_ids'].append('forged')
    assert run(service.get_analysis_artifact(user(), result.artifact_id)).provenance['file_ids'] == ['source']
    run(conn.execute("DELETE FROM collab_users WHERE user_id='alice'"))
    for table in ('requests', 'artifacts', 'promotions'):
        assert run(conn.fetchval(f'SELECT count(*) FROM collab_analysis_{table}')) == 0


def test_changed_file_hash_is_not_silently_rewritten_in_provenance(analysis):
    service, run, conn = analysis
    req = request(analysis)
    run(conn.execute("UPDATE collab_room_files SET sha256=repeat('c',64) WHERE file_id='source'"))
    with pytest.raises(CollabError) as error:
        run(service.record_analysis_output(req.request_id, output(), principal=agent()))
    assert error.value.status == 409


def test_concurrent_identical_outputs_create_one_artifact_and_audit(analysis, env):
    import asyncio
    import copy
    import os

    import asyncpg

    from herdr_engineering.control_plane.collab_analysis import CollabAnalysis

    service, run, conn = analysis
    req = request(analysis)

    async def race():
        other = await asyncpg.connect(os.environ['HERDR_E2E_PG_DSN'])
        try:
            schema = await conn.fetchval('SELECT current_schema()')
            await other.execute(f'SET search_path TO {schema}')
            registry = copy.copy(env[0])
            registry.conn = other
            return await asyncio.gather(
                service.record_analysis_output(req.request_id, output(), principal=agent()),
                CollabAnalysis(registry).record_analysis_output(req.request_id, output(), principal=agent()))
        finally:
            await other.close()

    first, second = run(race())
    assert first == second
    assert run(conn.fetchval('SELECT count(*) FROM collab_analysis_artifacts')) == 1
    assert run(conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE action='analysis.recorded'")) == 1


def test_backend_failure_is_sanitized_before_mcp_response(analysis):
    backend, run, conn = analysis
    service = CollabService(StaticTokenAuthenticator({}), analysis_service=backend, analysis_runner=run)
    run(conn.execute('DROP TABLE collab_analysis_requests CASCADE'))
    with pytest.raises(CollabError) as error:
        collab_mcp._call(service, agent(), 'analysis_get_request', {'request_id': 'private-id'})
    assert error.value.status == 503
    assert 'collab_analysis_requests' not in error.value.message


def test_service_rejects_unmigrated_event_rooms(analysis):
    backend, run, _ = analysis
    service = CollabService(StaticTokenAuthenticator({'alice': 'digest'}), analysis_service=backend, analysis_runner=run)
    service._rooms['room_one'] = {'id': 'room_one'}
    with pytest.raises(CollabError) as error:
        run(service.create_analysis_request(user(), 'room_one', 'plot', ['source'], target_agent_id='agent_alice'))
    assert error.value.status == 409

"""Storage contracts against the same opt-in Postgres DSN as test_e2e_live_db.

Each test owns a schema; no public tables or other test data are dropped.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest

from herdr_engineering.control_plane.db import Migrator

MIGRATIONS = Path(__file__).parents[2] / 'deploy/control-plane/migrations'
TABLES = {
    'users', 'rooms', 'room_memberships', 'room_invitations', 'agents',
    'agent_room_attachments', 'agent_permissions', 'agent_access_requests',
    'messages', 'session_logs', 'room_uploads', 'room_upload_parts', 'room_files',
    'room_archives', 'skills', 'skill_versions', 'skill_installations', 'audit_events',
}


def test_migration_and_store_are_present():
    assert (MIGRATIONS / '0003_collaboration_data_platform.sql').is_file()
    assert importlib.util.find_spec('herdr_engineering.control_plane.collab_store')


@pytest.fixture
def database():
    dsn = os.environ.get('HERDR_E2E_PG_DSN')
    if not dsn:
        pytest.skip('set HERDR_E2E_PG_DSN to a throwaway Postgres')
    import asyncpg

    schema = 'collab_test_' + uuid4().hex
    with asyncio.Runner() as runner:
        conn = runner.run(asyncpg.connect(dsn))
        try:
            runner.run(conn.execute(f'CREATE SCHEMA {schema}'))
            runner.run(conn.execute(f'SET search_path TO {schema}'))
            runner.run(Migrator(conn).apply(str(MIGRATIONS)))
            yield conn, runner.run
        finally:
            runner.run(conn.execute('SET search_path TO public'))
            runner.run(conn.execute(f'DROP SCHEMA {schema} CASCADE'))
            runner.run(conn.close())


@pytest.fixture
def seeded(database):
    conn, run = database
    run(conn.execute("""
        INSERT INTO collab_users(user_id, status) VALUES ('user_owner', 'approved'), ('user_member', 'approved');
        INSERT INTO collab_rooms(room_id, name, owner_id) VALUES ('room_one', 'One', 'user_owner'), ('room_two', 'Two', 'user_owner');
        INSERT INTO collab_room_memberships(room_id, user_id, status, role, can_upload)
            VALUES ('room_one', 'user_owner', 'approved', 'owner', true), ('room_one', 'user_member', 'approved', 'contributor', true);
        INSERT INTO collab_room_invitations(invitation_id, room_id, user_id, invited_by)
            VALUES ('invite_one', 'room_one', 'user_member', 'user_owner');
        INSERT INTO collab_agents(agent_id, owner_id, name) VALUES ('agent_one', 'user_member', 'Agent');
        INSERT INTO collab_agent_room_attachments(room_id, agent_id) VALUES ('room_one', 'agent_one');
        INSERT INTO collab_agent_permissions(room_id, agent_id, user_id) VALUES ('room_one', 'agent_one', 'user_member');
        INSERT INTO collab_agent_access_requests(request_id, room_id, agent_id, user_id) VALUES ('request_one', 'room_one', 'agent_one', 'user_member');
        INSERT INTO collab_messages(message_id, room_id, author_user_id, text) VALUES ('message_one', 'room_one', 'user_member', 'private');
        INSERT INTO collab_session_logs(log_id, room_id, author_user_id, content) VALUES ('log_one', 'room_one', 'user_member', 'private');
        INSERT INTO collab_room_uploads(upload_id, room_id, created_by, status) VALUES ('upload_one', 'room_one', 'user_member', 'created');
        INSERT INTO collab_room_upload_parts(upload_id, relative_path, part_number, size_bytes)
            VALUES ('upload_one', 'data.csv', 1, 12);
        INSERT INTO collab_room_files(file_id, room_id, upload_id, uploaded_by, relative_path, storage_key, size_bytes, sha256, status)
            VALUES ('file_one', 'room_one', 'upload_one', 'user_member', 'data.csv', 'opaque/blob', 12, repeat('a', 64), 'available');
        INSERT INTO collab_room_archives(archive_id, original_room_id, live_room_id, requested_by)
            VALUES ('archive_one', 'room_one', 'room_one', 'user_member');
        INSERT INTO collab_skills(skill_id, owner_id, room_id, name, scope) VALUES ('skill_one', 'user_member', 'room_one', 'Plot', 'room');
        INSERT INTO collab_skill_versions(skill_id, version, storage_key, sha256) VALUES ('skill_one', '1.0', 'skills/opaque', repeat('b', 64));
        INSERT INTO collab_skill_installations(installation_id, skill_id, version, agent_id, user_id)
            VALUES ('install_one', 'skill_one', '1.0', 'agent_one', 'user_member');
        INSERT INTO collab_audit_events(event_id, actor_id, action, entity_type, entity_id)
            VALUES ('event_one', 'user_member', 'created', 'room', 'room_one');
    """))
    return conn, run


def test_all_tables_coexist_with_control_plane_and_migration_is_idempotent(database):
    conn, run = database
    tables = {r['tablename'] for r in run(conn.fetch(
        'SELECT tablename FROM pg_tables WHERE schemaname = current_schema()'))}
    assert {'collab_' + name for name in TABLES} <= tables
    assert {'agents', 'audit_events', 'fabric_events'} <= tables
    assert run(Migrator(conn).apply(str(MIGRATIONS))) == []


@pytest.mark.parametrize('sql', [
    "INSERT INTO collab_room_memberships(room_id,user_id) VALUES ('room_one','user_member')",
    "INSERT INTO collab_agent_room_attachments(room_id,agent_id) VALUES ('room_one','agent_one')",
    "INSERT INTO collab_agent_permissions(room_id,agent_id,user_id) VALUES ('room_one','agent_one','user_member')",
    "INSERT INTO collab_agent_access_requests(request_id,room_id,agent_id,user_id) VALUES ('another','room_one','agent_one','user_member')",
    "INSERT INTO collab_room_upload_parts(upload_id,relative_path,part_number) VALUES ('upload_one','data.csv',1)",
    "INSERT INTO collab_skill_versions(skill_id,version,storage_key,sha256) VALUES ('skill_one','1.0','other',repeat('c',64))",
    "INSERT INTO collab_room_files(file_id,room_id,relative_path,storage_key,size_bytes) VALUES ('another','room_one','other','opaque/blob',0)",
    "INSERT INTO collab_audit_events(event_id,action,entity_type,entity_id) VALUES ('event_one','created','room','room_one')",
])
def test_unique_constraints_reject_duplicate_access_and_object_records(seeded, sql):
    import asyncpg
    conn, run = seeded
    with pytest.raises(asyncpg.UniqueViolationError):
        run(conn.execute(sql))


@pytest.mark.parametrize(('table', 'column'), [
    ('users', 'status'), ('rooms', 'status'), ('room_memberships', 'status'),
    ('room_memberships', 'role'), ('room_invitations', 'status'), ('agents', 'status'),
    ('agent_permissions', 'status'), ('agent_access_requests', 'status'),
    ('room_uploads', 'status'), ('room_upload_parts', 'status'), ('room_files', 'status'),
    ('room_archives', 'status'), ('skills', 'scope'), ('skills', 'status'),
    ('skill_versions', 'status'), ('skill_installations', 'status'),
])
def test_status_and_scope_checks(seeded, table, column):
    import asyncpg
    conn, run = seeded
    with pytest.raises(asyncpg.CheckViolationError):
        run(conn.execute(f"UPDATE collab_{table} SET {column} = 'invalid'"))


@pytest.mark.parametrize('sql', [
    "INSERT INTO collab_room_memberships(room_id,user_id) VALUES ('missing','user_owner')",
    "INSERT INTO collab_agent_permissions(room_id,agent_id,user_id) VALUES ('room_two','agent_one','user_owner')",
    "UPDATE collab_room_files SET room_id = 'room_two'",
    "UPDATE collab_room_upload_parts SET upload_id = 'missing'",
    "UPDATE collab_skill_installations SET version = 'missing'",
])
def test_foreign_keys_reject_orphans_and_cross_room_records(seeded, sql):
    import asyncpg
    conn, run = seeded
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        run(conn.execute(sql))


@pytest.mark.parametrize('parent', ['membership', 'attachment', 'agent'])
def test_removing_access_parent_cascades_grants_and_requests(seeded, parent):
    conn, run = seeded
    sql = {
        'membership': "DELETE FROM collab_room_memberships WHERE user_id = 'user_member'",
        'attachment': 'DELETE FROM collab_agent_room_attachments',
        'agent': 'DELETE FROM collab_agents',
    }[parent]
    run(conn.execute(sql))
    for table in ('agent_permissions', 'agent_access_requests'):
        assert run(conn.fetchval(f'SELECT count(*) FROM collab_{table}')) == 0


def test_user_deletion_scrubs_content_and_access_but_preserves_room_files(seeded):
    conn, run = seeded
    run(conn.execute("DELETE FROM collab_users WHERE user_id = 'user_member'"))
    for table in ('agents', 'agent_permissions', 'agent_access_requests', 'room_invitations',
                  'messages', 'session_logs', 'skills', 'skill_versions', 'skill_installations'):
        assert run(conn.fetchval(f'SELECT count(*) FROM collab_{table}')) == 0
    file = run(conn.fetchrow('SELECT * FROM collab_room_files'))
    assert file['storage_key'] == 'opaque/blob' and file['uploaded_by'] is None
    assert run(conn.fetchval('SELECT created_by FROM collab_room_uploads')) is None
    assert run(conn.fetchval('SELECT actor_id FROM collab_audit_events')) is None
    assert run(conn.fetchval('SELECT requested_by FROM collab_room_archives')) is None
    run(conn.execute("DELETE FROM collab_users WHERE user_id = 'user_owner'"))
    assert run(conn.fetchval("SELECT owner_id FROM collab_rooms WHERE room_id = 'room_one'")) is None
    assert run(conn.fetchval('SELECT count(*) FROM collab_room_files')) == 1


def test_room_deletion_cascades_live_records_but_preserves_archive_and_audit(seeded):
    conn, run = seeded
    run(conn.execute("DELETE FROM collab_rooms WHERE room_id = 'room_one'"))
    for table in ('room_memberships', 'room_invitations', 'agent_room_attachments',
                  'agent_permissions', 'agent_access_requests', 'messages', 'session_logs',
                  'room_uploads', 'room_upload_parts', 'room_files', 'skills', 'skill_versions',
                  'skill_installations'):
        assert run(conn.fetchval(f'SELECT count(*) FROM collab_{table}')) == 0
    archive = run(conn.fetchrow('SELECT * FROM collab_room_archives'))
    assert archive['original_room_id'] == 'room_one' and archive['live_room_id'] is None
    assert run(conn.fetchval('SELECT count(*) FROM collab_audit_events')) == 1
    assert run(conn.fetchval('SELECT count(*) FROM collab_agents')) == 1


def test_store_round_trips_idempotently_and_audits_once(seeded):
    from herdr_engineering.control_plane.collab_store import CollabStore
    conn, run = seeded
    store = CollabStore(conn)
    calls = [
        ('create_room', dict(room_id='room_new', name="Room ' parameter", owner_id='user_owner', project_id='project_legacy')),
        ('append_message', dict(message_id='message_new', room_id='room_new', author_user_id='user_owner', text="hello ' world")),
        ('register_upload', dict(upload_id='upload_new', room_id='room_new', created_by='user_owner', manifest={'files': [{'path': 'a.csv', 'size': 3}]})),
        ('record_archive', dict(archive_id='archive_new', room_id='room_new', requested_by='user_owner', manifest_key='env/test/archives/new/manifest.json')),
    ]
    for method, kwargs in calls:
        first = run(getattr(store, method)(**kwargs))
        assert first == run(getattr(CollabStore(conn), method)(**kwargs))
    assert run(conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE event_id != 'event_one'")) == 4
    assert run(store.create_room(**calls[0][1])).name == "Room ' parameter"
    assert run(store.append_message(**calls[1][1])).text == "hello ' world"
    assert run(store.register_upload(**calls[2][1])).manifest == {'files': [{'path': 'a.csv', 'size': 3}]}
    assert run(store.record_archive(**calls[3][1])).status == 'archiving'
    with pytest.raises(ValueError, match='conflict'):
        run(store.create_room(**(calls[0][1] | {'name': 'different'})))
    assert run(conn.fetchval("SELECT name FROM collab_rooms WHERE room_id='room_new'")) == "Room ' parameter"


def test_store_write_and_audit_are_atomic(seeded):
    import asyncpg

    from herdr_engineering.control_plane.collab_store import CollabStore
    conn, run = seeded
    run(conn.execute("ALTER TABLE collab_audit_events ADD CONSTRAINT reject_new CHECK (action != 'room.created')"))
    with pytest.raises(asyncpg.CheckViolationError):
        run(CollabStore(conn).create_room(room_id='room_rollback', name='Rollback', owner_id='user_owner'))
    assert run(conn.fetchval("SELECT count(*) FROM collab_rooms WHERE room_id='room_rollback'")) == 0


def test_room_files_are_hidden_until_upload_and_file_are_available(seeded):
    from herdr_engineering.control_plane.collab_store import CollabStore
    conn, run = seeded
    store = CollabStore(conn)
    for status in ('created', 'uploading', 'verifying', 'failed'):
        run(conn.execute('UPDATE collab_room_uploads SET status=$1', status))
        assert run(store.list_room_files('room_one')) == []
    run(conn.execute("UPDATE collab_room_uploads SET status='available'"))
    files = run(store.list_room_files('room_one'))
    assert [f.file_id for f in files] == ['file_one']
    assert files[0].size_bytes == 12 and files[0].sha256 == 'a' * 64
    assert run(store.list_room_files('room_two')) == []
    run(conn.execute("UPDATE collab_room_files SET status='verifying'"))
    assert run(store.list_room_files('room_one')) == []


def test_archive_retry_survives_live_room_deletion(seeded):
    from herdr_engineering.control_plane.collab_store import CollabStore
    conn, run = seeded
    store = CollabStore(conn)
    kwargs = dict(archive_id='archive_retry', room_id='room_one', requested_by='user_owner')
    run(store.record_archive(**kwargs))
    run(conn.execute("DELETE FROM collab_rooms WHERE room_id='room_one'"))
    archive = run(store.record_archive(**kwargs))
    assert archive.original_room_id == 'room_one' and archive.live_room_id is None
    assert run(conn.fetchval("SELECT count(*) FROM collab_rooms WHERE room_id='room_one'")) == 0
    assert run(conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE entity_id='archive_retry'")) == 1


def test_upload_retry_preserves_progress_and_rejects_changed_manifest(seeded):
    from herdr_engineering.control_plane.collab_store import CollabStore
    conn, run = seeded
    store = CollabStore(conn)
    kwargs = dict(upload_id='upload_retry', room_id='room_one', created_by='user_owner', manifest={'files': []})
    run(store.register_upload(**kwargs))
    run(conn.execute("UPDATE collab_room_uploads SET status='verifying' WHERE upload_id='upload_retry'"))
    assert run(store.register_upload(**kwargs)).status == 'verifying'
    with pytest.raises(ValueError, match='conflict'):
        run(store.register_upload(**(kwargs | {'manifest': {'files': ['different']}})))


def test_concurrent_room_retry_inserts_one_row_and_audit(database):
    import asyncpg

    from herdr_engineering.control_plane.collab_store import CollabStore
    conn, run = database
    run(conn.execute("INSERT INTO collab_users(user_id,status) VALUES ('user_owner','approved')"))
    schema = run(conn.fetchval('SELECT current_schema()'))

    async def concurrent_create():
        other = await asyncpg.connect(os.environ['HERDR_E2E_PG_DSN'])
        try:
            await other.execute(f'SET search_path TO {schema}')
            kwargs = dict(room_id='room_race', name='Race', owner_id='user_owner')
            return await asyncio.gather(
                CollabStore(conn).create_room(**kwargs), CollabStore(other).create_room(**kwargs))
        finally:
            await other.close()

    first, second = run(concurrent_create())
    assert first == second
    assert run(conn.fetchval('SELECT count(*) FROM collab_rooms')) == 1
    assert run(conn.fetchval('SELECT count(*) FROM collab_audit_events')) == 1


@pytest.mark.parametrize('expires_at', [None, 1_800_000_005.125])
def test_membership_expiry_round_trip_preserves_core_boundary(seeded, expires_at):
    from dataclasses import asdict

    from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
    from herdr_engineering.control_plane.collab_store import CollabStore

    conn, run = seeded
    kwargs = dict(membership_id='membership_roundtrip', room_id='room_two', user_id='user_member',
                  status='approved', role='guest', can_upload=False, expires_at=expires_at, joined_seq=17)
    first = run(CollabStore(conn).create_membership(**kwargs))
    restored = run(CollabStore(conn).get_membership('room_two', 'user_member'))
    assert restored == first and restored.expires_at == expires_at and restored.joined_seq == 17
    assert run(CollabStore(conn).create_membership(**kwargs)) == first
    room = {'members': {'user_member': asdict(restored)}}
    now = [1_800_000_005.124]
    service = CollabService(StaticTokenAuthenticator({}), now=lambda: now[0])
    assert service._member(room, 'user_member') is not None
    now[0] = 1_800_000_005.125
    assert (service._member(room, 'user_member') is None) == (expires_at is not None)


def test_attachment_round_trip_preserves_effective_actions_expiry_and_visibility(seeded):
    from dataclasses import asdict

    from herdr_engineering.control_plane.collab import (
        CollabService,
        Principal,
        StaticTokenAuthenticator,
    )
    from herdr_engineering.control_plane.collab_store import CollabStore

    conn, run = seeded
    store = CollabStore(conn)
    member = run(store.create_membership(membership_id='membership_agent', room_id='room_two',
                 user_id='user_member', status='approved', role='viewer', joined_seq=20))
    kwargs = dict(room_id='room_two', agent_id='agent_one', actions=['read', 'execute'],
                  expires_at=1_800_000_005.0, granted_seq=25)
    first = run(store.create_attachment(**kwargs))
    restored = run(CollabStore(conn).get_attachment('room_two', 'agent_one'))
    assert restored == first and restored.status == 'active'
    assert restored.actions == ['read', 'execute'] and restored.granted_seq == 25
    assert run(store.create_attachment(**kwargs)) == first
    service = CollabService(StaticTokenAuthenticator({'user_member': 'digest'}), now=lambda: 1_800_000_004.0)
    service._agents['agent_one'] = {'status': 'active', 'owner': 'user_member'}
    room = {'members': {'user_member': asdict(member)}, 'grants': {'agent_one': asdict(restored)}}
    # Persisted role and grant restrictions intersect; first visibility is retained.
    assert service._effective(Principal('agent', 'agent_one', 'user_member'), room) == (frozenset({'read'}), 25)
    service._now = lambda: 1_800_000_005.0
    assert service._effective(Principal('agent', 'agent_one', 'user_member'), room) is None


def test_attachment_renewal_inputs_survive_before_housekeeping(seeded):
    from herdr_engineering.control_plane.collab_store import CollabStore

    conn, run = seeded
    store = CollabStore(conn)
    original = dict(room_id='room_two', agent_id='agent_one', actions=['read', 'execute'],
                    expires_at=1_800_000_005.0, granted_seq=25)
    run(store.create_attachment(**original))
    run(conn.execute("""
        INSERT INTO collab_room_memberships(room_id,user_id,status) VALUES ('room_two','user_owner','approved');
        INSERT INTO collab_agent_permissions(room_id,agent_id,user_id) VALUES ('room_two','agent_one','user_owner');
    """))
    # An expired grant may still be marked active before housekeeping. Consumers
    # must receive its expiry, not mistake it for a perpetual/current grant.
    expired = run(CollabStore(conn).get_attachment('room_two', 'agent_one'))
    assert expired.status == 'active' and expired.expires_at <= 1_800_000_006.0
    assert expired.actions == ['read', 'execute']
    # Model the core's committed expiry/revocation followed by approved renewal;
    # the persistence boundary must retain both inputs without reviving delegation.
    run(conn.execute("""
        UPDATE collab_agent_room_attachments SET status='expired' WHERE room_id='room_two';
        UPDATE collab_agent_permissions SET status='revoked' WHERE room_id='room_two';
    """))
    assert run(store.get_attachment('room_two', 'agent_one')).status == 'expired'
    run(conn.execute("""
        UPDATE collab_agent_room_attachments SET status='active', actions=ARRAY['read'],
            expires_at=1800000060, granted_seq=30 WHERE room_id='room_two';
    """))
    renewed = run(CollabStore(conn).get_attachment('room_two', 'agent_one'))
    assert renewed.actions == ['read'] and renewed.expires_at == 1_800_000_060.0
    assert renewed.granted_seq == 30 and renewed.status == 'active'
    assert run(store.create_attachment(**original)) == renewed
    assert run(conn.fetchval("SELECT status FROM collab_agent_permissions WHERE room_id='room_two'")) == 'revoked'
    assert run(conn.fetchval("SELECT status FROM collab_agent_permissions WHERE room_id='room_one'")) == 'approved'
    run(conn.execute("UPDATE collab_agent_room_attachments SET status='revoked' WHERE room_id='room_two'"))
    assert run(store.get_attachment('room_two', 'agent_one')).status == 'revoked'


@pytest.mark.parametrize('sql', [
    "UPDATE collab_agent_room_attachments SET actions=ARRAY['invite']",
    "UPDATE collab_agent_room_attachments SET actions=ARRAY[NULL]::text[]",
    "UPDATE collab_agent_room_attachments SET status='invalid'",
    "UPDATE collab_agent_room_attachments SET expires_at='Infinity'",
    "UPDATE collab_room_memberships SET expires_at='NaN'",
])
def test_authorization_input_constraints_fail_closed(seeded, sql):
    import asyncpg

    conn, run = seeded
    with pytest.raises(asyncpg.CheckViolationError):
        run(conn.execute(sql))


def test_creation_retry_uses_original_room_request_after_rename(seeded):
    from herdr_engineering.control_plane.collab_store import CollabStore

    conn, run = seeded
    original = dict(room_id='room_renamed', name='Original', owner_id='user_owner')
    run(CollabStore(conn).create_room(**original))
    run(conn.execute("UPDATE collab_rooms SET name='Renamed' WHERE room_id='room_renamed'"))
    assert run(CollabStore(conn).create_room(**original)).name == 'Renamed'
    # Even matching the CURRENT row is a different creation request.
    with pytest.raises(ValueError, match='conflict'):
        run(CollabStore(conn).create_room(**(original | {'name': 'Renamed'})))
    assert run(conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE entity_id='room_renamed'")) == 1


def test_creation_retry_uses_original_archive_request_after_manifest_update(seeded):
    from herdr_engineering.control_plane.collab_store import CollabStore

    conn, run = seeded
    original = dict(archive_id='archive_updated', room_id='room_one', requested_by='user_owner')
    run(CollabStore(conn).record_archive(**original))
    run(conn.execute("""UPDATE collab_room_archives SET manifest_key='archive/manifest.json',
        manifest_sha256=repeat('c',64), verified_at=now(), status='verified'
        WHERE archive_id='archive_updated'"""))
    archive = run(CollabStore(conn).record_archive(**original))
    assert archive.manifest_key == 'archive/manifest.json' and archive.status == 'verified'
    with pytest.raises(ValueError, match='conflict'):
        run(CollabStore(conn).record_archive(**(original | {'manifest_key': 'archive/manifest.json'})))
    assert run(conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE entity_id='archive_updated'")) == 1


def test_creation_retry_does_not_restore_deleted_user_attribution(seeded):
    from herdr_engineering.control_plane.collab_store import CollabStore

    conn, run = seeded
    original = dict(room_id='room_scrubbed', name='Room', owner_id='user_owner')
    run(CollabStore(conn).create_room(**original))
    run(conn.execute("DELETE FROM collab_users WHERE user_id='user_owner'"))
    assert run(CollabStore(conn).create_room(**original)).owner_id is None
    assert run(conn.fetchval("SELECT actor_id FROM collab_audit_events WHERE entity_id='room_scrubbed'")) is None


def test_creation_retry_rejects_rows_without_original_request_evidence(seeded):
    from herdr_engineering.control_plane.collab_store import CollabStore

    conn, run = seeded
    # Raw legacy rows cannot prove their immutable creation input from current data.
    with pytest.raises(ValueError, match='conflict'):
        run(CollabStore(conn).create_room(room_id='room_one', name='One', owner_id='user_owner'))


def test_upload_integrity_migration_repairs_old_claims_and_tolerates_other_manifests(database):
    import json

    from herdr_engineering.control_plane.collab_store import CollabStore

    conn, run = database
    # Exercise the actual upgrade SQL from the previous table shape, not only
    # an empty database. The enclosing fixture still owns/cleans this schema.
    run(conn.execute("""
        ALTER TABLE collab_room_files DROP CONSTRAINT collab_room_files_check;
        ALTER TABLE collab_room_files DROP COLUMN verified_parts;
        ALTER TABLE collab_room_files ADD CONSTRAINT collab_room_files_check CHECK (status!='available' OR sha256 IS NOT NULL);
        ALTER TABLE collab_audit_events DROP COLUMN actor_kind, DROP COLUMN actor_agent_id, DROP COLUMN outcome;
        INSERT INTO collab_users(user_id,status) VALUES ('owner','approved');
        INSERT INTO collab_rooms(room_id,name,owner_id) VALUES ('room_one','Room','owner');
    """))
    parts = [{'size_bytes': 1, 'sha256': 'a' * 64}]
    run(conn.execute("INSERT INTO collab_room_uploads(upload_id,room_id,created_by,status,manifest) VALUES ('upload_old','room_one','owner','available',$1::jsonb),('upload_other','room_one','owner','created',$2::jsonb)",
                     json.dumps({'schema_version': 1, 'cleanup_done': True, 'files': [{'relative_path': 'a.csv', 'parts': parts}]}),
                     json.dumps({'files': 'unrelated legacy metadata'})))
    run(conn.execute("INSERT INTO collab_room_files(file_id,room_id,upload_id,relative_path,storage_key,size_bytes,sha256,status) VALUES ('file_old','room_one','upload_old','a.csv','opaque/blob',1,repeat('b',64),'available')"))
    run(conn.execute((MIGRATIONS / '0005_collaboration_upload_integrity_audit.sql').read_text()))
    files = run(CollabStore(conn).list_room_files('room_one'))
    assert files[0].sha256 is None and files[0].verified_parts == parts
    assert run(CollabStore(conn).get_upload('upload_old')).manifest['cleanup_done'] is False

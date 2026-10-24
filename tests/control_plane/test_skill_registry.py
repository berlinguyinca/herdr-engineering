"""Registry contracts: real Postgres, byte-preserving S3 transport double."""
from __future__ import annotations

import asyncio
import copy
import io
import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from herdr_engineering.control_plane.collab import CollabError, Principal
from herdr_engineering.control_plane.db import Migrator
from herdr_engineering.control_plane.skill_registry import SkillRegistry


class S3:
    def __init__(self):
        self.objects = {}
        self.fail = False
        self.cleanup_failure = None

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch, **kwargs):
        assert IfNoneMatch == '*'
        if self.fail:
            raise RuntimeError('provider secret must not escape')
        if (Bucket, Key) in self.objects:
            raise ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
        self.objects[Bucket, Key] = Body

    def get_object(self, *, Bucket, Key):
        return {'Body': io.BytesIO(self.objects[Bucket, Key])}


    def get_bucket_versioning(self, *, Bucket):
        return {'Status': 'Enabled'} if self.cleanup_failure == 'versioned' else {}

    def delete_object(self, *, Bucket, Key):
        if self.cleanup_failure == 'delete_error':
            raise RuntimeError('provider secret')
        if self.cleanup_failure != 'silent_delete':
            self.objects.pop((Bucket, Key), None)

    def head_object(self, *, Bucket, Key):
        if self.cleanup_failure == 'head_error':
            raise ClientError({'Error': {'Code': 'AccessDenied'}}, 'HeadObject')
        if (Bucket, Key) not in self.objects:
            raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
        return {'ContentLength': len(self.objects[Bucket, Key])}


@pytest.fixture
def env():
    dsn = os.environ.get('HERDR_E2E_PG_DSN')
    if not dsn:
        pytest.skip('set HERDR_E2E_PG_DSN to a throwaway Postgres')
    import asyncpg
    schema = 'skill_test_' + uuid4().hex
    with asyncio.Runner() as runner:
        run = runner.run
        conn = run(asyncpg.connect(dsn))
        journal_conn = run(asyncpg.connect(dsn))
        try:
            run(conn.execute(f'CREATE SCHEMA {schema}'))
            run(conn.execute(f'SET search_path TO {schema}'))
            run(journal_conn.execute(f'SET search_path TO {schema}'))
            run(Migrator(conn).apply(str(Path(__file__).parents[2] / 'deploy/control-plane/migrations')))
            run(conn.execute("""
                INSERT INTO collab_users(user_id,status) VALUES
                    ('alice','approved'),('bob','approved'),('admin','approved'),
                    ('maintainer','approved'),('pending','pending');
                INSERT INTO collab_rooms(room_id,name,owner_id) VALUES ('room_one','One','alice');
                INSERT INTO collab_room_memberships(room_id,user_id,status,role) VALUES
                    ('room_one','alice','approved','owner'),('room_one','bob','approved','contributor');
                INSERT INTO collab_agents(agent_id,owner_id,name) VALUES
                    ('agent_alice','alice','Alice'),('agent_bob','bob','Bob');
                INSERT INTO collab_agent_room_attachments(room_id,agent_id,actions)
                    VALUES ('room_one','agent_bob',ARRAY['read']);
            """))
            s3, key, invalid = S3(), Ed25519PrivateKey.generate(), set()
            def registry():
                return SkillRegistry(conn, journal_conn=journal_conn, s3=s3, bucket='skills', environment='test',
                                     signing_key=key, key_id='test-key',
                                     trusted_keys={'test-key': key.public_key()},
                                     principal_valid=lambda p: p.key not in invalid,
                                     maintainers={'maintainer'})
            yield registry(), run, conn, s3, registry, invalid
        finally:
            run(journal_conn.close())
            run(conn.execute('SET search_path TO public'))
            run(conn.execute(f'DROP SCHEMA {schema} CASCADE'))
            run(conn.close())


def user(name='alice'):
    p = Principal('user', name, name)
    p.admin = name == 'admin'
    return p


def agent(name='alice'):
    return Principal('agent', 'agent_' + name, name)


def metadata(**changes):
    return {
        'name': 'Summarize CSV', 'version': '1.0.0', 'request_key': str(uuid4()),
        'files': {'SKILL.md': b'# CSV summary', 'scripts/run.py': b'raise RuntimeError("never execute")',
                  'fixtures/input.csv': b'a\n1\n', 'requirements.lock': b''},
        'input_schema': {'type': 'object'}, 'output_schema': {'type': 'object'},
        'capabilities': ['files:read'], 'supported_file_types': ['text/csv'],
        'fixtures': ['fixtures/input.csv'], 'tests': [],
        'dependency_lock': 'requirements.lock', 'provenance': {'file_ids': ['file_source']},
        **changes,
    }


def publish(env, scope='private', owner=None, **changes):
    registry, run, *_ = env
    owner = owner or user()
    if scope == 'room':
        changes.setdefault('room_id', 'room_one')
    draft = run(registry.create_skill_draft(owner, metadata(**changes)))
    version = run(registry.publish_skill(owner, draft.draft_id, scope))
    return draft, version


def test_signed_manifest_survives_restart_and_keeps_bytes_out_of_postgres(env):
    registry, run, conn, s3, restart, _ = env
    draft, version = publish(env)
    package = run(restart().get_skill_version(version.skill_id, version.version, principal=agent()))
    assert registry.verify_skill_package(package).ok
    assert package.manifest['schema_version'] == 1
    assert package.manifest['version'] == '1.0.0'
    assert package.manifest['scope'] == 'private'
    assert package.manifest['creator_id'] == 'alice'
    for field in ('input_schema', 'output_schema', 'capabilities', 'supported_file_types',
                  'fixtures', 'tests', 'dependency_lock', 'provenance'):
        assert package.manifest[field] == metadata()[field]
    assert package.files == metadata()['files']
    assert package.signature and package.key_id == 'test-key'
    assert package.sha256 in version.storage_key
    row = dict(run(conn.fetchrow('SELECT * FROM collab_skill_versions')))
    assert 'never execute' not in json.dumps(row, default=str)
    assert 'never execute' not in str(run(conn.fetch('SELECT * FROM collab_skill_drafts')))
    assert len(s3.objects) == 2
    assert run(conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE entity_type='skill'")) == 2
    assert draft.skill_id == version.skill_id


@pytest.mark.parametrize('principal', [user('bob'), user('admin'), agent('bob')])
def test_private_packages_are_not_visible_to_other_principals(env, principal):
    registry, run, *_ = env
    _, version = publish(env)
    with pytest.raises(CollabError) as error:
        run(registry.get_skill_version(version.skill_id, version.version, principal=principal))
    assert error.value.status == 404


def test_creator_can_review_private_package_but_agent_cannot_publish(env):
    registry, run, *_ = env
    draft, version = publish(env)
    assert run(registry.get_skill_version(version.skill_id, version.version, principal=user()))
    with pytest.raises(CollabError):
        run(registry.publish_skill(agent(), draft.draft_id, 'private'))
    with pytest.raises(CollabError):
        run(registry.create_skill_draft(agent(), metadata()))


@pytest.mark.parametrize('principal', [user('pending'), Principal('user', 'absent', 'absent')])
def test_unapproved_users_cannot_create_drafts(env, principal):
    registry, run, *_ = env
    with pytest.raises(CollabError):
        run(registry.create_skill_draft(principal, metadata()))


@pytest.mark.parametrize('owner', [user('admin'), user('maintainer')])
def test_admin_and_configured_maintainer_can_publish_shared(env, owner):
    registry, run, *_ = env
    _, version = publish(env, 'shared', owner)
    assert run(registry.get_skill_version(version.skill_id, version.version, principal=agent('bob')))


@pytest.mark.parametrize('scope,owner,room', [('shared', user(), None), ('room', user('bob'), 'room_one'),
                                           ('room', user('admin'), 'room_one')])
def test_publication_requires_scope_authority(env, scope, owner, room):
    registry, run, conn, *_ = env
    draft = run(registry.create_skill_draft(owner, metadata(room_id=room)))
    with pytest.raises(CollabError):
        run(registry.publish_skill(owner, draft.draft_id, scope))
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_versions')) == 0


def test_other_user_cannot_publish_or_revise_someones_draft(env):
    registry, run, *_ = env
    draft = run(registry.create_skill_draft(user(), metadata()))
    with pytest.raises(CollabError):
        run(registry.publish_skill(user('admin'), draft.draft_id, 'shared'))
    with pytest.raises(CollabError):
        run(registry.create_skill_draft(user('bob'), metadata(skill_id=draft.skill_id)))


@pytest.mark.parametrize('sql', [
    "UPDATE collab_room_memberships SET status='removed' WHERE user_id='bob'",
    "UPDATE collab_room_memberships SET expires_at=1 WHERE user_id='bob'",
    "UPDATE collab_agent_room_attachments SET status='revoked'",
    "UPDATE collab_agent_room_attachments SET expires_at=1",
    "UPDATE collab_agent_room_attachments SET actions='{}'",
    "UPDATE collab_agents SET status='revoked' WHERE agent_id='agent_bob'",
    "UPDATE collab_users SET status='disabled' WHERE user_id='bob'",
    "UPDATE collab_rooms SET status='archiving'",
])
def test_room_access_rechecks_membership_attachment_and_revocation(env, sql):
    registry, run, conn, *_ = env
    _, version = publish(env, 'room')
    assert run(registry.get_skill_version(version.skill_id, version.version, principal=agent('bob')))
    run(conn.execute(sql))
    with pytest.raises(CollabError):
        run(registry.get_skill_version(version.skill_id, version.version, principal=agent('bob')))


def test_room_members_can_read_but_unattached_agents_and_outsiders_cannot(env):
    registry, run, *_ = env
    _, version = publish(env, 'room')
    assert run(registry.get_skill_version(version.skill_id, version.version, principal=user('bob')))
    for p in (agent(), user('maintainer')):
        with pytest.raises(CollabError):
            run(registry.get_skill_version(version.skill_id, version.version, principal=p))


def test_live_principal_validator_and_owner_binding_are_required(env):
    registry, run, _, _, _, invalid = env
    _, version = publish(env)
    invalid.add(agent().key)
    for p in (agent(), Principal('agent', 'agent_bob', 'alice')):
        with pytest.raises(CollabError):
            run(registry.get_skill_version(version.skill_id, version.version, principal=p))


def test_publication_is_retryable_but_versions_and_scope_are_immutable(env):
    registry, run, conn, *_ = env
    draft, version = publish(env)
    assert run(registry.publish_skill(user(), draft.draft_id, 'private')) == version
    conflict = run(registry.create_skill_draft(user(), metadata(skill_id=version.skill_id)))
    with pytest.raises(CollabError):
        run(registry.publish_skill(user(), conflict.draft_id, 'private'))
    new = run(registry.create_skill_draft(user(), metadata(skill_id=version.skill_id, version='2.0.0', room_id='room_one')))
    with pytest.raises(CollabError):
        run(registry.publish_skill(user(), new.draft_id, 'room'))
    assert run(registry.publish_skill(user(), new.draft_id, 'private')).version == '2.0.0'
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_versions')) == 2


@pytest.mark.parametrize('sql', [
    "UPDATE collab_skill_versions SET sha256=repeat('a',64)",
    "UPDATE collab_skill_versions SET manifest='{}'",
    "UPDATE collab_skill_versions SET storage_key='other'",
    "UPDATE collab_skill_versions SET signature='forged'",
    "UPDATE collab_skills SET scope='shared'",
    "DELETE FROM collab_skill_versions",
])
def test_database_rejects_mutating_published_identity_or_content(env, sql):
    import asyncpg
    _, run, conn, *_ = env
    publish(env)
    with pytest.raises(asyncpg.CheckViolationError):
        run(conn.execute(sql))


def test_revocation_cannot_be_undone_and_parent_deletion_can_scrub_versions(env):
    import asyncpg
    registry, run, conn, *_ = env
    _, version = publish(env)
    run(conn.execute("UPDATE collab_skill_versions SET status='revoked'"))
    with pytest.raises(CollabError):
        run(registry.get_skill_version(version.skill_id, version.version, principal=agent()))
    with pytest.raises(asyncpg.CheckViolationError):
        run(conn.execute("UPDATE collab_skill_versions SET status='published'"))
    run(conn.execute('DELETE FROM collab_skills'))
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_versions')) == 0


@pytest.mark.parametrize('tamper', ['bytes', 'checksum', 'signature', 'key', 'manifest', 'extra-file'])
def test_verification_rejects_tampering(env, tamper):
    registry, run, *_ = env
    _, version = publish(env)
    package = run(registry.get_skill_version(version.skill_id, version.version, principal=agent()))
    package = copy.deepcopy(package)
    if tamper == 'bytes':
        package.files['SKILL.md'] = b'changed'
    elif tamper == 'checksum':
        package = replace(package, sha256='0' * 64)
    elif tamper == 'signature':
        package = replace(package, signature='00' * 64)
    elif tamper == 'key':
        package = replace(package, key_id='untrusted')
    elif tamper == 'manifest':
        package.manifest['capabilities'] = ['execute:anything']
    else:
        package.files['extra.py'] = b''
    assert not registry.verify_skill_package(package).ok


def test_corrupt_s3_bytes_never_return_a_package(env):
    registry, run, _, s3, *_ = env
    _, version = publish(env)
    s3.objects['skills', version.storage_key] = b'{}'
    with pytest.raises(CollabError):
        run(registry.get_skill_version(version.skill_id, version.version, principal=agent()))


def test_storage_failure_leaves_no_published_version_or_audit(env):
    registry, run, conn, s3, *_ = env
    draft = run(registry.create_skill_draft(user(), metadata()))
    s3.fail = True
    with pytest.raises(CollabError, match='package storage unavailable'):
        run(registry.publish_skill(user(), draft.draft_id, 'private'))
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_versions')) == 0
    assert run(conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE action='skill.published'")) == 0


@pytest.mark.parametrize('change', [
    {'files': {'SKILL.md': b'x', '../escape': b'y'}}, {'files': {}},
    {'fixtures': ['missing']}, {'fixtures': [], 'tests': []}, {'dependency_lock': 'absent'},
    {'capabilities': 'execute'}, {'input_schema': []}, {'provenance': {}},
    {'version': '../bad'}, {'scope': 'shared'}, {'signature': 'client-controlled'},
])
def test_invalid_metadata_is_rejected_before_storage(env, change):
    registry, run, conn, s3, *_ = env
    with pytest.raises(CollabError):
        run(registry.create_skill_draft(user(), metadata(**change)))
    assert not s3.objects
    assert run(conn.fetchval('SELECT count(*) FROM collab_skills')) == 0


def test_private_draft_room_hint_does_not_block_room_deletion(env):
    registry, run, conn, *_ = env
    _, version = publish(env, room_id='room_one')
    run(conn.execute("DELETE FROM collab_rooms WHERE room_id='room_one'"))
    assert run(registry.get_skill_version(version.skill_id, version.version, principal=agent()))


@pytest.mark.parametrize('delete', ["DELETE FROM collab_rooms", "DELETE FROM collab_users WHERE user_id='alice'"])
def test_published_room_skills_follow_parent_scrub(env, delete):
    _, run, conn, *_ = env
    publish(env, 'room')
    run(conn.execute(delete))
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_versions')) == 0
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_drafts')) == 0


def test_drafts_are_immutable_in_sql(env):
    import asyncpg
    registry, run, conn, *_ = env
    run(registry.create_skill_draft(user(), metadata()))
    with pytest.raises(asyncpg.CheckViolationError):
        run(conn.execute("UPDATE collab_skill_drafts SET manifest='{}'"))


def test_read_and_publication_recheck_live_authority_after_storage(env):
    registry, run, conn, s3, _, invalid = env
    draft, version = publish(env)
    original = s3.get_object
    def revoke_on_read(**kwargs):
        invalid.add(agent().key)
        invalid.add(user().key)
        return original(**kwargs)
    s3.get_object = revoke_on_read
    with pytest.raises(CollabError):
        run(registry.get_skill_version(version.skill_id, version.version, principal=agent()))
    invalid.clear()
    with pytest.raises(CollabError):
        run(registry.publish_skill(user(), draft.draft_id, 'private'))
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_versions')) == 1


def test_corrupted_draft_cannot_be_published(env):
    registry, run, conn, s3, *_ = env
    draft = run(registry.create_skill_draft(user(), metadata()))
    key = run(conn.fetchval('SELECT storage_key FROM collab_skill_drafts'))
    s3.objects['skills', key] = b'{}'
    with pytest.raises(CollabError):
        run(registry.publish_skill(user(), draft.draft_id, 'private'))
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_versions')) == 0


def test_existing_s3_object_is_verified_and_never_overwritten(env):
    registry, run, _, s3, *_ = env
    _, version = publish(env)
    package = run(registry.get_skill_version(version.skill_id, version.version, principal=agent()))
    assert run(registry._write(package)) == version.storage_key
    s3.objects['skills', version.storage_key] = b'corrupt'
    with pytest.raises(CollabError):
        run(registry._write(package))
    assert s3.objects['skills', version.storage_key] == b'corrupt'


@pytest.mark.parametrize('header', [
    {'schema_version': True}, {'skill_id': ''}, {'creator_id': ''}, {'created_at': 'not-a-date'},
])
def test_valid_signature_does_not_excuse_invalid_manifest_headers(env, header):
    registry, run, *_ = env
    _, version = publish(env)
    package = run(registry.get_skill_version(version.skill_id, version.version, principal=agent()))
    altered = registry._sign({**package.manifest, **header}, package.files)
    assert not registry.verify_skill_package(altered).ok


def test_draft_request_key_replays_across_restart_without_new_object_or_audit(env):
    registry, run, conn, s3, restart, _ = env
    data = metadata(request_key='create-summary')
    first = run(registry.create_skill_draft(user(), data))
    reordered = dict(reversed(list(data.items())))
    reordered['files'] = dict(reversed(list(data['files'].items())))
    assert run(restart().create_skill_draft(user(), reordered)) == first
    assert len(s3.objects) == 1
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_drafts')) == 1
    assert run(conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE action='skill.drafted'")) == 1
    assert run(conn.fetchval('SELECT request_fingerprint FROM collab_skill_drafts'))


@pytest.mark.parametrize('change', [{'name': 'different'}, {'room_id': 'room_one'},
                                    {'files': {**metadata()['files'], 'SKILL.md': b'changed'}}])
def test_conflicting_draft_request_key_fails_without_storage_or_audit_changes(env, change):
    registry, run, conn, s3, *_ = env
    data = metadata(request_key='same-request')
    run(registry.create_skill_draft(user(), data))
    with pytest.raises(CollabError) as error:
        run(registry.create_skill_draft(user(), {**data, **change}))
    assert error.value.status == 409
    assert len(s3.objects) == 1
    assert run(conn.fetchval('SELECT count(*) FROM collab_audit_events')) == 1


def test_request_keys_are_scoped_to_authenticated_creator(env):
    registry, run, conn, _, _, invalid = env
    data = metadata(request_key='same-request')
    alice = run(registry.create_skill_draft(user(), data))
    bob = run(registry.create_skill_draft(user('bob'), data))
    assert alice != bob
    invalid.add(user().key)
    with pytest.raises(CollabError):
        run(registry.create_skill_draft(user(), data))
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_drafts')) == 2


def test_concurrent_identical_requests_create_one_draft(env):
    registry, run, conn, s3, *_ = env
    import asyncpg
    async def concurrent():
        other = await asyncpg.connect(os.environ['HERDR_E2E_PG_DSN'])
        try:
            schema = await conn.fetchval('SELECT current_schema()')
            await other.execute(f'SET search_path TO {schema}')
            second = copy.copy(registry)
            second.conn = other
            data = metadata(request_key='concurrent')
            return await asyncio.gather(registry.create_skill_draft(user(), data),
                                        second.create_skill_draft(user(), data))
        finally:
            await other.close()
    first, second = run(concurrent())
    assert first == second
    assert len(s3.objects) == 1
    assert run(conn.fetchval('SELECT count(*) FROM collab_audit_events')) == 1


def test_scrub_captures_package_references_and_finish_fails_closed(env):
    from herdr_engineering.control_plane.collab_store import CollabStore
    _, run, conn, s3, *_ = env
    publish(env)
    store = CollabStore(conn)
    payload = run(store.start_scrub('alice', 'scrub-test'))
    assert {(r['bucket'], r['storage_key']) for r in payload['skill_packages']} == set(s3.objects)
    with pytest.raises(ValueError, match='skill package cleanup'):
        run(store.finish_scrub('alice', 'scrub-test'))
    assert run(conn.fetchval("SELECT status FROM collab_users WHERE user_id='alice'")) == 'disabled'
    assert run(store.get_job('scrub-test'))['payload']['skill_packages']
    assert len(s3.objects) == 2


@pytest.mark.parametrize('failure', ['delete_error', 'silent_delete', 'head_error', 'versioned'])
def test_skill_scrub_verifies_deletion_and_retries_from_durable_references(env, failure):
    from herdr_engineering.control_plane.collab_archive import CollabArchive
    from herdr_engineering.control_plane.collab_storage import CollabStorage, S3Config, StorageError
    from herdr_engineering.control_plane.collab_store import CollabStore
    _, run, conn, s3, *_ = env
    publish(env)
    s3.objects['skills', 'unrelated'] = b'keep'
    store = CollabStore(conn)
    storage = CollabStorage(S3Config(environment='test'), s3=s3)
    s3.cleanup_failure = failure
    with pytest.raises(StorageError, match='skill package cleanup'):
        run(CollabArchive(store, storage).scrub_deleted_user('alice'))
    jobs = run(store.jobs('scrub'))
    assert len(jobs) == 1 and len(jobs[0]['payload']['skill_packages']) == 2
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_versions')) == 1
    assert not run(conn.fetchval("SELECT EXISTS(SELECT 1 FROM collab_audit_events WHERE action='user.deleted')"))
    s3.cleanup_failure = None
    assert run(CollabArchive(CollabStore(conn), storage).scrub_deleted_user('alice')).status == 'complete'
    assert s3.objects == {('skills', 'unrelated'): b'keep'}
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_versions')) == 0
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_drafts')) == 0
    assert run(store.jobs('scrub')) == []


def test_legacy_package_without_known_bucket_blocks_scrub(env):
    from herdr_engineering.control_plane.collab_archive import CollabArchive
    from herdr_engineering.control_plane.collab_storage import CollabStorage, S3Config, StorageError
    from herdr_engineering.control_plane.collab_store import CollabStore
    _, run, conn, s3, *_ = env
    run(conn.execute("""
        INSERT INTO collab_skills(skill_id,owner_id,name) VALUES ('legacy','alice','Legacy');
        INSERT INTO collab_skill_versions(skill_id,version,storage_key,sha256)
            VALUES ('legacy','1','unknown-location',repeat('a',64));
    """))
    with pytest.raises(StorageError, match='skill package cleanup'):
        run(CollabArchive(CollabStore(conn), CollabStorage(S3Config(environment='test'), s3=s3)).scrub_deleted_user('alice'))
    assert run(conn.fetchval("SELECT count(*) FROM collab_users WHERE user_id='alice'")) == 1
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_versions')) == 1


def test_forward_migration_upgrades_pre_registry_database_and_quarantines_unsigned_versions(env, tmp_path):
    import shutil
    _, run, conn, *_ = env
    migrations = Path(__file__).parents[2] / 'deploy/control-plane/migrations'
    old = tmp_path / 'old'
    old.mkdir()
    for source in sorted(migrations.glob('*.sql'))[:5]:
        shutil.copy(source, old / source.name)
    current = run(conn.fetchval('SELECT current_schema()'))
    schema = 'upgrade_test_' + uuid4().hex
    run(conn.execute(f'CREATE SCHEMA {schema}'))
    try:
        run(conn.execute(f'SET search_path TO {schema}'))
        run(Migrator(conn).apply(str(old)))
        assert run(conn.fetchval("SELECT to_regclass('collab_skill_drafts')")) is None
        run(conn.execute("""
            INSERT INTO collab_users(user_id,status) VALUES ('legacy-owner','approved');
            INSERT INTO collab_skills(skill_id,owner_id,name,status)
                VALUES ('legacy-skill','legacy-owner','Old','published');
            INSERT INTO collab_skill_versions(skill_id,version,storage_key,sha256,status)
                VALUES ('legacy-skill','1','old-package',repeat('b',64),'published');
        """))
        assert run(Migrator(conn).apply(str(migrations))) == [
            '0006_collaboration_skill_registry.sql', '0007_collaboration_analysis.sql',
            '0008_collaboration_analysis_request_keys.sql', '0009_collaboration_skill_package_journal.sql']
        row = run(conn.fetchrow('SELECT * FROM collab_skill_versions'))
        assert row['status'] == 'revoked' and row['legacy_unverified']
        assert row['storage_key'] == 'old-package' and row['storage_bucket'] is None
        assert run(conn.fetchval("SELECT to_regclass('collab_skill_drafts')"))
        assert run(Migrator(conn).apply(str(migrations))) == []
    finally:
        run(conn.execute(f'SET search_path TO {current}'))
        run(conn.execute(f'DROP SCHEMA {schema} CASCADE'))


def test_forward_migration_preserves_existing_signed_versions_and_drafts(env):
    _, run, conn, *_ = env
    publish(env)
    before = run(conn.fetchrow('SELECT * FROM collab_skill_versions'))
    path = Path(__file__).parents[2] / 'deploy/control-plane/migrations/0006_collaboration_skill_registry.sql'
    assert path.is_file()
    run(conn.execute(path.read_text()))
    assert run(conn.fetchrow('SELECT * FROM collab_skill_versions')) == before
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_drafts')) == 1


def test_draft_retry_rechecks_live_authority_after_waiting_for_request_lock(env):
    registry, run, *_ = env
    data = metadata(request_key='revoked-retry')
    run(registry.create_skill_draft(user(), data))
    checks = iter([True, False])
    registry.principal_valid = lambda p: next(checks)
    with pytest.raises(CollabError):
        run(registry.create_skill_draft(user(), data))


def test_cleanup_checkpoint_survives_cascade_and_old_scrub_job_is_upgraded(env):
    from herdr_engineering.control_plane.collab_archive import CollabArchive
    from herdr_engineering.control_plane.collab_storage import CollabStorage, S3Config
    from herdr_engineering.control_plane.collab_store import CollabStore
    _, run, conn, s3, *_ = env
    publish(env)
    store = CollabStore(conn)
    run(store.put_job('old-scrub', 'scrub', {'user_id': 'alice', 'agent_ids': ['agent_alice']}))
    payload = run(store.start_scrub('alice', 'old-scrub'))
    assert len(payload['skill_packages']) == 2
    run(conn.execute("DELETE FROM collab_users WHERE user_id='alice'"))
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_drafts')) == 0
    assert run(store.get_job('old-scrub'))['payload']['skill_packages'] == payload['skill_packages']
    assert run(CollabArchive(store, CollabStorage(S3Config(environment='test'), s3=s3)).resume_pending_archives()) == []
    assert not s3.objects and not run(store.jobs('scrub'))


@pytest.mark.parametrize('key', [None, '', ' ', 'a' * 201])
def test_request_key_is_required_and_bounded(env, key):
    registry, run, conn, s3, *_ = env
    with pytest.raises(CollabError) as error:
        run(registry.create_skill_draft(user(), metadata(request_key=key)))
    assert error.value.status == 400
    assert not s3.objects
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_drafts')) == 0


@pytest.mark.parametrize('operation', ['draft', 'publish'])
@pytest.mark.parametrize('failure', ['authority', 'commit'])
def test_failed_package_put_retains_ownership_and_blocks_false_deletion(env, operation, failure):
    from herdr_engineering.control_plane.collab_archive import CollabArchive
    from herdr_engineering.control_plane.collab_storage import CollabStorage, S3Config, StorageError
    from herdr_engineering.control_plane.collab_store import CollabStore
    registry, run, conn, s3, _, invalid = env
    draft = run(registry.create_skill_draft(user(), metadata())) if operation == 'publish' else None
    original = s3.put_object
    written = []
    def put_then_revoke(**kwargs):
        original(**kwargs)
        written.append((kwargs['Bucket'], kwargs['Key']))
        if failure == 'authority':
            invalid.add(user().key)
    s3.put_object = put_then_revoke
    if failure == 'commit':
        run(conn.execute("""
            CREATE FUNCTION reject_skill_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected deferred commit failure'; END $$;
            CREATE CONSTRAINT TRIGGER reject_skill_commit AFTER INSERT ON collab_audit_events
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            WHEN (NEW.action IN ('skill.drafted', 'skill.published')) EXECUTE FUNCTION reject_skill_commit();
        """))
    with pytest.raises(Exception, match='forbidden|injected deferred commit failure'):
        run(registry.publish_skill(user(), draft.draft_id, 'private') if draft
            else registry.create_skill_draft(user(), metadata(request_key='orphan')))
    assert len(written) == 1 and written[0] in s3.objects
    store = CollabStore(conn)
    storage = CollabStorage(S3Config(environment='test'), s3=s3)
    s3.cleanup_failure = 'silent_delete'
    with pytest.raises(StorageError, match='skill package cleanup'):
        run(CollabArchive(store, storage).scrub_deleted_user('alice'))
    assert run(conn.fetchval("SELECT status FROM collab_users WHERE user_id='alice'")) == 'disabled'
    assert not run(conn.fetchval("SELECT EXISTS(SELECT 1 FROM collab_audit_events WHERE action='user.deleted')"))
    assert any(ref['storage_key'] == written[0][1] for ref in run(store.jobs('scrub'))[0]['payload']['skill_packages'])
    s3.cleanup_failure = None
    assert run(CollabArchive(store, storage).scrub_deleted_user('alice')).status == 'complete'
    assert not s3.objects


def test_cancelled_package_write_keeps_lifecycle_lock_until_provider_stops(env):
    import threading

    from herdr_engineering.control_plane.collab_archive import CollabArchive
    from herdr_engineering.control_plane.collab_storage import CollabStorage, S3Config
    from herdr_engineering.control_plane.collab_store import CollabStore
    registry, run, conn, s3, *_ = env
    entered, release = threading.Event(), threading.Event()
    original = s3.put_object
    def slow_put(**kwargs):
        entered.set()
        assert release.wait(10)
        original(**kwargs)
    s3.put_object = slow_put
    async def cancel():
        task = asyncio.create_task(registry.create_skill_draft(user(), metadata()))
        try:
            async with asyncio.timeout(5):
                while not entered.is_set():
                    await asyncio.sleep(.001)
            task.cancel()
            await asyncio.sleep(.05)
            task.cancel()
            await asyncio.sleep(.05)
            assert not task.done(), 'must retain lifecycle ownership while the PUT can still finish'
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    run(cancel())
    assert len(s3.objects) == 1
    store = CollabStore(conn)
    assert len(run(store.skill_package_refs('alice'))) == 1
    assert run(CollabArchive(store, CollabStorage(S3Config(environment='test'), s3=s3)).scrub_deleted_user('alice')).status == 'complete'
    assert not s3.objects


def test_ownership_journal_is_visible_before_put_and_survives_uncaptured_cascade(env):
    from herdr_engineering.control_plane.collab_archive import CollabArchive
    from herdr_engineering.control_plane.collab_storage import CollabStorage, S3Config
    from herdr_engineering.control_plane.collab_store import CollabStore
    registry, run, conn, s3, *_ = env
    original = s3.put_object
    async def create():
        loop = asyncio.get_running_loop()
        def inspect_then_put(**kwargs):
            rows = asyncio.run_coroutine_threadsafe(registry.journal_conn.fetch(
                'SELECT * FROM collab_skill_package_journal WHERE storage_key=$1', kwargs['Key']), loop).result(5)
            assert len(rows) == 1
            assert rows[0]['creator_id'] == 'alice' and rows[0]['request_key'] == 'draft:durable-first'
            original(**kwargs)
        s3.put_object = inspect_then_put
        return await registry.create_skill_draft(user(), metadata(request_key='durable-first'))
    run(create())
    run(conn.execute("DELETE FROM collab_users WHERE user_id='alice'"))
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_drafts')) == 0
    store = CollabStore(conn)
    assert len(run(store.skill_package_refs('alice'))) == 1
    with pytest.raises(ValueError, match='skill package cleanup'):
        run(store.finish_scrub('alice', 'not-verified'))
    assert run(CollabArchive(store, CollabStorage(S3Config(environment='test'), s3=s3)).scrub_deleted_user('alice')).status == 'complete'
    assert not s3.objects
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_package_journal')) == 0


@pytest.mark.parametrize('broken', ['transaction', 'unavailable'])
def test_uncommittable_journal_prevents_any_put(env, broken):
    registry, run, conn, s3, *_ = env
    async def attempt():
        if broken == 'transaction':
            async with registry.journal_conn.transaction():
                await registry.create_skill_draft(user(), metadata())
        else:
            await registry.journal_conn.close()
            await registry.create_skill_draft(user(), metadata())
    with pytest.raises(CollabError, match='package journal'):
        run(attempt())
    assert not s3.objects
    assert run(conn.fetchval('SELECT count(*) FROM collab_skill_drafts')) == 0


def test_lost_registry_connection_cannot_race_scrub_against_inflight_put(env):
    import threading

    import asyncpg

    from herdr_engineering.control_plane.collab_archive import CollabArchive
    from herdr_engineering.control_plane.collab_storage import CollabStorage, S3Config
    from herdr_engineering.control_plane.collab_store import CollabStore
    registry, run, conn, s3, *_ = env
    entered, release = threading.Event(), threading.Event()
    original = s3.put_object
    def delayed_put(**kwargs):
        entered.set()
        assert release.wait(10)
        original(**kwargs)
    s3.put_object = delayed_put
    archive = CollabArchive(CollabStore(conn), CollabStorage(S3Config(environment='test'), s3=s3))
    async def disconnect():
        writer = await asyncpg.connect(os.environ['HERDR_E2E_PG_DSN'])
        await writer.execute('SET search_path TO ' + await conn.fetchval('SELECT current_schema()'))
        second = copy.copy(registry)
        second.conn = writer
        task = asyncio.create_task(second.create_skill_draft(user(), metadata()))
        try:
            async with asyncio.timeout(5):
                while not entered.is_set():
                    await asyncio.sleep(.001)
            await writer.close()
            with pytest.raises(ValueError, match='lifecycle busy'):
                await archive.scrub_deleted_user('alice')
        finally:
            release.set()
            with pytest.raises(Exception):
                await task
            await writer.close()
    run(disconnect())
    assert len(s3.objects) == 1
    assert run(archive.scrub_deleted_user('alice')).status == 'complete'
    assert not s3.objects


def test_forward_migration_accepts_previously_amended_0003_with_signed_rows(env):
    _, run, conn, *_ = env
    publish(env)
    before = dict(run(conn.fetchrow('SELECT * FROM collab_skill_versions')))
    # The amended 0003 has the signed fields/triggers but none of the new columns.
    run(conn.execute("""
        ALTER TABLE collab_skill_drafts DROP COLUMN request_key CASCADE,
            DROP COLUMN request_fingerprint CASCADE, DROP COLUMN storage_bucket;
        ALTER TABLE collab_skill_versions DROP COLUMN storage_bucket,
            DROP COLUMN legacy_unverified CASCADE;
        DELETE FROM schema_migrations WHERE version='0006_collaboration_skill_registry.sql';
    """))
    migrations = Path(__file__).parents[2] / 'deploy/control-plane/migrations'
    assert run(Migrator(conn).apply(str(migrations))) == ['0006_collaboration_skill_registry.sql']
    after = dict(run(conn.fetchrow('SELECT * FROM collab_skill_versions')))
    for key in ('manifest', 'sha256', 'signature', 'key_id', 'source_draft_id', 'status'):
        assert after[key] == before[key]
    assert not after['legacy_unverified'] and after['storage_bucket'] is None
    assert run(conn.fetchval('SELECT request_key FROM collab_skill_drafts')) is None

"""Lifecycle tests: real Postgres and storage adapter; only S3 transport is local."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from herdr_engineering.control_plane.collab_storage import CollabStorage, S3Config, live_key
from herdr_engineering.control_plane.collab_store import CollabStore
from herdr_engineering.control_plane.db import Migrator

ROOM = '00000000-0000-4000-8000-000000000001'
FILE = '00000000-0000-4000-8000-000000000002'
SESSION = '00000000-0000-4000-8000-000000000003'
BLOB = b'room-owned bytes'
DIGEST = hashlib.sha256(BLOB).hexdigest()
LIVE = 'herdr-collab-live'
ARCHIVE = 'herdr-collab-archive'


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.events = []
        self.corrupt_copy = False
        self.fail_delete = False
        self.fail_put = False
        self.on_delete = None

    def put_object(self, *, Bucket, Key, Body):
        if self.fail_put:
            raise OSError('secret provider detail')
        self.events.append(('put', Bucket, Key))
        self.objects[Bucket, Key] = Body

    def get_object(self, *, Bucket, Key):
        self.events.append(('get', Bucket, Key))
        return {'Body': io.BytesIO(self.objects[Bucket, Key])}

    def copy(self, source, bucket, key):
        self.events.append(('copy', bucket, key))
        data = self.objects[source['Bucket'], source['Key']]
        self.objects[bucket, key] = b'corrupt' if self.corrupt_copy else data

    def delete_object(self, *, Bucket, Key):
        if self.on_delete:
            self.on_delete(Bucket, Key)
        if self.fail_delete:
            raise OSError('secret provider detail')
        self.events.append(('delete', Bucket, Key))
        self.objects.pop((Bucket, Key), None)

    def list_objects_v2(self, *, Bucket, Prefix, **kwargs):
        return {'Contents': [{'Key': k} for b, k in self.objects if b == Bucket and k.startswith(Prefix)]}


@pytest.fixture
def env():
    dsn = os.environ.get('HERDR_E2E_PG_DSN')
    if not dsn:
        pytest.skip('set HERDR_E2E_PG_DSN to a throwaway Postgres')
    import asyncpg

    with asyncio.Runner() as runner:
        run = runner.run
        conn = run(asyncpg.connect(dsn))
        schema = 'archive_test_' + uuid4().hex
        run(conn.execute(f'CREATE SCHEMA {schema}'))
        run(conn.execute(f'SET search_path TO {schema}'))
        try:
            run(Migrator(conn).apply(str(Path(__file__).parents[2] / 'deploy/control-plane/migrations')))
            run(conn.execute("""
                INSERT INTO collab_users(user_id,status) VALUES ('owner','approved'),('member','approved');
                INSERT INTO collab_agents(agent_id,owner_id,name) VALUES ('agent_original','member','Helper');
            """))
            store = CollabStore(conn)
            run(store.create_room(room_id=ROOM, name='Research', owner_id='owner'))
            for user, role in [('owner', 'owner'), ('member', 'contributor')]:
                run(store.create_membership(membership_id='membership_' + user, room_id=ROOM,
                                           user_id=user, status='approved', role=role, can_upload=True,
                                           expires_at=2000000000.5, joined_seq=7))
            run(store.create_attachment(room_id=ROOM, agent_id='agent_original', actions=['read', 'post'],
                                        expires_at=2000000000.5, granted_seq=11))
            run(conn.execute("INSERT INTO collab_agent_permissions(room_id,agent_id,user_id,permission_id) VALUES ($1,'agent_original','owner','permission_old')", ROOM))
            run(store.append_message(message_id='message_owner', room_id=ROOM, author_user_id='owner', text='keep'))
            run(store.append_message(message_id='message_member', room_id=ROOM, author_user_id='member', text='private member'))
            run(store.append_message(message_id='message_agent', room_id=ROOM, author_agent_id='agent_original', text='private agent'))
            run(conn.execute("INSERT INTO collab_session_logs(log_id,room_id,session_id,author_user_id,content) VALUES ('log_old',$1,$2,'member','private session')", ROOM, SESSION))
            run(conn.execute("INSERT INTO collab_room_files(file_id,room_id,uploaded_by,relative_path,storage_key,size_bytes,sha256,status) VALUES ($1,$2,'member','data.csv',$3,$4,$5,'available')", FILE, ROOM, live_key('test', ROOM, FILE), len(BLOB), DIGEST))
            s3 = MemoryS3()
            s3.objects[LIVE, live_key('test', ROOM, FILE)] = BLOB
            s3.objects[LIVE, live_key('test', ROOM, FILE, 'thumbnail')] = b'preview'
            storage = CollabStorage(S3Config(environment='test'), s3=s3)
            yield conn, run, store, storage, s3
        finally:
            run(conn.execute('SET search_path TO public'))
            run(conn.execute(f'DROP SCHEMA {schema} CASCADE'))
            run(conn.close())


def lifecycle(env):
    mod = importlib.import_module('herdr_engineering.control_plane.collab_archive')
    return mod.CollabArchive(env[2], env[3])


def test_archive_verifies_all_exports_before_deleting_live_data(env):
    conn, run, _, _, s3 = env
    service = lifecycle(env)
    archive = run(service.archive_room(ROOM, 'owner'))
    assert archive.status == 'verified' and archive.live_room_id is None
    assert run(conn.fetchval('SELECT count(*) FROM collab_rooms')) == 0
    assert not any(b == LIVE for b, _ in s3.objects)
    manifest = json.loads(s3.objects[ARCHIVE, archive.manifest_key])
    assert hashlib.sha256(s3.objects[ARCHIVE, archive.manifest_key]).hexdigest() == archive.manifest_sha256
    names = {o['key'].split(f'{archive.archive_id}/')[1] for o in manifest['objects']}
    assert {'room.json', 'members.ndjson', 'messages.ndjson', f'sessions/{SESSION}.ndjson',
            f'files/{FILE}/metadata.json', f'files/{FILE}/blob', f'files/{FILE}/preview/thumbnail'} <= names
    first_delete = next(i for i, e in enumerate(s3.events) if e[0] == 'delete')
    for obj in manifest['objects']:
        assert ('get', ARCHIVE, obj['key']) in s3.events[:first_delete]
    again = run(lifecycle(env).archive_room(ROOM, 'owner'))
    assert again == archive
    assert run(conn.fetchval('SELECT count(*) FROM collab_room_archives')) == 1


def test_corrupt_copy_keeps_live_rows_and_bytes_and_retry_recovers(env):
    conn, run, _, _, s3 = env
    service = lifecycle(env)
    s3.corrupt_copy = True
    with pytest.raises(Exception, match='verification'):
        run(service.archive_room(ROOM, 'owner'))
    assert run(conn.fetchval('SELECT count(*) FROM collab_messages')) == 3
    assert s3.objects[LIVE, live_key('test', ROOM, FILE)] == BLOB
    assert not any(e[0] == 'delete' for e in s3.events)
    s3.corrupt_copy = False
    reports = run(lifecycle(env).resume_pending_archives())
    assert len(reports) == 1 and reports[0].status == 'verified'
    assert run(conn.fetchval('SELECT count(*) FROM collab_rooms')) == 0


def test_verified_archive_rejects_tampered_manifest_before_cleanup(env):
    conn, run, _, _, s3 = env
    s3.fail_delete = True
    with pytest.raises(Exception):
        run(lifecycle(env).archive_room(ROOM, 'owner'))
    row = run(conn.fetchrow('SELECT * FROM collab_room_archives'))
    doc = json.loads(s3.objects[ARCHIVE, row['manifest_key']])
    doc['objects'] = []
    s3.objects[ARCHIVE, row['manifest_key']] = json.dumps(doc).encode()
    s3.fail_delete = False
    reports = run(lifecycle(env).resume_pending_archives())
    assert reports[0].status == 'failed'
    assert run(conn.fetchval('SELECT count(*) FROM collab_rooms')) == 1
    assert s3.objects[LIVE, live_key('test', ROOM, FILE)] == BLOB


def test_worker_resumes_partial_deletion_without_reexport(env):
    from herdr_engineering.control_plane.worker import run_one_pass
    conn, run, _, _, s3 = env
    service = lifecycle(env)
    remaining = 1

    def fail_after_first(bucket, key):
        nonlocal remaining
        if remaining == 0:
            raise OSError('interrupted')
        remaining -= 1

    s3.on_delete = fail_after_first
    with pytest.raises(Exception):
        run(service.archive_room(ROOM, 'owner'))
    s3.on_delete = None
    summary = run(run_one_pass(conn, collab_archive=lifecycle(env)))
    assert summary['collab_archives'][0].status == 'verified'
    assert run(conn.fetchval('SELECT count(*) FROM collab_rooms')) == 0


def test_restore_remaps_relational_ids_and_prefixes_preserving_permissions(env):
    conn, run, _, _, s3 = env
    archive = run(lifecycle(env).archive_room(ROOM, 'owner'))
    restored = run(lifecycle(env).restore_room(archive.archive_id, 'owner'))
    assert restored.room_id != ROOM and restored.status == 'active'
    file = run(conn.fetchrow('SELECT * FROM collab_room_files'))
    assert file['file_id'] != FILE and file['room_id'] == restored.room_id
    assert s3.objects[LIVE, file['storage_key']] == BLOB
    assert s3.objects[LIVE, file['storage_key'].removesuffix('blob') + 'preview/thumbnail'] == b'preview'
    assert run(conn.fetchval('SELECT count(*) FROM collab_messages')) == 3
    assert set(run(conn.fetchval('SELECT array_agg(message_id) FROM collab_messages'))).isdisjoint({'message_owner', 'message_member', 'message_agent'})
    member = run(conn.fetchrow("SELECT * FROM collab_room_memberships WHERE user_id='member'"))
    assert member['can_upload'] and member['joined_seq'] == 7 and member['expires_at'] == 2000000000.5
    attachment = run(conn.fetchrow('SELECT * FROM collab_agent_room_attachments'))
    assert attachment['agent_id'] != 'agent_original' and attachment['actions'] == ['read', 'post']
    assert attachment['granted_seq'] == 11 and attachment['expires_at'] == 2000000000.5
    assert run(lifecycle(env).restore_room(archive.archive_id, 'owner')) == restored


def test_restore_corruption_does_not_publish_a_room(env):
    conn, run, _, _, s3 = env
    archive = run(lifecycle(env).archive_room(ROOM, 'owner'))
    key = next(k for b, k in s3.objects if b == ARCHIVE and k.endswith('/blob'))
    s3.objects[ARCHIVE, key] = b'tampered'
    with pytest.raises(Exception, match='verification'):
        run(lifecycle(env).restore_room(archive.archive_id, 'owner'))
    assert run(conn.fetchval('SELECT count(*) FROM collab_rooms')) == 0


def test_scrub_removes_live_and_archived_authorship_but_preserves_files(env):
    conn, run, store, _, s3 = env
    archive = run(lifecycle(env).archive_room(ROOM, 'owner'))
    live = run(lifecycle(env).restore_room(archive.archive_id, 'owner'))
    report = run(lifecycle(env).scrub_deleted_user('member'))
    assert report.status == 'complete'
    assert run(conn.fetchval("SELECT count(*) FROM collab_users WHERE user_id='member'")) == 0
    assert run(conn.fetchval('SELECT count(*) FROM collab_messages')) == 1
    assert run(conn.fetchval('SELECT count(*) FROM collab_session_logs')) == 0
    assert run(conn.fetchval('SELECT count(*) FROM collab_agents')) == 0
    files = run(store.list_room_files(live.room_id))
    assert len(files) == 1 and files[0].uploaded_by is None
    assert s3.objects[LIVE, files[0].storage_key] == BLOB
    for (bucket, key), data in s3.objects.items():
        if bucket == ARCHIVE and not (key.endswith('/blob') or '/preview/' in key):
            assert b'private member' not in data and b'private agent' not in data and b'private session' not in data
            assert b'"member"' not in data and b'agent_original' not in data
    # A different restorer gets a fresh room and cannot resurrect deleted content.
    run(conn.execute("INSERT INTO collab_users(user_id,status) VALUES ('operator','approved')"))
    fresh = run(lifecycle(env).restore_room(archive.archive_id, 'operator'))
    assert fresh.room_id not in {ROOM, live.room_id}
    assert len(run(store.list_room_files(fresh.room_id))) == 1
    assert run(conn.fetchval('SELECT count(*) FROM collab_messages')) == 2
    assert run(lifecycle(env).scrub_deleted_user('member')).status == 'complete'


def test_failed_scrub_is_durable_and_worker_retries_before_user_deletion(env):
    conn, run, _, _, s3 = env
    run(lifecycle(env).archive_room(ROOM, 'owner'))
    s3.fail_put = True
    with pytest.raises(Exception):
        run(lifecycle(env).scrub_deleted_user('member'))
    assert run(conn.fetchval("SELECT status FROM collab_users WHERE user_id='member'")) == 'disabled'
    s3.fail_put = False
    run(lifecycle(env).resume_pending_archives())
    assert run(conn.fetchval("SELECT count(*) FROM collab_users WHERE user_id='member'")) == 0
    assert any(data == BLOB for (bucket, _), data in s3.objects.items() if bucket == ARCHIVE)


def test_archiving_room_rejects_new_store_writes(env):
    conn, run, store, _, s3 = env
    s3.fail_put = True
    with pytest.raises(Exception):
        run(lifecycle(env).archive_room(ROOM, 'owner'))
    with pytest.raises(ValueError, match='active'):
        run(store.append_message(message_id='late', room_id=ROOM, author_user_id='owner', text='lost?'))
    assert run(conn.fetchval("SELECT count(*) FROM collab_messages WHERE message_id='late'")) == 0


def test_service_hooks_require_owner_or_operator_and_reject_agents(env):
    from herdr_engineering.control_plane.collab import (
        CollabError,
        CollabService,
        Principal,
        StaticTokenAuthenticator,
    )
    _, run, _, _, s3 = env
    service = CollabService(StaticTokenAuthenticator({'owner': hashlib.sha256(b'owner-token').hexdigest(), 'member': hashlib.sha256(b'member-token').hexdigest()}), archive_service=lifecycle(env))
    for pr in [Principal('user', 'member', 'member'), Principal('agent', 'agent_original', 'owner')]:
        with pytest.raises(CollabError):
            run(service.archive_room(ROOM, pr))
    assert not s3.events
    archive = run(service.archive_room(ROOM, Principal('user', 'owner', 'owner')))
    assert archive.status == 'verified'


def test_cleanup_retry_preserves_live_bytes_changed_since_verification(env):
    conn, run, _, _, s3 = env
    service = lifecycle(env)
    s3.fail_delete = True
    with pytest.raises(Exception):
        run(service.archive_room(ROOM, 'owner'))
    s3.fail_delete = False
    s3.objects[LIVE, live_key('test', ROOM, FILE)] = b'new bytes not archived'
    reports = run(lifecycle(env).resume_pending_archives())
    assert reports[0].status == 'failed'
    assert s3.objects[LIVE, live_key('test', ROOM, FILE)] == b'new bytes not archived'
    assert run(conn.fetchval('SELECT count(*) FROM collab_rooms')) == 1


def test_archiving_room_hides_files_from_normal_reads(env):
    _, run, store, _, s3 = env
    service = lifecycle(env)
    s3.fail_put = True
    with pytest.raises(Exception):
        run(service.archive_room(ROOM, 'owner'))
    assert run(store.list_room_files(ROOM)) == []


def test_deleted_restore_id_cannot_be_resurrected(env):
    _, run, _, _, _ = env
    service = lifecycle(env)
    archive = run(service.archive_room(ROOM, 'owner'))
    restored = run(service.restore_room(archive.archive_id, 'owner'))
    run(service.archive_room(restored.room_id, 'owner'))
    with pytest.raises(ValueError, match='completed'):
        run(service.restore_room(archive.archive_id, 'owner'))


def test_worker_resumes_abandoned_export_checkpoint(env):
    from uuid import NAMESPACE_URL, uuid5
    conn, run, store, _, _ = env
    service = lifecycle(env)
    archive_id = str(uuid5(NAMESPACE_URL, 'collab-archive:' + ROOM))
    run(store.begin_archive(archive_id, ROOM, 'owner'))
    reports = run(service.resume_pending_archives())
    assert reports[0].status == 'verified'
    assert run(conn.fetchval('SELECT count(*) FROM collab_lifecycle_jobs')) == 0


def test_unknown_live_objects_block_deletion(env):
    conn, run, _, _, s3 = env
    service = lifecycle(env)
    s3.objects[LIVE, f'env/test/rooms/{ROOM}/uploads/unregistered'] = b'unregistered'
    with pytest.raises(ValueError, match='unregistered'):
        run(service.archive_room(ROOM, 'owner'))
    assert not any(e[0] == 'delete' for e in s3.events)
    assert run(conn.fetchval('SELECT count(*) FROM collab_messages')) == 3


def test_scrub_resumes_after_partial_metadata_overwrite(env):
    conn, run, _, _, s3 = env
    service = lifecycle(env)
    archive = run(service.archive_room(ROOM, 'owner'))
    put = s3.put_object
    remaining = 1

    def interrupted(**kwargs):
        nonlocal remaining
        if remaining == 0:
            raise OSError('interrupted metadata rewrite')
        remaining -= 1
        return put(**kwargs)

    s3.put_object = interrupted
    with pytest.raises(Exception):
        run(service.scrub_deleted_user('member'))
    s3.put_object = put
    run(lifecycle(env).resume_pending_archives())
    assert run(conn.fetchval("SELECT count(*) FROM collab_users WHERE user_id='member'")) == 0
    assert run(conn.fetchval('SELECT count(*) FROM collab_lifecycle_jobs')) == 0
    restored = run(lifecycle(env).restore_room(archive.archive_id, 'owner'))
    assert restored.room_id != ROOM
    assert run(conn.fetchval('SELECT count(*) FROM collab_messages')) == 1


def test_new_scrub_finishes_older_rewrite_before_scrubbing_another_user(env):
    conn, run, _, _, s3 = env
    service = lifecycle(env)
    archive = run(service.archive_room(ROOM, 'owner'))
    s3.fail_put = True
    with pytest.raises(Exception):
        run(service.scrub_deleted_user('member'))
    s3.fail_put = False
    run(lifecycle(env).scrub_deleted_user('owner'))
    assert run(conn.fetchval('SELECT count(*) FROM collab_users')) == 0
    row = run(conn.fetchrow('SELECT * FROM collab_room_archives'))
    raw = json.loads(s3.objects[ARCHIVE, row['manifest_key']])
    room_obj = next(o for o in raw['objects'] if o['key'].endswith('/room.json'))
    snapshot = json.loads(s3.objects[ARCHIVE, room_obj['key']])
    assert snapshot['room']['owner_id'] is None
    assert snapshot['messages'] == []
    assert snapshot['room_memberships'] == []
    assert archive.archive_id == row['archive_id']


def test_restore_copy_failure_can_resume_and_scrub_requester_without_losing_files(env):
    conn, run, _, _, s3 = env
    service = lifecycle(env)
    archive = run(service.archive_room(ROOM, 'owner'))
    s3.corrupt_copy = True
    with pytest.raises(Exception, match='verification'):
        run(service.restore_room(archive.archive_id, 'member'))
    assert run(conn.fetchval('SELECT count(*) FROM collab_rooms')) == 0
    s3.corrupt_copy = False
    run(lifecycle(env).scrub_deleted_user('member'))
    assert run(conn.fetchval("SELECT count(*) FROM collab_users WHERE user_id='member'")) == 0
    assert run(conn.fetchval('SELECT count(*) FROM collab_room_files')) == 1
    file = run(conn.fetchrow('SELECT * FROM collab_room_files'))
    assert file['uploaded_by'] is None and s3.objects[LIVE, file['storage_key']] == BLOB


def test_scrub_partial_unverified_archive_does_not_leave_session_content(env):
    conn, run, _, _, s3 = env
    service = lifecycle(env)
    s3.corrupt_copy = True
    with pytest.raises(Exception):
        run(service.archive_room(ROOM, 'owner'))
    s3.corrupt_copy = False
    run(lifecycle(env).scrub_deleted_user('member'))
    run(lifecycle(env).resume_pending_archives())
    assert run(conn.fetchval('SELECT count(*) FROM collab_rooms')) == 0
    for (bucket, key), value in s3.objects.items():
        if bucket == ARCHIVE and not key.endswith('/blob') and '/preview/' not in key:
            assert b'private' not in value and b'"member"' not in value
    assert any(data == BLOB for data in s3.objects.values())


def test_unverified_archive_scrub_checks_written_bytes_before_completing(env):
    conn, run, _, _, s3 = env
    service = lifecycle(env)
    s3.corrupt_copy = True
    with pytest.raises(Exception):
        run(service.archive_room(ROOM, 'owner'))
    s3.corrupt_copy = False
    put = s3.put_object

    def corrupt_metadata(**kwargs):
        put(**kwargs)
        if kwargs['Key'].endswith('/room.json'):
            s3.objects[kwargs['Bucket'], kwargs['Key']] = b'private old content'

    s3.put_object = corrupt_metadata
    with pytest.raises(Exception, match='verification'):
        run(service.scrub_deleted_user('member'))
    assert run(conn.fetchval("SELECT status FROM collab_users WHERE user_id='member'")) == 'disabled'
    assert s3.objects[LIVE, live_key('test', ROOM, FILE)] == BLOB
    s3.put_object = put
    run(lifecycle(env).resume_pending_archives())
    assert run(conn.fetchval("SELECT count(*) FROM collab_users WHERE user_id='member'")) == 0


def test_cancellation_keeps_lifecycle_lock_until_storage_io_stops(env):
    import threading
    conn, run, _, _, s3 = env
    service = lifecycle(env)
    started, release = threading.Event(), threading.Event()
    copy_object = s3.copy

    def slow_copy(*args):
        started.set()
        assert release.wait(10)
        return copy_object(*args)

    s3.copy = slow_copy

    async def exercise():
        task = asyncio.create_task(service.archive_room(ROOM, 'owner'))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0.05)
            assert not task.done(), 'cancellation released ownership while object writes were running'
            assert await conn.fetchval("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid()") == 1
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert await conn.fetchval("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid()") == 0

    run(exercise())
    s3.copy = copy_object
    assert run(lifecycle(env).resume_pending_archives())[0].status == 'verified'


def test_service_rechecks_authority_after_acquiring_lifecycle_lock(env):
    from contextlib import asynccontextmanager

    from herdr_engineering.control_plane.collab import (
        CollabError,
        CollabService,
        Principal,
        StaticTokenAuthenticator,
    )
    conn, run, store, _, s3 = env
    service = CollabService(StaticTokenAuthenticator({'owner': 'digest'}), archive_service=lifecycle(env))
    original = store.lifecycle_lock

    @asynccontextmanager
    async def revoked_before_lock():
        async with original():
            await conn.execute("UPDATE collab_users SET status='disabled' WHERE user_id='owner'")
            yield

    store.lifecycle_lock = revoked_before_lock
    with pytest.raises(CollabError):
        run(service.archive_room(ROOM, Principal('user', 'owner', 'owner')))
    assert s3.events == []
    assert run(conn.fetchval('SELECT count(*) FROM collab_rooms')) == 1


def test_scrubbing_short_user_id_preserves_unrelated_audit_and_restore_idempotency(env):
    conn, run, _, _, _ = env
    service = lifecycle(env)
    archive = run(service.archive_room(ROOM, 'owner'))
    restored = run(service.restore_room(archive.archive_id, 'owner'))
    run(conn.execute("INSERT INTO collab_users(user_id,status) VALUES ('a','approved')"))
    before = run(conn.fetchval('SELECT count(*) FROM collab_audit_events'))
    run(service.scrub_deleted_user('a'))
    assert run(conn.fetchval('SELECT count(*) FROM collab_audit_events')) == before + 1
    assert run(service.restore_room(archive.archive_id, 'owner')) == restored


def test_empty_user_id_is_rejected_without_mutation(env):
    conn, run, _, _, _ = env
    service = lifecycle(env)
    before = run(conn.fetchval('SELECT count(*) FROM collab_audit_events'))
    with pytest.raises(ValueError, match='user'):
        run(service.scrub_deleted_user(''))
    assert run(conn.fetchval('SELECT count(*) FROM collab_audit_events')) == before


def test_same_store_cannot_reenter_lifecycle_while_an_operation_owns_it(env):
    _, run, store, _, _ = env

    async def exercise():
        async with store.lifecycle_lock():
            with pytest.raises(ValueError, match='busy'):
                async with store.lifecycle_lock():
                    pytest.fail('two operations acquired the same connection')
        async with store.lifecycle_lock():
            pass  # ownership is released for the next operation

    run(exercise())


@pytest.mark.parametrize('provider_fails', [False, True])
def test_event_loop_shutdown_cancels_outer_and_inner_io_without_hanging(env, provider_fails):
    import subprocess
    import sys
    import textwrap

    # A separate process bounds the old non-yielding cancellation loop. Use the
    # actual Runner shutdown, which cancels both operation and to_thread tasks.
    script = textwrap.dedent('''
        import asyncio
        import contextvars
        import os
        import sys
        import threading
        import asyncpg
        from herdr_engineering.control_plane.collab_archive import CollabArchive
        from herdr_engineering.control_plane.collab_store import CollabStore

        started, release, finished = (threading.Event() for _ in range(3))
        cancelled, ownership_held = threading.Event(), threading.Event()
        context = contextvars.ContextVar('provider_context')
        timer = None

        async def main():
            global timer
            conn = await asyncpg.connect(os.environ['HERDR_E2E_PG_DSN'])
            store = CollabStore(conn)
            service = CollabArchive(store, None)
            context.set('request-context')

            def provider():
                assert context.get() == 'request-context'
                started.set()
                assert release.wait(5)
                finished.set()
                if sys.argv[1] == 'True':
                    raise OSError('provider stopped with an error')
                return 'complete'

            async def operation():
                try:
                    async with store.lifecycle_lock():
                        try:
                            await service._io(provider)
                        except asyncio.CancelledError:
                            assert finished.is_set(), 'ownership released before provider stopped'
                            cancelled.set()
                            raise
                finally:
                    await conn.close()

            asyncio.create_task(operation())
            while not started.is_set():
                await asyncio.sleep(0.001)

            def stop_provider():
                if store._lifecycle_active:
                    ownership_held.set()
                release.set()

            timer = threading.Timer(0.1, stop_provider)
            timer.start()
            # Returning triggers asyncio.run's simultaneous cancellation of
            # all pending tasks, including the old implementation's inner task.

        asyncio.run(main())
        timer.join()
        assert finished.is_set() and cancelled.is_set() and ownership_held.is_set()
        print('shutdown completed with provider stopped and cancellation propagated')
    ''')
    try:
        result = subprocess.run([sys.executable, '-c', script, str(provider_fails)],
                                capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        pytest.fail('simultaneous cancellation hung after the provider thread was released')
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ''
    assert 'shutdown completed' in result.stdout


@pytest.mark.parametrize('interrupt_last_restore', [False, True])
def test_scrub_drains_101_restores_and_recovers_without_losing_room_files(env, interrupt_last_restore):
    from uuid import UUID, uuid5

    conn, run, store, _, s3 = env
    service = lifecycle(env)
    archives = [run(service.archive_room(ROOM, 'owner'))]
    for _ in range(100):
        room_id, file_id = str(uuid4()), str(uuid4())
        run(store.create_room(room_id=room_id, name='Pending restore', owner_id='owner'))
        key = live_key('test', room_id, file_id)
        run(conn.execute(
            "INSERT INTO collab_room_files(file_id,room_id,uploaded_by,relative_path,storage_key,size_bytes,sha256,status) "
            "VALUES ($1,$2,'member','data.csv',$3,$4,$5,'available')", file_id, room_id, key, len(BLOB), DIGEST))
        s3.objects[LIVE, key] = BLOB
        s3.objects[LIVE, live_key('test', room_id, file_id, 'thumbnail')] = b'preview'
        archives.append(run(service.archive_room(room_id, 'owner')))
    for archive in archives:
        new_room = str(uuid5(UUID(archive.archive_id), 'restore:member'))
        run(store.put_job('restore:' + new_room, 'restore',
                         {'archive_id': archive.archive_id, 'requested_by': 'member'}))
    assert run(conn.fetchval("SELECT count(*) FROM collab_lifecycle_jobs WHERE kind='restore'")) == 101

    if interrupt_last_restore:
        copy_object = s3.copy
        last_room = str(uuid5(UUID(archives[-1].archive_id), 'restore:member'))

        def fail_last_restore(source, bucket, key):
            if bucket == LIVE and f'/rooms/{last_room}/' in key:
                raise OSError('interrupted last restore')
            return copy_object(source, bucket, key)

        s3.copy = fail_last_restore
        with pytest.raises(Exception, match='verification'):
            run(service.scrub_deleted_user('member'))
        assert run(conn.fetchval("SELECT status FROM collab_users WHERE user_id='member'")) == 'disabled'
        assert run(conn.fetchval('SELECT count(*) FROM collab_room_files')) == 100
        assert run(conn.fetchval("SELECT count(*) FROM collab_lifecycle_jobs WHERE kind='restore'")) == 1
        assert run(conn.fetchval("SELECT count(*) FROM collab_lifecycle_jobs WHERE kind='scrub'")) == 1
        s3.copy = copy_object
        assert run(lifecycle(env).resume_pending_archives()) == []
    else:
        assert run(service.scrub_deleted_user('member')).status == 'complete'

    assert run(conn.fetchval('SELECT count(*) FROM collab_lifecycle_jobs')) == 0
    assert run(conn.fetchval("SELECT count(*) FROM collab_users WHERE user_id='member'")) == 0
    assert run(conn.fetchval('SELECT count(*) FROM collab_rooms WHERE owner_id IS NOT NULL')) == 0
    files = run(conn.fetch('SELECT * FROM collab_room_files'))
    assert len(files) == 101
    for file in files:
        assert file['uploaded_by'] is None
        assert s3.objects[LIVE, file['storage_key']] == BLOB
        assert s3.objects[LIVE, file['storage_key'].removesuffix('blob') + 'preview/thumbnail'] == b'preview'
    for (bucket, key), data in s3.objects.items():
        if bucket == ARCHIVE:
            if key.endswith('/blob'):
                assert data == BLOB
            elif '/preview/' in key:
                assert data == b'preview'
            else:
                assert b'"member"' not in data and b'private member' not in data
    # The final archive remains independently recoverable after account deletion.
    restored = run(lifecycle(env).restore_room(archives[-1].archive_id, 'owner'))
    restored_files = run(store.list_room_files(restored.room_id))
    assert len(restored_files) == 1 and restored_files[0].uploaded_by is None
    assert s3.objects[LIVE, restored_files[0].storage_key] == BLOB


def test_scrub_removes_stale_restore_job_for_already_published_room(env):
    _, run, store, _, s3 = env
    service = lifecycle(env)
    archive = run(service.archive_room(ROOM, 'owner'))
    restored = run(service.restore_room(archive.archive_id, 'member'))
    run(store.put_job('restore:' + restored.room_id, 'restore',
                     {'archive_id': archive.archive_id, 'requested_by': 'member'}))
    assert run(service.scrub_deleted_user('member')).status == 'complete'
    assert run(store.jobs('restore')) == []
    files = run(store.list_room_files(restored.room_id))
    assert len(files) == 1 and files[0].uploaded_by is None
    assert s3.objects[LIVE, files[0].storage_key] == BLOB

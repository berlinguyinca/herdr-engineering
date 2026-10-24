# ruff: noqa: F811  (pytest fixtures imported from sibling test modules)
"""Direct upload contracts: real Postgres, simulated external S3 transport."""
from __future__ import annotations

import base64
import hashlib
import importlib
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from herdr_engineering.control_plane.collab import (
    CollabError,
    CollabService,
    Principal,
    StaticTokenAuthenticator,
)
from herdr_engineering.control_plane.collab_storage import CollabStorage, S3Config
from tests.control_plane.test_collab_store import database  # noqa: F401

ROOM = '00000000-0000-4000-8000-000000000001'
PART_SIZE = 5 * 1024 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


def b64(hex_digest):
    return base64.b64encode(bytes.fromhex(hex_digest)).decode()


def manifest(data=b'abc', path='data/a.csv'):
    chunks = [data[i:i + PART_SIZE] for i in range(0, len(data), PART_SIZE)] or [b'']
    return {'files': [{'relative_path': path, 'size_bytes': len(data), 'sha256': sha(data),
                       'parts': [{'size_bytes': len(p), 'sha256': sha(p)} for p in chunks]}]}


class S3:
    """Simulates server-computed SHA256, pagination and upload URL constraints."""
    def __init__(self):
        self.uploads, self.objects, self.signed = {}, {}, {}
        self.fail_copy = False
        self.fail_abort = False

    def create_multipart_upload(self, **args):
        uid = uuid4().hex
        self.uploads[uid] = {**args, 'parts': {}}
        return {'UploadId': uid}

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        assert operation == 'upload_part' and 0 < ExpiresIn <= 900
        url = 'https://storage.invalid/upload?signature=' + uuid4().hex
        self.signed[url] = dict(Params)
        return url

    def send(self, grant, data):
        offset = 0
        for part in grant.files[0]['parts']:
            args = self.signed[part['url']]
            chunk = data[offset:offset + part['size_bytes']]
            offset += part['size_bytes']
            assert b64(sha(chunk)) == args['ChecksumSHA256']
            self.uploads[args['UploadId']]['parts'][args['PartNumber']] = chunk

    def list_parts(self, *, Bucket, Key, UploadId, PartNumberMarker=0):
        upload = self.uploads[UploadId]
        assert (upload['Bucket'], upload['Key']) == (Bucket, Key)
        nums = [n for n in sorted(upload['parts']) if n > PartNumberMarker]
        page = nums[:1]  # force real pagination even on tiny tests
        return {'Parts': [{'PartNumber': n, 'Size': len(upload['parts'][n]),
                           'ChecksumSHA256': b64(sha(upload['parts'][n])), 'ETag': f'"etag-{n}"'} for n in page],
                'IsTruncated': len(nums) > 1, 'NextPartNumberMarker': page[-1] if page else 0}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload):
        upload = self.uploads[UploadId]
        assert len(MultipartUpload['Parts']) == len(upload['parts'])
        parts = [upload['parts'][p['PartNumber']] for p in MultipartUpload['Parts']]
        checksum = b64(sha(b''.join(bytes.fromhex(sha(p)) for p in parts))) + '-' + str(len(parts))
        self.objects[Bucket, Key] = (b''.join(parts), checksum, 'COMPOSITE')
        del self.uploads[UploadId]
        return {}

    def head_object(self, *, Bucket, Key, ChecksumMode):
        if (Bucket, Key) not in self.objects:
            raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
        data, digest, kind = self.objects[Bucket, Key]
        return {'ContentLength': len(data), 'ChecksumSHA256': digest, 'ChecksumType': kind}

    def copy(self, source, bucket, key, **kwargs):
        if self.fail_copy:
            raise OSError('SECRET signed provider URL')
        data = self.objects[source['Bucket'], source['Key']][0]
        self.objects[bucket, key] = (data, b64(sha(data)), 'FULL_OBJECT')

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        if self.fail_abort:
            raise OSError('SECRET abort')
        if UploadId not in self.uploads:
            raise ClientError({'Error': {'Code': 'NoSuchUpload'}}, 'AbortMultipartUpload')
        del self.uploads[UploadId]

    def list_multipart_uploads(self, *, Bucket, Prefix, **kwargs):
        return {'Uploads': [{'Key': u['Key'], 'UploadId': uid} for uid, u in self.uploads.items()
                            if u['Bucket'] == Bucket and u['Key'].startswith(Prefix)]}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


@pytest.fixture
def upload_env(database):
    conn, run = database
    run(conn.execute("INSERT INTO collab_users(user_id,status) VALUES ('owner','approved'),('other','approved')"))
    from herdr_engineering.control_plane.collab_store import CollabStore
    store = CollabStore(conn)
    run(store.create_room(room_id=ROOM, name='Room', owner_id='owner'))
    run(store.create_membership(membership_id='membership_owner', room_id=ROOM, user_id='owner',
                               status='approved', role='owner', can_upload=True))
    transport = S3()
    storage = CollabStorage(S3Config(environment='test'), s3=transport)
    service = CollabService(StaticTokenAuthenticator({'owner': 'digest', 'other': 'other'}))
    principal = Principal('user', 'owner', 'owner')
    clock = [1000.0]
    return conn, run, store, storage, transport, service, principal, clock


def setup(env):
    _, run, store, storage, _, service, _, clock = env
    # Kept in test body so the red run reports missing behavior, not collection errors.
    assert hasattr(service, 'start_upload'), 'Task 4 upload service hooks are missing'
    module = importlib.import_module('herdr_engineering.control_plane.collab_uploads')
    service.upload_service = module.CollabUploads(store, storage, now=lambda: clock[0])
    service.upload_runner = run
    return service


def start(env, data=b'abc', options=None):
    return env[1](setup(env).start_upload(env[6], ROOM, manifest(data), options or {}))


def test_upload_preview_is_local_manifest_only_and_has_no_side_effects(upload_env):
    conn, run, _, _, s3, _, pr, _ = upload_env
    service = setup(upload_env)
    preview = run(service.preview_upload(pr, ROOM, manifest()))
    assert (preview.file_count, preview.total_bytes, preview.can_upload) == (1, 3, True)
    assert not s3.uploads and run(conn.fetchval('SELECT count(*) FROM collab_room_uploads')) == 0
    with pytest.raises(CollabError):
        run(service.preview_upload(pr, ROOM, '/etc/**/*'))


@pytest.mark.parametrize('sql', [
    "UPDATE collab_users SET status='disabled'",
    "UPDATE collab_room_memberships SET status='removed'",
    "UPDATE collab_room_memberships SET expires_at=1",
    "UPDATE collab_room_memberships SET can_upload=false,role='contributor'",
    "UPDATE collab_rooms SET status='archiving'",
])
def test_upload_permission_checks_before_grant(upload_env, sql):
    conn, run, _, _, s3, _, _, _ = upload_env
    run(conn.execute(sql))
    with pytest.raises(CollabError):
        start(upload_env)
    assert not s3.uploads and not s3.signed


def test_upload_grant_scoped_hidden_until_atomic_finalize(upload_env):
    conn, run, store, _, s3, service, pr, _ = upload_env
    grant = start(upload_env)
    assert 1000 < grant.expires_at <= 1900
    assert grant.prefix == f'env/test/rooms/{ROOM}/uploads/{grant.upload_id}/'
    assert all(p['Key'].startswith(grant.prefix) for p in s3.signed.values())
    assert run(store.list_room_files(ROOM)) == []
    s3.send(grant, b'abc')
    files = run(service.finalize_upload(pr, grant.upload_id, {'data/a.csv': sha(b'abc')}))
    assert len(files) == 1 and files[0].sha256 == sha(b'abc')
    assert '/files/' in files[0].storage_key
    assert run(store.list_room_files(ROOM)) == files
    assert run(service.finalize_upload(pr, grant.upload_id, {'data/a.csv': sha(b'abc')})) == files
    assert not s3.uploads and not any('/uploads/' in key for _, key in s3.objects)
    assert run(conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE action='upload.finalized'")) == 1


def test_upload_checksum_mismatch_keeps_entire_batch_hidden(upload_env):
    _, run, store, _, s3, service, pr, _ = upload_env
    grant = start(upload_env)
    s3.send(grant, b'abc')
    uid = next(iter(s3.uploads))
    s3.uploads[uid]['parts'][1] = b'bad'
    with pytest.raises(CollabError, match='checksum'):
        run(service.finalize_upload(pr, grant.upload_id, {'data/a.csv': sha(b'abc')}))
    assert run(store.list_room_files(ROOM)) == []


def test_upload_resume_reuses_session_and_only_signs_missing_parts(upload_env):
    _, run, _, _, s3, _, pr, clock = upload_env
    data = b'a' * PART_SIZE + b'b'
    grant = start(upload_env, data)
    uid = next(iter(s3.uploads))
    s3.uploads[uid]['parts'][1] = data[:PART_SIZE]
    clock[0] += 950
    service = setup(upload_env)  # service restart, durable state only
    resumed = run(service.start_upload(pr, ROOM, manifest(data), {'upload_id': grant.upload_id}))
    assert len(s3.uploads) == 1 and resumed.upload_id == grant.upload_id
    assert [p['part_number'] for p in resumed.files[0]['parts']] == [2]
    s3.uploads[uid]['parts'][2] = b'b'
    status = run(service.get_upload_status(pr, grant.upload_id))
    assert (status.uploaded_bytes, status.total_bytes, status.completed_parts) == (len(data), len(data), 2)
    assert len(run(service.finalize_upload(pr, grant.upload_id, {'data/a.csv': sha(data)}))) == 1


def test_upload_idempotency_rejects_different_manifest_and_principal(upload_env):
    _, run, _, _, s3, service, pr, _ = upload_env
    upload_id = str(uuid4())
    grant = start(upload_env, options={'upload_id': upload_id})
    assert run(service.start_upload(pr, ROOM, manifest(), {'upload_id': upload_id})).upload_id == grant.upload_id
    with pytest.raises(CollabError):
        run(service.start_upload(pr, ROOM, manifest(b'changed'), {'upload_id': upload_id}))
    with pytest.raises(CollabError):
        run(service.get_upload_status(Principal('user', 'other', 'other'), upload_id))
    assert len(s3.uploads) == 1


def test_upload_finalize_retries_after_copy_failure_without_provider_leak(upload_env):
    _, run, store, _, s3, service, pr, _ = upload_env
    grant = start(upload_env)
    s3.send(grant, b'abc')
    s3.fail_copy = True
    with pytest.raises(CollabError) as exc:
        run(service.finalize_upload(pr, grant.upload_id, {'data/a.csv': sha(b'abc')}))
    assert 'SECRET' not in str(exc.value) and run(store.list_room_files(ROOM)) == []
    s3.fail_copy = False
    service = setup(upload_env)
    assert len(run(service.finalize_upload(pr, grant.upload_id, {'data/a.csv': sha(b'abc')}))) == 1


def test_upload_cancel_and_worker_cleanup_are_durable(upload_env):
    _, run, store, _, s3, service, pr, clock = upload_env
    grant = start(upload_env)
    s3.fail_abort = True
    with pytest.raises(CollabError):
        run(service.cancel_upload(pr, grant.upload_id))
    s3.fail_abort = False
    clock[0] += 100000
    service = setup(upload_env)
    run(service.upload_service.cleanup_expired_uploads())
    assert not s3.uploads and run(store.list_room_files(ROOM)) == []
    assert run(service.get_upload_status(pr, grant.upload_id)).status == 'failed'
    run(service.cancel_upload(pr, grant.upload_id))
    with pytest.raises(CollabError):
        run(service.finalize_upload(pr, grant.upload_id, {'data/a.csv': sha(b'abc')}))


@pytest.mark.parametrize('path', ['../secret', '/etc/passwd', 'a//b', 'a\\b'])
def test_upload_rejects_unsafe_manifest_paths(upload_env, path):
    _, run, _, _, s3, _, pr, _ = upload_env
    with pytest.raises(CollabError):
        run(setup(upload_env).start_upload(pr, ROOM, manifest(path=path), {}))
    assert not s3.uploads


def test_upload_rechecks_authority_before_publication(upload_env):
    conn, run, store, _, s3, service, pr, _ = upload_env
    grant = start(upload_env)
    s3.send(grant, b'abc')
    original = service.upload_service._io

    async def revoke_after_copy(fn, *args, **kwargs):
        result = await original(fn, *args, **kwargs)
        if fn.__name__ == 'copy':
            await conn.execute("UPDATE collab_users SET status='disabled'")
        return result

    service.upload_service._io = revoke_after_copy
    with pytest.raises(CollabError):
        run(service.finalize_upload(pr, grant.upload_id, {'data/a.csv': sha(b'abc')}))
    assert run(store.list_room_files(ROOM)) == []


def agent(env):
    conn, run, _, _, _, service, _, _ = env
    run(conn.execute("INSERT INTO collab_agents(agent_id,owner_id,name) VALUES ('agent_one','owner','Agent')"))
    run(conn.execute("INSERT INTO collab_agent_room_attachments(room_id,agent_id,actions) VALUES ($1,'agent_one',ARRAY['read','post'])", ROOM))
    # Authentication remains the existing credential boundary.
    service._agents['agent_one'] = {'owner': 'owner', 'status': 'active', 'expires_at': 9999999999, 'cred_digest': 'credential'}
    service._by_cred['credential'] = 'agent_one'
    return Principal('agent', 'agent_one', 'owner', digest='credential')


def test_upload_agent_access_request_never_self_approves(upload_env):
    conn, run, _, _, s3, _, _, _ = upload_env
    service = setup(upload_env)
    pr = agent(upload_env)
    preview = run(service.preview_upload(pr, ROOM, manifest()))
    assert not preview.can_upload and preview.access_request_required
    with pytest.raises(CollabError):
        run(service.start_upload(pr, ROOM, manifest(), {}))
    first = run(service.request_upload_access(pr, ROOM))
    assert run(service.request_upload_access(pr, ROOM)) == first
    assert run(conn.fetchval('SELECT count(*) FROM collab_agent_access_requests')) == 1
    assert not s3.uploads
    run(conn.execute("INSERT INTO collab_agent_permissions(room_id,agent_id,user_id) VALUES ($1,'agent_one','owner')", ROOM))
    grant = run(service.start_upload(pr, ROOM, manifest(), {}))
    run(conn.execute("UPDATE collab_agent_permissions SET status='revoked'"))
    with pytest.raises(CollabError):
        run(service.get_upload_status(pr, grant.upload_id))


def test_upload_batch_registration_rolls_back_and_retries(upload_env):
    conn, run, store, _, s3, _, pr, _ = upload_env
    service = setup(upload_env)
    files = manifest()['files'] + manifest(b'def', 'b.csv')['files']
    grant = run(service.start_upload(pr, ROOM, {'files': files}, {}))
    for uploaded in grant.files:
        for part in uploaded['parts']:
            args = s3.signed[part['url']]
            s3.uploads[args['UploadId']]['parts'][1] = b'abc' if uploaded['relative_path'] == 'data/a.csv' else b'def'
    run(conn.execute("""CREATE FUNCTION reject_second() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.relative_path='b.csv' THEN RAISE EXCEPTION 'injected'; END IF; RETURN NEW; END $$;
        CREATE TRIGGER reject_second BEFORE INSERT ON collab_room_files FOR EACH ROW EXECUTE FUNCTION reject_second();"""))
    checksums = {'data/a.csv': sha(b'abc'), 'b.csv': sha(b'def')}
    with pytest.raises(CollabError):
        run(service.finalize_upload(pr, grant.upload_id, checksums))
    assert run(store.list_room_files(ROOM)) == []
    assert run(conn.fetchval('SELECT count(*) FROM collab_room_files')) == 0
    run(conn.execute('DROP TRIGGER reject_second ON collab_room_files'))
    assert len(run(service.finalize_upload(pr, grant.upload_id, checksums))) == 2


def test_upload_worker_cleans_orphan_multipart_after_checkpoint_loss(upload_env):
    conn, run, _, _, s3, service, _, clock = upload_env
    grant = start(upload_env)
    run(conn.execute('DELETE FROM collab_room_upload_parts'))
    clock[0] += 100000
    run(service.upload_service.cleanup_expired_uploads())
    assert not s3.uploads
    assert run(conn.fetchval('SELECT status FROM collab_room_uploads WHERE upload_id=$1', grant.upload_id)) == 'failed'


def test_upload_head_checksum_required_even_after_successful_completion(upload_env):
    _, run, store, _, s3, service, pr, _ = upload_env
    grant = start(upload_env)
    s3.send(grant, b'abc')
    head = s3.head_object

    def missing(**kwargs):
        result = head(**kwargs)
        result.pop('ChecksumSHA256')
        return result

    s3.head_object = missing
    with pytest.raises(CollabError, match='checksum'):
        run(service.finalize_upload(pr, grant.upload_id, {'data/a.csv': sha(b'abc')}))
    assert run(store.list_room_files(ROOM)) == []


def test_upload_early_finalize_does_not_prevent_resume(upload_env):
    _, run, _, _, _, service, pr, _ = upload_env
    grant = start(upload_env)
    with pytest.raises(CollabError, match='incomplete'):
        run(service.finalize_upload(pr, grant.upload_id, {'data/a.csv': sha(b'abc')}))
    resumed = run(service.start_upload(pr, ROOM, manifest(), {'upload_id': grant.upload_id}))
    assert len(resumed.files[0]['parts']) == 1


def test_upload_shorter_refresh_does_not_forget_previous_grant_expiry(upload_env):
    conn, run, _, _, _, service, pr, clock = upload_env
    grant = start(upload_env)
    run(service.start_upload(pr, ROOM, manifest(), {'upload_id': grant.upload_id, 'expires_in': 1}))
    clock[0] += 2
    run(service.cancel_upload(pr, grant.upload_id))
    state = run(conn.fetchval('SELECT manifest FROM collab_room_uploads WHERE upload_id=$1', grant.upload_id))
    import json
    assert json.loads(state)['cleanup_done'] is False


def test_upload_worker_pass_expires_and_cleans_sessions(upload_env):
    from herdr_engineering.control_plane.worker import run_one_pass
    conn, run, _, _, s3, service, _, clock = upload_env
    start(upload_env)
    clock[0] += 100000
    result = run(run_one_pass(conn, collab_uploads=service.upload_service))
    assert result['collab_uploads'][0]['status'] == 'cleaned'
    assert not s3.uploads


def test_upload_cancelled_session_does_not_block_archiving_after_cleanup(upload_env):
    _, run, store, _, _, service, pr, clock = upload_env
    grant = start(upload_env)
    clock[0] += 100000
    run(service.cancel_upload(pr, grant.upload_id))
    archive = run(store.begin_archive(str(uuid4()), ROOM, 'owner'))
    assert archive.status == 'archiving'


def test_upload_scrub_preserves_cleanup_checkpoint_for_unfinished_session(upload_env):
    _, run, store, _, _, _, _, _ = upload_env
    grant = start(upload_env)
    with pytest.raises(ValueError, match='upload cleanup'):
        run(store.finish_scrub('owner', 'scrub:owner'))
    assert run(store.get_upload(grant.upload_id)).manifest['files']


def test_upload_does_not_sign_after_grant_deadline(upload_env):
    _, run, _, _, s3, _, _, clock = upload_env
    create = s3.create_multipart_upload

    def slow_create(**kwargs):
        clock[0] += 2
        return create(**kwargs)

    s3.create_multipart_upload = slow_create
    with pytest.raises(CollabError, match='expired'):
        start(upload_env, options={'expires_in': 1})
    assert not s3.signed


def test_upload_grant_pages_are_bounded_and_resume_independently(upload_env):
    _, run, _, _, s3, _, pr, _ = upload_env
    service = setup(upload_env)
    files = manifest()['files'] + manifest(b'def', 'z.csv')['files']
    first = run(service.start_upload(pr, ROOM, {'files': files}, {'limit': 1}))
    assert len(first.files) == 1 and first.next_cursor == 1
    second = run(service.start_upload(pr, ROOM, {'files': files},
                                     {'upload_id': first.upload_id, 'limit': 1, 'cursor': first.next_cursor}))
    assert second.files[0]['relative_path'] == 'z.csv' and second.next_cursor is None
    assert len(s3.uploads) == 2


def test_upload_sdk_contract_validates_real_boto3_parameters(upload_env):
    import boto3
    from botocore.validate import validate_parameters

    _, run, _, _, s3, service, pr, _ = upload_env
    client = boto3.client('s3', region_name='us-east-1', aws_access_key_id='test', aws_secret_access_key='test')
    operations = {'create_multipart_upload': 'CreateMultipartUpload', 'head_object': 'HeadObject',
                  'list_parts': 'ListParts', 'complete_multipart_upload': 'CompleteMultipartUpload',
                  'list_multipart_uploads': 'ListMultipartUploads', 'delete_object': 'DeleteObject'}
    for name, operation in operations.items():
        original = getattr(s3, name)

        def checked(*, _original=original, _operation=operation, **kwargs):
            validate_parameters(kwargs, client.meta.service_model.operation_model(_operation).input_shape)
            return _original(**kwargs)

        setattr(s3, name, checked)
    signer = s3.generate_presigned_url

    def sign(operation, *, Params, ExpiresIn):
        signed = client.generate_presigned_url(operation, Params=Params, ExpiresIn=ExpiresIn)
        assert 'Signature=' in signed or 'X-Amz-Signature=' in signed
        return signer(operation, Params=Params, ExpiresIn=ExpiresIn)

    s3.generate_presigned_url = sign
    grant = start(upload_env)
    s3.send(grant, b'abc')
    assert len(run(service.finalize_upload(pr, grant.upload_id, {'data/a.csv': sha(b'abc')}))) == 1


def test_upload_preview_accepts_large_file_part_geometry(upload_env):
    _, run, _, _, _, _, pr, _ = upload_env
    service = setup(upload_env)
    large = {'files': [{'relative_path': 'large.bin', 'size_bytes': 2 * PART_SIZE + 1,
                       'sha256': 'a' * 64, 'parts': [
                           {'size_bytes': 2 * PART_SIZE, 'sha256': 'b' * 64},
                           {'size_bytes': 1, 'sha256': 'c' * 64}]}]}
    preview = run(service.preview_upload(pr, ROOM, large))
    assert preview.can_upload and preview.total_bytes == 2 * PART_SIZE + 1

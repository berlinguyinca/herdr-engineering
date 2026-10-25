# ruff: noqa: F811  (pytest fixtures imported from sibling test modules)
"""HTTP adapters for room files, upload status and file access.

Real Postgres; only the S3 transport is simulated. The adapters must reuse exact-agent upload
ownership, expose finalized files only, and never put provider references or grant material in
a response that a browser (or its history) can retain."""
from __future__ import annotations

import hashlib
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
from herdr_engineering.control_plane.collab_uploads import CollabUploads
from tests.control_plane.test_collab_store import database  # noqa: F401
from tests.control_plane.test_collab_uploads import (  # noqa: F401
    ROOM,
    S3,
    manifest,
    sha,
    upload_env,
)

USERS = ('owner', 'other', 'member', 'guest', 'outsider')


class ReadS3(S3):
    def __init__(self):
        super().__init__()
        self.reads = []
        self.read_url = 'https://objects.example/file?signature=abc'

    def generate_presigned_url(self, operation, *, Params, ExpiresIn, **kwargs):
        if operation != 'get_object':
            return super().generate_presigned_url(operation, Params=Params, ExpiresIn=ExpiresIn)
        if self.read_url is None:
            raise RuntimeError('SECRET provider detail')
        self.reads.append((dict(Params), ExpiresIn))
        return self.read_url


@pytest.fixture
def web(upload_env):
    conn, run, store, storage, _, _, _, clock = upload_env
    s3 = ReadS3()
    storage._archive._s3 = s3
    run(conn.execute("INSERT INTO collab_users(user_id,status) VALUES ('member','approved'),('guest','approved'),"
                     "('outsider','approved')"))
    for user, role, upload in (('member', 'contributor', True), ('guest', 'guest', False)):
        run(store.create_membership(membership_id='m_' + user, room_id=ROOM, user_id=user,
                                    status='approved', role=role, can_upload=upload))
    service = CollabService(StaticTokenAuthenticator({u: hashlib.sha256(f'{u}-token'.encode()).hexdigest() for u in USERS}),
                            upload_service=CollabUploads(store, storage, now=lambda: clock[0]), upload_runner=run)
    service.web_s3 = s3

    def call(method, parts, body=None, who='owner', query=None, **kw):
        return service.dispatch(method, parts, query or {}, body or {}, f'Bearer {who}-token', None, **kw)

    def agent(owner):
        info = service.connect_agent(service.authenticate(f'Bearer {owner}-token', None), {'harness_type': 'python'})
        pr = service.authenticate('Agent ' + info['credential'], info['device_id'])
        run(conn.execute("INSERT INTO collab_agents(agent_id,owner_id,name) VALUES ($1,$2,'Harness')", pr.id, owner))
        run(conn.execute("INSERT INTO collab_agent_room_attachments(room_id,agent_id,actions) "
                         "VALUES ($1,$2,ARRAY['read','post'])", ROOM, pr.id))
        run(conn.execute("INSERT INTO collab_agent_permissions(room_id,agent_id,user_id,permission_id) "
                         "VALUES ($1,$2,$3,$4)", ROOM, pr.id, owner, 'perm_' + pr.id))
        return pr, info

    def publish(pr, files):
        """Start, transfer and finalize one batch as the registered agent; return the grant."""
        files = files if isinstance(files, dict) else {'data/a.csv': files}
        entries = []
        for path, data in files.items():
            entries.append(manifest(data, path)['files'][0])
        grant = run(service.start_upload(pr, ROOM, {'files': entries}, {}))
        return grant, entries

    return type('Web', (), dict(service=service, call=call, agent=agent, publish=publish, run=run, conn=conn,
                                 store=store, s3=s3, clock=clock))


def finalize(web, pr, grant, files):
    for index, (path, data) in enumerate(files.items()):
        args_by_part = [web.s3.signed[p['url']] for p in grant.files[index]['parts']]
        offset = 0
        for part, args in zip(grant.files[index]['parts'], args_by_part):
            chunk = data[offset:offset + part['size_bytes']]
            offset += part['size_bytes']
            web.s3.uploads[args['UploadId']]['parts'][args['PartNumber']] = chunk
    return web.run(web.service.finalize_upload(pr, grant.upload_id, {p: sha(d) for p, d in files.items()}))


def finalized(web, files=None, owner='owner'):
    files = files or {'data/a.csv': b'abc'}
    pr, _ = web.agent(owner)
    grant, _ = web.publish(pr, files)
    return pr, grant, finalize(web, pr, grant, files)


# --------------------------------------------------------------- upload status
STATUS_FIELDS = {'upload_id', 'room_id', 'status', 'file_count', 'total_bytes', 'uploaded_bytes',
                 'completed_parts', 'total_parts'}


def test_room_owner_reads_aggregate_status_of_their_agents_batch_over_http(web):
    pr, info = web.agent('owner')
    grant, _ = web.publish(pr, b'abc')
    status, body = web.call('GET', ['uploads', grant.upload_id])
    assert status == 200 and set(body) == STATUS_FIELDS
    assert body['status'] in {'created', 'uploading'} and body['room_id'] == ROOM
    text = str(body)
    for secret in ('url', 'signature', 'prefix', 'bucket', 'env/test', 'ChecksumSHA256'):
        assert secret not in text
    # the exact registered agent keeps its own (unchanged) access
    assert web.service.dispatch('GET', ['uploads', grant.upload_id], {}, {}, 'Agent ' + info['credential'],
                                info['device_id']) == (200, body)


def test_owner_inspects_a_members_batch_but_other_members_cannot(web):
    pr, _ = web.agent('member')
    grant, _ = web.publish(pr, b'abc')
    assert web.call('GET', ['uploads', grant.upload_id], who='owner')[0] == 200      # room owner
    assert web.call('GET', ['uploads', grant.upload_id], who='member')[0] == 200     # creator's owner
    for who in ('guest', 'outsider'):
        assert web.call('GET', ['uploads', grant.upload_id], who=who)[0] == 404, who
    web.run(web.conn.execute("UPDATE collab_room_memberships SET role='contributor' WHERE user_id='owner'"))
    assert web.call('GET', ['uploads', grant.upload_id], who='owner')[0] == 404      # no longer owner nor creator


def test_another_users_agent_cannot_read_status_over_http(web):
    pr, _ = web.agent('member')
    grant, _ = web.publish(pr, b'abc')
    other_agent, info = web.agent('owner')
    status, _ = web.service.dispatch('GET', ['uploads', grant.upload_id], {}, {}, 'Agent ' + info['credential'],
                                     info['device_id'])
    assert status == 404                                           # exact-principal scoping is unchanged


def test_status_hides_removed_expired_and_archiving_rooms(web):
    pr, _ = web.agent('member')
    grant, _ = web.publish(pr, b'abc')
    for sql in ("UPDATE collab_room_memberships SET status='removed' WHERE user_id='member'",
                "UPDATE collab_users SET status='disabled' WHERE user_id='member'"):
        web.run(web.conn.execute(sql))
        assert web.call('GET', ['uploads', grant.upload_id], who='member')[0] in (403, 404)
        web.run(web.conn.execute(sql.replace("'removed'", "'approved'").replace("'disabled'", "'approved'")))
    web.run(web.conn.execute("UPDATE collab_rooms SET status='archiving'"))
    assert web.call('GET', ['uploads', grant.upload_id], who='owner')[0] == 404


def test_unknown_upload_and_invalid_ids_are_not_found_or_rejected(web):
    assert web.call('GET', ['uploads', str(uuid4())])[0] == 404
    assert web.call('POST', ['uploads', str(uuid4())])[0] == 404


# ------------------------------------------------------------------- file list
FILE_FIELDS = {'file_id', 'room_id', 'relative_path', 'size_bytes', 'status', 'created_at'}


def test_files_are_hidden_until_the_whole_batch_is_finalized_then_listed(web):
    pr, _ = web.agent('owner')
    files = {'data/a.csv': b'abc', 'data/b.csv': b'abcd'}
    grant, _ = web.publish(pr, files)
    status, body = web.call('GET', ['rooms', ROOM, 'files'])
    assert status == 200 and body == {'files': [], 'has_more': False, 'next_cursor': None}
    finalize(web, pr, grant, files)
    status, body = web.call('GET', ['rooms', ROOM, 'files'], who='member')
    assert status == 200 and [f['relative_path'] for f in body['files']] == ['data/a.csv', 'data/b.csv']
    for item in body['files']:
        assert set(item) == FILE_FIELDS and item['room_id'] == ROOM and item['status'] == 'available'
    assert 'storage_key' not in str(body) and 'uploaded_by' not in str(body) and 'env/test' not in str(body)


def test_file_listing_paginates_with_an_opaque_stable_cursor(web):
    files = {f'd/{n}.csv': bytes([65 + n]) * 3 for n in range(5)}
    finalized(web, files)
    web.service.upload_service.FILE_PAGE = 2
    seen, cursor = [], None
    for _ in range(5):
        status, page = web.call('GET', ['rooms', ROOM, 'files'], query={'cursor': cursor} if cursor else {})
        assert status == 200
        seen += [f['relative_path'] for f in page['files']]
        if not page['has_more']:
            assert page['next_cursor'] is None
            break
        cursor = page['next_cursor']
    assert seen == sorted(files)
    for bad in ('x', '!!!', 'bnVsbA', cursor + 'tamper'):
        assert web.call('GET', ['rooms', ROOM, 'files'], query={'cursor': bad})[0] == 400, bad


@pytest.mark.parametrize('who', ['guest', 'outsider'])
def test_non_members_and_guests_cannot_list_or_open_files(web, who):
    _, _, files = finalized(web)
    assert web.call('GET', ['rooms', ROOM, 'files'], who=who)[0] == 404
    assert web.call('POST', ['rooms', ROOM, 'files', files[0].file_id, 'access'], {'purpose': 'download'}, who=who)[0] == 404
    assert not web.s3.reads


def test_agents_are_refused_on_the_browser_file_routes(web):
    pr, info = web.agent('owner')
    for method, parts, body in (('GET', ['rooms', ROOM, 'files'], {}),
                                ('POST', ['rooms', ROOM, 'files', 'x', 'access'], {'purpose': 'download'})):
        status, _ = web.service.dispatch(method, parts, {}, body, 'Agent ' + info['credential'], info['device_id'])
        assert status == 403


def test_removed_member_and_archiving_room_lose_the_listing(web):
    finalized(web)
    web.run(web.conn.execute("UPDATE collab_room_memberships SET status='removed' WHERE user_id='member'"))
    assert web.call('GET', ['rooms', ROOM, 'files'], who='member')[0] == 404
    web.run(web.conn.execute("UPDATE collab_rooms SET status='archiving'"))
    assert web.call('GET', ['rooms', ROOM, 'files'])[0] == 404


# ----------------------------------------------------------------- file access
def test_access_returns_a_short_lived_https_object_url_for_a_finalized_file(web):
    _, _, files = finalized(web)
    file = files[0]
    status, grant = web.call('POST', ['rooms', ROOM, 'files', file.file_id, 'access'], {'purpose': 'download'},
                             who='member')
    assert status == 200
    assert set(grant) == {'url', 'expires_at', 'file_id', 'room_id', 'purpose', 'size_bytes'}
    assert grant['url'] == web.s3.read_url and urlsplit(grant['url']).scheme == 'https'
    assert web.clock[0] < grant['expires_at'] <= web.clock[0] + 300
    params, expires = web.s3.reads[-1]
    assert params['Key'] == file.storage_key and params['Bucket'] == web.service.upload_service.bucket
    assert expires <= 300
    assert params['ResponseContentDisposition'].startswith('attachment') and 'a.csv' in params['ResponseContentDisposition']
    assert params['ResponseContentType'] == 'application/octet-stream'      # never rendered as active content
    preview = web.call('POST', ['rooms', ROOM, 'files', file.file_id, 'access'], {'purpose': 'preview'})[1]
    assert preview['purpose'] == 'preview' and 'ResponseContentDisposition' not in web.s3.reads[-1][0]
    assert web.run(web.conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE action='file.access_granted'")) == 2


def test_access_refuses_unfinalized_other_room_and_malformed_requests(web):
    pr, _ = web.agent('owner')
    grant, _ = web.publish(pr, b'abc')                                # not finalized: no room file exists yet
    other = str(uuid4())
    assert web.call('POST', ['rooms', ROOM, 'files', other, 'access'], {'purpose': 'download'})[0] == 404
    _, _, files = finalized(web, {'data/b.csv': b'zzz'})
    path = ['rooms', ROOM, 'files', files[0].file_id, 'access']
    for body in ({}, {'purpose': 'stream'}, {'purpose': 'download', 'url': 'x'}, {'purpose': ['download']}):
        assert web.call('POST', path, body)[0] == 400, body
    assert web.call('POST', ['rooms', 'room_missing', 'files', files[0].file_id, 'access'], {'purpose': 'download'})[0] == 404
    assert not web.s3.reads


def test_access_fails_closed_without_leaking_provider_detail(web):
    _, _, files = finalized(web)
    path = ['rooms', ROOM, 'files', files[0].file_id, 'access']
    web.s3.read_url = None
    status, body = web.call('POST', path, {'purpose': 'download'})
    assert status == 503 and 'SECRET' not in str(body) and 'url' not in body
    for bad in ('http://objects.example/x', 'https://user:pw@objects.example/x', 'file:///etc/passwd', 'https:///x'):
        web.s3.read_url = bad
        status, body = web.call('POST', path, {'purpose': 'preview'})
        assert status == 503 and 'url' not in body, bad
    assert web.run(web.conn.fetchval("SELECT count(*) FROM collab_audit_events WHERE action='file.access_granted'")) == 0


# --------------------------------------------------------------- upload preview
def test_preview_is_a_dry_run_with_no_side_effects(web):
    status, body = web.call('POST', ['rooms', ROOM, 'uploads', 'preview'], {'manifest': manifest(b'abc')})
    assert status == 200 and body == {'room_id': ROOM, 'file_count': 1, 'total_bytes': 3,
                                      'can_upload': True, 'access_request_required': False}
    status, body = web.call('POST', ['rooms', ROOM, 'uploads', 'preview'], {'manifest': manifest(b'abc')}, who='guest')
    assert status == 404
    web.run(web.conn.execute("UPDATE collab_room_memberships SET can_upload=false WHERE user_id='member'"))
    status, body = web.call('POST', ['rooms', ROOM, 'uploads', 'preview'], {'manifest': manifest(b'abc')}, who='member')
    assert status == 200 and body['can_upload'] is False and body['access_request_required'] is True
    assert not web.s3.uploads and web.run(web.conn.fetchval('SELECT count(*) FROM collab_room_uploads')) == 0


@pytest.mark.parametrize('body', [{}, {'manifest': '/etc/**/*'}, {'manifest': manifest(), 'options': {}},
                                  {'manifest': {'files': []}}])
def test_preview_rejects_paths_unknown_keys_and_empty_manifests(web, body):
    assert web.call('POST', ['rooms', ROOM, 'uploads', 'preview'], body)[0] == 400


# ------------------------------------------------------- fail closed / cutover
def test_every_route_fails_closed_without_the_upload_backend(web):
    bare = CollabService(StaticTokenAuthenticator({'owner': hashlib.sha256(b'owner-token').hexdigest()}))
    for method, parts, body in (('GET', ['rooms', ROOM, 'files'], {}), ('GET', ['uploads', 'u1'], {}),
                                ('POST', ['rooms', ROOM, 'files', 'f', 'access'], {'purpose': 'download'}),
                                ('POST', ['rooms', ROOM, 'uploads', 'preview'], {'manifest': manifest()})):
        assert bare.dispatch(method, parts, {}, body, 'Bearer owner-token', None)[0] == 503, parts
    no_runner = CollabService(StaticTokenAuthenticator({'owner': hashlib.sha256(b'owner-token').hexdigest()}),
                              upload_service=web.service.upload_service)
    assert no_runner.dispatch('GET', ['rooms', ROOM, 'files'], {}, {}, 'Bearer owner-token', None)[0] == 503


def test_legacy_event_log_rooms_require_cutover_before_file_access(web):
    owner = web.service.authenticate('Bearer owner-token', None)
    project = web.service.create_project(owner, {'name': 'legacy'})['project_id']
    legacy = web.service.create_room(owner, project, {'name': 'old'})['room_id']
    for method, parts, body in (('GET', ['rooms', legacy, 'files'], {}),
                                ('POST', ['rooms', legacy, 'uploads', 'preview'], {'manifest': manifest()})):
        status, out = web.call(method, parts, body)
        assert status in (404, 409), parts


def test_dashboard_operator_listener_serves_the_same_records(web):
    _, _, files = finalized(web)
    public = web.call('GET', ['rooms', ROOM, 'files'], who='owner')
    dashboard = web.service.dispatch('GET', ['rooms', ROOM, 'files'], {}, {}, '', None, operator='owner')
    assert dashboard == public and dashboard[1]['files'][0]['file_id'] == files[0].file_id

# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""HTTP adapters for room/person lifecycle contracts that already exist as service methods.

Every adapter must reuse the service's authorization, reject unknown body keys, report the
returned record's real state (never a hard-coded success) and fail closed when its backend
is not injected. None may fall back to the legacy kick/removal path."""
from __future__ import annotations

import asyncio
import types
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
from herdr_engineering.control_plane.collab_archive import ScrubReport
from herdr_engineering.control_plane.collab_store import ArchiveRecord, MembershipRecord, RoomRecord
from tests.control_plane.test_collab import _digest
from tests.control_plane.test_collab_tasks import USERS, svc, w  # noqa: F401

ROOM = 'room_relational'


def bearer(name):
    return 'Bearer ' + USERS[name]


def call(s, method, parts, body=None, who='alice', **kw):
    return s.dispatch(method, parts, {}, body or {}, bearer(who) if who else '', None, **kw)


def committed(s, *types_):
    return [e for e in s._log if e['event_type'] in types_]


# ------------------------------------------------------------- member approval
def test_owner_approves_a_pending_room_member_over_http(w):
    w.s.invite_member(w.alice, w.room, {'user_id': 'mallory'})
    assert w.s.room_member(w.room, 'mallory')['room_membership_status'] == 'pending'
    status, member = call(w.s, 'POST', ['rooms', w.room, 'members', 'mallory', 'approve'],
                          {'idempotency_key': 'approve-1'})
    assert status == 200 and member['room_membership_status'] == 'approved'
    approvals = len(committed(w.s, 'room_member_approved'))
    assert call(w.s, 'POST', ['rooms', w.room, 'members', 'mallory', 'approve'],
                {'idempotency_key': 'approve-1'})[0] == 200
    assert len(committed(w.s, 'room_member_approved')) == approvals      # a retry is not a second approval


@pytest.mark.parametrize('who', ['bob', 'carol', 'mallory'])
def test_only_the_room_owner_may_approve_members(w, who):
    w.s.invite_member(w.alice, w.room, {'user_id': 'mallory'})
    status, _ = call(w.s, 'POST', ['rooms', w.room, 'members', 'mallory', 'approve'], who=who)
    assert status in (403, 404)
    assert w.s.room_member(w.room, 'mallory')['room_membership_status'] == 'pending'


def test_member_approval_rejects_unknown_body_keys_and_never_removes(w):
    w.s.invite_member(w.alice, w.room, {'user_id': 'mallory'})
    status, _ = call(w.s, 'POST', ['rooms', w.room, 'members', 'mallory', 'approve'],
                     {'decision': 'remove'})
    assert status == 400
    assert not committed(w.s, 'room_member_removed', 'user_decided')


# ------------------------------------------------------------ upload permission
def test_owner_grants_and_revokes_upload_permission_over_http(w):
    path = ['rooms', w.room, 'members', 'bob', 'upload-permission']
    status, member = call(w.s, 'POST', path, {'file_upload_permission': True})
    assert status == 200 and member['file_upload_permission'] is True
    status, member = call(w.s, 'POST', path, {'file_upload_permission': False})
    assert status == 200 and member['file_upload_permission'] is False


@pytest.mark.parametrize('body', [
    {}, {'file_upload_permission': 'true'}, {'file_upload_permission': 1},
    {'file_upload_permission': True, 'role': 'owner'}])
def test_upload_permission_requires_exactly_a_boolean(w, body):
    status, _ = call(w.s, 'POST', ['rooms', w.room, 'members', 'bob', 'upload-permission'], body)
    assert status == 400
    assert w.s.room_member(w.room, 'bob')['file_upload_permission'] is False


def test_a_member_cannot_grant_themselves_upload_permission(w):
    status, _ = call(w.s, 'POST', ['rooms', w.room, 'members', 'bob', 'upload-permission'],
                     {'file_upload_permission': True}, who='bob')
    assert status in (403, 404)
    assert w.s.room_member(w.room, 'bob')['file_upload_permission'] is False


# ------------------------------------------------------- archive / restore / scrub
def archive_record(**kw):
    return ArchiveRecord(**{
        'archive_id': 'arch1', 'original_room_id': ROOM, 'live_room_id': None, 'requested_by': 'alice',
        'status': 'verified', 'manifest_key': 'env/test/archives/arch1/manifest.json',
        'manifest_sha256': 'a' * 64, 'verified_at': datetime.now(UTC), 'created_at': datetime.now(UTC), **kw})


class FakeArchive:
    def __init__(self, record=None, scrub=None, error=None):
        self.record, self.scrub, self.error, self.calls = record or archive_record(), scrub, error, []

        async def approved(user_id):
            return True

        async def get_room(room_id):
            return RoomRecord(room_id=room_id, project_id=None, name='Room', owner_id='alice',
                              status='active', created_at=datetime.now(UTC))

        async def get_membership(room_id, user_id):
            return MembershipRecord(membership_id='m', room_id=room_id, user_id=user_id, status='approved',
                                    role='owner' if user_id == 'alice' else 'contributor', can_upload=True,
                                    expires_at=None, joined_seq=0, created_at=datetime.now(UTC))

        async def get_archive(archive_id):
            return self.record if archive_id == self.record.archive_id else None
        self.store = types.SimpleNamespace(approved_user=approved, get_room=get_room,
                                           get_membership=get_membership, get_archive=get_archive)

    async def archive_room(self, room_id, requested_by, *, authorize=None):
        await authorize()
        self.calls.append(('archive', room_id, requested_by))
        if self.error:
            raise self.error
        return self.record

    async def restore_room(self, archive_id, requested_by, *, authorize=None):
        await authorize()
        self.calls.append(('restore', archive_id, requested_by))
        return RoomRecord(room_id='room_restored', project_id=None, name='Restored', owner_id='alice',
                          status='active', created_at=datetime.now(UTC))

    async def scrub_deleted_user(self, user_id, *, authorize=None):
        await authorize()
        self.calls.append(('scrub', user_id))
        return self.scrub or ScrubReport('complete', 2)


def relational(backend, **kw):
    s = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}),
                      archive_service=backend, archive_runner=asyncio.run, **kw)
    return s


def test_archive_over_http_reports_the_records_real_state_and_no_internal_keys():
    backend = FakeArchive()
    s = relational(backend)
    status, out = call(s, 'POST', ['rooms', ROOM, 'archive'])
    assert status == 200
    assert out == {'archive_id': 'arch1', 'room_id': ROOM, 'status': 'verified', 'live_room_id': None}
    assert backend.calls == [('archive', ROOM, 'alice')]


@pytest.mark.parametrize('record', [
    archive_record(status='archiving', live_room_id=ROOM),
    archive_record(status='failed', live_room_id=ROOM),
    archive_record(status='verified', live_room_id=ROOM)])
def test_unfinished_archive_never_reads_as_deleted(record):
    s = relational(FakeArchive(record))
    status, out = call(s, 'POST', ['rooms', ROOM, 'archive'])
    # The browser treats anything other than verified + live_room_id null as "not deleted".
    assert out['status'] == record.status and out['live_room_id'] == ROOM
    assert status in (200, 202, 409, 503)


def test_archive_failure_is_a_visible_error_without_provider_detail():
    s = relational(FakeArchive(error=OSError('SECRET provider endpoint')))
    status, out = call(s, 'POST', ['rooms', ROOM, 'archive'])
    assert status == 503 and 'SECRET' not in str(out) and 'status' not in out


def test_archive_requires_an_injected_service_and_runner():
    s = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}))
    assert call(s, 'POST', ['rooms', ROOM, 'archive'])[0] == 503
    s = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}),
                      archive_service=FakeArchive())
    assert call(s, 'POST', ['rooms', ROOM, 'archive'])[0] == 503


def test_archive_route_rejects_a_body_and_legacy_rooms(w):
    backend = FakeArchive()
    s = relational(backend)
    assert call(s, 'POST', ['rooms', ROOM, 'archive'], {'force': True})[0] == 400
    assert not backend.calls
    legacy = relational(backend)
    legacy.hydrate(w.s._log)                                       # rooms known only to the event log
    status, out = call(legacy, 'POST', ['rooms', w.room, 'archive'])
    assert status == 409 and 'cutover' in out['error']
    assert not backend.calls


def test_restore_over_http_returns_the_new_room():
    backend = FakeArchive()
    s = relational(backend)
    status, out = call(s, 'POST', ['archives', 'arch1', 'restore'])
    assert status == 201 and out['room_id'] == 'room_restored' and out['status'] == 'active'
    assert backend.calls == [('restore', 'arch1', 'alice')]
    assert call(s, 'POST', ['archives', 'arch1', 'restore'], {'room_id': 'x'})[0] == 400


def test_delete_person_requires_an_operator_and_reports_real_scrub_state():
    backend = FakeArchive()
    s = relational(backend)
    status, out = call(s, 'POST', ['admin', 'users', 'bob', 'delete'], operator='alice')
    assert status == 200 and out == {'status': 'complete', 'archives_scrubbed': 2}
    assert backend.calls == [('scrub', 'bob')]
    pending = relational(FakeArchive(scrub=ScrubReport('pending', 0)))
    assert call(pending, 'POST', ['admin', 'users', 'bob', 'delete'], operator='alice')[1]['status'] == 'pending'


def test_delete_person_is_refused_for_non_operators_and_self():
    backend = FakeArchive()
    s = relational(backend)
    assert call(s, 'POST', ['admin', 'users', 'carol', 'delete'], who='bob')[0] in (403, 404)
    assert call(s, 'POST', ['admin', 'users', 'alice', 'delete'], operator='alice')[0] == 400
    assert not backend.calls


def test_delete_person_never_falls_back_to_legacy_kick_for_event_log_users(w):
    backend = FakeArchive()
    s = relational(backend)
    s.hydrate(w.s._log)
    status, out = call(s, 'POST', ['admin', 'users', 'bob', 'delete'], operator='alice')
    assert status == 409 and 'cutover' in out['error']
    assert not backend.calls
    assert not committed(s, 'room_member_removed', 'agent_revoked', 'user_decided')


def test_delete_person_rejects_a_body_and_unwired_services():
    s = relational(FakeArchive())
    assert call(s, 'POST', ['admin', 'users', 'bob', 'delete'], {'kick': True}, operator='alice')[0] == 400
    bare = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}))
    assert call(bare, 'POST', ['admin', 'users', 'bob', 'delete'], operator='alice')[0] == 503

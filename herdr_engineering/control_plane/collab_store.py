"""SQL boundary for collaboration's relational data platform.

Callers supply an asyncpg connection on its owning loop and perform authorization
before entering this repository. Stable IDs are creation idempotency keys: an
identical retry returns the current durable row; conflicting reuse raises ValueError.
Creation audit fingerprints retain the original request identity across updates.
Each new row and its minimal audit event commit together. No credentials or file
bytes are stored here. Existing CollabService lifecycle events remain authoritative
until an explicit migration/backfill integrates the two persistence paths.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class RoomRecord:
    room_id: str
    project_id: str | None
    name: str
    owner_id: str | None
    status: str
    created_at: datetime


@dataclass(frozen=True)
class MembershipRecord:
    membership_id: str | None
    room_id: str
    user_id: str
    status: str
    role: str
    can_upload: bool
    expires_at: float | None
    joined_seq: int
    created_at: datetime


@dataclass(frozen=True)
class AttachmentRecord:
    room_id: str
    agent_id: str
    actions: list[str]
    expires_at: float | None
    status: str
    granted_seq: int
    created_at: datetime


@dataclass(frozen=True)
class MessageRecord:
    message_id: str
    room_id: str
    author_user_id: str | None
    author_agent_id: str | None
    text: str
    created_at: datetime


@dataclass(frozen=True)
class UploadRecord:
    upload_id: str
    room_id: str
    created_by: str | None
    status: str
    manifest: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class RoomFileRecord:
    file_id: str
    room_id: str
    upload_id: str | None
    uploaded_by: str | None
    relative_path: str
    storage_key: str
    size_bytes: int
    sha256: str | None
    status: str
    created_at: datetime


@dataclass(frozen=True)
class ArchiveRecord:
    archive_id: str
    original_room_id: str
    live_room_id: str | None
    requested_by: str | None
    status: str
    manifest_key: str | None
    manifest_sha256: str | None
    verified_at: datetime | None
    created_at: datetime


class CollabStore:
    def __init__(self, conn: Any):
        self._conn = conn
        self._lifecycle_active = False

    async def _create(self, table: str, key: str | tuple[str, ...], values: dict[str, Any], *,
                      actor_id: str | None, entity_type: str) -> dict[str, Any]:
        # Table/column names are internal constants from the methods below;
        # every caller-supplied value is bound as an asyncpg parameter.
        columns = ', '.join(values)
        placeholders = ', '.join(f'${i}' for i in range(1, len(values) + 1))
        args = [json.dumps(v) if k == 'manifest' else v for k, v in values.items()]
        keys = (key,) if isinstance(key, str) else key
        identity = [values[k] for k in keys]
        entity_id = identity[0] if len(keys) == 1 else json.dumps(identity, separators=(',', ':'))
        event_id = f'{entity_type}.created:{entity_id}'
        fingerprint = hashlib.sha256(json.dumps(
            {'table': table, 'values': values}, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        async with self._conn.transaction():
            await self._conn.execute('SELECT pg_advisory_xact_lock_shared($1)', LIFECYCLE_LOCK)
            if table not in {'collab_rooms', 'collab_room_archives'} and 'room_id' in values:
                status = await self._conn.fetchval(
                    'SELECT status FROM collab_rooms WHERE room_id=$1 FOR UPDATE', values['room_id'])
                if status != 'active':
                    raise ValueError('room is not active')
            row = await self._conn.fetchrow(
                f'INSERT INTO {table} ({columns}) VALUES ({placeholders}) '
                f'ON CONFLICT ({", ".join(keys)}) DO NOTHING RETURNING *', *args)
            inserted = row is not None
            if row is None:
                where = ' AND '.join(f'{k} = ${i}' for i, k in enumerate(keys, 1))
                row = await self._conn.fetchrow(
                    f'SELECT * FROM {table} WHERE {where} FOR UPDATE', *identity)
            if row is None:
                raise ValueError(f'idempotency conflict for {entity_type}')
            record = dict(row)
            if 'manifest' in record and isinstance(record['manifest'], str):
                record['manifest'] = json.loads(record['manifest'])
            if inserted:
                await self._conn.execute(
                    'INSERT INTO collab_audit_events '
                    '(event_id, actor_id, action, entity_type, entity_id, request_fingerprint) '
                    'VALUES ($1, $2, $3, $4, $5, $6)',
                    event_id, actor_id, f'{entity_type}.created', entity_type, entity_id, fingerprint)
            else:
                original = await self._conn.fetchval(
                    'SELECT request_fingerprint FROM collab_audit_events WHERE event_id = $1',
                    event_id)
                if original != fingerprint:
                    raise ValueError(f'idempotency conflict for {entity_type}')
            return record

    async def create_room(self, *, room_id: str, name: str, owner_id: str,
                          project_id: str | None = None) -> RoomRecord:
        return RoomRecord(**await self._create(
            'collab_rooms', 'room_id',
            dict(room_id=room_id, name=name, owner_id=owner_id, project_id=project_id),
            actor_id=owner_id, entity_type='room'))

    async def create_membership(self, *, membership_id: str, room_id: str, user_id: str,
                                status: str = 'pending', role: str = 'viewer',
                                can_upload: bool = False, expires_at: float | None = None,
                                joined_seq: int = 0) -> MembershipRecord:
        return MembershipRecord(**await self._create(
            'collab_room_memberships', 'membership_id',
            dict(membership_id=membership_id, room_id=room_id, user_id=user_id,
                 status=status, role=role, can_upload=can_upload,
                 expires_at=float(expires_at) if expires_at is not None else None,
                 joined_seq=joined_seq), actor_id=None, entity_type='membership'))

    async def get_membership(self, room_id: str, user_id: str) -> MembershipRecord | None:
        """Return the persisted state, including expiry, for service authorization."""
        row = await self._conn.fetchrow(
            'SELECT * FROM collab_room_memberships WHERE room_id = $1 AND user_id = $2',
            room_id, user_id)
        return MembershipRecord(**dict(row)) if row is not None else None

    async def create_attachment(self, *, room_id: str, agent_id: str, actions: list[str],
                                expires_at: float | None = None, status: str = 'active',
                                granted_seq: int = 0) -> AttachmentRecord:
        return AttachmentRecord(**await self._create(
            'collab_agent_room_attachments', ('room_id', 'agent_id'),
            dict(room_id=room_id, agent_id=agent_id, actions=actions,
                 expires_at=float(expires_at) if expires_at is not None else None,
                 status=status, granted_seq=granted_seq),
            actor_id=None, entity_type='attachment'))

    async def get_attachment(self, room_id: str, agent_id: str) -> AttachmentRecord | None:
        """Return raw grant inputs, even if lapsed before housekeeping runs.

        Callers must exclude revoked/expired status and check expires_at against
        their clock before authorization or renewal. Renewal must revoke prior
        delegation first, as CollabService already does through its event log.
        """
        row = await self._conn.fetchrow(
            'SELECT * FROM collab_agent_room_attachments WHERE room_id = $1 AND agent_id = $2',
            room_id, agent_id)
        return AttachmentRecord(**dict(row)) if row is not None else None

    async def append_message(self, *, message_id: str, room_id: str, text: str,
                             author_user_id: str | None = None,
                             author_agent_id: str | None = None) -> MessageRecord:
        return MessageRecord(**await self._create(
            'collab_messages', 'message_id',
            dict(message_id=message_id, room_id=room_id, text=text,
                 author_user_id=author_user_id, author_agent_id=author_agent_id),
            actor_id=author_user_id, entity_type='message'))

    async def register_upload(self, *, upload_id: str, room_id: str, created_by: str,
                              manifest: dict[str, Any]) -> UploadRecord:
        """Register the upload manifest; this does not publish any room files."""
        return UploadRecord(**await self._create(
            'collab_room_uploads', 'upload_id',
            dict(upload_id=upload_id, room_id=room_id, created_by=created_by, manifest=manifest),
            actor_id=created_by, entity_type='upload'))

    async def list_room_files(self, room_id: str) -> list[RoomFileRecord]:
        rows = await self._conn.fetch(
            "SELECT f.* FROM collab_room_files f "
            "JOIN collab_rooms r ON r.room_id = f.room_id AND r.status = 'active' "
            "LEFT JOIN collab_room_uploads u ON u.room_id = f.room_id AND u.upload_id = f.upload_id "
            "WHERE f.room_id = $1 AND f.status = 'available' "
            "AND (f.upload_id IS NULL OR u.status = 'available') "
            "ORDER BY f.relative_path, f.file_id", room_id)
        return [RoomFileRecord(**dict(row)) for row in rows]

    async def record_archive(self, *, archive_id: str, room_id: str,
                             requested_by: str, manifest_key: str | None = None) -> ArchiveRecord:
        """Start a catalog entry; recording it never verifies or deletes a room."""
        return ArchiveRecord(**await self._create(
            'collab_room_archives', 'archive_id',
            dict(archive_id=archive_id, original_room_id=room_id, live_room_id=room_id,
                 requested_by=requested_by, manifest_key=manifest_key),
            actor_id=requested_by, entity_type='archive'))

    # Lifecycle calls use one dedicated connection on its owning event loop.
    # A session lock survives checkpoint commits and is released on disconnect.
    @asynccontextmanager
    async def lifecycle_lock(self):
        if self._lifecycle_active:
            raise ValueError('collaboration lifecycle busy; retry later')
        self._lifecycle_active = True
        acquired = False
        try:
            acquired = await self._conn.fetchval('SELECT pg_try_advisory_lock($1)', LIFECYCLE_LOCK)
            if not acquired:
                raise ValueError('collaboration lifecycle busy; retry later')
            yield
        finally:
            try:
                if acquired:
                    await self._conn.execute('SELECT pg_advisory_unlock($1)', LIFECYCLE_LOCK)
            finally:
                self._lifecycle_active = False

    async def get_room(self, room_id: str) -> RoomRecord | None:
        row = await self._conn.fetchrow('SELECT * FROM collab_rooms WHERE room_id=$1', room_id)
        return RoomRecord(**dict(row)) if row else None

    async def get_archive(self, archive_id: str) -> ArchiveRecord | None:
        row = await self._conn.fetchrow('SELECT * FROM collab_room_archives WHERE archive_id=$1', archive_id)
        return ArchiveRecord(**dict(row)) if row else None

    async def list_archives(self) -> list[ArchiveRecord]:
        return [ArchiveRecord(**dict(r)) for r in await self._conn.fetch(
            'SELECT * FROM collab_room_archives ORDER BY archive_id')]

    async def approved_user(self, user_id: str) -> bool:
        return await self._conn.fetchval('SELECT status FROM collab_users WHERE user_id=$1', user_id) == 'approved'

    async def put_job(self, job_id: str, kind: str, payload: dict) -> None:
        await self._conn.execute(
            'INSERT INTO collab_lifecycle_jobs(job_id,kind,payload) VALUES ($1,$2,$3::jsonb) '
            'ON CONFLICT(job_id) DO UPDATE SET payload=EXCLUDED.payload',
            job_id, kind, json.dumps(payload, default=_json_default))

    async def get_job(self, job_id: str) -> dict | None:
        row = await self._conn.fetchrow('SELECT * FROM collab_lifecycle_jobs WHERE job_id=$1', job_id)
        if row is None:
            return None
        result = dict(row)
        if isinstance(result['payload'], str):
            result['payload'] = json.loads(result['payload'])
        return result

    async def jobs(self, kind: str, limit: int = 100) -> list[dict]:
        rows = await self._conn.fetch(
            'SELECT job_id FROM collab_lifecycle_jobs WHERE kind=$1 ORDER BY created_at,job_id LIMIT $2',
            kind, limit)
        return [await self.get_job(r['job_id']) for r in rows]

    async def remove_job(self, job_id: str) -> None:
        await self._conn.execute('DELETE FROM collab_lifecycle_jobs WHERE job_id=$1', job_id)

    async def begin_archive(self, archive_id: str, room_id: str, requested_by: str) -> ArchiveRecord:
        async with self._conn.transaction():
            existing = await self.get_archive(archive_id)
            if existing:
                return existing
            room = await self._conn.fetchrow('SELECT * FROM collab_rooms WHERE room_id=$1 FOR UPDATE', room_id)
            if room is None or room['status'] != 'active':
                raise ValueError('room is not active')
            if await self._conn.fetchval('SELECT EXISTS(SELECT 1 FROM collab_skills WHERE room_id=$1)', room_id):
                raise ValueError('room skill storage requires an archive exporter')
            if await self._conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM collab_room_uploads WHERE room_id=$1 AND status!='available')", room_id):
                raise ValueError('room has unfinished uploads')
            snapshot = {'room': dict(room)}
            for name in ROOM_TABLES:
                rows = await self._conn.fetch(f'SELECT * FROM collab_{name} WHERE room_id=$1', room_id)
                snapshot[name] = [dict(r) for r in rows]
            snapshot['agents'] = [dict(r) for r in await self._conn.fetch(
                'SELECT * FROM collab_agents WHERE agent_id IN ('
                'SELECT agent_id FROM collab_agent_room_attachments WHERE room_id=$1 UNION '
                'SELECT author_agent_id FROM collab_messages WHERE room_id=$1 UNION '
                'SELECT author_agent_id FROM collab_session_logs WHERE room_id=$1)', room_id)]
            snapshot['room_upload_parts'] = [dict(r) for r in await self._conn.fetch(
                'SELECT p.* FROM collab_room_upload_parts p JOIN collab_room_uploads u USING(upload_id) WHERE u.room_id=$1', room_id)]
            # JSONB columns may be returned as text by asyncpg's default codec.
            for upload in snapshot['room_uploads']:
                if isinstance(upload['manifest'], str):
                    upload['manifest'] = json.loads(upload['manifest'])
            archive = await self.record_archive(archive_id=archive_id, room_id=room_id, requested_by=requested_by)
            await self.put_job(archive_id, 'archive', {'phase': 'export', 'snapshot': snapshot})
            await self._conn.execute("UPDATE collab_rooms SET status='archiving' WHERE room_id=$1", room_id)
            return archive

    async def archive_failed(self, archive_id: str) -> None:
        await self._conn.execute(
            "UPDATE collab_room_archives SET status='failed' WHERE archive_id=$1 AND status!='verified'", archive_id)

    async def verified_archive(self, archive_id: str, manifest_key: str, digest: str,
                               payload: dict | None = None) -> ArchiveRecord:
        async with self._conn.transaction():
            row = await self._conn.fetchrow(
                "UPDATE collab_room_archives SET status='verified',manifest_key=$2,manifest_sha256=$3,verified_at=now() "
                'WHERE archive_id=$1 RETURNING *', archive_id, manifest_key, digest)
            if payload is not None:
                await self.put_job(archive_id, 'archive', payload)
            await self._audit('archive.verified', archive_id)
            return ArchiveRecord(**dict(row))

    async def finish_archive(self, archive_id: str) -> ArchiveRecord:
        async with self._conn.transaction():
            archive = await self.get_archive(archive_id)
            if archive is None or archive.status != 'verified':
                raise ValueError('archive verification required')
            await self._conn.execute('DELETE FROM collab_rooms WHERE room_id=$1', archive.live_room_id)
            await self._audit('room.deleted', archive.original_room_id)
            await self.remove_job(archive_id)
            return await self.get_archive(archive_id)

    async def _audit(self, action: str, entity_id: str) -> None:
        await self._conn.execute(
            'INSERT INTO collab_audit_events(event_id,action,entity_type,entity_id) VALUES ($1,$2,$3,$4) '
            'ON CONFLICT(event_id) DO NOTHING', f'{action}:{entity_id}', action, action.split('.')[0], entity_id)

    async def restore_completed(self, room_id: str) -> bool:
        return bool(await self._conn.fetchval(
            'SELECT EXISTS(SELECT 1 FROM collab_audit_events WHERE event_id=$1)', 'room.restored:' + room_id))

    async def restore_snapshot(self, snapshot: dict, job_id: str) -> RoomRecord:
        async with self._conn.transaction():
            for name, rows in [('rooms', [snapshot['room']]), *[(t, snapshot[t]) for t in RESTORE_TABLES]]:
                for row in rows:
                    await self._conn.execute(
                        f'INSERT INTO collab_{name} SELECT * FROM jsonb_populate_record(NULL::collab_{name}, $1::jsonb)',
                        json.dumps(row))
            await self._audit('room.restored', snapshot['room']['room_id'])
            await self.remove_job(job_id)
            return await self.get_room(snapshot['room']['room_id'])

    async def start_scrub(self, user_id: str, job_id: str) -> dict:
        if not isinstance(user_id, str) or not user_id:
            raise ValueError('user ID must be nonempty')
        async with self._conn.transaction():
            job = await self.get_job(job_id)
            if job:
                return job['payload']
            payload = {'user_id': user_id, 'agent_ids': [r['agent_id'] for r in await self._conn.fetch(
                'SELECT agent_id FROM collab_agents WHERE owner_id=$1', user_id)]}
            await self.put_job(job_id, 'scrub', payload)
            await self._conn.execute("UPDATE collab_users SET status='disabled' WHERE user_id=$1", user_id)
            return payload

    async def finish_scrub(self, user_id: str, job_id: str) -> None:
        async with self._conn.transaction():
            # The approved FKs remove authored content and access, not room bytes.
            # Upload manifests are user-authored metadata; do not retain their content.
            await self._conn.execute("UPDATE collab_room_uploads SET manifest='{}'::jsonb WHERE created_by=$1", user_id)
            await self._conn.execute('DELETE FROM collab_users WHERE user_id=$1', user_id)
            # Retain opaque creation fingerprints; FK deletion has removed the
            # actor attribution. Match identities exactly, never by substring.
            await self._conn.execute(
                'DELETE FROM collab_audit_events WHERE entity_id=$1', user_id)
            await self._audit('user.deleted', hashlib.sha256(user_id.encode()).hexdigest())
            await self.remove_job(job_id)


LIFECYCLE_LOCK = 724918365
ROOM_TABLES = ('room_memberships', 'room_invitations', 'agent_room_attachments',
               'agent_permissions', 'agent_access_requests', 'messages', 'session_logs',
               'room_uploads', 'room_files')
RESTORE_TABLES = ('agents', 'room_memberships', 'room_invitations', 'agent_room_attachments',
                  'agent_permissions', 'agent_access_requests', 'messages', 'session_logs',
                  'room_uploads', 'room_upload_parts', 'room_files')


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError('unsupported snapshot value')

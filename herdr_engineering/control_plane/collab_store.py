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
    verified_parts: list[dict] | None = None


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

    async def upload_permission(self, room_id: str, user_id: str, agent_id: str | None,
                                now: float) -> bool | None:
        """None conceals inaccessible rooms; False permits preview/request only.

        SHARE locks also make this check safe inside the publication transaction.
        """
        member = await self._conn.fetchrow(
            "SELECT m.role,m.can_upload FROM collab_room_memberships m "
            "JOIN collab_users u USING(user_id) JOIN collab_rooms r USING(room_id) "
            "WHERE m.room_id=$1 AND m.user_id=$2 AND u.status='approved' "
            "AND r.status='active' AND m.status='approved' AND m.role!='guest' "
            "AND (m.expires_at IS NULL OR m.expires_at>$3) FOR SHARE OF m,u,r",
            room_id, user_id, now)
        if member is None:
            return None
        allowed = member['can_upload'] or member['role'] == 'owner'
        if agent_id is not None:
            attached = await self._conn.fetchrow(
                "SELECT a.agent_id,t.actions FROM collab_agents a "
                "JOIN collab_agent_room_attachments t USING(agent_id) "
                "WHERE a.agent_id=$1 AND a.owner_id=$2 AND t.room_id=$3 "
                "AND a.status='active' AND t.status='active' "
                "AND (t.expires_at IS NULL OR t.expires_at>$4) FOR SHARE OF a,t",
                agent_id, user_id, room_id, now)
            if attached is None or 'read' not in attached['actions']:
                return None
            permission = await self._conn.fetchval(
                'SELECT status FROM collab_agent_permissions WHERE room_id=$1 '
                'AND agent_id=$2 AND user_id=$3 FOR SHARE', room_id, agent_id, user_id)
            allowed = allowed and permission == 'approved' and 'post' in attached['actions']
        return bool(allowed)

    async def request_upload_access(self, room_id: str, agent_id: str, user_id: str) -> dict:
        from uuid import uuid4
        async with self._conn.transaction():
            row = await self._conn.fetchrow(
                "INSERT INTO collab_agent_access_requests(request_id,room_id,agent_id,user_id) "
                "VALUES ($1,$2,$3,$4) ON CONFLICT(room_id,agent_id,user_id) WHERE status='pending' "
                "DO UPDATE SET status='pending' RETURNING request_id,status",
                str(uuid4()), room_id, agent_id, user_id)
            await self._audit('upload.access_requested', row['request_id'])
            return dict(row)

    async def get_upload(self, upload_id: str, *, for_update=False) -> UploadRecord | None:
        row = await self._conn.fetchrow(
            'SELECT * FROM collab_room_uploads WHERE upload_id=$1' + (' FOR UPDATE' if for_update else ''), upload_id)
        if row is None:
            return None
        values = dict(row)
        if isinstance(values['manifest'], str):
            values['manifest'] = json.loads(values['manifest'])
        return UploadRecord(**values)

    async def update_upload(self, upload_id: str, status: str, manifest: dict, *,
                            action: str | None = None, actor=None) -> None:
        """Persist checkpoint and its sanitized audit together, deduplicating retries."""
        async with self._conn.transaction():
            old = await self.get_upload(upload_id, for_update=True)
            if old is None:
                raise ValueError('upload not found')
            event = action
            suffix = ''
            if action == 'grant':
                previous = old.manifest.get('grant_expires_at', 0)
                expiry = manifest['grant_expires_at']
                event = ('grant_renewed' if previous else 'grant_issued') if expiry > previous else None
                suffix = ':' + str(expiry)
            elif action in {'cancelled', 'expired'} and old.status == 'failed':
                event = None
            elif action == 'cleanup_completed' and old.manifest.get('cleanup_done'):
                event = None
            await self._conn.execute(
                'UPDATE collab_room_uploads SET status=$2,manifest=$3::jsonb WHERE upload_id=$1',
                upload_id, status, json.dumps(manifest))
            if event:
                await self.audit_upload(upload_id, event, actor=actor, suffix=suffix)

    async def audit_upload(self, upload_id: str, action: str, *, actor=None, suffix='') -> None:
        if action not in {'grant_issued', 'grant_renewed', 'cancelled', 'expired',
                          'cleanup_completed', 'cleanup_retry', 'cleanup_started'}:
            raise ValueError('unsupported upload audit action')
        # Fixed vocabulary only: never persist exception text, paths or signed grants.
        await self._conn.execute(
            'INSERT INTO collab_audit_events(event_id,actor_id,actor_kind,actor_agent_id,action,entity_type,entity_id,outcome) '
            'VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(event_id) DO NOTHING',
            'upload.' + action + ':' + upload_id + suffix,
            actor.owner if actor else None, actor.kind if actor else 'system',
            actor.id if actor and actor.is_agent else None, 'upload.' + action, 'upload', upload_id,
            'retry_required' if action == 'cleanup_retry' else 'complete')

    async def upload_parts(self, upload_id: str) -> list[dict]:
        return [dict(row) for row in await self._conn.fetch(
            'SELECT * FROM collab_room_upload_parts WHERE upload_id=$1 ORDER BY relative_path,part_number', upload_id)]

    async def save_upload_parts(self, upload_id: str, path: str, multipart_id: str, parts: list[dict]) -> None:
        await self._conn.executemany(
            'INSERT INTO collab_room_upload_parts(upload_id,relative_path,part_number,multipart_id,size_bytes,sha256) '
            'VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT(upload_id,relative_path,part_number) '
            'DO UPDATE SET multipart_id=EXCLUDED.multipart_id',
            [(upload_id, path, n, multipart_id, p['size_bytes'], p['sha256'])
             for n, p in enumerate(parts, 1)])

    async def publish_upload(self, upload_id: str, files: list[dict], authorize) -> list[RoomFileRecord]:
        async with self._conn.transaction():
            await authorize()
            row = await self._conn.fetchrow(
                'SELECT status FROM collab_room_uploads WHERE upload_id=$1 FOR UPDATE', upload_id)
            if row is None or row['status'] != 'verifying':
                raise ValueError('upload is not verifying')
            # One set-based insert and one visibility transition, regardless of file count.
            await self._conn.execute(
                "INSERT INTO collab_room_files(file_id,room_id,upload_id,uploaded_by,relative_path,storage_key,size_bytes,sha256,verified_parts,status) "
                "SELECT file_id,room_id,upload_id,uploaded_by,relative_path,storage_key,size_bytes,sha256,verified_parts,'available' "
                "FROM jsonb_to_recordset($1::jsonb) AS f(file_id text,room_id text,upload_id text,uploaded_by text,"
                "relative_path text,storage_key text,size_bytes bigint,sha256 text,verified_parts jsonb)", json.dumps(files))
            await self._conn.execute("UPDATE collab_room_uploads SET status='available' WHERE upload_id=$1", upload_id)
            await self._audit('upload.finalized', upload_id)
        return await self.upload_files(upload_id)

    async def upload_files(self, upload_id: str) -> list[RoomFileRecord]:
        return [_room_file(row) for row in await self._conn.fetch(
            "SELECT f.* FROM collab_room_files f JOIN collab_room_uploads u USING(upload_id) "
            "WHERE f.upload_id=$1 AND u.status='available' ORDER BY f.relative_path,f.file_id", upload_id)]

    async def uploads_for_cleanup(self, now: float, limit: int) -> list[UploadRecord]:
        rows = await self._conn.fetch(
            "SELECT upload_id FROM collab_room_uploads WHERE manifest->>'schema_version'='1' "
            "AND COALESCE((manifest->>'cleanup_done')::boolean,false)=false "
            "AND (status='available' OR status='failed' OR (manifest->>'session_expires_at')::float8 <= $1) "
            "ORDER BY created_at,upload_id LIMIT $2", now, limit)
        return [await self.get_upload(row['upload_id']) for row in rows]

    async def list_room_files(self, room_id: str) -> list[RoomFileRecord]:
        rows = await self._conn.fetch(
            "SELECT f.* FROM collab_room_files f "
            "JOIN collab_rooms r ON r.room_id = f.room_id AND r.status = 'active' "
            "LEFT JOIN collab_room_uploads u ON u.room_id = f.room_id AND u.upload_id = f.upload_id "
            "WHERE f.room_id = $1 AND f.status = 'available' "
            "AND (f.upload_id IS NULL OR u.status = 'available') "
            "ORDER BY f.relative_path, f.file_id", room_id)
        return [_room_file(row) for row in rows]

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
            if await self._conn.fetchval('SELECT EXISTS(SELECT 1 FROM collab_analysis_requests WHERE room_id=$1)', room_id):
                raise ValueError('room analysis provenance requires an archive exporter')
            if await self._conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM collab_room_uploads WHERE room_id=$1 AND (status!='available' OR manifest->>'schema_version'='1') AND COALESCE(manifest->>'cleanup_done','false')!='true')", room_id):
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
            for file in snapshot['room_files']:
                if isinstance(file.get('verified_parts'), str):
                    file['verified_parts'] = json.loads(file['verified_parts'])
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

    async def skill_package_refs(self, user_id: str) -> list[dict]:
        """Trusted object locations only; no manifest or code bytes in cleanup journals."""
        return [dict(r) for r in await self._conn.fetch(
            'SELECT d.storage_bucket AS bucket,d.storage_key FROM collab_skill_drafts d '
            'JOIN collab_skills s USING(skill_id) WHERE s.owner_id=$1 OR d.created_by=$1 '
            'UNION SELECT v.storage_bucket AS bucket,v.storage_key FROM collab_skill_versions v '
            'JOIN collab_skills s USING(skill_id) WHERE s.owner_id=$1 '
            'UNION SELECT storage_bucket AS bucket,storage_key FROM collab_skill_package_journal '
            'WHERE creator_id=$1 '
            'ORDER BY bucket,storage_key', user_id)]

    async def start_scrub(self, user_id: str, job_id: str) -> dict:
        if not isinstance(user_id, str) or not user_id:
            raise ValueError('user ID must be nonempty')
        async with self._conn.transaction():
            job = await self.get_job(job_id)
            if job and (job['kind'] != 'scrub' or job['payload']['user_id'] != user_id):
                raise ValueError('scrub job identity mismatch')
            payload = job['payload'] if job else {
                'user_id': user_id, 'agent_ids': [r['agent_id'] for r in await self._conn.fetch(
                    'SELECT agent_id FROM collab_agents WHERE owner_id=$1', user_id)]}
            # Refresh old/incomplete jobs before any cascade can discard locations.
            packages = payload.setdefault('skill_packages', [])
            for ref in await self.skill_package_refs(user_id):
                if ref not in packages:
                    packages.append(ref)
                    payload.pop('skill_packages_verified', None)
            await self.put_job(job_id, 'scrub', payload)
            await self._conn.execute("UPDATE collab_users SET status='disabled' WHERE user_id=$1", user_id)
            return payload

    async def finish_scrub(self, user_id: str, job_id: str) -> None:
        async with self._conn.transaction():
            if await self._conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM collab_room_uploads WHERE created_by=$1 "
                "AND manifest->>'schema_version'='1' AND COALESCE(manifest->>'cleanup_done','false')!='true')", user_id):
                raise ValueError('upload cleanup required before user deletion')
            packages = await self.skill_package_refs(user_id)
            job = await self.get_job(job_id)
            payload = job['payload'] if job else {}
            if packages or payload.get('skill_packages'):
                if (payload.get('user_id') != user_id or not payload.get('skill_packages_verified')
                        or any(ref not in payload.get('skill_packages', []) for ref in packages)):
                    raise ValueError('skill package cleanup required before user deletion')
            # The approved FKs remove authored content and access, not room bytes.
            # Upload manifests are user-authored metadata; do not retain their content.
            await self._conn.execute("UPDATE collab_room_uploads SET manifest='{}'::jsonb WHERE created_by=$1", user_id)
            await self._conn.execute('DELETE FROM collab_users WHERE user_id=$1', user_id)
            await self._conn.execute('DELETE FROM collab_skill_package_journal WHERE creator_id=$1', user_id)
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


def _room_file(row) -> RoomFileRecord:
    values = dict(row)
    if isinstance(values.get('verified_parts'), str):
        values['verified_parts'] = json.loads(values['verified_parts'])
    return RoomFileRecord(**values)

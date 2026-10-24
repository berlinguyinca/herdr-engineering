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

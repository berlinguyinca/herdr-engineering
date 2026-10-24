"""SQL boundary for collaboration's relational data platform.

Callers supply an asyncpg connection on its owning loop and perform authorization
before entering this repository. Stable IDs are creation idempotency keys: an
identical retry returns the durable row; conflicting reuse raises ValueError.
Each new row and its minimal audit event commit together. No credentials or file
bytes are stored here. Existing CollabService lifecycle events remain authoritative
until an explicit migration/backfill integrates the two persistence paths.
"""
from __future__ import annotations

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

    async def _create(self, table: str, key: str, values: dict[str, Any], *,
                      actor_id: str | None, entity_type: str) -> dict[str, Any]:
        # Table/column names are internal constants from the methods below;
        # every caller-supplied value is bound as an asyncpg parameter.
        columns = ', '.join(values)
        placeholders = ', '.join(f'${i}' for i in range(1, len(values) + 1))
        args = [json.dumps(v) if k == 'manifest' else v for k, v in values.items()]
        async with self._conn.transaction():
            row = await self._conn.fetchrow(
                f'INSERT INTO {table} ({columns}) VALUES ({placeholders}) '
                f'ON CONFLICT ({key}) DO NOTHING RETURNING *', *args)
            inserted = row is not None
            if row is None:
                row = await self._conn.fetchrow(
                    f'SELECT * FROM {table} WHERE {key} = $1 FOR UPDATE', values[key])
            if row is None:
                raise ValueError(f'idempotency conflict for {entity_type}')
            record = dict(row)
            if 'manifest' in record and isinstance(record['manifest'], str):
                record['manifest'] = json.loads(record['manifest'])
            # Archive deletion clears the live FK, but original_room_id remains
            # the immutable identity checked on retries. Never restore the FK.
            if any(record[k] != v for k, v in values.items() if k != 'live_room_id'):
                raise ValueError(f'idempotency conflict for {entity_type}')
            if inserted:
                await self._conn.execute(
                    'INSERT INTO collab_audit_events '
                    '(event_id, actor_id, action, entity_type, entity_id) '
                    'VALUES ($1, $2, $3, $4, $5)',
                    f'{entity_type}.created:{values[key]}', actor_id,
                    f'{entity_type}.created', entity_type, values[key])
            return record

    async def create_room(self, *, room_id: str, name: str, owner_id: str,
                          project_id: str | None = None) -> RoomRecord:
        return RoomRecord(**await self._create(
            'collab_rooms', 'room_id',
            dict(room_id=room_id, name=name, owner_id=owner_id, project_id=project_id),
            actor_id=owner_id, entity_type='room'))

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

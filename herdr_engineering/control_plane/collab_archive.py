"""Restartable relational room lifecycle, after authorization by CollabService.

One dedicated CollabStore connection owns each operation. The database journal is
trusted recovery state; object-store manifests never authorize their own contents.
S3 blobs are copied server-side, checksummed with bounded memory, then deleted.
The legacy fabric event log is deliberately not silently migrated or erased here.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from contextvars import copy_context
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from .collab_storage import CollabStorage, StorageError, archive_key, live_key, verify_manifest
from .collab_store import ArchiveRecord, CollabStore, RoomRecord


@dataclass(frozen=True)
class ArchiveReport:
    archive_id: str
    status: str
    error: str | None = None


@dataclass(frozen=True)
class ScrubReport:
    status: str
    archives_scrubbed: int


def _json(data) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode()


def _ndjson(rows) -> bytes:
    return b''.join(_json(row) + b'\n' for row in rows)


def _exports(snapshot: dict) -> dict[str, bytes]:
    # room.json is the complete relational snapshot; the named exports make
    # messages, permissions, sessions and file metadata independently usable.
    result = {'room.json': _json(snapshot),
              'members.ndjson': _ndjson(snapshot['room_memberships']),
              'messages.ndjson': _ndjson(snapshot['messages'])}
    sessions = {}
    for row in snapshot['session_logs']:
        session = row['session_id'] or str(uuid5(NAMESPACE_URL, 'collab-log:' + row['log_id']))
        sessions.setdefault(session, []).append(row)
    for session, rows in sessions.items():
        result[f'sessions/{session}.ndjson'] = _ndjson(rows)
    for file in snapshot['room_files']:
        result[f"files/{file['file_id']}/metadata.json"] = _json(file)
    return result


def _scrub(snapshot: dict, user_id: str, agent_ids: list[str]) -> dict:
    snapshot = copy.deepcopy(snapshot)
    agents = set(agent_ids) | {a['agent_id'] for a in snapshot['agents'] if a['owner_id'] == user_id}
    if snapshot['room']['owner_id'] == user_id:
        snapshot['room']['owner_id'] = None
    for name, rows in snapshot.items():
        if name == 'room':
            continue
        snapshot[name] = [r for r in rows if not (
            r.get('user_id') == user_id or r.get('author_user_id') == user_id
            or r.get('author_agent_id') in agents or r.get('agent_id') in agents
            or r.get('owner_id') == user_id or r.get('invited_by') == user_id)]
        for row in snapshot[name]:
            if row.get('uploaded_by') == user_id:
                row['uploaded_by'] = None
            if row.get('created_by') == user_id:
                row['created_by'] = None
                row['manifest'] = {}
    return snapshot


class CollabArchive:
    """Internal workflow. Public callers must use CollabService's principal gates."""

    def __init__(self, store: CollabStore, storage: CollabStorage):
        self.store, self.storage = store, storage

    async def _io(self, fn, *args):
        # S3's blocking SDK never stalls the owning asyncpg event loop.
        # Runner shutdown cancels every Task, including a to_thread wrapper.
        # An executor Future tracks the actual provider completion independently
        # of that task sweep; shielding it retains ownership until the thread stops.
        completion = asyncio.get_running_loop().run_in_executor(None, copy_context().run, fn, *args)
        cancelled = False
        while not completion.done():
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break  # Retrieve the provider outcome below, after completion.
        if cancelled:
            completion.exception()  # Consume any provider error before propagating cancellation.
            raise asyncio.CancelledError
        return completion.result()

    async def archive_room(self, room_id: str, requested_by: str, *, authorize=None) -> ArchiveRecord:
        async with self.store.lifecycle_lock():
            if authorize:
                await authorize()
            await self._finish_scrubs()
            if authorize:
                await authorize()
            archive_id = str(uuid5(NAMESPACE_URL, 'collab-archive:' + room_id))
            archive = await self.store.begin_archive(archive_id, room_id, requested_by)
            return await self._archive(archive)

    async def _trusted(self, archive: ArchiveRecord) -> dict:
        expected = archive_key(self.storage.config.environment, archive.archive_id, 'manifest.json')
        if archive.manifest_key != expected or not archive.manifest_sha256:
            raise StorageError('archive manifest verification failed')
        raw = await self._io(self.storage.read_archive, archive.archive_id, 'manifest.json', archive.manifest_sha256)
        manifest = json.loads(raw)
        if manifest.get('archive_id') != archive.archive_id or manifest.get('original_room_id') != archive.original_room_id:
            raise StorageError('archive identity verification failed')
        report = await self._io(verify_manifest, manifest, self.storage)
        if not report.ok:
            raise StorageError('archive object verification failed')
        return manifest

    async def _snapshot(self, archive: ArchiveRecord, manifest: dict) -> dict:
        key = archive_key(self.storage.config.environment, archive.archive_id, 'room.json')
        obj = next((o for o in manifest['objects'] if o['key'] == key), None)
        if obj is None:
            raise StorageError('archive snapshot verification failed')
        snapshot = json.loads(await self._io(self.storage.read_archive, archive.archive_id, 'room.json', obj['sha256']))
        if snapshot['room']['room_id'] != archive.original_room_id:
            raise StorageError('archive room verification failed')
        return snapshot

    async def _archive(self, archive: ArchiveRecord) -> ArchiveRecord:
        if archive.live_room_id is None:
            return archive
        try:
            job = await self.store.get_job(archive.archive_id)
            if job is None:
                raise ValueError('archive recovery journal missing')
            payload = job['payload']
            if payload['phase'] == 'export':
                snapshot = payload['snapshot']
                objects, sources = [], []
                inventory = set(await self._io(self.storage.live_inventory, archive.original_room_id))
                for file in snapshot['room_files']:
                    base = live_key(self.storage.config.environment, archive.original_room_id, file['file_id'])
                    if file['storage_key'] != base or not (file['sha256'] or file.get('verified_parts')):
                        raise ValueError('file metadata does not identify verified room bytes')
                    variants = [None] + [k[len(base.removesuffix('blob') + 'preview/'):]
                                         for k in sorted(inventory) if k.startswith(base.removesuffix('blob') + 'preview/')]
                    for variant in variants:
                        ref = await self._io(self.storage.archive_file, archive.archive_id,
                                             archive.original_room_id, file['file_id'], variant,
                                             file.get('verified_parts') if variant is None else None)
                        if variant is None:
                            if ref.size_bytes != file['size_bytes'] or (file['sha256'] and ref.sha256 != file['sha256']):
                                raise StorageError('live file verification failed')
                            # Composite evidence was checked against bytes while hashing.
                            # Export the independently computed whole-file hash for restore.
                            file['sha256'] = ref.sha256
                        objects.append({'key': ref.key, 'size_bytes': ref.size_bytes, 'sha256': ref.sha256})
                        sources.append({'file_id': file['file_id'], 'variant': variant})
                        inventory.discard(live_key(self.storage.config.environment, archive.original_room_id, file['file_id'], variant))
                if inventory:
                    raise ValueError('unregistered live objects prevent room deletion')
                for name, data in _exports(snapshot).items():
                    ref = await self._io(self.storage.write_archive, archive.archive_id, name, data)
                    objects.append({'key': ref.key, 'size_bytes': ref.size_bytes, 'sha256': ref.sha256})
                manifest = {'schema_version': 1, 'archive_id': archive.archive_id,
                            'original_room_id': archive.original_room_id, 'created_at': archive.created_at.isoformat(),
                            'encryption': {'mode': 'bucket-policy'}, 'integrity': {'algorithm': 'sha256'},
                            'objects': objects}
                ref = await self._io(self.storage.write_manifest, archive.archive_id, manifest)
                # Verify the exact bytes just written, not a reread self-asserted digest.
                raw = await self._io(self.storage.read_archive, archive.archive_id, 'manifest.json', ref.sha256)
                if not (await self._io(verify_manifest, json.loads(raw), self.storage)).ok:
                    raise StorageError('archive object verification failed')
                payload = {'phase': 'delete', 'sources': sources}
                archive = await self.store.verified_archive(archive.archive_id, ref.key, ref.sha256, payload)
            # Reverify from the durable trusted digest on every cleanup/retry.
            manifest = await self._trusted(archive)
            expected_keys = {live_key(self.storage.config.environment, archive.original_room_id,
                                      src['file_id'], src['variant']) for src in payload['sources']}
            current_keys = set(await self._io(self.storage.live_inventory, archive.original_room_id))
            if current_keys - expected_keys:
                raise ValueError('unregistered live objects prevent room deletion')
            # Validate ALL remaining live objects before deleting the first one.
            # Retried cleanup permits absent objects, never changed objects.
            archived_objects = {o['key']: o for o in manifest['objects']}
            for source in payload['sources']:
                file_id, variant = source['file_id'], source['variant']
                key = live_key(self.storage.config.environment, archive.original_room_id, file_id, variant)
                name = f'files/{file_id}/' + ('blob' if variant is None else f'preview/{variant}')
                expected = archived_objects.get(archive_key(self.storage.config.environment, archive.archive_id, name))
                if expected is None:
                    raise StorageError('archive cleanup inventory verification failed')
                if key in current_keys and await self._io(self.storage.live_checksum, archive.original_room_id, file_id, variant) != (expected['size_bytes'], expected['sha256']):
                    raise StorageError('live file verification failed')
            for source in payload['sources']:
                await self._io(self.storage.delete_live_file, archive.original_room_id,
                               source['file_id'], source['variant'])
            return await self.store.finish_archive(archive.archive_id)
        except Exception:
            await self.store.archive_failed(archive.archive_id)
            raise

    async def restore_room(self, archive_id: str, requested_by: str, *, authorize=None) -> RoomRecord:
        async with self.store.lifecycle_lock():
            if authorize:
                await authorize()
            await self._finish_scrubs()
            if authorize:
                await authorize()
            return await self._restore(archive_id, requested_by)

    async def _restore(self, archive_id: str, requested_by: str) -> RoomRecord:
        archive = await self.store.get_archive(archive_id)
        if archive is None or archive.status != 'verified' or archive.live_room_id is not None:
            raise ValueError('verified deleted archive required')
        # An archive+requester is a durable restore idempotency key; the old ID is never reused.
        room_id = str(uuid5(UUID(archive_id), 'restore:' + requested_by))
        existing = await self.store.get_room(room_id)
        if existing:
            return existing
        if await self.store.restore_completed(room_id):
            raise ValueError('restore already completed; its room was subsequently deleted')
        manifest = await self._trusted(archive)
        snapshot = await self._snapshot(archive, manifest)
        await self.store.put_job('restore:' + room_id, 'restore',
                                 {'archive_id': archive_id, 'requested_by': requested_by})
        original_files = {f['file_id'] for f in snapshot['room_files']}
        id_columns = {'membership_id', 'invitation_id', 'agent_id', 'author_agent_id', 'permission_id',
                      'request_id', 'message_id', 'log_id', 'session_id', 'upload_id', 'file_id'}

        def remap(value):
            return str(uuid5(UUID(room_id), value)) if value else value

        snapshot['room'].update(room_id=room_id, owner_id=requested_by, status='active', created_at=datetime.now(UTC).isoformat())
        for name, rows in snapshot.items():
            if name == 'room':
                continue
            for row in rows:
                for column in row:
                    if column == 'room_id':
                        row[column] = room_id
                    elif column in id_columns:
                        row[column] = remap(row[column])
                if name == 'room_files':
                    row['storage_key'] = live_key(self.storage.config.environment, room_id, row['file_id'])
        # The restoring principal owns the new room; prior permissions/expiry stay intact.
        owner = next((r for r in snapshot['room_memberships'] if r['user_id'] == requested_by), None)
        if owner:
            owner.update(role='owner', status='approved', expires_at=None, can_upload=True)
        else:
            snapshot['room_memberships'].append(dict(membership_id=remap('restore-owner'), room_id=room_id,
                user_id=requested_by, status='approved', role='owner', can_upload=True,
                expires_at=None, joined_seq=0, created_at=datetime.now(UTC).isoformat()))
        prefix = archive_key(self.storage.config.environment, archive_id, 'manifest.json').removesuffix('manifest.json')
        for obj in manifest['objects']:
            parts = obj['key'][len(prefix):].split('/')
            if len(parts) >= 3 and parts[0] == 'files' and (parts[2] == 'blob' or parts[2] == 'preview'):
                old_id = parts[1]
                if old_id not in original_files:
                    raise StorageError('archive file inventory verification failed')
                await self._io(self.storage.restore_file, archive_id, old_id, room_id, remap(old_id),
                               obj['size_bytes'], obj['sha256'], parts[3] if parts[2] == 'preview' else None)
        return await self.store.restore_snapshot(snapshot, 'restore:' + room_id)

    async def scrub_deleted_user(self, user_id: str, *, authorize=None) -> ScrubReport:
        async with self.store.lifecycle_lock():
            if authorize:
                await authorize()
            await self._finish_scrubs()
            if authorize:
                await authorize()
            job_id = 'scrub:' + hashlib.sha256(user_id.encode()).hexdigest()
            payload = await self.store.start_scrub(user_id, job_id)
            return await self._scrub_user(job_id, payload)

    async def _finish_scrubs(self):
        for job in await self.store.jobs('scrub'):
            await self._scrub_user(job['job_id'], job['payload'])

    async def _scrub_user(self, job_id: str, payload: dict) -> ScrubReport:
        # Refresh legacy checkpoints and retain package references before deletion.
        payload = await self.store.start_scrub(payload['user_id'], job_id)
        user, agents = payload['user_id'], payload['agent_ids']
        # Publish any interrupted restore before removing its requester. The
        # subsequent relational scrub then preserves its now room-owned bytes.
        while pending_restores := await self.store.jobs('restore'):
            for pending in pending_restores:
                await self._restore(**pending['payload'])
                # Also retire stale checkpoints whose room was already published.
                # Failed restores keep their checkpoint and abort account deletion.
                await self.store.remove_job(pending['job_id'])
        count = 0
        for archive in await self.store.list_archives():
            job = await self.store.get_job(archive.archive_id)
            if job and job['payload']['phase'] == 'export':
                # Partial exports may already contain authored data. Re-export all
                # metadata from the scrubbed checkpoint before account deletion.
                data = job['payload']
                before = data['snapshot']
                after = _scrub(before, user, agents)
                exports = _exports(after)
                for name in _exports(before):
                    exports.setdefault(name, b'')  # remove old session content, retain no authorship
                rewrite_id = 'rewrite:' + archive.archive_id
                rewrite = await self.store.get_job(rewrite_id)
                if rewrite is None:
                    rewrite = {'payload': {'exports': {n: b.decode() for n, b in exports.items()},
                                           'snapshot': after, 'manifest': None}}
                    await self.store.put_job(rewrite_id, 'rewrite', rewrite['payload'])
                for name, text in rewrite['payload']['exports'].items():
                    ref = await self._io(self.storage.write_archive, archive.archive_id, name, text.encode())
                    await self._io(self.storage.read_archive, archive.archive_id, name, ref.sha256)
                data['snapshot'] = rewrite['payload']['snapshot']
                await self.store.put_job(archive.archive_id, 'archive', data)
                await self.store.remove_job(rewrite_id)
                await self._archive(archive)
                count += 1
                continue
            if not archive.manifest_sha256:
                raise ValueError('archive has no trusted recovery checkpoint')
            rewrite_id = 'rewrite:' + archive.archive_id
            rewrite = await self.store.get_job(rewrite_id)
            if rewrite is None:
                manifest = await self._trusted(archive)
                before = await self._snapshot(archive, manifest)
                after = _scrub(before, user, agents)
                if before == after:
                    continue
                exports = _exports(after)
                for name in _exports(before):
                    exports.setdefault(name, b'')
                rewrite = {'payload': {'exports': {n: b.decode() for n, b in exports.items()}, 'manifest': manifest}}
                # Persist desired bytes before overwriting any trusted archive bytes.
                await self.store.put_job(rewrite_id, 'rewrite', rewrite['payload'])
            manifest = rewrite['payload']['manifest']
            prefix = archive_key(self.storage.config.environment, archive.archive_id, 'manifest.json').removesuffix('manifest.json')
            replacements = {}
            for name, text in rewrite['payload']['exports'].items():
                ref = await self._io(self.storage.write_archive, archive.archive_id, name, text.encode())
                replacements[prefix + name] = {'key': ref.key, 'size_bytes': ref.size_bytes, 'sha256': ref.sha256}
            manifest['objects'] = [replacements.get(o['key'], o) for o in manifest['objects']]
            ref = await self._io(self.storage.write_manifest, archive.archive_id, manifest)
            raw = await self._io(self.storage.read_archive, archive.archive_id, 'manifest.json', ref.sha256)
            if not (await self._io(verify_manifest, json.loads(raw), self.storage)).ok:
                raise StorageError('scrubbed archive verification failed')
            await self.store.verified_archive(archive.archive_id, ref.key, ref.sha256)
            await self.store.remove_job(rewrite_id)
            count += 1
        for ref in payload['skill_packages']:
            await self._io(self.storage.delete_skill_package, ref['bucket'], ref['storage_key'])
        payload['skill_packages_verified'] = True
        await self.store.put_job(job_id, 'scrub', payload)
        await self.store.finish_scrub(user, job_id)
        return ScrubReport('complete', count)

    async def resume_pending_archives(self) -> list[ArchiveReport]:
        reports = []
        async with self.store.lifecycle_lock():
            try:
                await self._finish_scrubs()
            except Exception:
                return [ArchiveReport('', 'failed', 'user scrub pending')]
            for job in await self.store.jobs('archive'):
                archive_id = job['job_id']
                try:
                    archive = await self.store.get_archive(archive_id)
                    result = await self._archive(archive)
                    reports.append(ArchiveReport(archive_id, result.status))
                except Exception:
                    reports.append(ArchiveReport(archive_id, 'failed', 'archive retry required'))
            for job in await self.store.jobs('restore'):
                try:
                    await self._restore(**job['payload'])
                except Exception:
                    reports.append(ArchiveReport(job['payload']['archive_id'], 'failed', 'restore retry required'))
        return reports

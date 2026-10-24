"""Durable, metadata-only direct S3 uploads. Public callers use CollabService.

The harness expands paths and hashes bytes locally. Only UploadPart is signed;
completion, server-side promotion and atomic publication remain server-owned.
SHA256 multipart checksums are composite, never ETags or user metadata. For a
multipart destination we verify the ordered part digests; the manifest's whole
file SHA256 remains a harness assertion until an independent full-object check.
No object body is downloaded or proxied by this workflow.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from contextvars import copy_context
from dataclasses import dataclass, field
from functools import partial
from uuid import NAMESPACE_URL, uuid4, uuid5

from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

from .collab import CollabError
from .collab_storage import _opaque_id, _relative_path, live_key, upload_key

MIN_PART = 5 * 1024 * 1024
MAX_PART = 5 * 1024**3
MAX_FILES = 10000
MAX_PARTS = 50000


@dataclass(frozen=True)
class UploadPreview:
    room_id: str
    file_count: int
    total_bytes: int
    can_upload: bool
    access_request_required: bool


@dataclass(frozen=True)
class UploadGrant:
    upload_id: str
    room_id: str
    prefix: str
    bucket: str
    expires_at: float
    file_count: int
    total_bytes: int
    files: list[dict] = field(repr=False)
    next_cursor: int | None = None


@dataclass(frozen=True)
class UploadStatus:
    upload_id: str
    room_id: str
    status: str
    file_count: int
    total_bytes: int
    uploaded_bytes: int
    completed_parts: int
    total_parts: int


def _b64(digest):
    return base64.b64encode(bytes.fromhex(digest)).decode('ascii')


def _composite(file):
    raw = b''.join(bytes.fromhex(p['sha256']) for p in file['parts'])
    return _b64(hashlib.sha256(raw).hexdigest()) + '-' + str(len(file['parts']))


def _manifest(value, part_size=None):
    if not isinstance(value, dict) or set(value) != {'files'}:
        raise CollabError(400, 'a locally expanded files manifest is required')
    files = value['files']
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise CollabError(400, 'manifest file count is invalid')
    seen, normalized, count = set(), [], 0
    for file in files:
        if not isinstance(file, dict) or set(file) != {'relative_path', 'size_bytes', 'sha256', 'parts'}:
            raise CollabError(400, 'invalid file manifest')
        path = file['relative_path']
        try:
            _relative_path(path)
            if len(path.encode('utf-8')) > 4096 or path in seen:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise CollabError(400, 'invalid or duplicate relative path') from None
        seen.add(path)
        parts = file['parts']
        if not isinstance(parts, list) or not 1 <= len(parts) <= 10000:
            raise CollabError(400, 'invalid multipart count')
        for obj in [file, *parts]:
            if (not isinstance(obj, dict) or type(obj.get('size_bytes')) is not int
                    or obj['size_bytes'] < 0 or not isinstance(obj.get('sha256'), str)
                    or not re.fullmatch('[0-9a-f]{64}', obj['sha256'])):
                raise CollabError(400, 'invalid size or SHA256')
        if (any(set(p) != {'size_bytes', 'sha256'} for p in parts)
                or any(not MIN_PART <= p['size_bytes'] <= MAX_PART for p in parts[:-1])
                or (part_size is not None and any(p['size_bytes'] != part_size for p in parts[:-1]))
                or parts[-1]['size_bytes'] > (part_size or MAX_PART)
                or (parts[-1]['size_bytes'] == 0 and len(parts) != 1)
                or sum(p['size_bytes'] for p in parts) != file['size_bytes']
                or file['size_bytes'] > 5 * 1024**4
                or (len(parts) == 1 and parts[0]['sha256'] != file['sha256'])):
            raise CollabError(400, 'invalid multipart layout or checksum')
        count += len(parts)
        normalized.append({**file, 'parts': [dict(p) for p in parts]})
    if count > MAX_PARTS:
        raise CollabError(400, 'manifest part count exceeds limit')
    return {'files': sorted(normalized, key=lambda f: f['relative_path'])}


class CollabUploads:
    def __init__(self, store, storage, *, now=time.time):
        self.store, self.storage, self.now = store, storage, now
        self.s3 = storage._archive._s3
        self.bucket = storage.config.live_bucket
        self.environment = storage.config.environment

    async def _io(self, fn, *args, **kwargs):
        # Keep lifecycle ownership until blocking S3 I/O has actually stopped,
        # including simultaneous cancellation during event-loop shutdown.
        call = partial(fn, *args, **kwargs)
        completion = asyncio.get_running_loop().run_in_executor(None, copy_context().run, call)
        cancelled = False
        while not completion.done():
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        if cancelled:
            completion.exception()
            raise asyncio.CancelledError
        return completion.result()

    async def preview_upload(self, principal, room_id, pattern_manifest, authorize):
        async with self.store.lifecycle_lock():
            allowed = await authorize(False)
            manifest = _manifest(pattern_manifest)
            return UploadPreview(room_id, len(manifest['files']),
                                 sum(f['size_bytes'] for f in manifest['files']), allowed, not allowed)

    async def request_upload_access(self, principal, room_id, authorize):
        async with self.store.lifecycle_lock():
            allowed = await authorize(False)
            if allowed:
                return {'status': 'approved'}
            if not principal.is_agent:
                raise CollabError(403, 'ask the room owner to enable file upload permission')
            # A pending request never grants attachment, delegation or can_upload.
            return await self.store.request_upload_access(room_id, principal.id, principal.owner)

    def _key(self, row, file):
        return upload_key(self.environment, row.room_id, row.upload_id, file['relative_path'])

    def _file_id(self, row, file):
        return str(uuid5(NAMESPACE_URL, row.upload_id + ':' + file['relative_path']))

    def _live(self, row, file):
        return live_key(self.environment, row.room_id, self._file_id(row, file))

    async def _owned(self, principal, upload_id, authorize):
        if not isinstance(upload_id, str):
            raise CollabError(400, 'upload_id is required')
        row = await self.store.get_upload(upload_id)
        if (row is None or row.created_by != principal.owner
                or row.manifest.get('principal') != principal.key):
            raise CollabError(404, 'not found')
        await authorize(True, row.room_id)
        return row

    async def _head(self, key):
        try:
            return await self._io(self.s3.head_object, Bucket=self.bucket, Key=key, ChecksumMode='ENABLED')
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
                return None
            raise

    def _verify_head(self, head, file):
        if head is None or head.get('ContentLength') != file['size_bytes']:
            raise CollabError(409, 'uploaded size mismatch or object missing')
        checksum = head.get('ChecksumSHA256')
        expected = _composite(file) if head.get('ChecksumType') == 'COMPOSITE' else _b64(file['sha256'])
        if checksum != expected:
            raise CollabError(409, 'uploaded checksum missing or mismatch')

    async def _parts(self, key, multipart_id, file):
        found, marker = [], 0
        while True:
            page = await self._io(self.s3.list_parts, Bucket=self.bucket, Key=key,
                                  UploadId=multipart_id, PartNumberMarker=marker)
            for p in page.get('Parts', []):
                n = p.get('PartNumber')
                if (type(n) is not int or not 1 <= n <= len(file['parts'])
                        or (found and n <= found[-1]['PartNumber'])):
                    raise CollabError(409, 'unexpected multipart part')
                expected = file['parts'][n - 1]
                if p.get('Size') != expected['size_bytes'] or p.get('ChecksumSHA256') != _b64(expected['sha256']):
                    raise CollabError(409, 'multipart checksum or size mismatch')
                found.append(p)
            if not page.get('IsTruncated'):
                return found
            next_marker = page.get('NextPartNumberMarker')
            if not isinstance(next_marker, int) or next_marker <= marker:
                raise CollabError(503, 'invalid multipart pagination')
            marker = next_marker

    async def start_upload(self, principal, room_id, manifest, options, authorize):
        if not isinstance(options, dict) or set(options) - {'upload_id', 'expires_in', 'part_size', 'cursor', 'limit'}:
            raise CollabError(400, 'invalid upload options')
        ttl, size = options.get('expires_in', 900), options.get('part_size', MIN_PART)
        cursor, limit = options.get('cursor', 0), options.get('limit', 1000)
        if (type(ttl) is not int or not 1 <= ttl <= 900 or type(size) is not int or not MIN_PART <= size <= MAX_PART
                or type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 1000):
            raise CollabError(400, 'invalid grant lifetime, part size or page')
        normalized = _manifest(manifest, size)
        upload_id = options.get('upload_id', str(uuid4()))
        try:
            _opaque_id(upload_id, 'upload')
        except ValueError:
            raise CollabError(400, 'invalid upload_id') from None
        async with self.store.lifecycle_lock():
            await authorize(True, room_id)
            row = await self.store.get_upload(upload_id)
            if row:
                if row.room_id != room_id or row.manifest.get('principal') != principal.key or row.created_by != principal.owner:
                    raise CollabError(404, 'not found')
                if row.manifest.get('files') != normalized['files'] or row.manifest.get('part_size') != size:
                    raise CollabError(409, 'upload manifest conflicts with existing session')
                if row.status not in {'created', 'uploading', 'verifying'} or row.manifest['session_expires_at'] <= self.now():
                    raise CollabError(409, 'upload session cannot be resumed')
            else:
                state = {**normalized, 'schema_version': 1, 'principal': principal.key,
                         'part_size': size, 'session_expires_at': self.now() + 86400}
                row = await self.store.register_upload(upload_id=upload_id, room_id=room_id,
                                                       created_by=principal.owner, manifest=state)
            expires = min(self.now() + ttl, row.manifest['session_expires_at'])
            state = {**row.manifest, 'grant_expires_at': max(expires, row.manifest.get('grant_expires_at', 0))}
            await self.store.update_upload(upload_id, 'uploading', state)
            records = await self.store.upload_parts(upload_id)
            multipart = {p['relative_path']: p['multipart_id'] for p in records}
            granted, index, emitted, next_cursor = [], 0, 0, None
            for file in row.manifest['files']:
                count = len(file['parts'])
                if index + count <= cursor:
                    index += count
                    continue
                key = self._key(row, file)
                head = await self._head(key)
                if head is not None:
                    self._verify_head(head, file)
                    index += count
                    continue
                mid = multipart.get(file['relative_path'])
                if mid is None:
                    result = await self._io(self.s3.create_multipart_upload, Bucket=self.bucket, Key=key,
                                            ChecksumAlgorithm='SHA256', ChecksumType='COMPOSITE')
                    mid = result['UploadId']
                    await self.store.save_upload_parts(upload_id, file['relative_path'], mid, file['parts'])
                present = {p['PartNumber'] for p in await self._parts(key, mid, file)}
                urls = []
                for n, part in enumerate(file['parts'], 1):
                    position = index + n - 1
                    if position < cursor or n in present:
                        continue
                    if emitted >= limit:
                        next_cursor = position
                        break
                    remaining = int(expires - self.now())
                    if remaining <= 0:
                        raise CollabError(409, 'upload grant expired; resume to renew')
                    url = await self._io(self.s3.generate_presigned_url, 'upload_part', Params={
                        'Bucket': self.bucket, 'Key': key, 'UploadId': mid, 'PartNumber': n,
                        'ChecksumSHA256': _b64(part['sha256']), 'ContentLength': part['size_bytes']},
                        ExpiresIn=remaining)
                    urls.append({'part_number': n, 'size_bytes': part['size_bytes'], 'url': url,
                                 'headers': {'x-amz-checksum-sha256': _b64(part['sha256'])}})
                    emitted += 1
                if urls:
                    granted.append({'relative_path': file['relative_path'], 'parts': urls})
                index += count
                if next_cursor is not None:
                    break
            await authorize(True, room_id)
            return UploadGrant(upload_id, room_id, self._key(row, row.manifest['files'][0]).rsplit('/', 1)[0] + '/',
                               self.bucket, expires, len(normalized['files']),
                               sum(f['size_bytes'] for f in normalized['files']), granted, next_cursor)

    async def get_upload_status(self, principal, upload_id, authorize):
        async with self.store.lifecycle_lock():
            row = await self._owned(principal, upload_id, authorize)
            files = row.manifest['files']
            total = sum(f['size_bytes'] for f in files)
            total_parts = sum(len(f['parts']) for f in files)
            uploaded = completed = 0
            if row.status == 'available':
                uploaded, completed = total, total_parts
            elif row.status != 'failed':
                multipart = {p['relative_path']: p['multipart_id'] for p in await self.store.upload_parts(upload_id)}
                for file in files:
                    key = self._key(row, file)
                    head = await self._head(key)
                    if head:
                        self._verify_head(head, file)
                        uploaded += file['size_bytes']
                        completed += len(file['parts'])
                    elif file['relative_path'] in multipart:
                        parts = await self._parts(key, multipart[file['relative_path']], file)
                        uploaded += sum(p['Size'] for p in parts)
                        completed += len(parts)
            await authorize(True, row.room_id)
            return UploadStatus(upload_id, row.room_id, row.status, len(files), total, uploaded, completed, total_parts)

    async def finalize_upload(self, principal, upload_id, checksums, authorize):
        async with self.store.lifecycle_lock():
            row = await self._owned(principal, upload_id, authorize)
            files = row.manifest['files']
            if checksums != {f['relative_path']: f['sha256'] for f in files}:
                raise CollabError(400, 'finalize checksums must match the manifest')
            if row.status == 'available':
                return await self.store.upload_files(upload_id)
            if row.status == 'failed' or row.manifest['session_expires_at'] <= self.now():
                raise CollabError(409, 'upload session is closed')
            await self.store.update_upload(upload_id, 'verifying', row.manifest)
            multipart = {p['relative_path']: p['multipart_id'] for p in await self.store.upload_parts(upload_id)}
            records = []
            for file in files:
                key = self._key(row, file)
                head = await self._head(key)
                if head is None:
                    mid = multipart.get(file['relative_path'])
                    if mid is None:
                        raise CollabError(409, 'upload is incomplete')
                    parts = await self._parts(key, mid, file)
                    if len(parts) != len(file['parts']):
                        raise CollabError(409, 'upload is incomplete')
                    await self._io(self.s3.complete_multipart_upload, Bucket=self.bucket, Key=key, UploadId=mid,
                                   MultipartUpload={'Parts': [{k: p[k] for k in ('PartNumber', 'ETag', 'ChecksumSHA256')} for p in parts]})
                    head = await self._head(key)
                self._verify_head(head, file)
                destination = self._live(row, file)
                await self._io(self.s3.copy, {'Bucket': self.bucket, 'Key': key}, self.bucket, destination,
                               ExtraArgs={'ChecksumAlgorithm': 'SHA256'},
                               Config=TransferConfig(multipart_threshold=MAX_PART,
                                                     multipart_chunksize=row.manifest['part_size']))
                self._verify_head(await self._head(destination), file)
                records.append({'file_id': self._file_id(row, file), 'room_id': row.room_id,
                                'upload_id': upload_id, 'uploaded_by': principal.owner,
                                'relative_path': file['relative_path'], 'storage_key': destination,
                                'size_bytes': file['size_bytes'], 'sha256': file['sha256']})
            await authorize(True, row.room_id)
            result = await self.store.publish_upload(upload_id, records, lambda: authorize(True, row.room_id))
            # A published batch is durable even if staging cleanup needs a retry.
            try:
                await self._cleanup(await self.store.get_upload(upload_id))
            except Exception:
                pass  # cleanup_done stays false; the worker retries without changing visibility
            return result

    async def cancel_upload(self, principal, upload_id, authorize):
        async with self.store.lifecycle_lock():
            row = await self._owned(principal, upload_id, authorize)
            if row.status == 'available':
                raise CollabError(409, 'available uploads cannot be cancelled')
            await self.store.update_upload(upload_id, 'failed', row.manifest)
            await self._cleanup(await self.store.get_upload(upload_id))

    async def _cleanup(self, row):
        # Prefix inventory also catches CreateMultipartUpload succeeding just
        # before a crash prevented its returned ID from reaching Postgres.
        files = row.manifest['files']
        prefix = self._key(row, files[0]).rsplit('/', 1)[0] + '/'
        pagination = {}
        while True:
            page = await self._io(self.s3.list_multipart_uploads, Bucket=self.bucket, Prefix=prefix, **pagination)
            for upload in page.get('Uploads', []):
                if not upload['Key'].startswith(prefix):
                    raise CollabError(503, 'invalid cleanup inventory')
                try:
                    await self._io(self.s3.abort_multipart_upload, Bucket=self.bucket,
                                   Key=upload['Key'], UploadId=upload['UploadId'])
                except ClientError as exc:
                    if exc.response.get('Error', {}).get('Code') != 'NoSuchUpload':
                        raise
            if not page.get('IsTruncated'):
                break
            next_page = {'KeyMarker': page['NextKeyMarker'], 'UploadIdMarker': page['NextUploadIdMarker']}
            if next_page == pagination:
                raise CollabError(503, 'invalid cleanup pagination')
            pagination = next_page
        for file in files:
            await self._io(self.s3.delete_object, Bucket=self.bucket, Key=self._key(row, file))
            if row.status != 'available':
                await self._io(self.s3.delete_object, Bucket=self.bucket, Key=self._live(row, file))
        # Wait out signed grants before marking aborted sessions fully cleaned.
        # In-flight UploadPart calls may finish after the first abort.
        done = row.status == 'available' or self.now() >= row.manifest.get('grant_expires_at', 0)
        await self.store.update_upload(row.upload_id, row.status, {**row.manifest, 'cleanup_done': done})

    async def cleanup_expired_uploads(self, limit=100):
        """Worker entrypoint; bounded, retryable, no user authority needed to expire grants."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('invalid cleanup limit')
        reports = []
        async with self.store.lifecycle_lock():
            for row in await self.store.uploads_for_cleanup(self.now(), limit):
                try:
                    if row.status != 'available':
                        await self.store.update_upload(row.upload_id, 'failed', row.manifest)
                        row = await self.store.get_upload(row.upload_id)
                    await self._cleanup(row)
                    reports.append({'upload_id': row.upload_id, 'status': 'cleaned'})
                except Exception:
                    reports.append({'upload_id': row.upload_id, 'status': 'retry'})
        return reports

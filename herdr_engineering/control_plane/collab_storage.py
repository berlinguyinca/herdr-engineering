"""Provider-neutral collaboration keys and archive manifest integrity.

Only opaque entity IDs belong in keys. Upload paths remain in relational metadata;
their UTF-8 SHA-256 identifies staged bytes without exposing filenames. Manifests
use sorted, compact UTF-8 JSON (not RFC 8785). This boundary does not authorize
callers, publish uploads, create buckets, or implement archive lifecycle actions.
Version 1 manifests require a timezone-aware ISO 8601 ``created_at``, an
``encryption`` mapping with a nonempty ``mode``, and an ``integrity`` mapping
declaring ``algorithm: sha256``. Additional JSON metadata is preserved.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from botocore.exceptions import ClientError

from . import ids
from .rustfs import RustFSClient


class StorageError(Exception):
    """Storage operation failed without exposing provider error details."""


def _segment(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError("expected a safe environment or variant token")
    return value


def _opaque_id(value: str, kind: str) -> str:
    if isinstance(value, str):
        if ids.is_valid_id(value, kind):
            return value
        try:
            if str(UUID(value)) == value:
                return value
        except ValueError:
            pass
    raise ValueError(f"expected an opaque {kind} ID (entity ID or canonical UUID)")


def _relative_path(value: str) -> list[str]:
    if (not isinstance(value, str) or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError("expected a normalized relative path")
    return value.split("/")


def live_key(environment: str, room_id: str, file_id: str, variant: str | None = None) -> str:
    prefix = f"env/{_segment(environment)}/rooms/{_opaque_id(room_id, 'room')}/files/{_opaque_id(file_id, 'file')}"
    return f"{prefix}/blob" if variant is None else f"{prefix}/preview/{_segment(variant)}"


def upload_key(environment: str, room_id: str, upload_id: str, relative_path: str) -> str:
    _relative_path(relative_path)
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
    return f"env/{_segment(environment)}/rooms/{_opaque_id(room_id, 'room')}/uploads/{_opaque_id(upload_id, 'upload')}/{digest}"


def archive_key(environment: str, archive_id: str, name: str) -> str:
    parts = _relative_path(name)
    if name in {"manifest.json", "room.json", "members.ndjson", "messages.ndjson", "tasks.ndjson"}:
        pass
    elif len(parts) == 2 and parts[0] == "sessions" and parts[1].endswith(".ndjson"):
        _opaque_id(parts[1][:-7], "session")
    elif len(parts) == 3 and parts[0] == "files" and parts[2] in {"metadata.json", "blob"}:
        _opaque_id(parts[1], "file")
    elif len(parts) == 4 and parts[0] == "files" and parts[2] == "preview":
        _opaque_id(parts[1], "file")
        _segment(parts[3])
    else:
        raise ValueError("unsupported archive object name")
    return f"env/{_segment(environment)}/archives/{_opaque_id(archive_id, 'archive')}/{name}"


@dataclass(frozen=True)
class S3Config:
    environment: str
    endpoint: str | None = None
    region: str = "us-east-1"
    live_bucket: str = "herdr-collab-live"
    archive_bucket: str = "herdr-collab-archive"
    access_key: str | None = field(default=None, repr=False)
    secret_key: str | None = field(default=None, repr=False)
    session_token: str | None = field(default=None, repr=False)
    addressing_style: str = "auto"

    def __post_init__(self) -> None:
        _segment(self.environment)
        if not self.live_bucket or not self.archive_bucket or self.live_bucket == self.archive_bucket:
            raise ValueError("live and archive buckets must be distinct and nonempty")
        if self.addressing_style not in {"auto", "path", "virtual"}:
            raise ValueError("unsupported S3 addressing style")
        if (self.access_key is None) != (self.secret_key is None):
            raise ValueError("access and secret keys must be configured together")


@dataclass(frozen=True)
class ObjectRef:
    bucket: str
    key: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class VerificationFailure:
    key: str
    reason: str


@dataclass(frozen=True)
class VerificationReport:
    objects_checked: int
    errors: tuple[VerificationFailure, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def _validate_json(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("manifest JSON keys must be strings")
            _validate_json(item)
    elif isinstance(value, list):
        for item in value:
            _validate_json(item)
    elif value is None or type(value) in {str, bool, int}:
        return
    elif type(value) is not float or not math.isfinite(value):
        raise ValueError("manifest values must be finite JSON values")


def _manifest_objects(manifest: dict[str, Any], environment: str,
                      archive_id: str) -> list[dict[str, Any]]:
    """Reject malformed or cross-archive references before any storage I/O."""
    _validate_json(manifest)
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1 or manifest.get("archive_id") != archive_id):
        raise ValueError("invalid manifest version or archive identity")
    _opaque_id(manifest.get("original_room_id"), "room")
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str):
        raise ValueError("manifest created_at must be an ISO 8601 timestamp")
    try:
        timestamp = datetime.fromisoformat(created_at)
    except ValueError:
        raise ValueError("manifest created_at must be an ISO 8601 timestamp") from None
    if timestamp.tzinfo is None:
        raise ValueError("manifest created_at must include a timezone")
    encryption = manifest.get("encryption")
    if (not isinstance(encryption, dict) or not isinstance(encryption.get("mode"), str)
            or not encryption["mode"].strip()):
        raise ValueError("manifest encryption must declare a nonempty mode")
    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "sha256":
        raise ValueError("manifest integrity must declare the sha256 algorithm")
    prefix = archive_key(environment, archive_id, "manifest.json").removesuffix("manifest.json")
    objects = manifest.get("objects")
    if not isinstance(objects, list):
        raise ValueError("manifest objects must be a list")
    seen = set()
    for obj in objects:
        if not isinstance(obj, dict):
            raise ValueError("manifest object must be a mapping")
        key, size, digest = obj.get("key"), obj.get("size_bytes"), obj.get("sha256")
        if not isinstance(key, str) or not key.startswith(prefix):
            raise ValueError("manifest object is outside the archive prefix")
        name = key[len(prefix):]
        if name == "manifest.json" or key in seen:
            raise ValueError("duplicate or self-referencing manifest object")
        archive_key(environment, archive_id, name)
        if (type(size) is not int or size < 0 or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("invalid object size or SHA-256")
        seen.add(key)
    return objects


class CollabStorage:
    """S3 adapter; omitted credentials use the SDK's standard credential chain.

    ``s3`` may supply a configured boto3-compatible transport. The returned
    manifest reference hashes the persisted bytes; verification checks the listed
    objects against a trusted manifest, not the authenticity of that manifest.
    """

    def __init__(self, config: S3Config, *, s3: Any | None = None):
        self.config = config
        self._archive = RustFSClient(
            endpoint=config.endpoint, bucket=config.archive_bucket, region=config.region,
            access_key=config.access_key, secret_key=config.secret_key,
            session_token=config.session_token, addressing_style=config.addressing_style, s3=s3,
        )

    def write_manifest(self, archive_id: str, manifest: dict[str, Any]) -> ObjectRef:
        _manifest_objects(manifest, self.config.environment, archive_id)
        data = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
        key = archive_key(self.config.environment, archive_id, "manifest.json")
        try:
            digest = self._archive.put_at(key, data)
        except Exception:
            # Suppress provider exception text and chained tracebacks, including signed URLs.
            raise StorageError("archive manifest write failed") from None
        return ObjectRef(self.config.archive_bucket, key, len(data), digest)

    def checksum(self, key: str) -> tuple[int, str]:
        """Stream an archive object, returning its actual size and SHA-256."""
        return self._archive.checksum(key)


    def read_archive(self, archive_id: str, name: str, expected_sha256: str) -> bytes:
        """Read bounded metadata only; file bytes use server-side copies."""
        key = archive_key(self.config.environment, archive_id, name)
        body = None
        try:
            try:
                body = self._archive._s3.get_object(Bucket=self.config.archive_bucket, Key=key)['Body']
                chunks, size = [], 0
                while chunk := body.read(1024 * 1024):
                    size += len(chunk)
                    if size > 64 * 1024 * 1024:
                        raise ValueError('metadata exceeds limit')
                    chunks.append(chunk)
                data = b''.join(chunks)
                if hashlib.sha256(data).hexdigest() != expected_sha256:
                    raise ValueError('checksum mismatch')
                return data
            finally:
                if body is not None:
                    body.close()
        except Exception:
            raise StorageError('archive metadata verification failed') from None

    def write_archive(self, archive_id: str, name: str, data: bytes) -> ObjectRef:
        key = archive_key(self.config.environment, archive_id, name)
        if len(data) > 64 * 1024 * 1024:
            raise StorageError('archive metadata exceeds limit')
        try:
            digest = self._archive.put_at(key, data)
            return ObjectRef(self.config.archive_bucket, key, len(data), digest)
        except Exception:
            raise StorageError('archive object write failed') from None

    def live_inventory(self, room_id: str) -> list[str]:
        prefix = f'env/{self.config.environment}/rooms/{_opaque_id(room_id, "room")}/'
        keys, continuation = [], {}
        try:
            while True:
                page = self._archive._s3.list_objects_v2(
                    Bucket=self.config.live_bucket, Prefix=prefix, **continuation)
                keys.extend(obj['Key'] for obj in page.get('Contents', []))
                if not page.get('IsTruncated'):
                    return sorted(keys)
                continuation = {'ContinuationToken': page['NextContinuationToken']}
        except Exception:
            raise StorageError('live inventory failed') from None

    def _live_client(self) -> RustFSClient:
        return RustFSClient(endpoint=self.config.endpoint, bucket=self.config.live_bucket,
                            s3=self._archive._s3)

    def archive_file(self, archive_id: str, room_id: str, file_id: str,
                     variant: str | None = None, verified_parts: list[dict] | None = None) -> ObjectRef:
        source = live_key(self.config.environment, room_id, file_id, variant)
        name = f'files/{file_id}/' + ('blob' if variant is None else f'preview/{variant}')
        key = archive_key(self.config.environment, archive_id, name)
        try:
            size, digest = self._live_client().checksum(source, parts=verified_parts)
            # boto3's managed copy supports multipart server-side copies, including >5GB.
            self._archive._s3.copy({'Bucket': self.config.live_bucket, 'Key': source},
                                   self.config.archive_bucket, key)
            return ObjectRef(self.config.archive_bucket, key, size, digest)
        except Exception:
            raise StorageError('archive file copy failed') from None

    def restore_file(self, archive_id: str, old_file_id: str, room_id: str, file_id: str,
                     size: int, digest: str, variant: str | None = None) -> str:
        name = f'files/{old_file_id}/' + ('blob' if variant is None else f'preview/{variant}')
        source = archive_key(self.config.environment, archive_id, name)
        key = live_key(self.config.environment, room_id, file_id, variant)
        try:
            self._archive._s3.copy({'Bucket': self.config.archive_bucket, 'Key': source},
                                   self.config.live_bucket, key)
            if self._live_client().checksum(key) != (size, digest):
                raise ValueError('checksum mismatch')
            return key
        except Exception:
            raise StorageError('restored file verification failed') from None

    def live_checksum(self, room_id: str, file_id: str, variant: str | None = None) -> tuple[int, str]:
        key = live_key(self.config.environment, room_id, file_id, variant)
        try:
            return self._live_client().checksum(key)
        except Exception:
            raise StorageError('live file verification failed') from None

    def delete_live_file(self, room_id: str, file_id: str, variant: str | None = None) -> None:
        key = live_key(self.config.environment, room_id, file_id, variant)
        try:
            self._live_client().delete(key)
        except Exception:
            raise StorageError('live file deletion failed') from None


    def require_unversioned_archive_scrub(self) -> None:
        """Current-object rewrites cannot erase historical versions or delete markers.

        Check even if the current snapshot is already clean: older versions may
        predate a previous scrub. Missing/denied inspection also fails closed.
        """
        try:
            if self._archive._s3.get_bucket_versioning(Bucket=self.config.archive_bucket).get('Status') is not None:
                raise ValueError('version-aware archive erasure required')
        except Exception:
            raise StorageError('archive version history cleanup could not be verified') from None

    def delete_skill_package(self, bucket: str | None, key: str) -> None:
        """Delete a journaled package and prove absence; never guess a legacy bucket.

        Versioned or suspended buckets require version-aware erasure, so fail
        closed rather than treating an S3 delete marker as proof of a scrub.
        The location comes from trusted relational metadata, not package input.
        """
        prefix = f'env/{self.config.environment}/skills/sha256/'
        try:
            if (not isinstance(bucket, str) or not bucket
                    or not isinstance(key, str) or not key.startswith(prefix)
                    or not re.fullmatch(r'[0-9a-f]{64}/package\.json', key[len(prefix):])):
                raise ValueError('unknown or unsupported skill package location')
            s3 = self._archive._s3
            if s3.get_bucket_versioning(Bucket=bucket).get('Status') is not None:
                raise ValueError('version-aware erasure required')
            s3.delete_object(Bucket=bucket, Key=key)
            try:
                s3.head_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                if exc.response.get('Error', {}).get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
                    return
                raise
            raise ValueError('skill package still exists')
        except Exception:
            raise StorageError('skill package cleanup could not be verified') from None


def verify_manifest(manifest: dict[str, Any], client: CollabStorage) -> VerificationReport:
    archive_id = manifest.get("archive_id") if isinstance(manifest, dict) else None
    objects = _manifest_objects(manifest, client.config.environment, archive_id)
    errors = []
    for obj in objects:
        key = obj["key"]
        try:
            size, digest = client.checksum(key)
        except Exception:
            # Provider errors may contain signed URLs or credentials. Never relay them.
            errors.append(VerificationFailure(key, "unavailable"))
            continue
        if size != obj["size_bytes"]:
            errors.append(VerificationFailure(key, "size_mismatch"))
        if digest != obj["sha256"]:
            errors.append(VerificationFailure(key, "sha256_mismatch"))
    return VerificationReport(len(objects), tuple(errors))

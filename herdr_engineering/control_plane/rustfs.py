"""RustFS / S3 content-addressed object store client (Spec §56-57, §69)."""
from __future__ import annotations

import hashlib
import io
import tempfile
from typing import Any


class IntegrityError(Exception):
    """SHA-256 verification failed for an object retrieved from RustFS."""


def content_key(data: bytes) -> str:
    hex_ = hashlib.sha256(data).hexdigest()
    return f"sha256/{hex_[:2]}/{hex_[2:4]}/{hex_}"


class RustFSClient:
    """Thin S3-compatible client over RustFS. ``s3`` is a boto3 client or fake."""

    def __init__(self, *, endpoint: str | None, bucket: str,
                 region: str = "us-east-1", s3: Any | None = None,
                 sha: Any = hashlib.sha256,
                 access_key: str | None = "herdr", secret_key: str | None = "herdr",
                 session_token: str | None = None, addressing_style: str = "auto"):
        self._bucket = bucket
        self._sha = sha
        if s3 is not None:
            self._s3 = s3
        else:
            import boto3
            from botocore.config import Config

            self._s3 = boto3.client(
                "s3", endpoint_url=endpoint,
                region_name=region,
                aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                aws_session_token=session_token,
                config=Config(signature_version="s3v4", s3={"addressing_style": addressing_style}),
            )

    def ensure_bucket(self) -> None:
        """Create the bucket if it does not already exist (idempotent)."""
        try:
            self._s3.head_bucket(Bucket=self._bucket)
        except Exception:
            self._s3.create_bucket(Bucket=self._bucket)

    def put(self, data: bytes) -> tuple[str, str]:
        digest = self._sha(data).hexdigest()
        key = self._key_for_digest(digest)
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data)
        return key, digest

    @staticmethod
    def _key_for_digest(digest: str) -> str:
        return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"

    def put_stream(self, stream: Any, *, expected_sha256: str | None = None,
                   expected_size: int | None = None, media_type: str | None = None,
                   chunk_size: int = 1024 * 1024) -> dict[str, Any]:
        """Upload a stream with bounded memory and verified content identity.

        The payload is spooled to a temporary file so large uploads never need
        to be held in RAM. The S3 client receives a seekable file object through
        ``upload_fileobj`` when available (boto3/MinIO/RustFS); the fallback
        ``put_object`` path keeps compatibility with small test clients.
        """
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        digest = self._sha()
        size = 0
        with tempfile.NamedTemporaryFile(mode="w+b") as spool:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise TypeError("artifact stream must return bytes")
                chunk = bytes(chunk)
                size += len(chunk)
                if expected_size is not None and size > expected_size:
                    raise IntegrityError(
                        f"size mismatch: expected {expected_size}, received at least {size}")
                spool.write(chunk)
                digest.update(chunk)
            actual = digest.hexdigest()
            if expected_size is not None and size != expected_size:
                raise IntegrityError(
                    f"size mismatch: expected {expected_size}, received {size}")
            if expected_sha256 is not None and actual != expected_sha256:
                raise IntegrityError(
                    f"sha256 mismatch: expected {expected_sha256}, received {actual}")
            key = self._key_for_digest(actual)
            spool.seek(0)
            extra = {"ContentType": media_type} if media_type else None
            upload_fileobj = getattr(self._s3, "upload_fileobj", None)
            if upload_fileobj is not None:
                if extra is None:
                    upload_fileobj(spool, self._bucket, key)
                else:
                    upload_fileobj(spool, self._bucket, key, ExtraArgs=extra)
            else:
                self._s3.put_object(Bucket=self._bucket, Key=key, Body=spool, **(extra or {}))
        return {"key": key, "sha256": actual, "size": size,
                "media_type": media_type}

    def iter_get(self, content_key: str, expected_sha256: str,
                 *, chunk_size: int = 1024 * 1024):
        """Yield bounded chunks; verify the digest when fully consumed."""
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        body = self._s3.get_object(Bucket=self._bucket, Key=content_key)["Body"]
        if not hasattr(body, "read") and not hasattr(body, "iter_chunks"):
            body = io.BytesIO(bytes(body))
        digest = self._sha()
        try:
            iterator = body.iter_chunks(chunk_size=chunk_size) \
                if hasattr(body, "iter_chunks") else None
            if iterator is not None:
                for chunk in iterator:
                    if chunk:
                        digest.update(chunk)
                        yield chunk
            else:
                while True:
                    chunk = body.read(chunk_size)
                    if not chunk:
                        break
                    digest.update(chunk)
                    yield chunk
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        actual = digest.hexdigest()
        if actual != expected_sha256:
            raise IntegrityError(f"sha256 mismatch for {content_key}")

    def get(self, content_key: str, expected_sha256: str) -> bytes:
        return b"".join(self.iter_get(content_key, expected_sha256))

    def put_at(self, key: str, data: bytes) -> str:
        """Write at a caller-selected key and return the bytes' SHA-256."""
        digest = self._sha(data).hexdigest()
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data)
        return digest

    def checksum(self, key: str, *, parts: list[dict] | None = None) -> tuple[int, str]:
        """Hash bytes with bounded memory; optionally verify each persisted part.

        Part evidence binds a composite-only upload to actual bytes. The returned
        whole-file hash is computed independently, not copied from the manifest.
        """
        body = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"]
        if not hasattr(body, 'read'):
            body = io.BytesIO(bytes(body))
        digest, size = self._sha(), 0
        try:
            if parts is not None:
                if not parts:
                    raise IntegrityError('missing part checksums')
                for part in parts:
                    remaining = part['size_bytes']
                    if type(remaining) is not int or remaining < 0:
                        raise IntegrityError('invalid part size')
                    part_digest = self._sha()
                    while remaining:
                        chunk = body.read(min(remaining, 1024 * 1024))
                        if not chunk or len(chunk) > remaining:
                            raise IntegrityError('part size mismatch')
                        digest.update(chunk)
                        part_digest.update(chunk)
                        size += len(chunk)
                        remaining -= len(chunk)
                    if part_digest.hexdigest() != part['sha256']:
                        raise IntegrityError('part checksum mismatch')
                if body.read(1):
                    raise IntegrityError('unexpected trailing bytes')
            else:
                while chunk := body.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
        finally:
            if hasattr(body, 'close'):
                body.close()
        return size, digest.hexdigest()

    def head(self, content_key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=content_key)
            return True
        except Exception:
            return False

    def delete(self, content_key: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=content_key)

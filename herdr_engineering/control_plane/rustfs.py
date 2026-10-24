"""RustFS / S3 content-addressed object store client (Spec §56-57, §69)."""
from __future__ import annotations

import hashlib
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
                config=Config(s3={"addressing_style": addressing_style}),
            )

    def ensure_bucket(self) -> None:
        """Create the bucket if it does not already exist (idempotent)."""
        try:
            self._s3.head_bucket(Bucket=self._bucket)
        except Exception:
            self._s3.create_bucket(Bucket=self._bucket)

    def put(self, data: bytes) -> tuple[str, str]:
        digest = self._sha(data).hexdigest()
        key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data)
        return key, digest

    def get(self, content_key: str, expected_sha256: str) -> bytes:
        body = self._s3.get_object(Bucket=self._bucket, Key=content_key)["Body"]
        data = body.read() if hasattr(body, "read") else bytes(body)
        if self._sha(data).hexdigest() != expected_sha256:
            raise IntegrityError(f"sha256 mismatch for {content_key}")
        return data

    def put_at(self, key: str, data: bytes) -> str:
        """Write at a caller-selected key and return the bytes' SHA-256."""
        digest = self._sha(data).hexdigest()
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data)
        return digest

    def checksum(self, key: str) -> tuple[int, str]:
        """Hash actual object bytes with bounded memory, never treating ETag as SHA-256."""
        body = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"]
        digest, size = self._sha(), 0
        try:
            if hasattr(body, "read"):
                while chunk := body.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            else:
                data = bytes(body)
                digest.update(data)
                size = len(data)
        finally:
            if hasattr(body, "close"):
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

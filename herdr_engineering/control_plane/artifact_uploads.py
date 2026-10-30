"""Resumable artifact uploads, proxied through the control plane.

A session is declared up front (size, SHA-256, media type); parts arrive in any order and may be re-sent; completion
streams the parts through ``RustFSClient.put_stream`` with the declared digest and size, so one wrong byte anywhere
fails the whole upload and nothing reaches the content-addressed store. Parts are staged under ``staging/uploads/`` (the
only keys an upload may write or delete: content-addressed keys are shared between artifacts). No browser-to-S3 traffic
exists, so no CSP origin is needed. Authorization (edit on the artifact) is the caller's job; every method here is
scoped by ``artifact_id`` so an upload id never works against another artifact.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from typing import Any

from .rustfs import IntegrityError

MAX_PART_BYTES = 64 * 1024 * 1024
DEFAULT_PART_BYTES = 16 * 1024 * 1024
MAX_PARTS = 10_000
MAX_UPLOAD_BYTES = 1024**3
DEFAULT_TTL_SECONDS = 24 * 3600
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def staging_key(upload_id: str, part_number: int) -> str:
    return f"staging/uploads/{upload_id}/{part_number:05d}"


class _PartsReader:
    """File-like reader over verified staged parts, in order, with bounded memory."""

    def __init__(self, rustfs: Any, parts: list[dict]):
        self._chunks = self._iterate(rustfs, parts)
        self._buffer = b""

    @staticmethod
    def _iterate(rustfs, parts):
        for part in parts:
            yield from rustfs.iter_get(part["storage_key"], part["sha256"])

    def read(self, size: int = -1) -> bytes:
        while size < 0 or len(self._buffer) < size:
            try:
                self._buffer += next(self._chunks)
            except StopIteration:
                break
        if size < 0:
            data, self._buffer = self._buffer, b""
        else:
            data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data


class ArtifactUploadMixin:
    _conn: Any
    _rf: Any

    async def create_upload(self, artifact_id: str, *, principal_id: str, filename: str, media_type: str,
                            size_bytes: int, sha256: str, part_size: int | None = None,
                            idempotency_key: str | None = None, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict:
        from .artifacts import ArtifactError
        if not principal_id:
            raise ArtifactError("principal is required")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or not 0 < size_bytes <= MAX_UPLOAD_BYTES:
            raise ArtifactError(f"size_bytes must be between 1 and {MAX_UPLOAD_BYTES}")
        if not isinstance(sha256, str) or not _SHA256.match(sha256):
            raise ArtifactError("sha256 must be 64 lowercase hex characters")
        if (not isinstance(media_type, str) or not media_type or len(media_type) > 255
                or not isinstance(filename, str) or not filename or len(filename) > 512
                or any(ord(c) < 32 for c in filename)):
            raise ArtifactError("filename and media_type are required and bounded")
        part_size = part_size if part_size is not None else min(size_bytes, DEFAULT_PART_BYTES)
        if isinstance(part_size, bool) or not isinstance(part_size, int) or not 0 < part_size <= MAX_PART_BYTES:
            raise ArtifactError(f"part_size must be between 1 and {MAX_PART_BYTES}")
        part_count = -(-size_bytes // part_size)
        if part_count > MAX_PARTS:
            raise ArtifactError(f"too many parts (max {MAX_PARTS}); raise part_size")
        if idempotency_key is not None and not (isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 256):
            raise ArtifactError("idempotency key must be a short string")
        fingerprint = (artifact_id, filename, media_type, size_bytes, sha256, part_size)
        if idempotency_key:
            prior = await self._conn.fetchrow(
                "SELECT * FROM artifact_upload_sessions WHERE principal_id=$1 AND idempotency_key=$2",
                principal_id, idempotency_key)
            if prior:
                same = (prior["artifact_id"], prior["filename"], prior["media_type"], prior["size_bytes"],
                        prior["sha256"], prior["part_size"]) == fingerprint
                if not same:
                    raise ArtifactError("idempotency key was reused for a different upload")
                return self._upload_view(prior)
        if await self._conn.fetchrow("SELECT 1 AS x FROM artifacts WHERE artifact_id=$1", artifact_id) is None:
            raise ArtifactError(f"no artifact {artifact_id}")
        upload_id = "upload_" + secrets.token_hex(16)
        await self._conn.execute(
            "INSERT INTO artifact_upload_sessions (upload_id, artifact_id, principal_id, idempotency_key, filename, "
            "media_type, size_bytes, sha256, part_size, part_count, expires_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, now() + ($11 * interval '1 second'))",
            upload_id, artifact_id, principal_id, idempotency_key, filename, media_type, size_bytes, sha256,
            part_size, part_count, float(max(60, min(7 * 86400, int(ttl_seconds)))))
        row = await self._get_upload_row(upload_id, artifact_id)
        return self._upload_view(row)

    async def _get_upload_row(self, upload_id: str, artifact_id: str):
        return await self._conn.fetchrow(
            "SELECT * FROM artifact_upload_sessions WHERE upload_id=$1 AND artifact_id=$2", upload_id, artifact_id)

    @staticmethod
    def _upload_view(row: Any) -> dict:
        return {"upload_id": row["upload_id"], "artifact_id": row["artifact_id"], "state": row["state"],
                "filename": row["filename"], "media_type": row["media_type"], "size_bytes": row["size_bytes"],
                "sha256": row["sha256"], "part_size": row["part_size"], "part_count": row["part_count"],
                "expires_at": row["expires_at"].isoformat() if row.get("expires_at") else None}

    async def get_upload(self, upload_id: str, artifact_id: str) -> dict | None:
        row = await self._get_upload_row(upload_id, artifact_id)
        if row is None:
            return None
        parts = await self._conn.fetch(
            "SELECT part_number, size_bytes, sha256 FROM artifact_upload_parts WHERE upload_id=$1 "
            "ORDER BY part_number", upload_id)
        have = {p["part_number"] for p in parts}
        view = self._upload_view(row)
        view["received"] = [{"part_number": p["part_number"], "size_bytes": p["size_bytes"], "sha256": p["sha256"]}
                            for p in parts]
        view["missing"] = [n for n in range(1, row["part_count"] + 1) if n not in have]
        return view

    async def put_upload_part(self, upload_id: str, artifact_id: str, part_number: int, data: bytes, *,
                              sha256: str | None = None) -> dict:
        from .artifacts import ArtifactConflict, ArtifactError
        row = await self._get_upload_row(upload_id, artifact_id)
        if row is None:
            raise ArtifactError("no such upload")
        if row["state"] != "open":
            raise ArtifactError(f"upload is {row['state']}")
        if await self._conn.fetchrow(
                "SELECT 1 AS x FROM artifact_upload_sessions WHERE upload_id=$1 AND expires_at > now()", upload_id) is None:
            raise ArtifactError("upload has expired")
        if isinstance(part_number, bool) or not isinstance(part_number, int) or not 1 <= part_number <= row["part_count"]:
            raise ArtifactError("part number out of range")
        expected = (row["size_bytes"] - (row["part_count"] - 1) * row["part_size"]
                    if part_number == row["part_count"] else row["part_size"])
        if len(data) != expected:
            raise ArtifactError(f"part {part_number} must be exactly {expected} bytes")
        digest = hashlib.sha256(data).hexdigest()
        if sha256 is not None and sha256 != digest:
            raise ArtifactError("part sha256 does not match its bytes")
        existing = await self._conn.fetchrow(
            "SELECT sha256 FROM artifact_upload_parts WHERE upload_id=$1 AND part_number=$2", upload_id, part_number)
        if existing:
            if existing["sha256"] != digest:
                raise ArtifactConflict(artifact_id, None)
            return {"part_number": part_number, "sha256": digest, "size_bytes": len(data), "replayed": True}
        key = staging_key(upload_id, part_number)
        self._rf.put_at(key, data)
        await self._conn.execute(
            "INSERT INTO artifact_upload_parts (upload_id, part_number, size_bytes, sha256, storage_key) "
            "VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING", upload_id, part_number, len(data), digest, key)
        return {"part_number": part_number, "sha256": digest, "size_bytes": len(data), "replayed": False}

    async def complete_upload(self, upload_id: str, artifact_id: str) -> dict:
        from .artifacts import ArtifactError
        row = await self._get_upload_row(upload_id, artifact_id)
        if row is None:
            raise ArtifactError("no such upload")
        if row["state"] == "complete":
            blob = await self._conn.fetchrow(
                "SELECT storage_key, blob_sha256, size_bytes, media_type FROM artifact_blobs "
                "WHERE scope=$1 AND blob_sha256=$2", artifact_id, row["sha256"])
            return {"upload_id": upload_id, "state": "complete", "storage_key": row["storage_key"],
                    "sha256": row["sha256"], "size_bytes": row["size_bytes"],
                    "media_type": blob["media_type"] if blob else row["media_type"]}
        if row["state"] != "open":
            raise ArtifactError(f"upload is {row['state']}")
        parts = [dict(p) for p in await self._conn.fetch(
            "SELECT part_number, size_bytes, sha256, storage_key FROM artifact_upload_parts WHERE upload_id=$1 "
            "ORDER BY part_number", upload_id)]
        have = {p["part_number"] for p in parts}
        missing = [n for n in range(1, row["part_count"] + 1) if n not in have]
        if missing:
            raise ArtifactError(f"missing parts: {missing[:20]}")
        try:
            stored = await asyncio.to_thread(
                self._rf.put_stream, _PartsReader(self._rf, parts), expected_sha256=row["sha256"],
                expected_size=row["size_bytes"], media_type=row["media_type"])
        except IntegrityError as exc:
            # Fail closed: a corrupt session can never complete, and its staging bytes are removed.
            await self._finish_upload(upload_id, parts, "aborted")
            raise ArtifactError("upload failed verification; start a new upload") from exc
        await self.register_report_blob(artifact_id, storage_key=stored["key"], sha256=stored["sha256"],
                                        size_bytes=stored["size"], media_type=row["media_type"])
        await self._finish_upload(upload_id, parts, "complete", storage_key=stored["key"])
        return {"upload_id": upload_id, "state": "complete", "storage_key": stored["key"],
                "sha256": stored["sha256"], "size_bytes": stored["size"], "media_type": row["media_type"]}

    async def abort_upload(self, upload_id: str, artifact_id: str) -> dict:
        from .artifacts import ArtifactError
        row = await self._get_upload_row(upload_id, artifact_id)
        if row is None:
            raise ArtifactError("no such upload")
        if row["state"] == "open":
            await self._finish_upload(upload_id, await self._staged_parts(upload_id), "aborted")
        return {"upload_id": upload_id, "state": "aborted" if row["state"] == "open" else row["state"]}

    async def _staged_parts(self, upload_id: str) -> list[dict]:
        return [dict(p) for p in await self._conn.fetch(
            "SELECT part_number, storage_key FROM artifact_upload_parts WHERE upload_id=$1", upload_id)]

    async def _finish_upload(self, upload_id: str, parts: list[dict], state: str, storage_key: str | None = None):
        for part in parts:
            key = part["storage_key"]
            if key.startswith("staging/uploads/"):        # never a shared, content-addressed key
                try:
                    self._rf.delete(key)
                except Exception:                          # noqa: BLE001 - leftover staging is swept by expiry
                    pass
        await self._conn.execute("DELETE FROM artifact_upload_parts WHERE upload_id=$1", upload_id)
        await self._conn.execute(
            "UPDATE artifact_upload_sessions SET state=$2, storage_key=COALESCE($3, storage_key), "
            "completed_at=CASE WHEN $2='complete' THEN now() ELSE completed_at END WHERE upload_id=$1",
            upload_id, state, storage_key)

    async def expire_uploads(self, limit: int = 100) -> int:
        rows = await self._conn.fetch(
            "SELECT upload_id FROM artifact_upload_sessions WHERE state='open' AND expires_at <= now() "
            "ORDER BY expires_at LIMIT $1", int(limit))
        for row in rows:
            await self._finish_upload(row["upload_id"], await self._staged_parts(row["upload_id"]), "expired")
        return len(rows)


__all__ = ["ArtifactUploadMixin", "staging_key", "MAX_PART_BYTES", "MAX_PARTS", "MAX_UPLOAD_BYTES"]

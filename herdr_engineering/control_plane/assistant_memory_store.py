"""Bounded PostgreSQL memory journal; no backend network calls.

Caller supplies trusted server scope and approval metadata. This is NOT an
 authorization boundary and does not interpret approval claims in memory text.
Use one store per dedicated asyncpg connection; do not share that connection
with unrelated operations. RETAIN deliveries coalesce; revision history does
not. Tombstones are permanent and cannot be resurrected by record().
"""

from __future__ import annotations

import asyncio
import json


class RevisionConflict(ValueError):
    """Missing, stale, deleted, or unavailable document (no scope disclosure)."""


class AssistantMemoryStore:
    """Scoped row-dict API. Caller owns connection lifetime and backend delivery.

    expected_revision=0 creates; updates/forget require the current revision.
    Trusted project/personal domains share by (tenant, domain_type, domain_id).
    Assistant domains additionally require the exact user/assistant pair. The
    gateway authorizes domain membership; these arguments are not user input.
    Canonical rows preserve their creator; journal and outbox record the actual
    submitting user/assistant for each revision, including shared-domain writes.
    Limits bound individual writes and reads, not total journal retention.
    """

    def __init__(
        self,
        conn,
        tenant_id: str,
        user_id: str,
        assistant_id: str,
        *,
        domain_type: str = "assistant",
        domain_id: str | None = None,
        max_content_bytes: int = 65536,
        max_metadata_bytes: int = 16384,
    ):
        if any(
            not isinstance(v, str) or not v or len(v.encode()) > 512
            for v in (tenant_id, user_id, assistant_id)
        ):
            raise ValueError("invalid scope")
        if not 1 <= max_content_bytes <= 65536 or not 1 <= max_metadata_bytes <= 16384:
            raise ValueError("invalid bounds")
        if domain_type not in ("assistant", "project", "personal"):
            raise ValueError("invalid domain type")
        if domain_id is not None and (
            not isinstance(domain_id, str) or not domain_id or len(domain_id.encode()) > 512
        ):
            raise ValueError("invalid domain id")
        if domain_type != "assistant" and domain_id is None:
            raise ValueError("project/personal domain requires a domain id")
        self.conn = conn
        self.scope = (tenant_id, user_id, assistant_id)
        self._scope = (*self.scope, domain_type, domain_id)
        self.max_content_bytes = max_content_bytes
        self.max_metadata_bytes = max_metadata_bytes
        self._lock = asyncio.Lock()

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        return limit

    @staticmethod
    def _document(memory_id, expected_revision):
        if not isinstance(memory_id, str) or not memory_id or len(memory_id.encode()) > 512:
            raise ValueError("invalid memory id")
        if type(expected_revision) is not int or not 0 <= expected_revision < 2147483647:
            raise ValueError("invalid expected revision")

    @staticmethod
    def _row(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("provenance", "approval_metadata"):
            if isinstance(result.get(key), str):
                result[key] = json.loads(result[key])
        return result

    async def record(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        content: str,
        provenance: dict,
        capture_kind: str,
        approval_metadata: dict,
    ):
        """CAS row, append revision, and enqueue RETAIN; preserve nonblank content exactly."""
        self._document(memory_id, expected_revision)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a nonblank string")
        if len(content.encode()) > self.max_content_bytes:
            raise ValueError("content exceeds byte limit")
        if capture_kind not in ("summary", "fact"):
            raise ValueError("invalid capture kind")
        metadata = []
        for value in (provenance, approval_metadata):
            if not isinstance(value, dict):
                raise ValueError("metadata must be objects")
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode()) > self.max_metadata_bytes:
                raise ValueError("metadata exceeds byte limit")
            metadata.append(encoded)
        return await self._mutate(
            memory_id, expected_revision, content, metadata[0], capture_kind, metadata[1]
        )

    async def forget(self, memory_id: str, *, expected_revision: int):
        """Soft-delete, append tombstone, replace pending delivery with DELETE."""
        self._document(memory_id, expected_revision)
        row = await self._mutate(memory_id, expected_revision, "", "{}", "fact", "{}", delete=True)
        return row["revision"]

    async def _mutate(
        self, memory_id, expected, content, provenance, kind, approval, *, delete=False
    ):
        async with self._lock, self.conn.transaction():
            # pa_memory IDs are globally unique, so the advisory key is global too.
            await self.conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", memory_id
            )
            current = await self.conn.fetchrow(
                """SELECT * FROM pa_memory WHERE
                tenant_id=$1 AND domain_type=$4 AND domain_id IS NOT DISTINCT FROM $5
                AND ($4 <> 'assistant' OR (user_id=$2 AND assistant_id=$3)) AND memory_id=$6""",
                *self._scope,
                memory_id,
            )
            if current is None:
                if expected != 0 or delete:
                    raise RevisionConflict("revision conflict")
                inserted = await self.conn.fetchrow(
                    """INSERT INTO pa_memory
                    (tenant_id,user_id,assistant_id,domain_type,domain_id,memory_id,content,provenance,capture_kind,approval_metadata,created_at,updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10::jsonb,now(),now())
                    ON CONFLICT (memory_id) DO NOTHING RETURNING *""",
                    *self._scope,
                    memory_id,
                    content,
                    provenance,
                    kind,
                    approval,
                )
                if inserted is None:
                    raise RevisionConflict("revision conflict")
                row = inserted
            else:
                if current["deleted_at"] is not None or current["revision"] != expected:
                    raise RevisionConflict("revision conflict")
                if delete:
                    # Tombstones keep their kind for filtered UI/sync counts.
                    kind = current["capture_kind"]
                row = await self.conn.fetchrow(
                    """UPDATE pa_memory SET content=$7,
                    provenance=$8::jsonb,capture_kind=$9,approval_metadata=$10::jsonb,
                    revision=revision+1,updated_at=now(),deleted_at=CASE WHEN $11 THEN now() ELSE NULL END
                    WHERE tenant_id=$1 AND domain_type=$4 AND domain_id IS NOT DISTINCT FROM $5
                AND ($4 <> 'assistant' OR (user_id=$2 AND assistant_id=$3)) AND memory_id=$6 RETURNING *""",
                    *self._scope,
                    memory_id,
                    content,
                    provenance,
                    kind,
                    approval,
                    delete,
                )
            operation = "DELETE" if delete else "RETAIN"
            await self.conn.execute(
                """INSERT INTO pa_memory_revision
                (tenant_id,user_id,assistant_id,domain_type,domain_id,memory_id,revision,operation,content,provenance,capture_kind,approval_metadata)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12::jsonb)""",
                *self._scope,
                memory_id,
                row["revision"],
                operation,
                content,
                provenance,
                kind,
                approval,
            )
            await self.conn.execute(
                """INSERT INTO pa_memory_delivery
                (tenant_id,user_id,assistant_id,domain_type,domain_id,memory_id,revision,operation)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (memory_id) DO UPDATE SET
                revision=EXCLUDED.revision,operation=EXCLUDED.operation,
                user_id=EXCLUDED.user_id,assistant_id=EXCLUDED.assistant_id,created_at=now()""",
                *self._scope,
                memory_id,
                row["revision"],
                operation,
            )
            return self._row(row)

    async def get(self, memory_id: str):
        async with self._lock:
            return self._row(
                await self.conn.fetchrow(
                    """SELECT * FROM pa_memory WHERE
                tenant_id=$1 AND domain_type=$4 AND domain_id IS NOT DISTINCT FROM $5
                AND ($4 <> 'assistant' OR (user_id=$2 AND assistant_id=$3)) AND memory_id=$6 AND deleted_at IS NULL""",
                    *self._scope,
                    memory_id,
                )
            )

    async def history(self, memory_id: str, *, after_revision: int = 0, limit: int = 100):
        """Ascending bounded journal page, including tombstones."""
        self._document(memory_id, after_revision)
        self._limit(limit)
        async with self._lock:
            return [
                self._row(r)
                for r in await self.conn.fetch(
                    """SELECT * FROM pa_memory_revision WHERE
                tenant_id=$1 AND domain_type=$4 AND domain_id IS NOT DISTINCT FROM $5
                AND ($4 <> 'assistant' OR (user_id=$2 AND assistant_id=$3)) AND memory_id=$6 AND revision>$7
                ORDER BY revision LIMIT $8""",
                    *self._scope,
                    memory_id,
                    after_revision,
                    limit,
                )
            ]

    async def fetch_pending_deliveries(self, *, limit: int = 100):
        """Read retryable deliveries; backend I/O must happen after this returns.

        Fetch does not lease work. Consumers must tolerate duplicates and enforce
        document revision ordering remotely; an in-flight RETAIN may race DELETE.
        """
        self._limit(limit)
        async with self._lock:
            return [
                self._row(r)
                for r in await self.conn.fetch(
                    """SELECT r.* FROM pa_memory_delivery d
                JOIN pa_memory_revision r USING (memory_id,revision)
                WHERE d.tenant_id=$1 AND d.domain_type=$4 AND d.domain_id IS NOT DISTINCT FROM $5
                AND ($4 <> 'assistant' OR (d.user_id=$2 AND d.assistant_id=$3))
                ORDER BY d.created_at,d.memory_id LIMIT $6""",
                    *self._scope,
                    limit,
                )
            ]

    async def ack_delivery(self, memory_id: str, revision: int):
        """True only when this exact pending revision was removed."""
        self._document(memory_id, revision)
        async with self._lock:
            row = await self.conn.fetchrow(
                """DELETE FROM pa_memory_delivery WHERE
                tenant_id=$1 AND domain_type=$4 AND domain_id IS NOT DISTINCT FROM $5
                AND ($4 <> 'assistant' OR (user_id=$2 AND assistant_id=$3)) AND memory_id=$6 AND revision=$7
                RETURNING revision""",
                *self._scope,
                memory_id,
                revision,
            )
            return row is not None

    @staticmethod
    def _capture_kind(capture_kind):
        if capture_kind is not None and capture_kind not in ("summary", "fact"):
            raise ValueError("capture_kind must be summary, fact, or None")

    async def list_records(self, *, capture_kind=None, limit: int = 50, offset: int = 0):
        """Bounded active canonical rows, ordered by memory_id within trusted scope.

        Offset pages are separate database snapshots; concurrent mutations may
        change later pages. These rows, unlike native index hits, are canonical.
        """
        self._capture_kind(capture_kind)
        self._limit(limit)
        if type(offset) is not int or not 0 <= offset <= 100000:
            raise ValueError("offset must be 0..100000")
        async with self._lock:
            return [
                self._row(row)
                for row in await self.conn.fetch(
                    """SELECT * FROM pa_memory WHERE
                    tenant_id=$1 AND domain_type=$4 AND domain_id IS NOT DISTINCT FROM $5
                    AND ($4 <> 'assistant' OR (user_id=$2 AND assistant_id=$3))
                    AND deleted_at IS NULL AND ($6::text IS NULL OR capture_kind=$6)
                    ORDER BY memory_id LIMIT $7 OFFSET $8""",
                    *self._scope,
                    capture_kind,
                    limit,
                    offset,
                )
            ]

    async def is_pending(self, memory_id: str, revision: int):
        """Exact scoped pending check; never infers completion from a delivery page."""
        self._document(memory_id, revision)
        async with self._lock:
            return await self.conn.fetchval(
                """SELECT EXISTS (SELECT 1 FROM pa_memory_delivery WHERE
                tenant_id=$1 AND domain_type=$4 AND domain_id IS NOT DISTINCT FROM $5
                AND ($4 <> 'assistant' OR (user_id=$2 AND assistant_id=$3))
                AND memory_id=$6 AND revision=$7)""",
                *self._scope,
                memory_id,
                revision,
            )

    async def status(self, *, capture_kind=None):
        """Scoped counts only, optionally filtered by kind across all three fields."""
        self._capture_kind(capture_kind)
        async with self._lock:
            return dict(
                await self.conn.fetchrow(
                    """SELECT
                (SELECT count(*) FROM pa_memory WHERE tenant_id=$1 AND domain_type=$4 AND domain_id IS NOT DISTINCT FROM $5
                AND ($4 <> 'assistant' OR (user_id=$2 AND assistant_id=$3)) AND deleted_at IS NULL
                AND ($6::text IS NULL OR capture_kind=$6)) AS active,
                (SELECT count(*) FROM pa_memory WHERE tenant_id=$1 AND domain_type=$4 AND domain_id IS NOT DISTINCT FROM $5
                AND ($4 <> 'assistant' OR (user_id=$2 AND assistant_id=$3)) AND deleted_at IS NOT NULL
                AND ($6::text IS NULL OR capture_kind=$6)) AS deleted,
                (SELECT count(*) FROM pa_memory_delivery d
                JOIN pa_memory_revision r USING (memory_id,revision)
                WHERE d.tenant_id=$1 AND d.domain_type=$4 AND d.domain_id IS NOT DISTINCT FROM $5
                AND ($4 <> 'assistant' OR (d.user_id=$2 AND d.assistant_id=$3))
                AND ($6::text IS NULL OR r.capture_kind=$6)) AS pending""",
                    *self._scope,
                    capture_kind,
                )
            )

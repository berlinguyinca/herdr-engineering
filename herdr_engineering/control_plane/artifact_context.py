"""Registry lookups by project / task / issue / room, and the bindings that make them possible.

Visibility is decided in SQL, before ``LIMIT``, counts, ``has_more`` and search, with the same predicate
``ArtifactStore.list_records`` uses (owner, or an unexpired read/admin grant). Nothing here consults the legacy
"unowned artifacts are public" rule, so these routes cannot leak what the older catalog still exposes.
A context that does not exist and a context with nothing visible produce the same empty page.
"""
from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any

from .artifact_preview import preview_kind
from .artifact_provenance import (
    CONTEXT_KINDS,
    ProvenanceError,
    ctx_key,
    validate_context,
)

MAX_PAGE = 200
VISIBLE = (
    "(a.created_by=$1 OR EXISTS (SELECT 1 FROM artifact_acl acl "
    "WHERE acl.artifact_id=a.artifact_id AND acl.principal_id=$1 "
    "AND (acl.expires_at IS NULL OR acl.expires_at > now()) "
    "AND (acl.permissions ? 'read' OR acl.permissions ? 'admin')))"
)


def _encode_cursor(created_at: datetime, artifact_id: str) -> str:
    raw = json.dumps([created_at.isoformat(), artifact_id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created, artifact_id = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if not isinstance(artifact_id, str):
            raise ValueError
        return datetime.fromisoformat(created), artifact_id
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ProvenanceError("invalid cursor") from exc


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class ArtifactContextMixin:
    _conn: Any

    async def link_context(self, artifact_id: str, *, created_by: str | None = None,
                           task_id: str | None = None, project_id: str | None = None,
                           issue_ref: str | None = None, room_id: str | None = None,
                           link_kind: str = "reference", version_id: str | None = None,
                           source_event_id: str | None = None) -> dict:
        """Idempotently bind an artifact to work objects. Duplicate events and repeats are no-ops."""
        if not any((task_id, project_id, issue_ref, room_id)):
            raise ProvenanceError("a link needs a task, project, issue or room")
        for kind, value in (("task", task_id), ("project", project_id), ("issue", issue_ref), ("room", room_id)):
            if value is not None:
                validate_context(kind, value)
        if link_kind not in {"origin", "reference", "derived"}:
            raise ProvenanceError("unknown link_kind")
        if source_event_id is not None and (not isinstance(source_event_id, str) or not 0 < len(source_event_id) <= 256):
            raise ProvenanceError("source_event_id must be a short string")
        if version_id is not None:
            if await self._conn.fetchrow(
                    "SELECT version_id FROM artifact_versions WHERE version_id=$1 AND artifact_id=$2",
                    version_id, artifact_id) is None:
                raise ProvenanceError("version does not belong to the artifact")
        return {"linked": await self._insert_link(
            artifact_id, created_by=created_by, task_id=task_id, project_id=project_id, issue_ref=issue_ref,
            room_id=room_id, link_kind=link_kind, version_id=version_id, source_event_id=source_event_id)}

    async def _insert_link(self, artifact_id, *, created_by, task_id, project_id, issue_ref, room_id,
                           link_kind, version_id, source_event_id) -> bool:
        result = await self._conn.execute(
            "INSERT INTO artifact_bindings (artifact_id, created_by, task_id, project_id, issue_ref, room_id, "
            "link_kind, version_id, source_event_id, ctx_key) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
            "ON CONFLICT DO NOTHING",
            artifact_id, created_by, task_id, project_id, issue_ref, room_id, link_kind, version_id,
            source_event_id,
            ctx_key(link_kind, task_id=task_id, project_id=project_id, issue_ref=issue_ref, room_id=room_id))
        return result == "INSERT 0 1" if isinstance(result, str) else True

    async def list_links(self, artifact_id: str) -> list[dict]:
        rows = await self._conn.fetch(
            "SELECT task_id, project_id, issue_ref, room_id, link_kind, version_id, created_at "
            "FROM artifact_bindings WHERE artifact_id=$1 AND ctx_key IS NOT NULL ORDER BY binding_id", artifact_id)
        return [self._jsonable(dict(row)) for row in rows]

    async def list_by_context(self, *, principal_id: str, kind: str, ref: str, query: str | None = None,
                              media_type: str | None = None, limit: int = 50,
                              cursor: str | None = None) -> dict:
        column, ref = validate_context(kind, ref)
        if not principal_id:
            raise ProvenanceError("principal is required")
        limit = max(1, min(MAX_PAGE, int(limit)))
        join = ("FROM artifacts a JOIN artifact_versions v ON v.version_id=a.current_version_id "
                f"WHERE {VISIBLE} AND a.lifecycle='active' AND a.status='available' "
                f"AND EXISTS (SELECT 1 FROM artifact_bindings b WHERE b.artifact_id=a.artifact_id AND b.{column}=$2)")
        args: list = [principal_id, ref]
        if query:
            args.append(_like(query))
            join += (f" AND (a.original_filename ILIKE ${len(args)} OR a.display_name ILIKE ${len(args)} "
                     f"OR a.artifact_id ILIKE ${len(args)})")
        if media_type:
            args.append(media_type)
            join += f" AND COALESCE(a.mime_type, v.output_manifest->0->>'media_type')=${len(args)}"
        total = int((await self._conn.fetchrow(f"SELECT count(*) AS n {join}", *args))["n"])
        page_args = list(args)
        page_where = join
        if cursor:
            created, last_id = _decode_cursor(cursor)
            page_args.extend([created, last_id])
            page_where += f" AND (a.created_at, a.artifact_id) < (${len(page_args) - 1}::timestamptz, ${len(page_args)})"
        page_args.append(limit + 1)
        rows = await self._conn.fetch(
            "SELECT a.artifact_id, a.original_filename, a.display_name, a.mime_type, a.artifact_type, a.created_at, "
            "a.created_by, a.current_version_id, v.version_number, v.committed_at, v.output_manifest, "
            "v.origin_room_id, v.origin_task_id, v.origin_issue_ref, v.origin_project_id, v.model_id, v.commit_sha, "
            "(SELECT count(*) FROM artifact_versions x WHERE x.artifact_id=a.artifact_id) AS version_count "
            f"{page_where} ORDER BY a.created_at DESC, a.artifact_id DESC LIMIT ${len(page_args)}", *page_args)
        records = [self._context_row(row) for row in rows[:limit]]
        has_more = len(rows) > limit
        next_cursor = None
        if has_more and rows:
            last = rows[limit - 1]
            next_cursor = _encode_cursor(last["created_at"], last["artifact_id"])
        return {"kind": kind, "ref": ref, "artifacts": records, "total": total, "has_more": has_more,
                "next_cursor": next_cursor}

    def _context_row(self, row: Any) -> dict:
        value = self._row(row)
        manifest = value.get("output_manifest") or []
        outputs = []
        for output in manifest[:20]:
            if not isinstance(output, dict):
                continue
            media = output.get("media_type") or output.get("mime_type")
            outputs.append({"name": output.get("name"), "media_type": media,
                            "size_bytes": output.get("size_bytes"),
                            "sha256": output.get("sha256") or output.get("blob_sha256"),
                            "preview_kind": preview_kind(media, output.get("name"))})
        primary = outputs[0] if outputs else {}
        return {
            "artifact_id": value["artifact_id"],
            "name": value.get("display_name") or value.get("original_filename"),
            "artifact_type": value.get("artifact_type"),
            "media_type": value.get("mime_type") or primary.get("media_type"),
            "preview_kind": primary.get("preview_kind") or preview_kind(value.get("mime_type"),
                                                                        value.get("original_filename")),
            "current_version_id": value.get("current_version_id"),
            "version_number": value.get("version_number"),
            "version_count": int(value.get("version_count") or 0),
            "created_at": value.get("created_at"),
            "committed_at": value.get("committed_at"),
            "origin": {"room_id": value.get("origin_room_id"), "task_id": value.get("origin_task_id"),
                       "issue_ref": value.get("origin_issue_ref"), "project_id": value.get("origin_project_id"),
                       "model_id": value.get("model_id"), "commit_sha": value.get("commit_sha")},
            "outputs": outputs,
        }


__all__ = ["ArtifactContextMixin", "CONTEXT_KINDS", "VISIBLE", "MAX_PAGE"]

"""Files for the Rakazo mobile bridge, stored in the versioned artifact registry the dashboard already uses.

A file the phone creates is an ordinary registry artifact owned by the account's own principal (``rakazo:<id>``), so it has the
registry's versions, hashes and ACLs and shows up for that principal only. Which bot, group or run a file belongs to, and which
space, is a small per-space record in prefs. "Remove" hides the file from the app; nothing in the registry is deleted.

The registry is reached through a narrow backend (``call``, ``put``, ``get``, ``grant``) so this module never touches a
database or object store directly, and tests can supply an in-memory one.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import secrets
from typing import Any

from . import rakazo_events as ev

META_KIND = "artifact_meta"
MAX_BYTES = 8 * 1024 * 1024           # what the phone opens or uploads in one call (base64 must fit the bridge body limit)
MAX_PAGE = 60
SCAN = 400
ARTIFACT_TYPE = "rakazo_file"
_ALL = {"read", "edit", "review", "share", "admin"}


class RegistryBackend:
    """The real registry: ``server.artifact_store`` (a sync adapter or callable), ``server.rustfs`` and ``server.artifact_acl``."""

    def __init__(self, server):
        self.store, self.rustfs, self.acl = server.artifact_store, server.rustfs, server.artifact_acl
        self.available = self.store is not None and self.rustfs is not None

    def call(self, method: str, *args, **kwargs):
        return (getattr(self.store, "sync_call", None) or self.store)(method, *args, **kwargs)

    def put(self, data: bytes) -> tuple[str, str]:
        return self.rustfs.put(data)

    def get(self, key: str, sha256: str) -> bytes:
        return self.rustfs.get(key, sha256)

    def grant(self, artifact_id: str, principal: str) -> None:
        if self.acl is not None:
            self.acl.grant(artifact_id, principal, _ALL)


class Artifacts:
    def __init__(self, bridge, error_type, backend_fn):
        self.b = bridge
        self.Error = error_type
        self.prefs = bridge.prefs
        self._backend_fn = backend_fn

    def procs(self) -> dict[str, Any]:
        return {"artifacts/list": self.list, "artifacts/listSpace": self.list_space, "artifacts/listVersions": self.versions,
                "artifacts/create": self.create, "artifacts/get": self.get, "artifacts/getById": self.get,
                "artifacts/remove": self.remove}

    def backend(self, *, required: bool = True):
        backend = self._backend_fn() if self._backend_fn else None
        if backend is None or not getattr(backend, "available", True):
            if required:
                raise self.Error(400, "BAD_REQUEST", "File storage is not enabled on this server.")
            return None
        return backend

    # helpers ------------------------------------------------------------------------------------------------
    def _meta(self, ctx) -> dict[str, Any]:
        return self.prefs.items(ctx.account.account_id, ctx.space, META_KIND)

    def _version_info(self, backend, artifact_id: str) -> list[dict[str, Any]]:
        return sorted(backend.call("list_versions", artifact_id), key=lambda v: v["version_number"])

    def _view(self, record, versions, meta, content: bytes | None = None) -> dict[str, Any]:
        latest = versions[-1] if versions else None
        manifest = (latest or {}).get("output_manifest") or [{}]
        view = {"id": record["artifact_id"], "botId": meta.get("botId"), "groupId": meta.get("groupId"),
                "runId": meta.get("runId"), "name": record.get("original_filename") or record["artifact_id"],
                "description": meta.get("description") or "", "mimeType": record.get("mime_type") or "application/octet-stream",
                "size": int(manifest[0].get("size_bytes") or 0), "version": int((latest or {}).get("version_number") or 1),
                "createdAt": str(record.get("created_at") or "")}
        if content is not None:
            view["contentBase64"] = base64.b64encode(content).decode()
        return view

    def _mine(self, ctx, backend, artifact_id):
        meta = self._meta(ctx).get(artifact_id)
        if not isinstance(artifact_id, str) or meta is None or meta.get("hidden"):
            raise self.Error(404, "NOT_FOUND", "Not found.")
        record = backend.call("get_record", artifact_id, principal_id=ctx.account.principal_id)
        if record is None:
            raise self.Error(404, "NOT_FOUND", "Not found.")
        return record, meta

    # procedures ---------------------------------------------------------------------------------------------
    def _visible(self, ctx, backend, bot_id=None, offset=0, limit=MAX_PAGE):
        meta = self._meta(ctx)
        records = backend.call("list_records", principal_id=ctx.account.principal_id, limit=SCAN, offset=0)
        out = []
        for record in records:
            m = meta.get(record["artifact_id"])
            if m is None or m.get("hidden") or (bot_id is not None and m.get("botId") != bot_id):
                continue
            out.append((record, m))
        return out[offset:offset + limit + 1]

    def list(self, ctx, inp):
        bot_id = inp.get("botId")
        self.b._assistant(ctx, bot_id)
        backend = self.backend(required=False)
        if backend is None:
            return []
        return [self._view(r, self._version_info(backend, r["artifact_id"]), m)
                for r, m in self._visible(ctx, backend, bot_id, 0, SCAN)]

    def list_space(self, ctx, inp):
        limit = inp.get("limit", MAX_PAGE)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
            raise self.Error(400, "BAD_REQUEST", f"limit must be 1 to {MAX_PAGE}.")
        cursor = inp.get("cursor") or "0"
        if not cursor.isdigit():
            raise self.Error(400, "BAD_REQUEST", "That cursor is not valid.")
        bot_id = inp.get("botId")
        if bot_id:
            self.b._assistant(ctx, bot_id)
        backend = self.backend(required=False)
        if backend is None:
            return {"items": [], "nextCursor": None}
        offset = int(cursor)
        rows = self._visible(ctx, backend, bot_id, offset, limit)
        page = rows[:limit]
        items = []
        for record, m in page:
            versions = self._version_info(backend, record["artifact_id"])
            items.append({**self._view(record, versions, m), "versionCount": len(versions)})
        return {"items": items, "nextCursor": str(offset + limit) if len(rows) > limit else None}

    def versions(self, ctx, inp):
        backend = self.backend()
        record, _ = self._mine(ctx, backend, inp.get("familyId"))
        return [{"id": f'{record["artifact_id"]}:{v["version_id"]}', "version": int(v["version_number"]),
                 "name": record.get("original_filename") or record["artifact_id"], "createdAt": str(v.get("created_at") or "")}
                for v in reversed(self._version_info(backend, record["artifact_id"]))]

    def get(self, ctx, inp):
        backend = self.backend()
        ident = inp.get("artifactId")
        artifact_id, _, version_id = (ident if isinstance(ident, str) else "").partition(":")
        record, meta = self._mine(ctx, backend, artifact_id)
        versions = self._version_info(backend, artifact_id)
        if version_id:
            versions = [v for v in versions if v["version_id"] == version_id]
            if not versions:
                raise self.Error(404, "NOT_FOUND", "Not found.")
        entry = ((versions[-1].get("output_manifest") or [None])[0]) if versions else None
        if entry is None:
            raise self.Error(404, "NOT_FOUND", "Not found.")
        if int(entry.get("size_bytes") or 0) > MAX_BYTES:
            raise self.Error(400, "BAD_REQUEST", "That file is too large to open in the app.")
        data = backend.get(entry["storage_key"], entry["sha256"])
        return self._view(record, versions, meta, data)

    def create(self, ctx, inp):
        backend = self.backend()
        name, mime, body = inp.get("name"), inp.get("mimeType"), inp.get("contentBase64")
        if not isinstance(name, str) or not name.strip() or len(name) > 255 or "/" in name or "\\" in name:
            raise self.Error(400, "BAD_REQUEST", "A file needs a name of up to 255 characters, without slashes.")
        if not isinstance(mime, str) or not mime or len(mime) > 200:
            raise self.Error(400, "BAD_REQUEST", "mimeType is required.")
        description = inp.get("description") or ""
        if not isinstance(description, str) or len(description) > 280:
            raise self.Error(400, "BAD_REQUEST", "The description is too long.")
        if not isinstance(body, str) or not body or len(body) > MAX_BYTES * 4 // 3 + 8:
            raise self.Error(400, "BAD_REQUEST", "contentBase64 is required and must be at most 8 MB.")
        try:
            data = base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError):
            raise self.Error(400, "BAD_REQUEST", "contentBase64 is not valid base64.") from None
        if not data or len(data) > MAX_BYTES:
            raise self.Error(400, "BAD_REQUEST", "The file must be between 1 byte and 8 MB.")
        meta: dict[str, Any] = {"description": description}
        if inp.get("botId"):
            self.b._assistant(ctx, inp["botId"])
            meta["botId"] = inp["botId"]
        if inp.get("groupId"):
            if not isinstance(inp["groupId"], str):
                raise self.Error(400, "BAD_REQUEST", "groupId is not valid.")
            meta["groupId"] = inp["groupId"]
        owner = ctx.account.principal_id
        nonce = inp.get("clientNonce")
        idem = hashlib.sha256(f"rakazo:{owner}:{ctx.space}:{nonce or secrets.token_hex(16)}".encode()).hexdigest()[:64]
        prior = self.prefs.get(ctx.account.account_id, ctx.space, META_KIND + "_nonce", idem)
        if prior is not None:
            return self._summary(ctx, backend, prior)
        storage_key, sha256 = backend.put(data)
        record = backend.call("register_logical", original_filename=name.strip(), artifact_type=ARTIFACT_TYPE,
                              created_by=owner, mime_type=mime)
        artifact_id = record["artifact_id"]
        backend.grant(artifact_id, owner)
        backend.call("commit_version", artifact_id, base_version_id=None, source_manifest_blob=None,
                     output_manifest=[{"storage_key": storage_key, "sha256": sha256, "size_bytes": len(data), "name": name.strip()}],
                     provenance={"principal_id": owner, "origin": "rakazo-app"}, created_by=owner, idempotency_key=idem)
        self.b.prefs_put(ctx.account.account_id, ctx.space, META_KIND, artifact_id, meta)
        if nonce:
            self.b.prefs_put(ctx.account.account_id, ctx.space, META_KIND + "_nonce", idem, artifact_id)
        return self._summary(ctx, backend, artifact_id)

    def _summary(self, ctx, backend, artifact_id):
        record, meta = self._mine(ctx, backend, artifact_id)
        return self._view(record, self._version_info(backend, artifact_id), meta)

    def remove(self, ctx, inp):
        """Hide the file from the app (the registry keeps every version)."""
        backend = self.backend()
        _, meta = self._mine(ctx, backend, inp.get("artifactId"))
        self.b.prefs_put(ctx.account.account_id, ctx.space, META_KIND, inp["artifactId"],
                         {**meta, "hidden": ev.iso(self.b.svc.now())})
        return {"ok": True}

"""Member service layer (plan 5, Task 6): what a group member may do with the artifacts they own, independent of the
channel.  The MCP tools (``collab_member_tools``) and, later, the web API call exactly these methods, so an agent and the
signed-in web session of the same person behave identically.

Transport-neutral: every method takes an ``actor`` first (see ``member_actor``) and raises ``MemberServiceError`` (a
``CollabError`` carrying a stable ``code``).  Nothing here touches a database: ``collab-public`` has none.  The service
reads the caller's OWN member view (``member-views/<member_owner_key>.json`` of the mirror, written by the control-plane
worker; the path is derived from the caller's principal, never from the request), stages file chunks under the member's
owner key (plan 3's chunk protocol, ``stage_chunk_for``) and appends intents (``member.*`` kinds) that the worker
re-checks and applies.

Fail closed, no existence oracle: an artifact that is not the caller's own (or, for reads, shared with them) answers
exactly like one that does not exist, and the refusal is audited through ``CollabService._audit_denied``.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from .artifact_member_intents import KIND_CREATE, KIND_DELETE, KIND_PUBLISH, MEMBER_VIEWS_DIR
from .artifact_members import member_owner_key
from .collab import CollabError, CollabService, Principal
from .collab_artifacts import CollabArtifactBridge
from .collab_bridge_files import (
    CHUNK_BYTES,
    SHA_RE,
    UPLOAD_RE,
    VERIFIED_MARKER,
    Actor,
    BridgeError,
    BridgeLimits,
    file_exists_beneath,
    list_parts,
    read_json_beneath,
    read_staged_meta,
    remove_tree_beneath,
    staging_parts,
    tree_entry_cap,
    upload_complete,
    upload_id_for,
    validate_chunk,
)

NOT_A_MEMBER: Final = "artifact tools are for group members"
NOT_FOUND: Final = "not found"
VIEW_MAX_BYTES: Final = 16 * 1024 * 1024
MAX_RANGES: Final = 1000          # missing ranges reported by upload_status
_SHA = re.compile(SHA_RE)
_IDEMPOTENCY = re.compile(r"[\x21-\x7e]{8,128}\Z")
_UPLOAD = re.compile(UPLOAD_RE)
_TIB: Final = 1 << 40
_CODES: Final = {400: "bad_request", 403: "forbidden", 404: "not_found", 409: "conflict", 413: "too_large",
                 429: "rate_limited", 502: "integrity", 503: "unavailable"}


class MemberServiceError(CollabError):
    """``status`` (an HTTP-style number), a stable ``code`` and a ``message``; also a ``CollabError`` so the MCP and HTTP
    layers that already report those keep working."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(status, message, code=code)


def _positive(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class MembersConfig:
    """The sanity, rate and staging limits of the member channel (plan 5, Global Constraints).  ``max_upload_bytes`` is the
    per-file limit handed to ``validate_chunk``; ``staging_bytes_per_member`` is the member's staging cap (reserved by
    declared size), on top of the whole-volume ``staging_bytes_total`` of the bridge limits."""
    max_upload_bytes: int = _TIB
    http_chunks_per_request: int = 32
    max_publish_files: int = 200
    max_message_chars: int = 500
    chunks_per_minute: int = 1200
    writes_per_minute: int = 30
    staging_bytes_per_member: int = _TIB
    result_wait_seconds: float = 5.0

    def __post_init__(self) -> None:
        for f in dataclasses.fields(self):
            if f.name == "result_wait_seconds":
                v = self.result_wait_seconds
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 20:
                    raise ValueError("result_wait_seconds must be 0 to 20")
            else:
                _positive(f.name, getattr(self, f.name))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> MembersConfig:
        raw = dict(raw or {})
        known = {f.name for f in dataclasses.fields(cls)}
        if unknown := sorted(set(raw) - known):
            raise ValueError("unknown member limit(s): " + ", ".join(unknown))
        return cls(**raw)


def member_actor(pr: Principal) -> dict[str, Any]:
    """The service's actor for an authenticated principal (an agent credential or a signed-in person)."""
    if not isinstance(pr, Principal) or pr.kind not in ("agent", "user"):
        raise MemberServiceError(403, "forbidden", NOT_A_MEMBER)
    return {"kind": pr.kind, "id": pr.id, "pr": pr}


@dataclass(frozen=True)
class _Caller:
    pr: Principal
    actor: Actor
    principal: str
    owner_key: str


def _present(ticket: str, doc: Mapping[str, Any]) -> dict[str, Any]:
    if doc.get("status") == "pending" or "result" not in doc:
        return {"ticket": ticket, "status": "pending"}
    keep = ("result", "artifact_id", "version_id", "version_number", "head_version_id", "reason")
    return {"ticket": ticket, "status": "done", **{k: doc[k] for k in keep if doc.get(k) is not None}}


class MemberService:
    def __init__(self, service: CollabService, bridge: CollabArtifactBridge, config: MembersConfig | None = None):
        self.service, self.bridge = service, bridge
        self.config = config or MembersConfig()

    # ------------------------------------------------------------------------------------------ identity
    def _fail(self, caller: Principal | None, tool: str, reason: str, status: int, code: str, message: str,
              artifact_id: Any = None) -> MemberServiceError:
        if isinstance(caller, Principal):
            resource = artifact_id[:128] if isinstance(artifact_id, str) else None
            self.service._audit_denied(caller, f"artifact.{tool}", reason, resource_id=resource,
                                       agent_id=caller.id if caller.is_agent else None)
        self.service.stats["policy_denied"] += 1
        return MemberServiceError(status, code, message)

    def _caller(self, actor: Mapping[str, Any], tool: str) -> _Caller:
        pr = actor.get("pr") if isinstance(actor, Mapping) else None
        if not isinstance(pr, Principal) or (pr.kind, pr.id) != (actor.get("kind"), actor.get("id")):
            raise MemberServiceError(403, "forbidden", NOT_A_MEMBER)
        check = self.service.member_check(pr)
        if not check.ok or check.principal is None:
            raise self._fail(pr, tool, check.reason or "not_member", 403, "forbidden", NOT_A_MEMBER)
        return _Caller(pr, Actor(pr.kind, pr.id), check.principal, member_owner_key(check.principal))

    def _limits(self) -> BridgeLimits:
        try:
            base = self.bridge.limits()
        except CollabError as exc:
            raise MemberServiceError(exc.status, _CODES.get(exc.status, "error"), exc.message) from None
        # the member default may only tighten what the operator configured, never raise it
        return dataclasses.replace(base, max_file_bytes=min(base.max_file_bytes, self.config.max_upload_bytes))

    def _chunk_rate(self, limits: BridgeLimits) -> int:
        return min(self.config.chunks_per_minute, limits.chunks_per_minute)

    def _staging_cap(self, limits: BridgeLimits) -> int:
        return min(self.config.staging_bytes_per_member, limits.staging_bytes_per_agent)

    # ------------------------------------------------------------------------------------------ the member view
    def _view(self, who: _Caller) -> dict[str, Any]:
        """The caller's own view file, or an empty view.  Never a caller-supplied path; a file that names another
        principal is treated as absent."""
        doc = read_json_beneath(self.bridge.mirror, [MEMBER_VIEWS_DIR, who.owner_key + ".json"],
                                max_bytes=VIEW_MAX_BYTES)
        if doc is None or doc.get("principal") != who.principal:
            return {"principal": who.principal, "artifacts": [], "shared_with_me": [], "links": [], "usage": None,
                    "available": False}
        return {**doc, "available": True}

    @staticmethod
    def _entry(rows: Any, artifact_id: Any) -> Mapping[str, Any] | None:
        if not isinstance(rows, list) or not isinstance(artifact_id, str):
            return None
        for row in rows:
            if isinstance(row, dict) and row.get("artifact_id") == artifact_id:
                return row
        return None

    def _own(self, who: _Caller, tool: str, artifact_id: Any, *, active: bool = True) -> Mapping[str, Any]:
        row = self._entry(self._view(who).get("artifacts"), artifact_id)
        if row is None or (active and row.get("lifecycle") != "active"):
            raise self._fail(who.pr, tool, "not_owner", 404, "not_found", NOT_FOUND, artifact_id)
        return row

    def can_read(self, actor: Mapping[str, Any], artifact_id: Any) -> bool:
        """Whether ``artifact_id`` is in the caller's own view (owned or shared with them); never audits."""
        pr = actor.get("pr")
        if not isinstance(pr, Principal):
            return False
        check = self.service.member_check(pr)
        if not check.ok or check.principal is None:
            return False
        who = _Caller(pr, Actor(pr.kind, pr.id), check.principal, member_owner_key(check.principal))
        view = self._view(who)
        return self._entry(view.get("artifacts"), artifact_id) is not None \
            or self._entry(view.get("shared_with_me"), artifact_id) is not None

    # ------------------------------------------------------------------------------------------ throttles
    def _throttle(self, who: _Caller, kind: str, limit: int, n: int = 1) -> None:
        """``n`` hits against a per-member sliding minute, shared by every channel of the member."""
        svc = self.service
        with svc._lock:
            now = svc._now()
            key = f"member{kind}:{who.principal}"
            hits = [t for t in svc._hits[key] if now - t < 60.0]
            if len(hits) + n > limit:
                svc._hits[key] = hits
                svc.stats["rate_limited"] += 1
                raise MemberServiceError(429, "rate_limited", f"{kind} rate limit exceeded")
            hits.extend([now] * n)
            svc._hits[key] = hits

    # ------------------------------------------------------------------------------------------ intents
    def _submit(self, who: _Caller, kind: str, payload: dict, artifact_id: str | None, tool: str,
                idempotency_key: Any = None) -> dict[str, Any]:
        if idempotency_key is not None and (not isinstance(idempotency_key, str)
                                            or not _IDEMPOTENCY.match(idempotency_key)):
            raise MemberServiceError(400, "bad_request", "idempotency_key must be 8 to 128 printable characters")
        limits = self._limits()
        self._throttle(who, "write", min(self.config.writes_per_minute, limits.commits_per_minute))
        try:
            ticket = self.bridge.append_intent(who.actor, kind, payload, artifact_id=artifact_id,
                                               idempotency_key=idempotency_key)
        except CollabError as exc:
            raise MemberServiceError(exc.status, _CODES.get(exc.status, "error"), exc.message) from None
        try:
            return self._wait(who, ticket, self.config.result_wait_seconds)
        except CollabError:
            # the intent is written: whatever stops the wait (no free waiter slot, a bad feed), the caller must get
            # its ticket, or it retries and duplicates the write
            return {"ticket": ticket, "status": "pending"}

    def _wait(self, who: _Caller, ticket: str, seconds: float) -> dict[str, Any]:
        try:
            doc = self.bridge.read_result(who.actor, ticket, wait=seconds,
                                          still_valid=lambda: self.service.still_valid(who.pr))
        except CollabError as exc:
            raise MemberServiceError(exc.status, _CODES.get(exc.status, "error"), exc.message) from None
        return _present(ticket, doc)

    def create(self, actor: Mapping[str, Any], title: Any, filename: Any, profile: Any,
               idempotency_key: Any = None) -> dict[str, Any]:
        who = self._caller(actor, "create")
        return self._submit(who, KIND_CREATE, {"title": title, "filename": filename, "profile": profile}, None,
                            "create", idempotency_key)

    def publish(self, actor: Mapping[str, Any], artifact_id: Any, *, base_version_id: Any = None, files: Any,
                message: Any, entry: Any = None, idempotency_key: Any = None) -> dict[str, Any]:
        who = self._caller(actor, "publish")
        self._own(who, "publish", artifact_id)
        cap = min(self.config.max_publish_files, self._limits().max_changes_per_commit)
        if not isinstance(files, list) or not 1 <= len(files) <= cap:
            raise MemberServiceError(400, "bad_request", f"files must list 1 to {cap} files")
        if not isinstance(message, str) or len(message) > self.config.max_message_chars:
            raise MemberServiceError(400, "bad_request", "message is invalid")
        payload: dict[str, Any] = {"base_version_id": base_version_id, "files": files, "message": message}
        if entry is not None:
            payload["entry"] = entry
        return self._submit(who, KIND_PUBLISH, payload, artifact_id, "publish", idempotency_key)

    def delete(self, actor: Mapping[str, Any], artifact_id: Any, confirm: Any) -> dict[str, Any]:
        who = self._caller(actor, "delete")
        if confirm is not True:
            raise MemberServiceError(400, "bad_request", "confirm must be true to delete an artifact")
        self._own(who, "delete", artifact_id, active=False)
        return self._submit(who, KIND_DELETE, {"confirm": True}, artifact_id, "delete")

    def result(self, actor: Mapping[str, Any], ticket: Any, seconds: Any = 20) -> dict[str, Any]:
        who = self._caller(actor, "result")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 0 <= seconds <= 20:
            raise MemberServiceError(400, "bad_request", "seconds must be an integer from 0 to 20")
        if not isinstance(ticket, str):
            raise MemberServiceError(404, "not_found", NOT_FOUND)
        return self._wait(who, ticket, float(seconds))

    # ------------------------------------------------------------------------------------------ reads
    def mine(self, actor: Mapping[str, Any]) -> dict[str, Any]:
        who = self._caller(actor, "mine")
        view = self._view(who)
        return {k: view.get(k) for k in ("available", "updated", "usage", "artifacts", "shared_with_me", "links")}

    def _readable(self, who: _Caller, tool: str, artifact_id: Any) -> tuple[Mapping[str, Any], bool]:
        view = self._view(who)
        own = self._entry(view.get("artifacts"), artifact_id)
        if own is not None:
            return own, True
        shared = self._entry(view.get("shared_with_me"), artifact_id)
        if shared is None:
            raise self._fail(who.pr, tool, "not_visible", 404, "not_found", NOT_FOUND, artifact_id)
        return shared, False

    def head(self, actor: Mapping[str, Any], artifact_id: Any) -> dict[str, Any]:
        who = self._caller(actor, "head")
        row, own = self._readable(who, "head", artifact_id)
        out = {"artifact_id": row.get("artifact_id"), "title": row.get("title"),
               "head_version_id": row.get("head_version_id"), "head_version_number": row.get("head_version_number"),
               "permission": "owner" if own else row.get("permissions")}
        if own:
            out.update(lifecycle=row.get("lifecycle"), bytes=row.get("bytes"))
        return out

    def history(self, actor: Mapping[str, Any], artifact_id: Any, limit: int = 50) -> dict[str, Any]:
        who = self._caller(actor, "history")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise MemberServiceError(400, "bad_request", "limit must be an integer from 1 to 200")
        row, own = self._readable(who, "history", artifact_id)
        if own:
            versions = [v for v in (row.get("versions") or []) if isinstance(v, dict)]
            return {"artifact_id": artifact_id, "head_version_id": row.get("head_version_id"),
                    "versions": versions[:limit], "truncated": len(versions) > limit}
        head = {"version_id": row.get("head_version_id"), "version_number": row.get("head_version_number")}
        return {"artifact_id": artifact_id, "head_version_id": row.get("head_version_id"), "versions": [head],
                "truncated": True}       # the view carries only the head of an artifact shared with the caller

    # ------------------------------------------------------------------------------------------ uploads
    def _stage(self, who: _Caller, args: dict, limits: BridgeLimits) -> dict[str, Any]:
        try:
            chunk = validate_chunk(args, limits)
        except BridgeError as exc:
            raise MemberServiceError(400, "bad_request", exc.reason if hasattr(exc, "reason") else str(exc)) from None
        return self._stage_validated(who, chunk, limits)

    def _stage_validated(self, who: _Caller, chunk: Any, limits: BridgeLimits) -> dict[str, Any]:
        try:
            got = self.bridge.stage_chunk_for(who.owner_key, chunk, limits=limits,
                                              staging_cap=self._staging_cap(limits))
        except CollabError as exc:
            raise MemberServiceError(exc.status, _CODES.get(exc.status, "error"), exc.message) from None
        out = {"upload_id": got["upload_id"], "received": got.get("received"), "total": chunk.total,
               "replayed": got.get("stored") is False}
        if "complete" in got:
            out["complete"] = got["complete"]
        return out

    def upload_chunk(self, actor: Mapping[str, Any], args: Any) -> dict[str, Any]:
        who = self._caller(actor, "upload_chunk")
        if not isinstance(args, dict):
            raise MemberServiceError(400, "bad_request", "chunk arguments are missing or unknown")
        self._own(who, "upload_chunk", args.get("artifact_id"))
        limits = self._limits()
        self._throttle(who, "chunk", self._chunk_rate(limits))
        return self._stage(who, args, limits)

    def upload_run(self, actor: Mapping[str, Any], artifact_id: Any, name: Any, size_bytes: Any, file_sha256: Any,
                   first_index: Any, data: Any, run_sha256: Any) -> dict[str, Any]:
        """A run of consecutive chunks in one request (web).  Everything is verified before the first chunk is
        written: the run hash, the structure and every chunk; a failure stages nothing."""
        who = self._caller(actor, "upload_run")
        self._own(who, "upload_run", artifact_id)
        if not isinstance(data, (bytes, bytearray)) or not data or isinstance(first_index, bool) \
                or not isinstance(first_index, int) or first_index < 0:
            raise MemberServiceError(400, "bad_request", "run is invalid")
        pieces = [bytes(data[i:i + CHUNK_BYTES]) for i in range(0, len(data), CHUNK_BYTES)]
        if len(pieces) > self.config.http_chunks_per_request:
            raise MemberServiceError(400, "bad_request", "too many chunks in one request")
        limits = self._limits()
        self._throttle(who, "chunk", self._chunk_rate(limits), len(pieces))
        if not isinstance(run_sha256, str) or not _SHA.match(run_sha256) \
                or hashlib.sha256(data).hexdigest() != run_sha256:
            raise MemberServiceError(400, "bad_request", "run sha256 does not match its data")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 1:
            raise MemberServiceError(400, "bad_request", "size_bytes is out of range")
        total = -(-size_bytes // CHUNK_BYTES)
        chunks = []
        for offset, piece in enumerate(pieces):
            args = {"artifact_id": artifact_id, "name": name, "size_bytes": size_bytes, "file_sha256": file_sha256,
                    "total": total, "index": first_index + offset, "data": base64.b64encode(piece).decode("ascii"),
                    "sha256": hashlib.sha256(piece).hexdigest()}
            try:
                chunks.append(validate_chunk(args, limits))
            except BridgeError as exc:
                raise MemberServiceError(400, "bad_request", str(exc)) from None
        last: dict[str, Any] = {}
        replayed = True
        for chunk in chunks:
            last = self._stage_validated(who, chunk, limits)
            replayed = replayed and last["replayed"]
        return {**last, "replayed": replayed, "chunks": len(chunks)}

    def _upload_key(self, artifact_id: Any, name: Any, size_bytes: Any, file_sha256: Any, who: _Caller,
                    limits: BridgeLimits) -> str:
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) \
                or not 1 <= size_bytes <= limits.max_file_bytes or not isinstance(file_sha256, str) \
                or not _SHA.match(file_sha256):
            raise MemberServiceError(400, "bad_request", "size_bytes or file_sha256 is invalid")
        try:
            return upload_id_for(who.owner_key, artifact_id, name, file_sha256, size_bytes)
        except (BridgeError, TypeError):
            raise MemberServiceError(400, "bad_request", "upload arguments are invalid") from None

    def upload_status(self, actor: Mapping[str, Any], artifact_id: Any, name: Any, size_bytes: Any,
                      file_sha256: Any) -> dict[str, Any]:
        who = self._caller(actor, "upload_status")
        self._own(who, "upload_status", artifact_id)
        limits = self._limits()
        upload_id = self._upload_key(artifact_id, name, size_bytes, file_sha256, who, limits)
        total = -(-size_bytes // CHUNK_BYTES)
        outbox = self.bridge.outbox
        meta = read_staged_meta(outbox, who.owner_key, upload_id, limits)
        parts = staging_parts(who.owner_key, upload_id)
        verified = file_exists_beneath(outbox, parts, VERIFIED_MARKER)
        if meta is None and not verified:
            return {"upload_id": upload_id, "total": total, "missing_ranges": [[0, total - 1]], "complete": False,
                    "verified": False, "truncated": False}
        try:
            present = [] if verified else list_parts(outbox, who.owner_key, upload_id, max_parts=total)
        except BridgeError:
            raise MemberServiceError(409, "conflict", "staged upload directory is invalid") from None
        ranges: list[list[int]] = []
        have = set(present)
        start: int | None = None
        for i in range(total + 1):
            absent = i < total and i not in have and not verified
            if absent and start is None:
                start = i
            elif not absent and start is not None:
                ranges.append([start, i - 1])
                start = None
            if len(ranges) >= MAX_RANGES:
                break
        return {"upload_id": upload_id, "total": total, "missing_ranges": ranges,
                "complete": bool(verified or upload_complete(outbox, who.owner_key, upload_id, limits)),
                "verified": verified, "truncated": len(ranges) >= MAX_RANGES}

    def upload_abort(self, actor: Mapping[str, Any], artifact_id: Any, upload_id: Any) -> dict[str, Any]:
        who = self._caller(actor, "upload_abort")
        self._own(who, "upload_abort", artifact_id)
        if not isinstance(upload_id, str) or not _UPLOAD.match(upload_id):
            raise MemberServiceError(400, "bad_request", "upload_id is invalid")
        limits = self._limits()
        meta = read_staged_meta(self.bridge.outbox, who.owner_key, upload_id, limits)
        if meta is not None and meta.artifact_id != artifact_id:
            raise self._fail(who.pr, "upload_abort", "not_owner", 404, "not_found", NOT_FOUND, artifact_id)
        removed = remove_tree_beneath(self.bridge.outbox, staging_parts(who.owner_key, upload_id),
                                      max_entries=tree_entry_cap(limits))
        self.bridge.release_owner_usage(who.owner_key)             # the freed reservation counts at once
        return {"upload_id": upload_id, "aborted": bool(removed)}

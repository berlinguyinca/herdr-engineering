"""The ``collab-public`` side of the artifact collaboration bridge (plan 3, Tasks 7a-7c).

``collab-public`` has no database: it reads the worker's read-only mirror (``grants.json``, ``artifacts/<id>/head.json``
and content-addressed ``blobs/<sha256>``, see ``collab_mirror``) and writes the outbox (``agents.json`` here; intents and
staging in Tasks 7b/7c).

Isolation is enforced HERE. The mirror is one shared tree, so every method re-reads ``grants.json`` and checks the grant
for exactly that agent and artifact, failing closed. A missing grant, an ungranted artifact and an unknown artifact all
give the identical 404 "not found" (audited ``audit_denied`` with one reason, ``no_grant``), so existence never leaks.
A stale (older than ``FEED_MAX_AGE``), missing or invalid ``grants.json`` makes every tool method 503
"collaboration bridge unavailable".

Every mirror access goes through the no-follow helpers of ``collab_bridge_files`` with the trusted mount point as root.
Blob reads verify the sha256 against the manifest; a successful verification is cached per
``(path, st_ino, st_size, st_mtime_ns)`` so repeated slices of one large blob are hashed once.

Allowed calls are audited ``artifact_tool_used`` at most once per ``AUDIT_EVERY`` s per (agent, artifact, tool); the
event carries ``calls`` = the calls since the previous event, this one included. If that event cannot be written the
call is refused (503), never served unaudited. No artifact text, credential or name of a file is ever logged.

The methods are synchronous and do blocking file I/O and hashing: an async caller runs them on an executor
(``asyncio.to_thread``), never on the event loop.
"""
from __future__ import annotations

import collections
import hashlib
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .collab import CollabError, Principal
from .collab_bridge_files import (
    FEED_MAX_AGE,
    SHA_RE,
    BridgeError,
    BridgeLimits,
    atomic_write_json,
    open_beneath,
    read_json_beneath,
)
from .collab_mirror import ARTIFACTS_DIR, BLOBS_DIR, GRANTS_NAME, HEAD_NAME

log = logging.getLogger(__name__)

AGENTS_FEED = "agents.json"                 # the worker reads it as collab_intents.AGENTS_FEED
UNAVAILABLE = "collaboration bridge unavailable"
NOT_FOUND = "not found"
NOT_MIRRORED = "version not mirrored"
BINARY = "binary files are not readable through collaboration"
INTEGRITY = "mirror integrity"
AUDIT_EVERY = 600.0
MAX_READ_BYTES = 1024 * 1024
DEFAULT_READ_BYTES = 262_144
MAX_HISTORY = 200
FEED_INTERVAL = 15.0
GRANTS_MAX_BYTES = 64 * 1024 * 1024
HEAD_MAX_BYTES = 256 * 1024 * 1024
MAX_NAME = 1024
_HASH_BLOCK = 1 << 20
_ARTIFACT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\Z")   # collab_mirror's artifact id rule
_VERSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\Z")
_SHA = re.compile(SHA_RE)
_GRANT_PERMISSIONS = frozenset({"read", "write"})
_TOOL_ACTIONS = {"artifacts_list": "artifact.list", "artifact_head": "artifact.head",
                 "artifact_read": "artifact.read", "artifact_history": "artifact.history"}


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _utf8_slice(buf: bytes, max_bytes: int) -> int:
    """How many leading bytes of ``buf`` to return: at most ``max_bytes``, ending on a UTF-8 boundary. When
    ``max_bytes`` is smaller than the first character, that whole character (so a slice is never empty)."""
    if len(buf) <= max_bytes:
        return len(buf)
    end = max_bytes
    while end > 0 and (buf[end] & 0xC0) == 0x80:
        end -= 1
    if end == 0:
        end = 1
        while end < len(buf) and (buf[end] & 0xC0) == 0x80:
            end += 1
    return end


class CollabArtifactBridge:
    VERIFY_CACHE_MAX = 4096
    USE_COUNTER_MAX = 50_000

    def __init__(self, service, mirror: Path, outbox: Path, *, clock: Callable[[], float] = time.time,
                 feed_interval: float = FEED_INTERVAL):
        self.service = service
        self.mirror = Path(mirror)
        self.outbox = Path(outbox)
        self.clock = clock
        self.feed_interval = float(feed_interval)
        self.stats: collections.Counter = collections.Counter()
        self._verified: collections.OrderedDict = collections.OrderedDict()
        self._verified_lock = threading.Lock()
        # (agent id, artifact id, tool) -> [last event time, calls since]; guarded by the service lock
        self._uses: collections.OrderedDict = collections.OrderedDict()
        self._feed_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # -- grants.json ------------------------------------------------------------------------------------------
    def _grants(self) -> tuple[dict, BridgeLimits] | None:
        """The fresh ``grants.json`` agents map and its limits, or None (missing, stale, invalid)."""
        doc = read_json_beneath(self.mirror, [GRANTS_NAME], max_bytes=GRANTS_MAX_BYTES)
        if doc is None:
            return None
        generated, agents = doc.get("generated_at"), doc.get("agents")
        if not _finite(generated) or self.clock() - generated > FEED_MAX_AGE or not isinstance(agents, dict):
            return None
        try:
            limits = BridgeLimits.from_mapping(doc.get("limits"))
        except (BridgeError, TypeError):
            return None
        return agents, limits

    def _require(self) -> tuple[dict, BridgeLimits]:
        got = self._grants()
        if got is None:
            raise CollabError(503, UNAVAILABLE)
        return got

    def _live_grants(self, agents: dict, pr: Principal) -> dict[str, str]:
        """artifact id -> permission for every unexpired, well-formed grant of exactly this agent."""
        if not isinstance(pr, Principal) or not pr.is_agent:
            return {}
        mine = agents.get(pr.id)
        if not isinstance(mine, dict):
            return {}
        now, out = self.clock(), {}
        for artifact_id, grant in mine.items():
            if not isinstance(artifact_id, str) or not _ARTIFACT_ID.match(artifact_id) or not isinstance(grant, dict):
                continue
            permission, expires = grant.get("permission"), grant.get("expires_at")
            if permission in _GRANT_PERMISSIONS and _finite(expires) and expires > now:
                out[artifact_id] = permission
        return out

    def available(self) -> bool:
        return self._grants() is not None

    def limits(self) -> BridgeLimits:
        return self._require()[1]

    def has_grants(self, pr: Principal) -> bool:
        got = self._grants()
        return got is not None and bool(self._live_grants(got[0], pr))

    def has_write_grant(self, pr: Principal) -> bool:
        got = self._grants()
        return got is not None and "write" in self._live_grants(got[0], pr).values()

    # -- access -----------------------------------------------------------------------------------------------
    def _deny(self, pr: Principal, tool: str, artifact_id) -> CollabError:
        resource = artifact_id[:128] if isinstance(artifact_id, str) else None
        if isinstance(pr, Principal):
            self.service._audit_denied(pr, f"artifact.{tool}", "no_grant", resource_id=resource,
                                       agent_id=pr.id if pr.is_agent else None)
        self.service.stats["policy_denied"] += 1
        return CollabError(404, NOT_FOUND)

    def _head_doc(self, artifact_id: str) -> dict | None:
        doc = read_json_beneath(self.mirror, [ARTIFACTS_DIR, artifact_id, HEAD_NAME], max_bytes=HEAD_MAX_BYTES)
        if doc is None:
            return None
        head, versions = doc.get("head"), doc.get("versions")
        if not isinstance(head, dict) or not isinstance(versions, list) or not versions \
                or not all(isinstance(v, dict) and isinstance(v.get("files"), list) for v in versions):
            log.warning("collab artifacts: malformed head.json artifact=%s", artifact_id)
            raise CollabError(502, INTEGRITY)
        return doc

    def _open(self, pr: Principal, tool: str, artifact_id) -> tuple[str, dict]:
        """Step order for every per-artifact method: availability (503), then agent, id shape, grant and a listed
        ``head.json``, all failing with the same audited 404. Returns (permission, head document)."""
        agents, _limits = self._require()
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID.match(artifact_id):
            raise self._deny(pr, tool, artifact_id)
        permission = self._live_grants(agents, pr).get(artifact_id)
        if permission is None:
            raise self._deny(pr, tool, artifact_id)
        doc = self._head_doc(artifact_id)
        if doc is None:
            raise self._deny(pr, tool, artifact_id)
        return permission, doc

    def _used(self, pr: Principal, artifact_id: str, tool: str) -> None:
        """Audit an allowed call, coalesced per (agent, artifact, tool) per ``AUDIT_EVERY`` s. Raises 503 (and leaves
        the counter as it was) when the event cannot be written."""
        svc = self.service
        with svc._lock:
            key = (pr.id, artifact_id, tool)
            now = self.clock()
            state = self._uses.get(key)
            if state is not None and now - state[0] < AUDIT_EVERY:
                state[1] += 1
                self._uses.move_to_end(key)
                return
            calls = (state[1] if state is not None else 0) + 1
            svc._commit("artifact_tool_used", svc._actor(
                pr, action=_TOOL_ACTIONS[tool], outcome="allowed", tool=tool, calls=calls, artifact_id=artifact_id or None,
                resource_id=artifact_id or None, project_ids=[]))
            self._uses[key] = [now, 0]
            self._uses.move_to_end(key)
            while len(self._uses) > self.USE_COUNTER_MAX:     # an evicted key's suppressed calls are dropped
                self._uses.popitem(last=False)
                self.stats["use_counter_evicted"] += 1

    # -- tools ------------------------------------------------------------------------------------------------
    def list(self, pr: Principal) -> dict:
        agents, _limits = self._require()
        out = []
        for artifact_id, permission in sorted(self._live_grants(agents, pr).items()):
            try:
                doc = self._head_doc(artifact_id)
            except CollabError:
                continue                                  # one malformed head.json never hides the others
            if doc is None:
                continue                                  # not mirrored (yet): not listed
            head = doc["head"]
            out.append({"artifact_id": artifact_id, "title": doc.get("title"), "permission": permission,
                        "writable": permission == "write" and doc.get("writable") is True,
                        "head_version_id": head.get("version_id"), "head_version_number": head.get("version_number")})
        if isinstance(pr, Principal) and pr.is_agent:
            self._used(pr, "", "artifacts_list")
        return {"artifacts": out}

    @staticmethod
    def _file_view(f: dict) -> dict:
        return {"name": f.get("name"), "size_bytes": f.get("size_bytes"), "sha256": f.get("sha256"),
                "media_type": f.get("media_type"), "text": f.get("text") is True, "mirrored": f.get("mirrored") is True}

    @staticmethod
    def _head_version(doc: dict) -> dict:
        head_id = doc["head"].get("version_id")
        version = next((v for v in doc["versions"] if v.get("version_id") == head_id), None)
        if version is None:
            raise CollabError(502, INTEGRITY)
        return version

    def head(self, pr: Principal, artifact_id: str) -> dict:
        permission, doc = self._open(pr, "head", artifact_id)
        version = self._head_version(doc)
        out = {"artifact_id": artifact_id, "title": doc.get("title"), "artifact_type": doc.get("artifact_type"),
               "permission": permission, "writable": permission == "write" and doc.get("writable") is True,
               "writable_reason": doc.get("writable_reason"),
               "head_version_id": version.get("version_id"), "head_version_number": version.get("version_number"),
               "parent_version_ids": list(version.get("parent_version_ids") or []),
               "message": version.get("message"), "created_at": version.get("created_at"),
               "files": [self._file_view(f) for f in version["files"] if isinstance(f, dict)]}
        self._used(pr, artifact_id, "artifact_head")
        return out

    def history(self, pr: Principal, artifact_id: str, *, limit: int = 50) -> dict:
        _permission, doc = self._open(pr, "history", artifact_id)
        if not _int(limit) or not 1 <= limit <= MAX_HISTORY:
            raise CollabError(400, f"limit must be an integer from 1 to {MAX_HISTORY}")
        versions = []
        for v in doc["versions"][:limit]:
            versions.append({
                "version_id": v.get("version_id"), "version_number": v.get("version_number"),
                "parent_version_ids": list(v.get("parent_version_ids") or []),
                "created_via": v.get("created_via"), "collaborator_agent_id": v.get("collaborator_agent_id"),
                "author_label": v.get("author_label"), "message": v.get("message"),
                "created_at": v.get("created_at"), "merge": v.get("merge") is True,
                "mirrored": v.get("mirrored") is True,
                "files": [{"name": f.get("name"), "size_bytes": f.get("size_bytes"), "text": f.get("text") is True,
                           "mirrored": f.get("mirrored") is True} for f in v["files"] if isinstance(f, dict)]})
        out = {"artifact_id": artifact_id, "head_version_id": doc["head"].get("version_id"), "versions": versions,
               "truncated": len(doc["versions"]) > limit}
        self._used(pr, artifact_id, "artifact_history")
        return out

    def read(self, pr: Principal, artifact_id: str, name: str, *, version_id: str | None = None, offset: int = 0,
             max_bytes: int = DEFAULT_READ_BYTES) -> dict:
        _permission, doc = self._open(pr, "read", artifact_id)
        if not isinstance(name, str) or not name or len(name) > MAX_NAME:
            raise CollabError(400, "name must be a file name of the artifact")
        if version_id is not None and (not isinstance(version_id, str) or not _VERSION_ID.match(version_id)):
            raise CollabError(400, "version_id is invalid")
        if not _int(offset) or offset < 0:
            raise CollabError(400, "offset must be a non-negative integer")
        if not _int(max_bytes) or not 1 <= max_bytes <= MAX_READ_BYTES:
            raise CollabError(400, f"max_bytes must be an integer from 1 to {MAX_READ_BYTES}")
        for attempt in range(2):
            if attempt:                                   # the blob was collected after the head read: look again
                doc = self._head_doc(artifact_id)
                if doc is None:
                    raise self._deny(pr, "read", artifact_id)
            vid = doc["head"].get("version_id") if version_id is None else version_id
            version = next((v for v in doc["versions"] if v.get("version_id") == vid), None)
            f = None if version is None else next(
                (f for f in version["files"] if isinstance(f, dict) and f.get("name") == name), None)
            if f is None:
                raise CollabError(404, NOT_FOUND)
            if f.get("text") is not True:
                raise CollabError(415, BINARY)
            if version.get("mirrored") is not True or f.get("mirrored") is not True:
                raise CollabError(404, NOT_MIRRORED)
            sha, size = f.get("sha256"), f.get("size_bytes")
            if not isinstance(sha, str) or not _SHA.match(sha) or not _int(size) or size < 0:
                log.warning("collab artifacts: malformed manifest entry artifact=%s", artifact_id)
                raise CollabError(502, INTEGRITY)
            got = self._slice(artifact_id, sha, size, offset, max_bytes)
            if got is None:
                continue
            text, next_offset = got
            out = {"name": name, "version_id": vid, "size_bytes": size, "offset": offset, "text": text,
                   "next_offset": next_offset}
            self._used(pr, artifact_id, "artifact_read")
            return out
        raise CollabError(404, NOT_MIRRORED)

    # -- blobs ------------------------------------------------------------------------------------------------
    def _remember_verified(self, key: tuple) -> None:
        with self._verified_lock:
            self._verified[key] = True
            self._verified.move_to_end(key)
            while len(self._verified) > self.VERIFY_CACHE_MAX:
                self._verified.popitem(last=False)

    def _is_verified(self, key: tuple) -> bool:
        with self._verified_lock:
            if key in self._verified:
                self._verified.move_to_end(key)
                return True
            return False

    def _hash_fd(self, fd: int) -> str:
        self.stats["blob_hashes"] += 1
        h, pos = hashlib.sha256(), 0
        while True:
            block = os.pread(fd, _HASH_BLOCK, pos)
            if not block:
                return h.hexdigest()
            h.update(block)
            pos += len(block)

    def _slice(self, artifact_id: str, sha: str, size: int, offset: int, max_bytes: int) -> tuple[str, int | None] | None:
        """The verified slice of blob ``sha`` and the next offset; None when the blob is gone (collected)."""
        parts = [ARTIFACTS_DIR, artifact_id, BLOBS_DIR, sha]
        fh = open_beneath(self.mirror, parts)
        if fh is None:
            return None
        with fh:
            fd = fh.fileno()
            st = os.fstat(fd)
            if st.st_size != size:
                log.warning("collab artifacts: blob size mismatch artifact=%s sha256=%s", artifact_id, sha)
                raise CollabError(502, INTEGRITY)
            key = ("/".join(parts), st.st_ino, st.st_size, st.st_mtime_ns)
            if not self._is_verified(key):
                if self._hash_fd(fd) != sha:
                    log.warning("collab artifacts: blob digest mismatch artifact=%s sha256=%s", artifact_id, sha)
                    raise CollabError(502, INTEGRITY)
                self._remember_verified(key)
            if offset > size:
                raise CollabError(400, "offset is past the end of the file")
            buf = os.pread(fd, max_bytes + 4, offset)
        if buf and (buf[0] & 0xC0) == 0x80:
            raise CollabError(400, "offset is not on a character boundary")
        end = _utf8_slice(buf, max_bytes)
        try:
            text = buf[:end].decode("utf-8")
        except UnicodeDecodeError:
            log.warning("collab artifacts: blob is not UTF-8 artifact=%s sha256=%s", artifact_id, sha)
            raise CollabError(502, INTEGRITY) from None
        stop = offset + end
        return text, (stop if stop < size else None)

    # -- agents.json ------------------------------------------------------------------------------------------
    def _feed(self) -> dict:
        """The feed body, snapshotted under the service lock. Only non-revoked agents are listed (a missing agent
        is not active to the worker), which keeps the feed bounded; an agent whose owner may not use the network
        (disabled, kicked, denied) is listed ``disabled``."""
        svc = self.service
        with svc._lock:
            join_ids = {digest: jid for jid, digest in svc._joins_by_id.items()}
            agents = {}
            for agent_id, a in svc._agents.items():
                if a.get("status") != "active":
                    continue
                x = a.get("external")
                agents[agent_id] = {
                    "owner": a["owner"], "status": "active" if svc._ok(a["owner"]) else "disabled",
                    "expires_at": a.get("expires_at"), "external": bool(x),
                    "join_id": join_ids.get(a.get("join")) if a.get("join") else None,
                    "hard_expiry": x.get("hard_expiry") if isinstance(x, dict) else None}
        return {"generated_at": self.clock(), "agents": agents}

    def write_agents_feed(self) -> None:
        """Atomically (tmp + rename in the outbox) rewrite ``agents.json``. Never raises: a failure is logged by type."""
        try:
            with self._feed_lock:
                atomic_write_json(self.outbox, AGENTS_FEED, self._feed())
            self.stats["feed_writes"] += 1
        except Exception as exc:                          # noqa: BLE001 - a feed write never breaks a caller
            self.stats["feed_failures"] += 1
            log.warning("collab artifacts: agents feed write failed error=%s", type(exc).__name__)

    def agents_changed(self) -> None:
        """Wake the feed thread for an early rewrite (cheap; safe under the service lock)."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.feed_interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            self.write_agents_feed()

    def start(self) -> None:
        """Write the feed now and start the daemon thread that rewrites it every ``feed_interval`` s (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.write_agents_feed()
        self._thread = threading.Thread(target=self._run, name="collab-agents-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=10)

    def feed_thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


__all__ = ["AGENTS_FEED", "AUDIT_EVERY", "BINARY", "CollabArtifactBridge", "INTEGRITY", "MAX_READ_BYTES",
           "NOT_FOUND", "NOT_MIRRORED", "UNAVAILABLE"]

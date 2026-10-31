"""The read-only collaborator mirror the worker publishes for ``collab-public`` (plan 3, Task 4).

Layout under the mirror root (worker-owned; mounted read-only into ``collab-public``)::

    grants.json                         {generated_at, limits, agents: {agent_id: {artifact_id: {permission, expires_at}}}}
    artifacts/<artifact_id>/head.json   manifests of the non-retracted versions, newest first
    artifacts/<artifact_id>/blobs/<sha256>   text files of the head and the previous ``mirror_history_versions``

``head.json`` is written last and atomically, so a reader never sees a manifest whose mirrored blobs are missing, and a
crash mid-refresh leaves the previous ``head.json`` (and its blobs) in place. Blobs a new ``head.json`` no longer
references are collected only after the swap, under the artifact's lock. Nothing else in an artifact dir is touched by a
refresh (plan 4 keeps ``annotations.json`` there); ``remove`` deletes the whole dir.

Timestamps in ``grants.json`` (``generated_at``, ``expires_at``) are epoch seconds (float). No owner identity
(``created_by``), storage key or provenance beyond the capped commit message is ever written.

Every blocking step (RustFS streaming, fsync, directory sweeps) runs in a worker thread. Every write goes through
fd-relative, no-follow operations or ``atomic_write_json``; every listing and delete goes through
``list_beneath``/``unlink_beneath``/``remove_tree_beneath``.
"""
from __future__ import annotations

import asyncio
import codecs
import logging
import os
import re
import stat
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from .artifact_collab_store import CollabGrantStore
from .artifact_merge import _is_text_like
from .collab_bridge_files import (
    MAX_MESSAGE,
    SHA_RE,
    BridgeError,
    BridgeLimits,
    atomic_write_json,
    list_beneath,
    remove_tree_beneath,
    unlink_beneath,
)

log = logging.getLogger(__name__)

GRANTS_NAME = "grants.json"
HEAD_NAME = "head.json"
ARTIFACTS_DIR = "artifacts"
BLOBS_DIR = "blobs"
REPORT_READ_ONLY = "report artifacts are read-only for collaborators"
RENDERED_READ_ONLY = "renderer-built artifacts are read-only for collaborators"
# The mirror is the worker's own tree (collab-public mounts it read-only), so these are fail-safe bounds on listings
# and sweeps, not anti-abuse limits: no other writer can flood it.
MIRROR_ENTRY_SLACK = 4096
MIRROR_MAX_ENTRIES = 1_000_000
_BINARY_CACHE_MAX = 100_000
_SHA = re.compile(SHA_RE)
_ARTIFACT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\Z")


class _NotText(Exception):
    """Raised inside a blob stream that turned out not to be UTF-8 (or grew past its cap): discard the temp file."""


def _epoch(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _walk_create(root: Path, parts: list[str]) -> int:
    """An fd on ``root/parts...``; each component is created if missing and always opened ``O_NOFOLLOW``."""
    dfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts:
            if not part or part in (".", "..") or "/" in part or "\0" in part:
                raise BridgeError("unsafe path component")
            try:
                os.mkdir(part, 0o755, dir_fd=dfd)
            except FileExistsError:
                pass
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
            os.close(dfd)
            dfd = nfd
    except BaseException:
        os.close(dfd)
        raise
    return dfd


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _stream_blob(root: Path, parts: list[str], name: str, chunks: Iterable[bytes], *, max_bytes: int) -> bool:
    """Stream ``chunks`` into ``root/parts.../name`` (tmp, fsync, rename in the dir fd) if they are UTF-8 text of at
    most ``max_bytes``; False (nothing left behind) otherwise. Any other error propagates, also leaving nothing."""
    dfd = _walk_create(root, parts)
    try:
        tmp = ".tmp-" + os.urandom(8).hex()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        try:
            try:
                decoder = codecs.getincrementaldecoder("utf-8")()
                size = 0
                iterator = iter(chunks)
                try:
                    for chunk in iterator:
                        size += len(chunk)
                        if size > max_bytes:
                            raise _NotText
                        try:
                            decoder.decode(chunk)
                        except UnicodeDecodeError:
                            raise _NotText from None
                        _write_all(fd, chunk)
                    try:
                        decoder.decode(b"", final=True)
                    except UnicodeDecodeError:
                        raise _NotText from None
                finally:
                    close = getattr(iterator, "close", None)
                    if close is not None:
                        close()
                os.fchmod(fd, 0o644)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except _NotText:
            _unlink_quiet(tmp, dfd)
            return False
        except BaseException:
            _unlink_quiet(tmp, dfd)
            raise
        os.fsync(dfd)
        return True
    finally:
        os.close(dfd)


def _unlink_quiet(name: str, dfd: int) -> None:
    try:
        os.unlink(name, dir_fd=dfd)
    except OSError:
        pass


def _is_blob(root: Path, parts: list[str], name: str) -> bool:
    """A regular file (never a link) at ``root/parts.../name``; any unsafe component is "no"."""
    try:
        dfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        for part in parts:
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
            os.close(dfd)
            dfd = nfd
        return stat.S_ISREG(os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode)
    except OSError:
        return False
    finally:
        os.close(dfd)


class CollabMirror:
    def __init__(self, root: Path, *, rustfs, limits: BridgeLimits, clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self.rustfs = rustfs
        self.limits = limits
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = {}      # never popped: a waiter and a newcomer must share one lock
        self._fingerprints: dict[str, tuple] = {}
        self._binary: set[str] = set()
        self._grants_body: dict | None = None
        self._grants_written_at: float | None = None

    # -- grants.json ------------------------------------------------------------------------------------------
    async def publish_grants(self, grants, store, *, heartbeat: float = 30.0) -> bool:
        """Write ``grants.json`` when its content changed, else at least every ``heartbeat`` s; True if written.

        ``grants`` is a ``CollabGrantStore`` (or the list ``live_grants`` returns). ``store`` (an ``ArtifactStore`` or
        None) drops artifacts retracted as a whole, so ``artifacts_list`` stops showing them with the next write.
        Never takes an artifact lock.
        """
        rows = await grants.live_grants() if hasattr(grants, "live_grants") else list(grants)
        retracted = set()
        if store is not None:
            retracted = {r["artifact_id"] for r in await store.load_retractions() if not r.get("version_id")}
        agents: dict[str, dict] = {}
        for row in rows:
            if row["artifact_id"] in retracted:
                continue
            agents.setdefault(row["agent_id"], {})[row["artifact_id"]] = {
                "permission": row["permission"], "expires_at": _epoch(row["expires_at"])}
        body = {"limits": self.limits.to_dict(), "agents": agents}
        now = self.clock()
        if body == self._grants_body and self._grants_written_at is not None \
                and now - self._grants_written_at < heartbeat:
            return False
        await asyncio.to_thread(self._ensure_root)
        await asyncio.to_thread(atomic_write_json, self.root, GRANTS_NAME, {"generated_at": now, **body})
        self._grants_body, self._grants_written_at = body, now
        return True

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    # -- per-artifact mirror ----------------------------------------------------------------------------------
    def _lock(self, artifact_id: str) -> asyncio.Lock:
        lock = self._locks.get(artifact_id)
        if lock is None:
            lock = self._locks[artifact_id] = asyncio.Lock()
        return lock

    async def refresh(self, store, artifact_id: str, *, gate) -> dict:
        """Rewrite ``artifacts/<artifact_id>`` from Postgres; the ``head.json`` written, or ``{}`` if it was removed.

        An artifact that is missing, retracted (durably or by ``gate``) or whose head version is retracted is removed
        from the mirror instead (fail closed). Errors while streaming a blob propagate and leave the previous
        ``head.json`` untouched.
        """
        if not _ARTIFACT_ID.match(artifact_id or ""):
            raise BridgeError("invalid artifact id")
        async with self._lock(artifact_id):
            artifact = await store.get(artifact_id)
            versions = await store.list_versions(artifact_id) if artifact is not None else []
            doc, fetch = self._plan(artifact, versions, gate)
            if doc is None:
                await self._remove_dir(artifact_id)
                self._fingerprints.pop(artifact_id, None)
                return {}
            await asyncio.to_thread(self._write, artifact_id, doc, fetch)
            self._fingerprints[artifact_id] = self._fingerprint(artifact, versions, gate)
            log.info("collab mirror refreshed artifact=%s head=%s", artifact_id, doc["head"]["version_id"])
            return doc

    def _plan(self, artifact, versions: list[dict], gate) -> tuple[dict | None, list[dict]]:
        """The ``head.json`` to write (``mirrored`` flags provisional) and the files to fetch; (None, []) to remove."""
        if artifact is None:
            return None, []
        artifact_id = artifact["artifact_id"]
        if artifact["lifecycle"] == "retracted" or gate.artifact_retracted(artifact_id):
            return None, []
        live = [v for v in versions
                if v.get("state") != "retracted" and not gate.version_retracted(artifact_id, v["version_id"])]
        head_id = artifact["current_version_id"]
        head = next((v for v in live if v["version_id"] == head_id), None)
        if head is None:                                    # no head, or the head itself is retracted
            return None, []
        live = [head] + [v for v in live if v["version_id"] != head_id]
        live = live[:self.limits.mirror_max_manifests]
        window = min(self.limits.mirror_history_versions + 1, len(live))
        writable_reason = None
        if artifact["artifact_type"] == "report":
            writable_reason = REPORT_READ_ONLY
        elif head.get("report_descriptor") or head.get("source_manifest_blob"):
            writable_reason = RENDERED_READ_ONLY
        out_versions, fetch, budget, chosen = [], [], self.limits.mirror_max_artifact_bytes, {}
        for index, version in enumerate(live):
            in_window = index < window
            files = []
            for item in version.get("output_manifest") or []:
                if not isinstance(item, dict):
                    continue
                sha = item.get("sha256") or item.get("blob_sha256")
                size = item.get("size_bytes")
                size = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None
                media_type = item.get("media_type") if isinstance(item.get("media_type"), str) else None
                f = {"name": item.get("name"), "size_bytes": size, "sha256": sha, "media_type": media_type,
                     "text": sha not in self._binary and _is_text_like(item), "mirrored": False}
                files.append(f)
                if not in_window or not isinstance(sha, str) or not _SHA.match(sha) or sha in self._binary:
                    continue
                if sha in chosen:
                    chosen[sha]["refs"].append(f)
                    continue
                if size is None or size > self.limits.mirror_max_file_bytes or size > budget:
                    continue
                budget -= size
                chosen[sha] = {"sha256": sha, "size_bytes": size, "refs": [f],
                               "storage_key": item.get("storage_key") or item.get("blob_sha256")}
                fetch.append(chosen[sha])
            prov = version.get("provenance") if isinstance(version.get("provenance"), dict) else {}
            message = prov.get("message") if isinstance(prov.get("message"), str) else ""
            parents = list(version.get("parent_version_ids") or [])
            collaborator = version.get("collaborator_agent_id")
            out_versions.append({
                "version_id": version["version_id"], "version_number": version["version_number"],
                "parent_version_ids": parents, "created_via": version.get("created_via") or "owner",
                "collaborator_agent_id": collaborator,
                "author_label": f"collaborator {collaborator}" if version.get("created_via") == "collaborator"
                else "owner",
                "message": message[:MAX_MESSAGE], "created_at": version.get("created_at"),
                "merge": len(parents) > 1, "mirrored": in_window, "files": files})
        doc = {"artifact_id": artifact_id, "title": artifact["original_filename"],
               "artifact_type": artifact["artifact_type"], "writable": writable_reason is None,
               "writable_reason": writable_reason,
               "head": {"version_id": head["version_id"], "version_number": head["version_number"]},
               "versions": out_versions}
        return doc, fetch

    def _write(self, artifact_id: str, doc: dict, fetch: list[dict]) -> None:
        """Blocking: stream missing blobs, swap ``head.json``, then collect unreferenced blobs."""
        parts = [ARTIFACTS_DIR, artifact_id, BLOBS_DIR]
        self._ensure_root()
        os.close(_walk_create(self.root, parts))
        for item in fetch:
            sha = item["sha256"]
            if _is_blob(self.root, parts, sha):
                ok = True
            else:
                key = item["storage_key"]
                if isinstance(key, str) and _SHA.match(key):
                    key = f"sha256/{key[:2]}/{key[2:4]}/{key}"
                ok = _stream_blob(self.root, parts, sha, self.rustfs.iter_get(key, sha),
                                  max_bytes=min(item["size_bytes"], self.limits.mirror_max_file_bytes))
                if not ok:
                    if len(self._binary) >= _BINARY_CACHE_MAX:
                        self._binary.clear()
                    self._binary.add(sha)
            for ref in item["refs"]:
                ref["mirrored"] = ok
                ref["text"] = ok or ref["text"] and sha not in self._binary
        atomic_write_json(self.root / ARTIFACTS_DIR / artifact_id, HEAD_NAME, doc)
        keep = {f["sha256"] for v in doc["versions"] for f in v["files"] if f["mirrored"]}
        try:
            entries = list_beneath(self.root, parts, max_entries=len(keep) + MIRROR_MAX_ENTRIES)
        except BridgeError:
            log.warning("collab mirror blob sweep skipped artifact=%s: too many entries", artifact_id)
            return
        for name, _st in entries or []:
            if name not in keep:
                unlink_beneath(self.root, parts, name)

    def _fingerprint(self, artifact, versions: list[dict], gate) -> tuple:
        artifact_id = artifact["artifact_id"]
        return (artifact["current_version_id"], artifact["lifecycle"], artifact["artifact_type"],
                tuple(sorted(v["version_id"] for v in versions if v.get("state") != "retracted"
                             and not gate.version_retracted(artifact_id, v["version_id"]))))

    async def remove(self, artifact_id: str) -> bool:
        """Delete ``artifacts/<artifact_id>`` entirely (annotations included), under the artifact's lock."""
        if not _ARTIFACT_ID.match(artifact_id or ""):
            raise BridgeError("invalid artifact id")
        async with self._lock(artifact_id):
            self._fingerprints.pop(artifact_id, None)
            return await self._remove_dir(artifact_id)

    async def _remove_dir(self, artifact_id: str) -> bool:
        def go() -> bool:
            if not os.path.lexists(self.root / ARTIFACTS_DIR / artifact_id):
                return True
            return remove_tree_beneath(self.root, [ARTIFACTS_DIR, artifact_id], max_entries=MIRROR_MAX_ENTRIES)
        removed = await asyncio.to_thread(go)
        if removed:
            log.info("collab mirror removed artifact=%s", artifact_id)
        else:
            log.warning("collab mirror could not remove artifact=%s", artifact_id)
        return removed

    # -- sync -------------------------------------------------------------------------------------------------
    async def sync(self, store, *, gate, force: set[str] | frozenset[str] = frozenset(),
                   grants: CollabGrantStore | None = None) -> dict:
        """Refresh every granted artifact whose head or version set changed (or that is in ``force``) and remove
        mirrored artifacts with no live grant or that are retracted. Does not publish ``grants.json``.

        ``grants`` defaults to a ``CollabGrantStore`` over ``store``'s connection. Per-artifact failures are logged
        and reported, never raised, so one broken artifact cannot stall the rest.
        """
        grants = grants if grants is not None else CollabGrantStore(store._conn)
        granted = sorted({g["artifact_id"] for g in await grants.live_grants()})
        out = {"refreshed": [], "removed": [], "failed": []}
        for artifact_id in granted:
            try:
                artifact = await store.get(artifact_id)
                if artifact is None or artifact["lifecycle"] == "retracted" or gate.artifact_retracted(artifact_id):
                    if await self._known(artifact_id):
                        await self.remove(artifact_id)
                        out["removed"].append(artifact_id)
                    continue
                if artifact_id not in force and artifact_id in self._fingerprints:
                    versions = await store.list_versions(artifact_id)
                    if self._fingerprint(artifact, versions, gate) == self._fingerprints[artifact_id]:
                        continue
                doc = await self.refresh(store, artifact_id, gate=gate)
                out["refreshed" if doc else "removed"].append(artifact_id)
            except Exception:                                  # noqa: BLE001 - one artifact never stops the sync
                log.exception("collab mirror refresh failed artifact=%s", artifact_id)
                out["failed"].append(artifact_id)
        wanted = set(granted)
        try:
            entries = await asyncio.to_thread(
                list_beneath, self.root, [ARTIFACTS_DIR], max_entries=len(wanted) + MIRROR_ENTRY_SLACK)
        except BridgeError:
            log.warning("collab mirror stale-dir sweep skipped: too many entries")
            entries = []
        for name, st in entries or []:
            if name in wanted or not _ARTIFACT_ID.match(name) or not stat.S_ISDIR(st.st_mode):
                continue
            await self.remove(name)
            out["removed"].append(name)
        return out

    async def _known(self, artifact_id: str) -> bool:
        return artifact_id in self._fingerprints or await asyncio.to_thread(
            os.path.lexists, self.root / ARTIFACTS_DIR / artifact_id)


__all__ = ["CollabMirror", "GRANTS_NAME", "HEAD_NAME", "REPORT_READ_ONLY", "RENDERED_READ_ONLY"]

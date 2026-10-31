"""The read-only collaborator mirror the worker publishes for ``collab-public`` (plan 3, Task 4).

Layout under the mirror root (worker-owned; mounted read-only into ``collab-public``)::

    grants.json                         {generated_at, limits, agents: {agent_id: {artifact_id: {permission, expires_at}}}}
    artifacts/<artifact_id>/head.json   manifests of the non-retracted versions, newest first
    artifacts/<artifact_id>/blobs/<sha256>   text files of the head and the previous ``mirror_history_versions``

Contract for consumers:

* ``grants.json`` timestamps (``generated_at``, ``expires_at``) are epoch seconds (float); ``head.json``'s
  ``created_at`` is an ISO-8601 string.
* A missing ``head.json`` means the artifact is not listed.
* A file's ``text`` flag is authoritative only when ``mirrored`` is true (its bytes were checked as UTF-8). For a file
  that was never downloaded (outside the history window, over a cap, or whose fetch failed) it is a media-type /
  extension guess.
* No owner identity (``created_by``), storage key or provenance beyond the capped commit message is ever written.

Refresh order: a ``head.json`` that drops a version (retraction) is written, and its blobs collected, BEFORE any
download, so a slow or failing RustFS never keeps a retracted version readable. Downloads come next; a blob whose fetch
fails is listed ``mirrored: false`` (and not retried for ``BAD_BLOB_TTL`` seconds), ``head.json`` is still written and
the artifact is retried on the next ``sync``. ``head.json`` is always swapped atomically and after the blobs it marks
mirrored are in place; blobs it no longer references are collected after each swap, under the artifact's lock. Nothing
else in an artifact dir is touched by a refresh (plan 4 keeps ``annotations.json`` there); ``remove`` deletes the dir.

Every blocking step runs off the event loop: mirror refreshes on the default executor, the small ``grants.json`` write
on a dedicated single thread so its heartbeat never queues behind a long download or sweep. Blobs are written through
``stream_new_beneath`` (no-follow, never replaced, never partial); listings and deletes go through
``list_beneath``/``unlink_beneath``/``remove_tree_beneath``.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .artifact_collab_store import CollabGrantStore
from .artifact_merge import _is_text_like
from .collab_bridge_files import (
    DIR_MODE,
    MAX_MESSAGE,
    SHA_RE,
    BridgeError,
    BridgeLimits,
    atomic_write_json,
    ensure_dir,
    file_exists_beneath,
    list_beneath,
    make_dirs_beneath,
    remove_tree_beneath,
    stream_new_beneath,
    unlink_beneath,
    utf8_accept,
)

log = logging.getLogger(__name__)

GRANTS_NAME = "grants.json"
HEAD_NAME = "head.json"
ARTIFACTS_DIR = "artifacts"
BLOBS_DIR = "blobs"
REPORT_READ_ONLY = "report artifacts are read-only for collaborators"
RENDERED_READ_ONLY = "renderer-built artifacts are read-only for collaborators"
BAD_BLOB_TTL = 600.0
# The mirror is the worker's own tree (collab-public mounts it read-only), so these are fail-safe bounds on listings
# and sweeps, not anti-abuse limits: no other writer can flood it.
MIRROR_ENTRY_SLACK = 4096
MIRROR_MAX_ENTRIES = 1_000_000
_CACHE_MAX = 100_000
_SHA = re.compile(SHA_RE)
_ARTIFACT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\Z")


@dataclass(frozen=True)
class GrantsPublish:
    """What ``publish_grants`` did: truthy when ``grants.json`` was written; ``changed`` holds the artifact ids whose
    grant entries differ from the previous write (all of them on the first write), for an immediate mirror sync."""
    written: bool
    changed: frozenset[str]

    def __bool__(self) -> bool:
        return self.written


def _epoch(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


class _Tap:
    """Iterates a RustFS stream counting bytes, without ever closing it: ``stream_new_beneath`` stops reading at a
    validator or size refusal, and the mirror then decides (by draining the rest) whether the object is genuinely
    non-text or corrupt. ``RustFSClient.iter_get`` verifies the digest only once fully read."""

    def __init__(self, iterable):
        self.inner = iter(iterable)
        self.seen = 0

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        chunk = next(self.inner)
        self.seen += len(chunk)
        return chunk

    def drain(self, size: int) -> bool:
        """Read the rest; True only when it ends cleanly (digest verified) at exactly ``size`` bytes."""
        for _chunk in self:
            if self.seen > size:
                return False
        return self.seen == size

    def release(self) -> None:
        """Close the underlying stream (deliberately not named ``close``: the writer would call it on refusal)."""
        close = getattr(self.inner, "close", None)
        if close is not None:
            close()


def _by_artifact(agents: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for agent_id, arts in agents.items():
        for artifact_id, grant in arts.items():
            out.setdefault(artifact_id, {})[agent_id] = grant
    return out


class CollabMirror:
    def __init__(self, root: Path, *, rustfs, limits: BridgeLimits, clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self.rustfs = rustfs
        self.limits = limits
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = {}      # never popped: a waiter and a newcomer must share one lock
        self._fingerprints: dict[str, tuple] = {}      # set only after a refresh with every download done
        self._listed: dict[str, frozenset[str]] = {}   # version ids the last head.json written listed
        self._binary: set[str] = set()                 # content-addressed, so "not UTF-8" never expires
        self._bad: dict[str, float] = {}               # sha -> retry-after (epoch) for failed fetches
        self._cache_lock = threading.Lock()            # _bad/_binary are touched from worker threads
        self._grants_lock = asyncio.Lock()             # one publish at a time: a stale snapshot never lands last
        self._grants_body: dict | None = None
        self._grants_written_at: float | None = None
        self._grants_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="collab-grants")

    def close(self) -> None:
        self._grants_pool.shutdown(wait=False)

    # -- grants.json ------------------------------------------------------------------------------------------
    async def publish_grants(self, grants, store, *, heartbeat: float = 30.0, gate=None,
                             retracted: set[str] | frozenset[str] | None = None) -> GrantsPublish:
        """Write ``grants.json`` when its content changed, else at least every ``heartbeat`` s.

        ``grants`` is a ``CollabGrantStore`` (or the list ``live_grants`` returns). Artifacts retracted as a whole are
        left out, so ``artifacts_list`` stops showing them with the next write; they come from ``retracted`` (a set of
        artifact ids), else ``gate.artifact_retracted`` (the worker's pre-loaded ``ArtifactGraphGate``), else
        ``store.load_retractions()`` (``store`` may be None). Never takes an artifact lock; the file write runs on a
        dedicated thread, never on the default executor the mirror's downloads use. Calls are serialized, so a
        snapshot read earlier can never overwrite a newer ``grants.json``.
        """
        async with self._grants_lock:
            return await self._publish_grants(grants, store, heartbeat=heartbeat, gate=gate, retracted=retracted)

    async def _publish_grants(self, grants, store, *, heartbeat: float, gate, retracted) -> GrantsPublish:
        rows = await grants.live_grants() if hasattr(grants, "live_grants") else list(grants)
        if retracted is not None:
            is_retracted = set(retracted).__contains__
        elif gate is not None:
            is_retracted = gate.artifact_retracted
        elif store is not None:
            is_retracted = {r["artifact_id"] for r in await store.load_retractions()
                            if not r.get("version_id")}.__contains__
        else:
            def is_retracted(_artifact_id: str) -> bool:
                return False
        agents: dict[str, dict] = {}
        for row in rows:
            if is_retracted(row["artifact_id"]):
                continue
            agents.setdefault(row["agent_id"], {})[row["artifact_id"]] = {
                "permission": row["permission"], "expires_at": _epoch(row["expires_at"])}
        body = {"limits": self.limits.to_dict(), "agents": agents}
        before = _by_artifact(self._grants_body["agents"]) if self._grants_body is not None else {}
        after = _by_artifact(agents)
        changed = frozenset(a for a in before.keys() | after.keys() if before.get(a) != after.get(a))
        now = self.clock()
        if body == self._grants_body and self._grants_written_at is not None \
                and now - self._grants_written_at < heartbeat:
            return GrantsPublish(False, frozenset())
        doc = {"generated_at": now, **body}

        def write() -> None:
            ensure_dir(self.root, mode=DIR_MODE)
            atomic_write_json(self.root, GRANTS_NAME, doc)
        await asyncio.get_running_loop().run_in_executor(self._grants_pool, write)
        self._grants_body, self._grants_written_at = body, now
        return GrantsPublish(True, changed)

    # -- per-artifact mirror ----------------------------------------------------------------------------------
    def _lock(self, artifact_id: str) -> asyncio.Lock:
        lock = self._locks.get(artifact_id)
        if lock is None:
            lock = self._locks[artifact_id] = asyncio.Lock()
        return lock

    async def refresh(self, store, artifact_id: str, *, gate) -> dict:
        """Rewrite ``artifacts/<artifact_id>`` from Postgres; the ``head.json`` written, or ``{}`` if it was removed.

        An artifact that is missing, retracted (durably or by ``gate``) or whose head version is retracted is removed
        from the mirror instead (fail closed). A failed blob fetch leaves that file ``mirrored: false`` and the
        artifact due for a retry on the next ``sync``; an exception that is not an ``Exception`` (a crash) propagates
        and leaves the previous ``head.json`` untouched.
        """
        return await self._refresh(store, artifact_id, gate=gate, fetch=True)

    async def _refresh(self, store, artifact_id: str, *, gate, fetch: bool) -> dict:
        if not _ARTIFACT_ID.match(artifact_id or ""):
            raise BridgeError("invalid artifact id")
        async with self._lock(artifact_id):
            artifact = await store.get(artifact_id)
            versions = await store.list_versions(artifact_id) if artifact is not None else []
            doc, candidates = self._plan(artifact, versions, gate)
            if doc is None:
                await self._remove_dir(artifact_id)
                self._forget(artifact_id)
                return {}
            listed = frozenset(v["version_id"] for v in doc["versions"])
            previous = self._listed.get(artifact_id)
            shrunk = previous is None or bool(previous - listed)
            # the thread cannot be stopped: on cancellation keep the lock until it is done, so a later refresh (for
            # example the prune after a retraction) never races with this one's final head.json
            work = asyncio.ensure_future(asyncio.to_thread(self._write, artifact_id, doc, candidates,
                                                           prune_first=shrunk, fetch=fetch))
            cancelled = False
            while True:
                try:
                    complete = await asyncio.shield(work)
                    break
                except asyncio.CancelledError:
                    if work.done():
                        raise
                    cancelled = True
            self._listed[artifact_id] = listed
            if cancelled:
                self._fingerprints.pop(artifact_id, None)
                raise asyncio.CancelledError
            if fetch and complete:
                self._fingerprints[artifact_id] = self._fingerprint(artifact, versions, gate)
            else:
                self._fingerprints.pop(artifact_id, None)
            log.info("collab mirror %s artifact=%s head=%s", "refreshed" if fetch else "pruned", artifact_id,
                     doc["head"]["version_id"])
            return doc

    def _forget(self, artifact_id: str) -> None:
        self._fingerprints.pop(artifact_id, None)
        self._listed.pop(artifact_id, None)

    def _live(self, artifact_id: str, versions: list[dict], gate) -> list[dict]:
        return [v for v in versions
                if v.get("state") != "retracted" and not gate.version_retracted(artifact_id, v["version_id"])]

    def _plan(self, artifact, versions: list[dict], gate) -> tuple[dict | None, list[dict]]:
        """The ``head.json`` to write (every file ``mirrored: false`` for now) and the blob candidates in priority
        order (head first, deduplicated by sha); (None, []) when the artifact must leave the mirror."""
        if artifact is None:
            return None, []
        artifact_id = artifact["artifact_id"]
        if artifact["lifecycle"] == "retracted" or gate.artifact_retracted(artifact_id):
            return None, []
        live = self._live(artifact_id, versions, gate)
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
        out_versions, candidates, chosen = [], [], {}
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
                binary = self._is_binary(sha)
                f = {"name": item.get("name"), "size_bytes": size, "sha256": sha, "media_type": media_type,
                     "text": not binary and _is_text_like(item), "mirrored": False}
                files.append(f)
                if not in_window or not isinstance(sha, str) or not _SHA.match(sha) or binary:
                    continue
                if sha in chosen:
                    chosen[sha]["refs"].append(f)
                    continue
                if size is None or size > self.limits.mirror_max_file_bytes:
                    continue
                chosen[sha] = {"sha256": sha, "size_bytes": size, "refs": [f],
                               "storage_key": item.get("storage_key") or item.get("blob_sha256")}
                candidates.append(chosen[sha])
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
        return doc, candidates

    @staticmethod
    def _mark(candidate: dict, ok: bool) -> None:
        for ref in candidate["refs"]:
            ref["mirrored"] = ok
            if ok:
                ref["text"] = True

    def _publish_head(self, artifact_id: str, doc: dict) -> None:
        """Swap ``head.json``, then collect every blob it does not mark mirrored (stale ``.tmp-*`` included)."""
        parts = [ARTIFACTS_DIR, artifact_id, BLOBS_DIR]
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

    def _bad_blob(self, sha: str) -> bool:
        with self._cache_lock:
            return self._bad.get(sha, 0.0) > self.clock()

    def _remember_bad(self, sha: str) -> None:
        now = self.clock()
        with self._cache_lock:
            if len(self._bad) >= _CACHE_MAX:
                self._bad = {k: v for k, v in self._bad.items() if v > now}
                if len(self._bad) >= _CACHE_MAX:
                    self._bad.clear()
            self._bad[sha] = now + BAD_BLOB_TTL

    def _remember_binary(self, sha: str) -> None:
        with self._cache_lock:
            if len(self._binary) >= _CACHE_MAX:
                self._binary.clear()
            self._binary.add(sha)

    def _is_binary(self, sha) -> bool:
        with self._cache_lock:
            return sha in self._binary

    def _fetch(self, artifact_id: str, parts: list[str], candidate: dict) -> bool | None:
        """Download one candidate: True mirrored, False genuinely non-text (fully read, digest verified, right size),
        None failed (transport, digest, wrong size; remembered for ``BAD_BLOB_TTL``)."""
        sha, size = candidate["sha256"], candidate["size_bytes"]
        key = candidate["storage_key"]
        if isinstance(key, str) and _SHA.match(key):
            key = f"sha256/{key[:2]}/{key[2:4]}/{key}"
        tap = None
        try:
            tap = _Tap(self.rustfs.iter_get(key, sha))
            if stream_new_beneath(self.root, parts, sha, tap, max_bytes=size, accept=utf8_accept()):
                return True
            if tap.seen <= size and tap.drain(size):
                self._remember_binary(sha)
                return False
            reason = "size mismatch"
        except Exception as exc:                            # noqa: BLE001 - RustFS/IntegrityError/OS: per blob
            reason = type(exc).__name__
        finally:
            if tap is not None:
                tap.release()
        log.warning("collab mirror blob fetch failed artifact=%s sha256=%s error=%s", artifact_id, sha, reason)
        self._remember_bad(sha)
        return None

    def _write(self, artifact_id: str, doc: dict, candidates: list[dict], *, prune_first: bool,
               fetch: bool) -> bool:
        """Blocking. When versions left the listing (or downloads are off), first publish a ``head.json`` marking only
        blobs already present. Then walk the candidates once in priority order (head first, then older versions newest
        first): a present blob that fits the artifact budget is kept, a missing one that fits is downloaded and
        charged only once in place. Publish again. True when nothing is left to reconsider: no download failed and no
        blob was skipped for budget while a lower-priority one was kept."""
        parts = [ARTIFACTS_DIR, artifact_id, BLOBS_DIR]
        ensure_dir(self.root, mode=DIR_MODE)
        make_dirs_beneath(self.root, parts, dir_mode=DIR_MODE)
        if prune_first or not fetch:
            budget = self.limits.mirror_max_artifact_bytes
            for candidate in candidates:
                if candidate["size_bytes"] <= budget and file_exists_beneath(self.root, parts, candidate["sha256"]):
                    budget -= candidate["size_bytes"]
                    self._mark(candidate, True)
            self._publish_head(artifact_id, doc)
            if not fetch:
                return True
            for candidate in candidates:
                for ref in candidate["refs"]:
                    ref["mirrored"] = False
        budget = self.limits.mirror_max_artifact_bytes
        complete, skipped = True, False
        for candidate in candidates:
            sha, size = candidate["sha256"], candidate["size_bytes"]
            if size > budget:
                skipped = True
                continue
            if file_exists_beneath(self.root, parts, sha):
                ok = True
            elif self._bad_blob(sha):
                complete = False
                continue
            else:
                ok = self._fetch(artifact_id, parts, candidate)
                if ok is None:
                    complete = False
                    continue
            if ok:
                budget -= size
                complete = complete and not skipped
            else:
                for ref in candidate["refs"]:
                    ref["text"] = False
            self._mark(candidate, ok)
        self._publish_head(artifact_id, doc)
        return complete

    def _fingerprint(self, artifact, versions: list[dict], gate) -> tuple:
        artifact_id = artifact["artifact_id"]
        return (artifact["current_version_id"], artifact["lifecycle"], artifact["artifact_type"],
                tuple(sorted(v["version_id"] for v in self._live(artifact_id, versions, gate))))

    async def remove(self, artifact_id: str) -> bool:
        """Delete ``artifacts/<artifact_id>`` entirely (annotations included), under the artifact's lock."""
        if not _ARTIFACT_ID.match(artifact_id or ""):
            raise BridgeError("invalid artifact id")
        async with self._lock(artifact_id):
            self._forget(artifact_id)
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
        """Bring the mirror in line with Postgres; does not publish ``grants.json``.

        Pass 1 (no downloads): remove mirrored artifacts that are retracted or have no live grant, and publish a pruned
        ``head.json`` for every artifact whose listed versions shrank. Pass 2: refresh every granted artifact whose
        head or version set changed, whose last refresh left a download undone, or that is in ``force``.

        ``grants`` defaults to a ``CollabGrantStore`` over ``store``'s connection. Per-artifact failures are logged
        and reported, never raised, so one broken artifact cannot stall the rest.

        Known race (accepted): an artifact granted after ``live_grants`` ran and refreshed meanwhile by the applier can
        be swept as "not granted" in pass 1; the next ``sync`` (or applier refresh) mirrors it again.
        """
        grants = grants if grants is not None else CollabGrantStore(store._conn)
        granted = sorted({g["artifact_id"] for g in await grants.live_grants()})
        out = {"refreshed": [], "removed": [], "failed": []}
        due, prune_failed = [], set()
        for artifact_id in granted:
            try:
                artifact = await store.get(artifact_id)
                if artifact is None or artifact["lifecycle"] == "retracted" or gate.artifact_retracted(artifact_id):
                    if await self._known(artifact_id):
                        await self._remove_into(artifact_id, out)
                    continue
                versions = await store.list_versions(artifact_id)
                if artifact_id not in force and \
                        self._fingerprints.get(artifact_id) == self._fingerprint(artifact, versions, gate):
                    continue
                due.append(artifact_id)
                listed = self._listed.get(artifact_id)
                live = {v["version_id"] for v in self._live(artifact_id, versions, gate)}
                if listed is None or listed - live:
                    if not await self._refresh(store, artifact_id, gate=gate, fetch=False):
                        due.remove(artifact_id)
                        out["removed"].append(artifact_id)
            except Exception:                                  # noqa: BLE001 - one artifact never stops the sync
                log.exception("collab mirror prune failed artifact=%s", artifact_id)
                prune_failed.add(artifact_id)
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
            await self._remove_into(name, out)
        for artifact_id in due:
            try:
                doc = await self.refresh(store, artifact_id, gate=gate)
                out["refreshed" if doc else "removed"].append(artifact_id)
                prune_failed.discard(artifact_id)
            except Exception:                                  # noqa: BLE001 - one artifact never stops the sync
                log.exception("collab mirror refresh failed artifact=%s", artifact_id)
                out["failed"].append(artifact_id)
                prune_failed.discard(artifact_id)
        out["failed"].extend(sorted(prune_failed))             # pruned nor refreshed: still failed
        return out

    async def _remove_into(self, artifact_id: str, out: dict) -> None:
        if await self.remove(artifact_id):
            out["removed"].append(artifact_id)
        else:
            out["failed"].append(artifact_id)

    async def _known(self, artifact_id: str) -> bool:
        return artifact_id in self._fingerprints or await asyncio.to_thread(
            os.path.lexists, self.root / ARTIFACTS_DIR / artifact_id)


__all__ = ["BAD_BLOB_TTL", "CollabMirror", "GRANTS_NAME", "GrantsPublish", "HEAD_NAME", "REPORT_READ_ONLY",
           "RENDERED_READ_ONLY"]

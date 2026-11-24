"""The host's own background loop: what the control-plane worker used to do for this feature.

``Housekeeper.run_once(now)`` runs every duty once and returns a ``SweepReport`` of counts; ``run_forever`` repeats it
(the public role starts and stops it with ``PublicServer``).  ``now`` is always a parameter.  Duties:

* **expiry**: a live share past ``expires_at`` gets one ``expired`` event (the state column has no ``expired`` value;
  every reader already derives the grey "expired" look from ``expires_at``).  Nothing is deleted.
* **directory / removed_shares / agent_tokens / groups**: the removed-member grace.  Evidence comes only from a FRESH,
  readable, non-empty ``members.json`` (``SyncedDirectory.snapshot``); stale, missing, malformed or empty means no
  evidence and nothing is revoked, because revocation is irreversible.  A member missing from a fresh listing is out
  once ``removed_grace`` seconds have passed since housekeeping first saw them missing (``member_usage.removed_since``,
  cleared when they are listed again); an account the directory marks ``disabled`` is out at any age.  Out members
  lose their pending and live shares, their active agent tokens and their cached group list.  Sharing switched off by
  the operator revokes shares with no directory at all.  Cached groups also lapse ``groups_ttl`` seconds after the
  member last signed in.
* **staging**: ``MemberService.sweep_staging``.
* **blobs**: blob files with no ``artifact_blobs`` row and no manifest reference, older than ``blob_grace``, are
  deleted under the per-sha advisory lock publish holds (try-lock: a sha being published is skipped).
* **versions**: a version that no longer backs anything is marked ``purged`` and the blobs only it used are deleted
  (the ``artifact_blobs`` row and the file), so the quota (current versions of live artifacts) matches what is stored.
  Purgeable: a superseded version once a newer one is ``version_grace`` seconds old, and every version of a retracted or
  tombstoned artifact once it was last touched ``version_grace`` seconds ago.  Never purged: the current version of a
  live artifact, anything under ``legal_hold``, a version a pending or live share points at, and any blob a kept
  version's manifest still names.  A version whose blob is mid-publish (per-sha lock held) waits for the next pass.
* **usage**: ``member_usage.bytes_used`` is recomputed from the artifacts under the member_usage row lock.

Pending-intake needs no sweep: the cap is enforced when an item is claimed and the original never expired pending items.
Each duty is isolated: one that raises is named in ``SweepReport.failed`` (logged by exception type only).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import asyncpg

from .blobs import BlobStore
from .cognito import DirectorySnapshot
from .db import LOCK_NS_BLOB, LOCK_NS_HOUSEKEEPING
from .member_api import MemberService

if TYPE_CHECKING:
    from .member_api import MemberSurface

log = logging.getLogger("herdr_artifact_host.housekeeping")

DEFAULT_INTERVAL: Final = 60.0
GROUPS_TTL: Final = 24 * 3600.0
REMOVED_GRACE: Final = 3600.0
STAGING_MAX_AGE: Final = 24 * 3600.0
BLOB_GRACE: Final = 3600.0
BLOB_GC_EVERY: Final = 900.0
VERSION_GRACE: Final = 3600.0
EXPIRY_FLOOR: Final = timedelta(days=7)
_BLOB_BATCH: Final = 500
_PURGE_BATCH: Final = 200
_LIVE_QUOTA: Final = "lifecycle NOT IN ('retracted','tombstoned')"


@dataclass(frozen=True)
class SweepReport:
    shares_expired: int = 0
    shares_revoked: int = 0
    tokens_revoked: int = 0
    tokens_pruned: int = 0
    groups_cleared: int = 0
    staging_removed: int = 0
    blobs_removed: int = 0
    versions_purged: int = 0
    usage_corrected: int = 0
    failed: tuple[str, ...] = ()
    skipped: bool = False  # another replica holds the leader lock: nothing was swept


@dataclass(frozen=True)
class HousekeepingConfig:
    groups_ttl: float = GROUPS_TTL
    removed_grace: float = REMOVED_GRACE
    staging_max_age: float = STAGING_MAX_AGE
    blob_grace: float = BLOB_GRACE
    version_grace: float = VERSION_GRACE
    blob_gc_every: float = (
        BLOB_GC_EVERY  # the blob walk is the costly duty: it runs on this slower cadence
    )


async def run_to_completion[T](fn: Callable[..., T], *args: object) -> T:
    """``to_thread`` that a cancellation cannot abandon half way: the thread is awaited to its end before the
    ``CancelledError`` surfaces (the caller still holds its locks until then)."""
    work = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:
                await asyncio.wait({work})
            except asyncio.CancelledError:
                continue
        if not work.cancelled():
            work.exception()  # retrieved: nobody else will look
        raise


def _utc(now: datetime) -> datetime:
    return now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)


class Housekeeper:
    def __init__(
        self,
        pool: asyncpg.Pool,
        blobs: BlobStore,
        service: MemberService,
        snapshot: Callable[[], DirectorySnapshot | None],
        *,
        group: str,
        config: HousekeepingConfig = HousekeepingConfig(),
    ) -> None:
        self.pool, self.blobs, self.service, self._snapshot = pool, blobs, service, snapshot
        self.group, self.config = group, config
        self._last_blob_gc: datetime | None = None

    @property
    def groups_ttl(self) -> float:
        return self.config.groups_ttl

    @property
    def removed_grace(self) -> float:
        return self.config.removed_grace

    @classmethod
    def for_member_surface(
        cls, pool: asyncpg.Pool, blobs: BlobStore, member: MemberSurface
    ) -> Housekeeper:
        return cls(pool, blobs, member.svc, member.s.directory_snapshot, group=member.s.group)

    # -- the pass

    async def run_once(self, now: datetime, *, force_blob_gc: bool = False) -> SweepReport:
        """One pass, if this replica is the leader (a session advisory lock held for the pass; otherwise skipped)."""
        now = _utc(now)
        async with self.pool.acquire() as leader:
            if not await leader.fetchval(
                "SELECT pg_try_advisory_lock($1, hashtext('sweep'))", LOCK_NS_HOUSEKEEPING
            ):
                return SweepReport(skipped=True)
            try:
                report = await self._sweep(now, force_blob_gc)
            finally:
                await leader.execute(
                    "SELECT pg_advisory_unlock($1, hashtext('sweep'))", LOCK_NS_HOUSEKEEPING
                )
        self._log(report)
        return report

    @staticmethod
    def _log(report: SweepReport) -> None:
        changed = [
            f"{f.name}={getattr(report, f.name)}"
            for f in fields(report)
            if f.name not in ("failed", "skipped") and getattr(report, f.name)
        ]
        if changed:  # counts only: no ids, names or secrets
            log.info("housekeeping: %s", " ".join(changed))

    def _blob_gc_due(self, now: datetime, force: bool) -> bool:
        last = self._last_blob_gc
        return force or last is None or (now - last).total_seconds() >= self.config.blob_gc_every

    async def _sweep(self, now: datetime, force_blob_gc: bool) -> SweepReport:
        counts: dict[str, int] = {}
        failed: list[str] = []

        async def duty(name: str, field: str, work: Awaitable[int]) -> None:
            try:
                counts[field] = counts.get(field, 0) + await work
            except (
                Exception
            ) as exc:  # one failing duty must not stop the others; the type name is all we log
                failed.append(name)
                log.error("housekeeping: %s failed (%s)", name, type(exc).__name__)

        out: set[str] = set()

        async def evaluate() -> int:
            out.update(await self._evaluate_directory(now))
            return 0

        await duty("expiry", "shares_expired", self._expire_shares(now))
        await duty("directory", "groups_cleared", evaluate())
        await duty("removed_shares", "shares_revoked", self._revoke_shares(now, out))
        await duty("agent_tokens", "tokens_revoked", self._revoke_tokens(now, out))
        await duty("agent_tokens", "tokens_pruned", self.service.tokens.prune())
        await duty("groups", "groups_cleared", self._lapse_groups(now, out))
        await duty("staging", "staging_removed", self._staging())
        if self._blob_gc_due(now, force_blob_gc):
            await duty("blobs", "blobs_removed", self._orphan_blobs(now))
            if (
                "blobs" not in failed
            ):  # a failed pass is retried on the next sweep, not after blob_gc_every
                self._last_blob_gc = now
        await duty("versions", "versions_purged", self._purge_versions(now))
        await duty("usage", "usage_corrected", self._reconcile_usage())
        return SweepReport(**counts, failed=tuple(dict.fromkeys(failed)))

    async def run_forever(self, interval: float = DEFAULT_INTERVAL) -> None:
        """Sweep every ``interval`` seconds (first pass after one interval) until cancelled."""
        while True:
            await asyncio.sleep(interval)
            try:
                await self.run_once(datetime.now(UTC))
            except Exception as exc:  # run_once isolates duties; this is the last line of defence
                log.error("housekeeping: pass failed (%s)", type(exc).__name__)

    # -- expiry

    async def _expire_shares(self, now: datetime) -> int:
        rows = await self.pool.fetch(
            "INSERT INTO share_events (share_id, kind, actor, at) "
            "SELECT s.share_id, 'expired', 'housekeeping', s.expires_at FROM shares s "
            "WHERE s.state='live' AND s.expires_at IS NOT NULL AND s.expires_at <= $1 "
            "AND s.expires_at > $2 "
            "ON CONFLICT (share_id, at) WHERE kind='expired' DO NOTHING RETURNING share_id",
            now,
            now - EXPIRY_FLOOR,
        )
        return len(rows)

    # -- the removed-member grace

    async def _evaluate_directory(self, now: datetime) -> set[str]:
        """Members positively out, from fresh evidence only (an empty set when there is none)."""
        try:
            snap = await asyncio.to_thread(self._snapshot)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            snap = None  # stale, missing, unreadable or malformed: no evidence
        if snap is None:
            return set()
        listed, disabled = snap.members, snap.disabled
        # Only members the host has seen (a member_usage row) under a bare id can be "removed".  Control-plane
        # principals (member:<id>, agent:<id>), API-key / CI creators and legacy imports are not directory members.
        known = {
            r["member_id"]
            for r in await self.pool.fetch(
                "SELECT member_id FROM member_usage WHERE position(':' in member_id) = 0"
            )
        }
        absent = sorted(known - listed)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE member_usage SET removed_since=NULL WHERE removed_since IS NOT NULL AND member_id = ANY($1)",
                sorted(listed - disabled),
            )
            await conn.execute(
                "UPDATE member_usage SET removed_since=$2 WHERE removed_since IS NULL AND member_id = ANY($1)",
                absent,
                now,
            )
            lapsed = await conn.fetch(
                "SELECT member_id FROM member_usage WHERE member_id = ANY($1) AND removed_since <= $2",
                absent,
                now - timedelta(seconds=self.config.removed_grace),
            )
        return {r["member_id"] for r in lapsed} | (disabled & known)

    async def _revoke_shares(self, now: datetime, out: set[str]) -> int:
        rows = await self.pool.fetch(
            "WITH r AS (UPDATE shares SET state='revoked', revoked_at=$2 "
            "WHERE state IN ('pending','live') AND (created_by = ANY($1) "
            "OR created_by IN (SELECT member_id FROM member_usage WHERE sharing_disabled) "
            "OR artifact_id IN (SELECT artifact_id FROM artifacts WHERE owner = ANY($1))) "
            "RETURNING share_id) "
            "INSERT INTO share_events (share_id, kind, actor, detail, at) "
            "SELECT share_id, 'revoked', 'housekeeping', '{\"reason\": \"member_out_or_sharing_off\"}'::jsonb, $2 "
            "FROM r RETURNING share_id",
            sorted(out),
            now,
        )
        return len(rows)

    async def _revoke_tokens(self, now: datetime, out: set[str]) -> int:
        if not out:
            return 0
        done = await self.pool.execute(
            "UPDATE agent_tokens SET revoked_at=$2 WHERE member_id = ANY($1) AND revoked_at IS NULL",
            sorted(out),
            now,
        )
        return int(done.rsplit(" ", 1)[-1])

    async def _lapse_groups(self, now: datetime, out: set[str]) -> int:
        """Forget cached group claims of out members and of members unseen for ``groups_ttl`` seconds."""
        done = await self.pool.execute(
            "UPDATE member_usage SET groups='{}', updated_at=now() WHERE cardinality(groups) > 0 "
            "AND (member_id = ANY($1) OR coalesce(last_used_at, updated_at) < $2)",
            sorted(out),
            now - timedelta(seconds=self.config.groups_ttl),
        )
        return int(done.rsplit(" ", 1)[-1])

    # -- files

    async def _staging(self) -> int:
        return await asyncio.to_thread(self.service.sweep_staging, self.config.staging_max_age)

    async def _orphan_blobs(self, now: datetime) -> int:
        cutoff = now.timestamp() - self.config.blob_grace
        old = await asyncio.to_thread(
            lambda: [sha for sha, mtime in self.blobs.iter_blobs() if mtime < cutoff]
        )
        removed = 0
        for start in range(0, len(old), _BLOB_BATCH):
            chunk = old[start : start + _BLOB_BATCH]
            known = {
                r["sha256"]
                for r in await self.pool.fetch(
                    "SELECT sha256 FROM artifact_blobs WHERE sha256 = ANY($1)", chunk
                )
            }
            for sha in chunk:
                if sha not in known and await self._collect(sha, cutoff):
                    removed += 1
        return removed

    async def _collect(self, sha: str, cutoff: float) -> bool:
        """Delete one unreferenced blob under publish's per-sha lock; a sha somebody holds is skipped (try-lock)."""
        async with self.pool.acquire() as conn:
            if not await conn.fetchval(
                "SELECT pg_try_advisory_lock($1, hashtext($2))", LOCK_NS_BLOB, sha
            ):
                return False
            try:
                if await conn.fetchval("SELECT 1 FROM artifact_blobs WHERE sha256=$1", sha):
                    return False
                if await conn.fetchval(
                    "SELECT 1 FROM artifact_versions WHERE state <> 'purged' AND output_manifest @> $1::jsonb LIMIT 1",
                    json.dumps([{"sha256": sha}]),
                ):
                    return False
                if not await asyncio.to_thread(self._still_old, sha, cutoff):
                    return False
                if not await run_to_completion(self.blobs.remove, sha):
                    return False
                await run_to_completion(self.blobs.prune_empty_dirs, sha)
                return True
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1, hashtext($2))", LOCK_NS_BLOB, sha)

    def _still_old(self, sha: str, cutoff: float) -> bool:
        try:
            return self.blobs._path(sha).stat().st_mtime < cutoff
        except OSError:
            return False

    # -- versions

    async def _purge_versions(self, now: datetime) -> int:
        cutoff = now - timedelta(seconds=self.config.version_grace)
        due = await self.pool.fetch(
            "SELECT v.version_id FROM artifact_versions v JOIN artifacts a ON a.artifact_id = v.artifact_id "
            "WHERE v.state <> 'purged' AND NOT a.legal_hold "
            "AND ((a.lifecycle IN ('retracted','tombstoned') AND a.updated_at < $1) "
            "  OR (a.lifecycle NOT IN ('retracted','tombstoned') AND v.version_id IS DISTINCT FROM a.current_version_id "
            "      AND EXISTS (SELECT 1 FROM artifact_versions n WHERE n.artifact_id = v.artifact_id "
            "                  AND n.version_number > v.version_number AND n.created_at < $1))) "
            "AND NOT EXISTS (SELECT 1 FROM shares s WHERE s.version_id = v.version_id "
            "                AND s.state IN ('pending','live')) "
            "ORDER BY v.created_at LIMIT $2",
            cutoff,
            _PURGE_BATCH,
        )
        return sum([1 for r in due if await self._purge_one(r["version_id"], cutoff)])

    async def _purge_one(self, version_id: str, cutoff: datetime) -> bool:
        """Mark one version purged and delete the blobs only it used, under publish's per-sha locks (try-lock: a sha
        somebody holds defers the whole version to the next pass).  The due-conditions are re-checked under the locks'
        protection by the UPDATE itself, so a share or publish that landed since the listing is respected."""
        async with self.pool.acquire() as conn:
            manifest = await conn.fetchval(
                "SELECT output_manifest::text FROM artifact_versions WHERE version_id=$1 AND state <> 'purged'",
                version_id,
            )
            if manifest is None:
                return False
            shas = sorted(
                {f["sha256"] for f in json.loads(manifest) if isinstance(f.get("sha256"), str)}
            )
            held: list[str] = []
            try:
                for sha in shas:
                    if not await conn.fetchval(
                        "SELECT pg_try_advisory_lock($1, hashtext($2))", LOCK_NS_BLOB, sha
                    ):
                        return False
                    held.append(sha)
                done = await conn.execute(
                    "UPDATE artifact_versions v SET state='purged', purged_at=now() FROM artifacts a "
                    "WHERE v.version_id=$1 AND v.state <> 'purged' AND a.artifact_id = v.artifact_id "
                    "AND NOT a.legal_hold "
                    "AND ((a.lifecycle IN ('retracted','tombstoned') AND a.updated_at < $2) "
                    "  OR (a.lifecycle NOT IN ('retracted','tombstoned') "
                    "      AND v.version_id IS DISTINCT FROM a.current_version_id)) "
                    "AND NOT EXISTS (SELECT 1 FROM shares s WHERE s.version_id = v.version_id "
                    "                AND s.state IN ('pending','live'))",
                    version_id,
                    cutoff,
                )
                if done.rsplit(" ", 1)[-1] != "1":
                    return False
                for sha in shas:
                    await self._drop_unused_blob(conn, sha)
                return True
            finally:
                for sha in held:
                    await conn.execute(
                        "SELECT pg_advisory_unlock($1, hashtext($2))", LOCK_NS_BLOB, sha
                    )

    async def _drop_unused_blob(self, conn: asyncpg.Connection, sha: str) -> None:
        """Delete a blob's row and file unless a kept version still names it.  The caller holds the sha's lock."""
        if await conn.fetchval(
            "SELECT 1 FROM artifact_versions WHERE state <> 'purged' AND output_manifest @> $1::jsonb LIMIT 1",
            json.dumps([{"sha256": sha}]),
        ):
            return
        await conn.execute("DELETE FROM artifact_blobs WHERE sha256=$1", sha)
        if await run_to_completion(self.blobs.remove, sha):
            await run_to_completion(self.blobs.prune_empty_dirs, sha)

    # -- quotas

    async def _reconcile_usage(self) -> int:
        drifted = await self.pool.fetch(
            "SELECT u.member_id FROM member_usage u LEFT JOIN (SELECT owner, sum(size_bytes) AS s FROM artifacts "
            f"WHERE {_LIVE_QUOTA} GROUP BY owner) a ON a.owner = u.member_id "
            "WHERE u.bytes_used <> coalesce(a.s, 0)"
        )
        fixed = 0
        for row in drifted:
            member = row["member_id"]
            async with self.pool.acquire() as conn, conn.transaction():
                # member_usage first, as publish and delete lock it: the sum below cannot race a writer
                await conn.execute(
                    "SELECT 1 FROM member_usage WHERE member_id=$1 FOR UPDATE", member
                )
                actual = await conn.fetchval(
                    f"SELECT coalesce(sum(size_bytes), 0) FROM artifacts WHERE owner=$1 AND {_LIVE_QUOTA}",
                    member,
                )
                done = await conn.execute(
                    "UPDATE member_usage SET bytes_used=$2, updated_at=now() WHERE member_id=$1 AND bytes_used <> $2",
                    member,
                    int(actual),
                )
                fixed += int(done.rsplit(" ", 1)[-1])
        return fixed

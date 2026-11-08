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
* **usage**: ``member_usage.bytes_used`` is recomputed from the artifacts under the member_usage row lock.

Pending-intake needs no sweep: the cap is enforced when an item is claimed and the original never expired pending items.
Each duty is isolated: one that raises is named in ``SweepReport.failed`` (logged by exception type only).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import asyncpg

from .blobs import BlobStore
from .cognito import DirectorySnapshot
from .db import LOCK_NS_BLOB
from .member_api import MemberService

if TYPE_CHECKING:
    from .member_api import MemberSurface

log = logging.getLogger("herdr_artifact_host.housekeeping")

DEFAULT_INTERVAL: Final = 60.0
GROUPS_TTL: Final = 24 * 3600.0
REMOVED_GRACE: Final = 3600.0
STAGING_MAX_AGE: Final = 24 * 3600.0
BLOB_GRACE: Final = 3600.0
_BLOB_BATCH: Final = 500
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
    usage_corrected: int = 0
    failed: tuple[str, ...] = ()


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
        groups_ttl: float = GROUPS_TTL,
        removed_grace: float = REMOVED_GRACE,
        staging_max_age: float = STAGING_MAX_AGE,
        blob_grace: float = BLOB_GRACE,
    ) -> None:
        self.pool, self.blobs, self.service, self._snapshot = pool, blobs, service, snapshot
        self.group = group
        self.groups_ttl, self.removed_grace = groups_ttl, removed_grace
        self.staging_max_age, self.blob_grace = staging_max_age, blob_grace

    @classmethod
    def for_member_surface(
        cls, pool: asyncpg.Pool, blobs: BlobStore, member: MemberSurface
    ) -> Housekeeper:
        return cls(pool, blobs, member.svc, member.s.directory_snapshot, group=member.s.group)

    # -- the pass

    async def run_once(self, now: datetime) -> SweepReport:
        now = _utc(now)
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
        await duty("blobs", "blobs_removed", self._orphan_blobs(now))
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
            "AND NOT EXISTS (SELECT 1 FROM share_events e WHERE e.share_id=s.share_id AND e.kind='expired') "
            "RETURNING share_id",
            now,
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
        known = {
            r["member_id"]
            for r in await self.pool.fetch(
                "SELECT member_id FROM member_usage "
                "UNION SELECT created_by FROM shares WHERE state IN ('pending','live') AND created_by IS NOT NULL "
                "UNION SELECT member_id FROM agent_tokens WHERE revoked_at IS NULL"
            )
        }
        absent = sorted(known - listed)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE member_usage SET removed_since=NULL WHERE removed_since IS NOT NULL AND member_id = ANY($1)",
                sorted(listed - disabled),
            )
            await conn.execute(
                "INSERT INTO member_usage (member_id) SELECT unnest($1::text[]) ON CONFLICT DO NOTHING",
                absent,
            )
            await conn.execute(
                "UPDATE member_usage SET removed_since=$2 WHERE removed_since IS NULL AND member_id = ANY($1)",
                absent,
                now,
            )
            lapsed = await conn.fetch(
                "SELECT member_id FROM member_usage WHERE member_id = ANY($1) AND removed_since <= $2",
                absent,
                now - timedelta(seconds=self.removed_grace),
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
            now - timedelta(seconds=self.groups_ttl),
        )
        return int(done.rsplit(" ", 1)[-1])

    # -- files

    async def _staging(self) -> int:
        return await asyncio.to_thread(self.service.sweep_staging, self.staging_max_age)

    async def _orphan_blobs(self, now: datetime) -> int:
        cutoff = now.timestamp() - self.blob_grace
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
                    "SELECT 1 FROM artifact_versions WHERE output_manifest @> $1::jsonb LIMIT 1",
                    json.dumps([{"sha256": sha}]),
                ):
                    return False
                if not await asyncio.to_thread(self._still_old, sha, cutoff):
                    return False
                return await asyncio.to_thread(self.blobs.remove, sha)
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1, hashtext($2))", LOCK_NS_BLOB, sha)

    def _still_old(self, sha: str, cutoff: float) -> bool:
        try:
            return self.blobs._path(sha).stat().st_mtime < cutoff
        except OSError:
            return False

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

"""Shared helpers for the library tests: real Postgres, no doubles."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg
import pytest

from herdr_artifact_host.library import FeedPage, FeedQuery, LibraryStore, Viewer

NOW = datetime.now(UTC)


@dataclass(frozen=True)
class Who:
    me: str
    you: str
    email: str
    g1: str
    g2: str

    @property
    def viewer(self) -> Viewer:
        return Viewer(self.me, self.email, frozenset())

    @property
    def other(self) -> Viewer:
        return Viewer(self.you)

    def viewer_in(self, *groups: str) -> Viewer:
        return Viewer(self.me, self.email, frozenset(groups))


@pytest.fixture
def who() -> Who:
    """Fresh member ids per test."""
    me, you = "a" + uuid.uuid4().hex[:8], "b" + uuid.uuid4().hex[:8]
    return Who(me, you, f"{me}@x.org", "g1-" + me, "g2-" + me)


def run[T](dsn: str, body: Callable[[LibraryStore, asyncpg.Pool], Awaitable[T]]) -> T:
    async def go() -> T:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        try:
            return await body(LibraryStore(pool), pool)
        finally:
            await pool.close()

    return asyncio.run(go())


async def artifact(
    pool: asyncpg.Pool,
    owner: str,
    *,
    name: str | None = None,
    intake: str = "accepted",
    lifecycle: str = "active",
) -> str:
    aid = "artifact_" + uuid.uuid4().hex
    await pool.execute(
        "INSERT INTO artifacts (artifact_id, owner, created_by, display_name, intake, lifecycle) "
        "VALUES ($1,$2,$2,$3,$4::intake_state,$5)",
        aid,
        owner,
        name,
        intake,
        lifecycle,
    )
    return aid


async def share(
    pool: asyncpg.Pool,
    aid: str,
    by: str,
    *,
    state: str = "live",
    audience: str = "lab",
    emails: tuple[str, ...] = (),
    groups: tuple[str, ...] = (),
    expires: datetime | None = None,
    title: str = "Shared",
) -> str:
    sid, vid = "share_" + uuid.uuid4().hex[:24], "version_" + uuid.uuid4().hex
    await pool.execute(
        "INSERT INTO artifact_versions (version_id, artifact_id, version_number, state) VALUES ($1,$2,$3,'ready')",
        vid,
        aid,
        secrets.randbelow(10**9) + 1,
    )
    await pool.execute(
        "INSERT INTO shares (share_id, artifact_id, version_id, audience, allow_emails, allow_groups, state, "
        "expires_at, title, created_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
        sid,
        aid,
        vid,
        audience,
        list(emails),
        list(groups),
        state,
        expires,
        title,
        by,
    )
    return sid


async def grant(pool: asyncpg.Pool, aid: str, member: str, by: str) -> None:
    await pool.execute(
        "INSERT INTO artifact_acl (artifact_id, principal_id, granted_by) VALUES ($1,$2,$3)",
        aid,
        member,
        by,
    )


async def feed(store: LibraryStore, viewer: Viewer, **kw: object) -> FeedPage:
    return await store.feed(viewer, FeedQuery(**kw))  # type: ignore[arg-type]

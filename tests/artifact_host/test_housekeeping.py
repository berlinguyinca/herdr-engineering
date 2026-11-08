"""The host's background housekeeping over real Postgres and real files; ``now`` is a parameter, never patched."""

from __future__ import annotations

import asyncio
import io
import json
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg

from herdr_artifact_host.blobs import BlobStore
from herdr_artifact_host.cognito import SyncedDirectory
from herdr_artifact_host.db import LOCK_NS_BLOB, LOCK_NS_HOUSEKEEPING, create_pool
from herdr_artifact_host.housekeeping import (
    Housekeeper,
    SweepReport,
    run_to_completion,
)
from herdr_artifact_host.library import LibraryStore
from herdr_artifact_host.member_api import MemberLimits, MemberService
from herdr_artifact_host.public_server import PublicServer
from tests.artifact_host.seed import running_host

GROUP = "local.dev"
HOUR = timedelta(hours=1)


def _write_directory(
    sync: Path, listed: list[str], *, disabled: list[str] | None = None, age: float = 0.0
) -> None:
    sync.mkdir(parents=True, exist_ok=True)
    doc: dict[str, object] = {"updated": time.time() - age, "groups": {GROUP: listed}}
    if disabled is not None:
        doc["disabled"] = disabled
    (sync / "members.json").write_text(json.dumps(doc))


class Env:
    def __init__(self, pool: asyncpg.Pool, tmp: Path) -> None:
        self.pool, self.tmp = pool, tmp
        self.sync = tmp / "sync"
        self.blobs = BlobStore(tmp / "blobs")
        self.service = MemberService(pool, self.blobs, LibraryStore(pool), MemberLimits(), None)
        self.keeper = Housekeeper(
            pool,
            self.blobs,
            self.service,
            SyncedDirectory(str(self.sync), GROUP).snapshot,
            group=GROUP,
        )

    async def share(
        self,
        owner: str,
        *,
        state: str = "live",
        expires: datetime | None = None,
        seen: bool = True,
    ) -> str:
        if seen:
            await self.pool.execute(
                "INSERT INTO member_usage (member_id) VALUES ($1) ON CONFLICT DO NOTHING", owner
            )
        n = os.urandom(6).hex()
        aid, vid, sid = f"artifact_{n}", f"version_{n}", f"share_{n}"
        await self.pool.execute(
            "INSERT INTO artifacts (artifact_id, owner, created_by) VALUES ($1,$2,$2)", aid, owner
        )
        await self.pool.execute(
            "INSERT INTO artifact_versions (version_id, artifact_id, version_number) VALUES ($1,$2,1)",
            vid,
            aid,
        )
        await self.pool.execute(
            "INSERT INTO shares (share_id, artifact_id, version_id, audience, token_hash, state, entry, "
            "expires_at, created_by) VALUES ($1,$2,$3,'public',$4,$5,'index.html',$6,$7)",
            sid,
            aid,
            vid,
            os.urandom(32).hex(),
            state,
            expires,
            owner,
        )
        return sid

    async def state(self, share_id: str) -> str:
        return str(await self.pool.fetchval("SELECT state FROM shares WHERE share_id=$1", share_id))

    async def token(self, member: str) -> None:
        await self.pool.execute(
            "INSERT INTO agent_tokens (digest, member_id) VALUES ($1,$2)",
            os.urandom(32).hex(),
            member,
        )

    async def active_tokens(self, member: str) -> int:
        return int(
            await self.pool.fetchval(
                "SELECT count(*) FROM agent_tokens WHERE member_id=$1 AND revoked_at IS NULL",
                member,
            )
        )

    def blob(self, data: bytes, *, age: timedelta = timedelta(0)) -> str:
        sha = self.blobs.put(io.BytesIO(data))
        if age:
            past = time.time() - age.total_seconds()
            os.utime(self.blobs._path(sha), (past, past))
        return sha


def run(dsn: str, tmp: Path, body: Callable[[Env], Awaitable[None]]) -> None:
    async def go() -> None:
        pool = await create_pool(dsn)
        try:
            await body(Env(pool, tmp))
        finally:
            await pool.close()

    asyncio.run(go())


def utc() -> datetime:
    return datetime.now(UTC)


def test_an_expired_share_is_recorded_once_and_a_live_one_is_left(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        now = utc()
        soon = await env.share("alice", expires=now + HOUR)
        never = await env.share("alice", expires=None)
        assert (await env.keeper.run_once(now)).shares_expired == 0
        report = await env.keeper.run_once(now + 2 * HOUR)
        assert report.shares_expired == 1 and report.failed == ()
        events = await env.pool.fetch(
            "SELECT share_id, kind FROM share_events WHERE kind='expired'"
        )
        assert [r["share_id"] for r in events] == [soon]
        assert (
            await env.state(never) == "live" and await env.state(soon) == "live"
        )  # nothing deleted
        assert (await env.keeper.run_once(now + 3 * HOUR)).shares_expired == 0

    run(migrated_dsn, tmp_path, body)


def test_a_removed_members_shares_revoke_only_after_the_grace(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        now = utc()
        gone, stays = await env.share("alice"), await env.share("bob")
        await env.token("alice")
        _write_directory(env.sync, ["bob", "carol"])
        first = await env.keeper.run_once(now)  # first sighting starts the clock
        assert first.shares_revoked == 0 and await env.state(gone) == "live"
        assert (await env.keeper.run_once(now + timedelta(seconds=3599))).shares_revoked == 0
        assert await env.active_tokens("alice") == 1
        later = await env.keeper.run_once(now + timedelta(seconds=3601))
        assert later.shares_revoked == 1 and later.tokens_revoked == 1
        assert await env.state(gone) == "revoked" and await env.state(stays) == "live"
        assert await env.active_tokens("alice") == 0
        assert (await env.keeper.run_once(now + 2 * HOUR)).shares_revoked == 0

    run(migrated_dsn, tmp_path, body)


def test_a_member_listed_again_restarts_the_grace(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        now = utc()
        sid = await env.share("alice")
        _write_directory(env.sync, ["bob"])
        await env.keeper.run_once(now)
        _write_directory(env.sync, ["alice", "bob"])
        await env.keeper.run_once(now + timedelta(minutes=30))
        _write_directory(env.sync, ["bob"])
        await env.keeper.run_once(now + timedelta(minutes=50))
        assert (
            await env.keeper.run_once(now + timedelta(seconds=4000))
        ).shares_revoked == 0  # only 11 minutes since
        assert await env.state(sid) == "live"

    run(migrated_dsn, tmp_path, body)


def test_a_disabled_account_revokes_at_any_age(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        now = utc()
        sid, other = await env.share("alice"), await env.share("bob")
        await env.token("alice")
        _write_directory(
            env.sync, ["alice", "bob"], disabled=["alice"]
        )  # still listed, but disabled
        report = await env.keeper.run_once(now)
        assert report.shares_revoked == 1 and report.tokens_revoked == 1
        assert await env.state(sid) == "revoked" and await env.state(other) == "live"

    run(migrated_dsn, tmp_path, body)


def test_a_stale_or_missing_or_empty_directory_revokes_nothing(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        now = utc()
        sid = await env.share("alice")
        await env.token("alice")
        for setup in (
            lambda: None,  # no file at all
            lambda: _write_directory(env.sync, ["bob"], disabled=["alice"], age=1000),  # stale
            lambda: (env.sync / "members.json").write_text("{not json"),
            lambda: _write_directory(env.sync, []),  # fresh but empty: a failed sync
        ):
            setup()
            for later in (now, now + 5 * HOUR, now + 30 * HOUR):
                report = await env.keeper.run_once(later)
                assert report.shares_revoked == 0 and report.tokens_revoked == 0
        assert await env.state(sid) == "live" and await env.active_tokens("alice") == 1

    run(migrated_dsn, tmp_path, body)


def test_sharing_switched_off_revokes_without_a_directory(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        sid = await env.share("alice")
        await env.pool.execute(
            "INSERT INTO member_usage (member_id, sharing_disabled) VALUES ('alice', true) "
            "ON CONFLICT (member_id) DO UPDATE SET sharing_disabled=true"
        )
        assert (await env.keeper.run_once(utc())).shares_revoked == 1
        assert await env.state(sid) == "revoked"

    run(migrated_dsn, tmp_path, body)


def test_stale_group_claims_are_forgotten_after_the_ttl(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        now = utc()
        await env.pool.execute(
            "INSERT INTO member_usage (member_id, email, groups, last_used_at) VALUES "
            "('old','o@x.org','{staff}',$1), ('new','n@x.org','{staff}',$2)",
            now - timedelta(hours=30),
            now - timedelta(hours=1),
        )
        report = await env.keeper.run_once(now)
        assert report.groups_cleared == 1
        rows = {
            r["member_id"]: list(r["groups"])
            for r in await env.pool.fetch("SELECT * FROM member_usage")
        }
        assert rows["old"] == [] and rows["new"] == ["staff"]
        assert (await env.keeper.run_once(now)).groups_cleared == 0

    run(migrated_dsn, tmp_path, body)


def test_old_staging_is_removed_and_fresh_staging_kept(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        up = env.blobs.tmp_dir / "up"
        old, fresh = up / "m1" / "a1" / "u_old", up / "m1" / "a1" / "u_new"
        for folder in (old, fresh):
            folder.mkdir(parents=True)
            (folder / "0000000").write_bytes(b"x")
        past = time.time() - 3 * 24 * 3600
        os.utime(old / "0000000", (past, past))
        os.utime(old, (past, past))
        report = await env.keeper.run_once(utc())
        assert report.staging_removed == 1
        assert not old.exists() and fresh.is_dir()
        assert (await env.keeper.run_once(utc())).staging_removed == 0

    run(migrated_dsn, tmp_path, body)


def test_revoked_tokens_older_than_90_days_are_pruned(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        await env.pool.execute(
            "INSERT INTO agent_tokens (digest, member_id, revoked_at) VALUES ($1,'a', now() - interval '100 days'),"
            " ($2,'a', now() - interval '1 day')",
            "a" * 64,
            "b" * 64,
        )
        assert (await env.keeper.run_once(utc())).tokens_pruned == 1
        assert (await env.keeper.run_once(utc())).tokens_pruned == 0

    run(migrated_dsn, tmp_path, body)


def test_orphan_blobs_past_grace_go_and_referenced_or_busy_ones_stay(
    migrated_dsn, tmp_path
) -> None:
    async def body(env: Env) -> None:
        orphan = env.blob(b"orphan", age=2 * HOUR)
        young = env.blob(b"young", age=timedelta(minutes=5))
        kept = env.blob(b"referenced", age=2 * HOUR)
        busy = env.blob(b"being published", age=2 * HOUR)
        await env.pool.execute(
            "INSERT INTO artifact_blobs (sha256, size_bytes) VALUES ($1, 10)", kept
        )
        async with env.pool.acquire() as holder:
            await holder.execute("SELECT pg_advisory_lock($1, hashtext($2))", LOCK_NS_BLOB, busy)
            try:
                report = await env.keeper.run_once(utc())
            finally:
                await holder.execute("SELECT pg_advisory_unlock_all()")
        assert report.blobs_removed == 1
        assert not env.blobs.exists(orphan)
        assert env.blobs.exists(young) and env.blobs.exists(kept) and env.blobs.exists(busy)
        again = await env.keeper.run_once(
            utc(), force_blob_gc=True
        )  # lock released: the (still unreferenced) blob goes now
        assert again.blobs_removed == 1 and not env.blobs.exists(busy)
        assert (await env.keeper.run_once(utc(), force_blob_gc=True)).blobs_removed == 0

    run(migrated_dsn, tmp_path, body)


def test_a_blob_whose_row_appears_under_the_lock_is_kept(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        sha = env.blob(b"racing publish", age=2 * HOUR)
        await env.pool.execute(
            "INSERT INTO artifact_blobs (sha256, size_bytes) VALUES ($1, 1)", sha
        )
        assert (await env.keeper.run_once(utc())).blobs_removed == 0 and env.blobs.exists(sha)

    run(migrated_dsn, tmp_path, body)


def test_a_blob_named_by_a_version_manifest_is_kept(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        sha = env.blob(b"in a manifest", age=2 * HOUR)
        await env.pool.execute("INSERT INTO artifacts (artifact_id, owner) VALUES ('a1','alice')")
        await env.pool.execute(
            "INSERT INTO artifact_versions (version_id, artifact_id, version_number, output_manifest) "
            "VALUES ('v1','a1',1,$1::jsonb)",
            json.dumps([{"name": "i.html", "sha256": sha, "size_bytes": 1}]),
        )
        assert (await env.keeper.run_once(utc())).blobs_removed == 0 and env.blobs.exists(sha)

    run(migrated_dsn, tmp_path, body)


def test_usage_drift_is_reported_and_fixed(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        await env.pool.execute(
            "INSERT INTO artifacts (artifact_id, owner, size_bytes, lifecycle) VALUES "
            "('a1','alice',100,'active'), ('a2','alice',50,'active'), ('a3','alice',999,'retracted'),"
            "('a4','bob',10,'active')"
        )
        await env.pool.execute(
            "INSERT INTO member_usage (member_id, bytes_used) VALUES ('alice', 7), ('bob', 10), ('ghost', 5)"
        )
        report = await env.keeper.run_once(utc())
        assert report.usage_corrected == 2  # alice (7 -> 150) and ghost (5 -> 0)
        used = {
            r["member_id"]: r["bytes_used"]
            for r in await env.pool.fetch("SELECT * FROM member_usage")
        }
        assert used == {"alice": 150, "bob": 10, "ghost": 0}
        assert (await env.keeper.run_once(utc())).usage_corrected == 0

    run(migrated_dsn, tmp_path, body)


def test_one_failing_duty_does_not_stop_the_others(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        now = utc()
        sid = await env.share("alice", expires=now + HOUR)
        orphan = env.blob(b"orphan", age=2 * HOUR)
        await env.pool.execute("DROP TABLE agent_tokens")  # a real failure in the token duty only
        report = await env.keeper.run_once(now + 2 * HOUR)
        assert "agent_tokens" in report.failed
        assert report.shares_expired == 1 and report.blobs_removed == 1
        assert not env.blobs.exists(orphan) and sid

    run(migrated_dsn, tmp_path, body)


def test_the_report_is_a_frozen_value() -> None:
    report = SweepReport()
    assert report.shares_revoked == 0 and report.failed == ()
    try:
        report.shares_revoked = 1  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("SweepReport must be frozen")


def test_run_forever_starts_and_stops_with_the_public_server(migrated_dsn, tmp_path) -> None:
    async def go() -> None:
        pool = await create_pool(migrated_dsn)
        try:
            env = Env(pool, tmp_path)
            sid = await env.share("alice", expires=utc() - HOUR)
            from herdr_artifact_host.public_server import build_app
            from herdr_artifact_host.share_http import DEFAULT_STATIC_DIR
            from tests.artifact_host.seed import default_config, make_sync_dir

            sync = make_sync_dir(tmp_path / "sync2")
            app = build_app(
                default_config(DEFAULT_STATIC_DIR, sync), pool, env.blobs, LibraryStore(pool)
            )
            keeper = Housekeeper(
                pool,
                env.blobs,
                env.service,
                SyncedDirectory(str(sync), GROUP).snapshot,
                group=GROUP,
            )
            server = PublicServer(app, housekeeper=keeper, housekeeping_interval=0.05)
            await server.start()
            try:
                assert server.housekeeping_running
                for _ in range(100):
                    if await pool.fetchval(
                        "SELECT 1 FROM share_events WHERE share_id=$1 AND kind='expired'", sid
                    ):
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("the loop never swept")
            finally:
                await server.stop()
            assert not server.housekeeping_running
            await server.stop()  # idempotent
        finally:
            await pool.close()

    asyncio.run(go())


def test_run_forever_survives_a_failing_pass_and_cancels_cleanly(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        await env.pool.execute("DROP TABLE agent_tokens")
        task = asyncio.create_task(env.keeper.run_forever(interval=0.01))
        await asyncio.sleep(0.15)
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.cancelled() or task.done()

    run(migrated_dsn, tmp_path, body)


def test_the_default_server_wiring_builds_a_housekeeper(migrated_dsn, tmp_path) -> None:
    with running_host(migrated_dsn, tmp_path) as host:
        assert host.server is not None and host.server.housekeeping_running


# ---------------------------------------------------------------- fix round 1


def test_only_bare_member_ids_the_host_has_seen_can_be_removed(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        now = utc()
        keep = [
            await env.share("member:bob", seen=False),  # control-plane principal shape
            await env.share("agent:ag_1", seen=True),  # even with a usage row: not a bare member id
            await env.share("ci-key:7", seen=True),
            await env.share(
                "legacy_import", seen=False
            ),  # bare, but the host never saw this member
        ]
        gone = await env.share("alice")  # seen and missing from the listing
        _write_directory(env.sync, ["zed"])
        await env.keeper.run_once(now)
        report = await env.keeper.run_once(now + 2 * HOUR)
        assert report.shares_revoked == 1 and await env.state(gone) == "revoked"
        assert [await env.state(s) for s in keep] == ["live"] * 4

    run(migrated_dsn, tmp_path, body)


def test_the_real_sync_script_output_drives_disabled_revocation(migrated_dsn, tmp_path) -> None:
    import re
    import subprocess
    import sys

    script = (Path(__file__).parents[2] / "deploy/control-plane/collab-sync.sh").read_text()
    match = re.search(r"python3 - ([^\n]*members\.json[^\n]*) <<'PY'\n(.*?)\nPY\n", script, re.S)
    assert match is not None and "disabled" in match.group(2)
    work = tmp_path / "script"
    work.mkdir()
    (work / "users.json").write_text(json.dumps(["alice", "bob", "carol"]))
    (work / "disabled.json").write_text(json.dumps(["alice"]))
    (work / "rakazo.json").write_text("[]")
    argv = {
        "$tmp/users.json": str(work / "users.json"),
        "$tmp/members.json": str(work / "members.json"),
        "$GROUP": GROUP,
        "$tmp/rakazo.json": str(work / "rakazo.json"),
        "$RAKAZO_GROUP": "",
        "$tmp/disabled.json": str(work / "disabled.json"),
    }
    words = [argv[w.strip('"')] for w in match.group(1).split()]
    subprocess.run([sys.executable, "-", *words], input=match.group(2), text=True, check=True)
    doc = json.loads((work / "members.json").read_text())
    assert doc["groups"] == {GROUP: ["alice", "bob", "carol"]} and doc["disabled"] == ["alice"]
    snap = SyncedDirectory(str(work), GROUP).snapshot()
    assert snap.members == {"alice", "bob", "carol"} and snap.disabled == {"alice"}
    assert SyncedDirectory(str(work), GROUP).groups("bob") == [
        GROUP
    ]  # existing consumers unaffected

    async def body(env: Env) -> None:
        sid, other = await env.share("alice"), await env.share("bob")
        env.sync.mkdir(parents=True, exist_ok=True)
        (env.sync / "members.json").write_text((work / "members.json").read_text())
        report = await env.keeper.run_once(utc())
        assert report.shares_revoked == 1
        assert await env.state(sid) == "revoked" and await env.state(other) == "live"

    run(migrated_dsn, tmp_path, body)


def test_blob_gc_runs_on_its_own_slower_cadence(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        now = utc()
        assert env.keeper.config.blob_gc_every == 900
        first = env.blob(b"first", age=2 * HOUR)
        assert (await env.keeper.run_once(now)).blobs_removed == 1 and not env.blobs.exists(first)
        second = env.blob(b"second", age=2 * HOUR)
        assert (await env.keeper.run_once(now + timedelta(minutes=1))).blobs_removed == 0
        assert env.blobs.exists(second)
        assert (await env.keeper.run_once(now + timedelta(minutes=14))).blobs_removed == 0
        assert (await env.keeper.run_once(now + timedelta(minutes=16))).blobs_removed == 1
        third = env.blob(b"third", age=2 * HOUR)
        assert (
            await env.keeper.run_once(now + timedelta(minutes=17), force_blob_gc=True)
        ).blobs_removed == 1
        assert not env.blobs.exists(third)

    run(migrated_dsn, tmp_path, body)


def test_collect_rechecks_the_ledger_under_the_lock(migrated_dsn, tmp_path) -> None:
    """A publish that commits its row after the listing but before the lock: _collect itself must keep the blob."""

    async def body(env: Env) -> None:
        cutoff = time.time() - 3600
        control = env.blob(b"nobody published this", age=2 * HOUR)
        assert (
            await env.keeper._collect(control, cutoff) is True
        )  # the same call does delete an orphan
        sha = env.blob(b"published meanwhile", age=2 * HOUR)
        await env.pool.execute(
            "INSERT INTO artifact_blobs (sha256, size_bytes) VALUES ($1, 1)", sha
        )
        assert await env.keeper._collect(sha, cutoff) is False and env.blobs.exists(sha)

    run(migrated_dsn, tmp_path, body)


def test_collect_rechecks_manifests_under_the_lock(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        sha = env.blob(b"manifest meanwhile", age=2 * HOUR)
        await env.pool.execute("INSERT INTO artifacts (artifact_id, owner) VALUES ('a1','alice')")
        await env.pool.execute(
            "INSERT INTO artifact_versions (version_id, artifact_id, version_number, output_manifest) "
            "VALUES ('v1','a1',1,$1::jsonb)",
            json.dumps([{"name": "i.html", "sha256": sha, "size_bytes": 1}]),
        )
        assert await env.keeper._collect(sha, time.time() - 3600) is False
        assert env.blobs.exists(sha)

    run(migrated_dsn, tmp_path, body)


def test_collect_keeps_a_blob_rewritten_after_it_was_listed(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        sha = env.blob(b"listed old", age=2 * HOUR)
        cutoff = time.time() - 3600
        old = [s for s, m in env.blobs.iter_blobs() if m < cutoff]
        assert old == [sha]
        os.utime(env.blobs._path(sha))  # a publish re-wrote the same content after the listing
        assert await env.keeper._collect(sha, cutoff) is False and env.blobs.exists(sha)

    run(migrated_dsn, tmp_path, body)


def test_a_share_expiry_is_recorded_once_per_expiry_even_when_extended(
    migrated_dsn, tmp_path
) -> None:
    async def body(env: Env) -> None:
        now = utc()
        sid = await env.share("alice", expires=now + HOUR)
        assert (await env.keeper.run_once(now + 2 * HOUR)).shares_expired == 1
        await env.pool.execute(
            "UPDATE shares SET expires_at=$2 WHERE share_id=$1", sid, now + 5 * HOUR
        )
        assert (await env.keeper.run_once(now + 3 * HOUR)).shares_expired == 0
        assert (
            await env.keeper.run_once(now + 6 * HOUR)
        ).shares_expired == 1  # a new expiry, a new event
        assert (await env.keeper.run_once(now + 7 * HOUR)).shares_expired == 0
        count = await env.pool.fetchval("SELECT count(*) FROM share_events WHERE kind='expired'")
        assert count == 2

    run(migrated_dsn, tmp_path, body)


def test_expiry_does_not_rescan_the_distant_past(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        now = utc()
        await env.share("alice", expires=now - timedelta(days=8))
        recent = await env.share("alice", expires=now - timedelta(days=6))
        assert (await env.keeper.run_once(now)).shares_expired == 1
        row = await env.pool.fetchval("SELECT share_id FROM share_events WHERE kind='expired'")
        assert row == recent

    run(migrated_dsn, tmp_path, body)


def test_only_one_replica_sweeps_at_a_time(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        now = utc()
        await env.share("alice", expires=now - HOUR)
        async with env.pool.acquire() as holder:
            await holder.execute(
                "SELECT pg_advisory_lock($1, hashtext('sweep'))", LOCK_NS_HOUSEKEEPING
            )
            try:
                report = await env.keeper.run_once(now)
            finally:
                await holder.execute("SELECT pg_advisory_unlock_all()")
        assert report.skipped and report.shares_expired == 0
        report = await env.keeper.run_once(
            now
        )  # the lock was released (and run_once releases its own)
        assert not report.skipped and report.shares_expired == 1
        assert not (await env.keeper.run_once(now)).skipped

    run(migrated_dsn, tmp_path, body)


def test_a_pass_that_changed_something_logs_counts_only(migrated_dsn, tmp_path, caplog) -> None:
    async def body(env: Env) -> None:
        now = utc()
        await env.share("alice-secret-name", expires=now - HOUR)
        with caplog.at_level("INFO", logger="herdr_artifact_host.housekeeping"):
            await env.keeper.run_once(now)
            quiet = len(caplog.records)
            await env.keeper.run_once(now)  # nothing changed: no line
            assert len(caplog.records) == quiet
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "shares_expired=1" in text and "alice" not in text and "share_" not in text

    run(migrated_dsn, tmp_path, body)


def test_run_to_completion_finishes_the_thread_before_cancellation_surfaces() -> None:
    done: list[str] = []

    def slow() -> str:
        time.sleep(0.3)
        done.append("finished")
        return "ok"

    async def go() -> None:
        task = asyncio.create_task(run_to_completion(slow))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            assert done == ["finished"]  # the unlink-like work was not abandoned half way
        else:
            raise AssertionError("cancellation was swallowed")

    asyncio.run(go())


def test_gc_removes_empty_shard_directories_but_not_busy_ones(migrated_dsn, tmp_path) -> None:
    async def body(env: Env) -> None:
        orphan = env.blob(b"alone in its shard", age=2 * HOUR)
        shard = env.blobs._path(orphan).parent
        assert (await env.keeper.run_once(utc())).blobs_removed == 1
        assert not shard.exists() and not shard.parent.exists()
        sibling = env.blob(b"sibling", age=2 * HOUR)
        keeper_shard = env.blobs._path(sibling).parent
        (keeper_shard / ("f" * 64)).write_bytes(b"x")  # something else lives in the shard
        (await env.keeper.run_once(utc(), force_blob_gc=True))
        assert keeper_shard.is_dir()

    run(migrated_dsn, tmp_path, body)

"""Member identity, accounting and audit tables on an isolated, Unix-socket-only PostgreSQL."""
import asyncio
import json
import uuid

import asyncpg
import pytest

from herdr_engineering.control_plane.artifact_members import (
    MemberLimits,
    MemberStore,
    is_member_principal,
    member_for_actor,
    member_owner_key,
    member_principal,
)
from tests.control_plane.test_share_store import MIGRATIONS, run, share_postgres  # noqa: F401  (fixture)

GROUP = "lab"
AGENTS = {
    "a_ok": {"owner": "alice", "status": "active", "external": False},
    "a_ext": {"owner": "alice", "status": "active", "external": True},
    "a_noflag": {"owner": "alice", "status": "active"},
    "a_rev": {"owner": "alice", "status": "revoked", "external": False},
}
DIRECTORY = {
    "alice": {"status": "approved", "groups": [GROUP]},
    "bob": {"status": "disabled", "groups": [GROUP]},
    "dan": {"status": "disabled", "groups": ["other"]},
    "erin": {"status": "approved", "groups": GROUP},
    "carol": {"status": "approved", "groups": ["other"]},
    "eu:r+x": {"status": "approved", "groups": [GROUP]},
}


def as_obj(value):
    """jsonb arrives as str without a codec, as a decoded object with one."""
    return json.loads(value) if isinstance(value, str) else value


def check(actor):
    return member_for_actor(actor, agents=AGENTS, group=GROUP, directory=DIRECTORY)


def test_principal_and_owner_key():
    assert member_principal("alice") == "member:alice"
    assert member_principal("eu:r+x") == "member:eu:r+x"
    with pytest.raises(ValueError):
        member_principal("bad user")
    assert is_member_principal("member:alice") and not is_member_principal("collab:x")
    assert not is_member_principal(None) and not is_member_principal("member:bad user")
    a, b = member_owner_key("member:alice"), member_owner_key("member:bob")
    assert a == member_owner_key("member:alice") and a != b
    assert a.startswith("m_") and len(a) == 26


def test_member_for_actor_reasons():
    good = check({"kind": "agent", "agent_id": "a_ok"})
    assert (good.ok, good.principal, good.reason) == (True, "member:alice", None)
    assert check({"kind": "agent", "agent_id": "nope"}).reason == "unknown_agent"
    assert check({"kind": "agent", "agent_id": "a_ext"}).reason == "external_agent"
    assert check({"kind": "agent", "agent_id": "a_noflag"}).reason == "external_agent"  # fail closed
    assert check({"kind": "agent", "agent_id": "a_rev"}).reason == "agent_inactive"
    assert check({"kind": "user", "user_id": "zed"}).reason == "unknown_user"
    assert check({"kind": "user", "user_id": "bob"}).reason == "user_disabled"
    assert check({"kind": "user", "user_id": "carol"}).reason == "not_in_group"
    assert check({"kind": "user", "user_id": "dan"}).reason == "not_in_group"  # both: group is checked first
    assert check({"kind": "user", "user_id": "erin"}).reason == "not_in_group"  # str groups: no substring match
    ok = check({"kind": "user", "user_id": "eu:r+x"})
    assert ok.ok and ok.principal == "member:eu:r+x" and ok.reason is None


def test_effective_limits():
    assert MemberLimits.effective({}, {}) == MemberLimits(None, None, None)
    row = {"max_artifact_bytes": 5, "max_member_bytes": None, "max_artifacts": None}
    assert MemberLimits.effective(row, {"max_member_bytes": 9, "max_artifact_bytes": 1}) == MemberLimits(5, 9, None)


@pytest.fixture
def migrated(share_postgres):  # noqa: F811
    async def go():
        conn = await asyncpg.connect(share_postgres)
        try:
            for _ in range(2):  # applying twice is a no-op
                await conn.execute((MIGRATIONS / "0048_member_artifacts.sql").read_text())
        finally:
            await conn.close()
    asyncio.run(go())
    return share_postgres


def test_usage_row_and_set_limits(migrated):
    async def body(_store, conn):
        store = MemberStore(conn)
        u = await store.usage("member:alice")
        assert u["limits"] == {"max_artifact_bytes": None, "max_member_bytes": None, "max_artifacts": None}
        assert u["bytes_used"] == 0 and u["live_links"] == {"lab": 0, "public": 0}
        assert u["artifact_count"] == 0 and u["version_count"] == 0
        for bad in (0, -1):
            with pytest.raises(ValueError):
                await store.set_limits("member:alice", max_artifacts=bad, actor="op")
        for huge in (2**31, 2**70):
            with pytest.raises(ValueError):
                await store.set_limits("member:alice", max_artifacts=huge, actor="op")
        with pytest.raises(ValueError):
            await store.set_limits("member:alice", max_member_bytes=2**63, actor="op")
        u = await store.set_limits("member:alice", max_member_bytes=100, sharing_disabled=True, actor="op")
        assert u["limits"]["max_member_bytes"] == 100 and u["sharing_disabled"] is True
        u = await store.set_limits("member:alice", max_artifacts=3, actor="op")
        assert u["limits"]["max_member_bytes"] == 100 and u["limits"]["max_artifacts"] == 3
        u = await store.set_limits("member:alice", max_member_bytes=None, actor="op")
        assert u["limits"]["max_member_bytes"] is None and u["limits"]["max_artifacts"] == 3
        ev = await conn.fetch("SELECT actor, action FROM artifact_member_events WHERE member_principal='member:alice'"
                              " ORDER BY event_id")
        assert [e["action"] for e in ev] == ["set_limits"] * 3 and as_obj(ev[0]["actor"])["id"] == "op"
        assert "alice" in [m["member_principal"].split(":")[1] for m in await store.list_members()]
    run(migrated, body)


def test_record_and_checks(migrated):
    async def body(_store, conn):
        store = MemberStore(conn)
        await store.record("member:bob", "share_create", actor={"kind": "user", "id": "bob"}, share_id="sh1",
                           audience="public", outcome="refused", code="not_found", detail={"x": 1})
        row = await conn.fetchrow("SELECT * FROM artifact_member_events WHERE member_principal='member:bob'")
        assert (row["share_id"], row["audience"], row["outcome"], row["code"]) == ("sh1", "public", "refused",
                                                                                   "not_found")
        with pytest.raises(asyncpg.CheckViolationError):
            await store.record("member:bob", "x", actor={}, outcome="bogus")
        await store.usage("member:bob")
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE artifact_member_usage SET bytes_used=-1 WHERE member_principal='member:bob'")
    run(migrated, body)


async def seed_member(conn, principal, *, lifecycle="active", versions=1):
    aid = "artifact_" + uuid.uuid4().hex
    await conn.execute("INSERT INTO artifacts (artifact_id,created_by,lifecycle) VALUES ($1,$2,$3)",
                       aid, principal, lifecycle)
    vids = []
    for n in range(1, versions + 1):
        vid = "version_" + uuid.uuid4().hex
        await conn.execute("INSERT INTO artifact_versions (version_id,artifact_id,version_number,state) "
                           "VALUES ($1,$2,$3,'ready')", vid, aid, n)
        vids.append(vid)
    return aid, vids


async def seed_share(conn, aid, vid, principal, audience, state):
    await conn.execute(
        "INSERT INTO artifact_shares (share_id,artifact_id,version_id,audience,state,created_by)"
        " VALUES ($1,$2,$3,$4,$5,$6)", "share_" + uuid.uuid4().hex, aid, vid, audience, state, principal)


def test_usage_counts_real_rows_and_excludes_other_members(migrated):
    async def body(_store, conn):
        store = MemberStore(conn)
        me, other = "member:counter", "member:other"
        a1, v1 = await seed_member(conn, me, versions=2)
        a2, v2 = await seed_member(conn, me, lifecycle="retracted")
        oa, ov = await seed_member(conn, other, versions=3)
        await seed_share(conn, a1, v1[0], me, "lab", "live")
        await seed_share(conn, a1, v1[1], me, "lab", "live")
        await seed_share(conn, a1, v1[1], me, "public", "live")
        await seed_share(conn, a1, v1[1], me, "public", "revoked")
        await seed_share(conn, a1, v1[1], me, "public", "pending")
        await seed_share(conn, oa, ov[0], other, "public", "live")
        await seed_share(conn, oa, ov[0], "owner", "public", "live")  # operator-made link on someone's artifact
        u = await store.usage(me)
        assert (u["artifact_count"], u["version_count"]) == (2, 3)  # retracted artifacts still count (D7)
        assert u["live_links"] == {"lab": 2, "public": 1}
        o = await store.usage(other)
        assert (o["artifact_count"], o["version_count"], o["live_links"]) == (1, 3, {"lab": 0, "public": 1})
        assert u["bytes_used"] == 0 and u["bytes_retracted"] == 0
        await conn.execute("UPDATE artifact_member_usage SET bytes_used=7, bytes_retracted=2"
                           " WHERE member_principal=$1", me)
        u = await store.usage(me)
        assert (u["bytes_used"], u["bytes_retracted"]) == (7, 2)
    run(migrated, body)

"""Collaborator grants and the intent ledger (migration 0045) on an isolated PostgreSQL with every migration applied."""
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from herdr_engineering.control_plane.artifact_collab_store import (
    CollabGrantStore,
    InviteGrantConflict,
)
from tests.control_plane.registry_pg import ROOT, registry_pg, uid  # noqa: F401 - imported fixture

GB5 = 5 * 1024 ** 3


@dataclass(frozen=True)
class Actor:
    kind: str
    id: str


def now():
    return datetime.now(UTC)


def run(pg, body):
    async def go():
        conn = await asyncpg.connect(pg["dsn"])
        try:
            return await body(CollabGrantStore(conn), conn)
        finally:
            await conn.close()
    return asyncio.run(go())


async def artifact(conn):
    aid = uid("art")
    await conn.execute("INSERT INTO artifacts (artifact_id, created_by) VALUES ($1, 'o')", aid)
    return aid


async def received(store, ticket, actor, aid="a", **kw):
    args = dict(kind="commit", artifact_id=aid, base_version_id=None, idempotency_key=None, request_sha256="h",
                bytes=10)
    args.update(kw)
    return await store.record_received(ticket, args.pop("kind"), actor, args.pop("artifact_id"),
                                       args.pop("base_version_id"), args.pop("idempotency_key"),
                                       args.pop("request_sha256"), args.pop("bytes"))


def test_migration_is_numbered_0045_and_applies_twice(registry_pg):
    async def body(store, conn):
        sql = (ROOT / "deploy/control-plane/migrations/0045_artifact_collab_bridge.sql").read_text()
        assert "-- DOWN:" in sql
        await conn.execute(sql)
        await conn.execute(sql)
        assert await conn.fetchval("SELECT count(*) FROM schema_migrations "
                                   "WHERE version='0045_artifact_collab_bridge.sql'") == 1
    run(registry_pg, body)


def test_grant_writes_acl_rows(registry_pg):
    async def body(store, conn):
        aid = await artifact(conn)
        row = await store.grant(aid, "a1", "read", expires_at=now() + timedelta(days=1), granted_by="o")
        assert row["permission"] == "read"
        got = await conn.fetchrow("SELECT permissions, principal_id FROM artifact_acl WHERE artifact_id=$1", aid)
        assert got["principal_id"] == "collab:a1"
        import json
        assert json.loads(got["permissions"]) == ["read"]
        await store.grant(aid, "a1", "write", expires_at=now() + timedelta(days=1), granted_by="o")
        got = await conn.fetchrow("SELECT permissions FROM artifact_acl WHERE artifact_id=$1", aid)
        assert json.loads(got["permissions"]) == ["read", "edit"]
        assert (await store.grant_for(aid, "a1"))["permission"] == "write"
        assert await store.grant_for(aid, "zz") is None
    run(registry_pg, body)


def test_grant_validation(registry_pg):
    async def body(store, conn):
        aid = await artifact(conn)
        with pytest.raises(ValueError):
            await store.grant(aid, "a1", "admin", expires_at=now() + timedelta(days=1), granted_by="o")
        with pytest.raises(ValueError):
            await store.grant(aid, "a1", "read", expires_at=now() + timedelta(days=91), granted_by="o")
        await store.grant(aid, "a1", "read", expires_at=now() + timedelta(days=89), granted_by="o")
    run(registry_pg, body)


def test_live_grants_and_revoke(registry_pg):
    async def body(store, conn):
        aid = await artifact(conn)
        await store.grant(aid, "live", "read", expires_at=now() + timedelta(hours=1), granted_by="o")
        await store.grant(aid, "dead", "read", expires_at=now() - timedelta(hours=1), granted_by="o")
        await conn.execute("INSERT INTO artifact_acl (artifact_id, principal_id, granted_by) VALUES ($1,'user:x','o')",
                           aid)
        live = [g for g in await store.live_grants() if g["artifact_id"] == aid]
        assert [(g["agent_id"], g["permission"]) for g in live] == [("live", "read")]
        assert live[0]["expires_at"] is not None
        assert await store.revoke(aid, "live") is True
        assert await store.revoke(aid, "live") is False
        assert await store.grant_for(aid, "live") is None
        assert await conn.fetchval("SELECT count(*) FROM artifact_acl WHERE principal_id='user:x'") >= 1
    run(registry_pg, body)


def test_revoke_write_and_all(registry_pg):
    async def body(store, conn):
        a, b = await artifact(conn), await artifact(conn)
        exp = now() + timedelta(days=1)
        await store.grant(a, "r", "read", expires_at=exp, granted_by="o")
        await store.grant(a, "w", "write", expires_at=exp, granted_by="o")
        await store.grant(b, "w", "write", expires_at=exp, granted_by="o")
        await conn.execute("INSERT INTO artifact_acl (artifact_id, principal_id, permissions, granted_by) "
                           "VALUES ($1,'user:x','[\"read\",\"edit\"]','o')", a)
        before = await conn.fetchval("SELECT count(*) FROM artifact_acl WHERE principal_id='user:x'")
        assert await store.revoke_write_grants(a) == 1
        assert await store.grant_for(a, "r") is not None and await store.grant_for(a, "w") is None
        assert await store.grant_for(b, "w") is not None
        assert await conn.fetchval("SELECT count(*) FROM artifact_acl WHERE principal_id='user:x'") == before
        await store.grant(a, "w", "write", expires_at=exp, granted_by="o")
        assert await store.revoke_all_grants(a) == 2
        assert await store.grant_for(a, "r") is None and await store.grant_for(b, "w") is not None
        assert await conn.fetchval("SELECT count(*) FROM artifact_acl WHERE principal_id='user:x'") == before
    run(registry_pg, body)


def test_record_received_is_idempotent_and_stores_digest(registry_pg):
    async def body(store, conn):
        t, actor = uid("t"), Actor("agent", uid("ag"))
        assert await received(store, t, actor, request_sha256="abc") is None
        again = await received(store, t, actor, request_sha256="different")
        assert again["request_sha256"] == "abc" and again["outcome"] == "received"
        await store.record_outcome(t, "created", version_id="v1", reason=None)
        row = await conn.fetchrow("SELECT * FROM artifact_collab_intents WHERE ticket_id=$1", t)
        assert row["outcome"] == "created" and row["version_id"] == "v1" and row["decided_at"] is not None
    run(registry_pg, body)


def test_bytes_last_24h(registry_pg):
    async def body(store, conn):
        ag = uid("ag")
        actor = Actor("agent", ag)
        t1, t2, t3, t4, t5 = (uid("t") for _ in range(5))
        await received(store, t1, actor, bytes=100)
        await store.record_outcome(t1, "created")
        await received(store, t2, actor, bytes=1000)
        await store.record_outcome(t2, "rejected", reason="no")
        await received(store, t3, actor, bytes=7)             # still 'received'
        await received(store, t4, actor, bytes=10_000)
        await store.record_outcome(t4, "created")
        await conn.execute("UPDATE artifact_collab_intents SET received_at = now() - interval '25 hours' "
                           "WHERE ticket_id=$1", t4)
        await received(store, t5, actor, kind="x.test", bytes=500)
        assert await store.bytes_last_24h(ag) == 100 + 7 + 500 - 500   # non-commit kind excluded
        assert await store.bytes_last_24h(ag, exclude_ticket=t3) == 100
        # a 5 GB verified upload counts once even when two tickets reference it
        await store.record_verified_upload(ag, "u1", artifact_id="a", name="big", sha256="s", size_bytes=GB5,
                                           storage_key="k", media_type="x/y", charged=True)
        await store.record_verified_upload(ag, "u1", artifact_id="a", name="big", sha256="s", size_bytes=GB5,
                                           storage_key="k", media_type="x/y", charged=True)
        assert await store.bytes_last_24h(ag) == 107 + GB5
        # uncharged uploads, or ones under another owner key, never count
        await store.record_verified_upload(ag, "u2", artifact_id="a", name="n", sha256="s", size_bytes=GB5,
                                           storage_key="k", media_type="x/y", charged=False)
        await store.record_verified_upload("m_x", "u3", artifact_id="a", name="n", sha256="s", size_bytes=GB5,
                                           storage_key="k", media_type="x/y", charged=False)
        assert await store.bytes_last_24h(ag) == 107 + GB5
        # an old verified upload ages out
        await conn.execute("UPDATE artifact_collab_uploads SET verified_at = now() - interval '25 hours' "
                           "WHERE owner_key=$1 AND upload_id='u1'", ag)
        assert await store.bytes_last_24h(ag) == 107
    run(registry_pg, body)


def test_verified_upload_is_idempotent(registry_pg):
    async def body(store, conn):
        ag = uid("ag")
        assert await store.verified_upload(ag, "u") is None
        first = await store.record_verified_upload(ag, "u", artifact_id="a", name="n", sha256="s", size_bytes=3,
                                                   storage_key="k", media_type="text/plain", charged=True)
        second = await store.record_verified_upload(ag, "u", artifact_id="a", name="other", sha256="z", size_bytes=9,
                                                    storage_key="k2", media_type="text/plain", charged=False)
        assert first["sha256"] == second["sha256"] == "s" and second["size_bytes"] == 3
        assert await conn.fetchval("SELECT count(*) FROM artifact_collab_uploads WHERE owner_key=$1", ag) == 1
        assert (await store.verified_upload(ag, "u"))["storage_key"] == "k"
    run(registry_pg, body)


def test_decided_for_key(registry_pg):
    async def body(store, conn):
        ag, other = Actor("agent", uid("ag")), Actor("agent", uid("ag"))
        key = uid("key")
        t, t2 = uid("t"), uid("t")
        await received(store, t, ag, idempotency_key=key, request_sha256="digest")
        assert await store.decided_for_key(ag, "commit", key, frozenset({"created"})) is None
        await store.record_outcome(t, "rejected")
        assert await store.decided_for_key(ag, "commit", key, frozenset({"created", "merged"})) is None
        await received(store, t2, ag, idempotency_key=key, request_sha256="digest")
        await store.record_outcome(t2, "created")
        t = t2
        row = await store.decided_for_key(ag, "commit", key, frozenset({"created", "merged"}))
        assert row["ticket_id"] == t and row["request_sha256"] == "digest"
        assert await store.decided_for_key(ag, "other", key, frozenset({"created"})) is None
        assert await store.decided_for_key(other, "commit", key, frozenset({"created"})) is None
    run(registry_pg, body)


def test_user_actor_with_custom_kind_and_outcome(registry_pg):
    async def body(store, conn):
        t = uid("t")
        await received(store, t, Actor("user", "u1"), kind="x.test", artifact_id=None)
        await store.record_outcome(t, "stored")
        assert await conn.fetchval("SELECT outcome FROM artifact_collab_intents WHERE ticket_id=$1", t) == "stored"
        t2 = uid("t")
        await received(store, t2, Actor("user", "u1"), kind="x.test", artifact_id=None)
        with pytest.raises(asyncpg.CheckViolationError):
            await store.record_outcome(t2, "Bad Outcome")
    run(registry_pg, body)


def test_invite_grants_resolve_capped_and_idempotent(registry_pg):
    async def body(store, conn):
        aid = await artifact(conn)
        far = now() + timedelta(days=30)
        soon = now() + timedelta(days=2)
        await store.add_invite_grant("inv1", aid, "write", expires_at=far, granted_by="o")
        await store.add_invite_grant("inv2", aid, "read", expires_at=soon, granted_by="o")
        assert sorted(g["invite_id"] for g in await store.pending_invite_grants(aid)) == ["inv1", "inv2"]
        joins = {"inv1": {"agent_id": "ag1", "hard_expiry": soon.timestamp()},
                 "inv2": {"agent_id": "ag2", "hard_expiry": far.timestamp()}}
        assert await store.resolve_invite_grants(joins) == 2
        assert await store.resolve_invite_grants(joins) == 0
        assert await store.pending_invite_grants(aid) == []
        g1 = await store.grant_for(aid, "ag1")
        assert g1["permission"] == "write" and abs((g1["expires_at"] - soon).total_seconds()) < 1
        g2 = await store.grant_for(aid, "ag2")
        assert g2["permission"] == "read" and abs((g2["expires_at"] - soon).total_seconds()) < 1
        assert await conn.fetchval("SELECT resolved_agent_id FROM artifact_collab_invite_grants "
                                   "WHERE invite_id='inv1'") == "ag1"
    run(registry_pg, body)


def test_collaborators_and_attempts(registry_pg):
    async def body(store, conn):
        aid = await artifact(conn)
        ag = uid("ag")
        actor = Actor("agent", ag)
        await store.grant(aid, ag, "write", expires_at=now() + timedelta(days=1), granted_by="o")
        tickets = {name: uid("t") for name in ("c1", "c2", "x", "r")}
        for name, outcome in (("c1", "created"), ("c2", "merged"), ("x", "conflict"), ("r", "rejected")):
            await received(store, tickets[name], actor, aid=aid)
            await store.record_outcome(tickets[name], outcome, reason="why" if outcome == "rejected" else None)
        await received(store, uid("t"), Actor("user", ag), aid=aid)          # user actors are not collaborators
        await conn.execute("INSERT INTO artifact_collab_attempts (attempt_id, artifact_id, collaborator_agent_id, "
                           "conflict_names, ticket_id) VALUES ($1,$2,$3,'[\"a.md\"]',$4)",
                           uid("att"), aid, ag, tickets["x"])
        rows = await store.collaborators(aid)
        assert len(rows) == 1
        row = rows[0]
        assert row["agent_id"] == ag and row["permission"] == "write"
        assert (row["commits"], row["conflicts"], row["rejected"]) == (2, 1, 1)
        assert row["last_used_at"] is not None
        got = await store.attempts(aid)
        assert {r["ticket_id"]: r["outcome"] for r in got} == {tickets["x"]: "conflict", tickets["r"]: "rejected"}
        by = {r["ticket_id"]: r for r in got}
        assert by[tickets["x"]]["conflict_names"] == ["a.md"] and by[tickets["r"]]["conflict_names"] == []
        assert len(await store.attempts(aid, limit=1)) == 1
    run(registry_pg, body)


def test_record_outcome_decides_once(registry_pg):
    async def body(store, conn):
        t = uid("t")
        await received(store, t, Actor("agent", uid("ag")))
        assert await store.record_outcome(t, "created", version_id="v1") is True
        assert await store.record_outcome(t, "rejected", reason="late") is False
        row = await conn.fetchrow("SELECT outcome, version_id, reason FROM artifact_collab_intents "
                                  "WHERE ticket_id=$1", t)
        assert (row["outcome"], row["version_id"], row["reason"]) == ("created", "v1", None)
        assert await store.record_outcome(uid("nope"), "created") is False
    run(registry_pg, body)


def test_resolve_is_atomic_and_retryable(registry_pg, monkeypatch):
    async def body(store, conn):
        aid = await artifact(conn)
        await store.add_invite_grant("inv-atomic", aid, "write", expires_at=now() + timedelta(days=5), granted_by="o")

        async def boom(*a, **k):
            raise RuntimeError("injected")
        monkeypatch.setattr(store, "_upsert_acl", boom)
        joins = {"inv-atomic": {"agent_id": "agx", "hard_expiry": (now() + timedelta(days=9)).timestamp()}}
        with pytest.raises(RuntimeError):
            await store.resolve_invite_grants(joins)
        assert [g["invite_id"] for g in await store.pending_invite_grants(aid)] == ["inv-atomic"]
        assert await store.grant_for(aid, "agx") is None
        monkeypatch.undo()
        assert await store.resolve_invite_grants(joins) == 1
        assert (await store.grant_for(aid, "agx"))["permission"] == "write"
    run(registry_pg, body)


def test_resolve_never_downgrades(registry_pg):
    async def body(store, conn):
        aid = await artifact(conn)
        late, soon = now() + timedelta(days=40), now() + timedelta(days=2)
        await store.grant(aid, "ag", "write", expires_at=late, granted_by="o")
        await store.add_invite_grant("inv-d1", aid, "read", expires_at=soon, granted_by="o")
        assert await store.resolve_invite_grants({"inv-d1": {"agent_id": "ag"}}) == 1
        g = await store.grant_for(aid, "ag")
        assert g["permission"] == "write" and abs((g["expires_at"] - late).total_seconds()) < 1
        # a stronger, longer invite upgrades both
        await store.add_invite_grant("inv-d2", aid, "write", expires_at=now() + timedelta(days=60), granted_by="o")
        await store.grant(aid, "ag2", "read", expires_at=soon, granted_by="o")
        await store.add_invite_grant("inv-d3", aid, "write", expires_at=late, granted_by="o")
        await store.resolve_invite_grants({"inv-d3": {"agent_id": "ag2"}})
        g2 = await store.grant_for(aid, "ag2")
        assert g2["permission"] == "write" and abs((g2["expires_at"] - late).total_seconds()) < 1
        # the later expiry is still capped at 90 days from now
        await conn.execute("UPDATE artifact_acl SET expires_at = now() + interval '200 days' "
                           "WHERE artifact_id=$1 AND principal_id='collab:ag2'", aid)
        await store.add_invite_grant("inv-d4", aid, "read", expires_at=soon, granted_by="o")
        await store.resolve_invite_grants({"inv-d4": {"agent_id": "ag2"}})
        g2 = await store.grant_for(aid, "ag2")
        assert g2["expires_at"] <= now() + timedelta(days=90, minutes=1)
        # an expired existing write grant does not keep write
        await conn.execute("UPDATE artifact_acl SET expires_at = now() - interval '1 day' "
                           "WHERE artifact_id=$1 AND principal_id='collab:ag'", aid)
        await store.add_invite_grant("inv-d5", aid, "read", expires_at=soon, granted_by="o")
        await store.resolve_invite_grants({"inv-d5": {"agent_id": "ag"}})
        assert (await store.grant_for(aid, "ag"))["permission"] == "read"
    run(registry_pg, body)


def test_lapsed_invite_resolves_without_grant(registry_pg):
    async def body(store, conn):
        aid = await artifact(conn)
        await store.add_invite_grant("inv-old", aid, "read", expires_at=now() + timedelta(days=1), granted_by="o")
        await conn.execute("UPDATE artifact_collab_invite_grants SET expires_at = now() - interval '1 hour' "
                           "WHERE invite_id='inv-old'")
        assert await store.resolve_invite_grants({"inv-old": {"agent_id": "agold"}}) == 0
        assert await store.pending_invite_grants(aid) == []
        assert await store.grant_for(aid, "agold") is None
    run(registry_pg, body)


def test_hard_expiry_parsing():
    from herdr_engineering.control_plane.artifact_collab_store import _moment
    want = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert _moment("2026-11-01T12:00:00Z") == want
    assert _moment(datetime(2026, 11, 1, 12, 0)) == want
    assert _moment(want) == want
    assert _moment(want.timestamp()) == want
    for bad in (True, want.timestamp() * 1000, datetime(3001, 1, 1, tzinfo=UTC), "not a date"):
        with pytest.raises(ValueError):
            _moment(bad)


def test_attempts_ticket_index_exists(registry_pg):
    async def body(store, conn):
        assert await conn.fetchval("SELECT count(*) FROM pg_indexes WHERE tablename='artifact_collab_attempts' "
                                   "AND indexname='artifact_collab_attempts_ticket_idx'") == 1
    run(registry_pg, body)


def test_add_invite_grant_is_idempotent_for_the_same_grant_and_refuses_any_other(registry_pg):
    async def body(store, conn):
        a, b = await artifact(conn), await artifact(conn)
        far = now() + timedelta(days=30)
        first = await store.add_invite_grant("inv-x", a, "read", expires_at=far, granted_by="o")
        again = await store.add_invite_grant("inv-x", a, "read", expires_at=now() + timedelta(days=5), granted_by="o")
        assert again["invite_id"] == "inv-x" and abs((again["expires_at"] - first["expires_at"]).total_seconds()) < 1
        for other in ((b, "read", "o"), (a, "write", "o"), (a, "read", "someone-else")):
            with pytest.raises(InviteGrantConflict):
                await store.add_invite_grant("inv-x", other[0], other[1], expires_at=far, granted_by=other[2])
        rows = await conn.fetch("SELECT artifact_id, permission FROM artifact_collab_invite_grants "
                                "WHERE invite_id='inv-x'")
        assert [(r["artifact_id"], r["permission"]) for r in rows] == [(a, "read")]
        assert await store.pending_invite_grants(b) == []
    run(registry_pg, body)


def test_revoke_neutralises_pending_invites_in_scope_and_they_never_convert(registry_pg):
    async def body(store, conn):
        a, b = await artifact(conn), await artifact(conn)
        far = now() + timedelta(days=5)
        for inv, aid, perm in (("iw", a, "write"), ("ir", a, "read"), ("ow", b, "write"), ("or", b, "read")):
            await store.add_invite_grant(f"{inv}-{aid}", aid, perm, expires_at=far, granted_by="o")
        await store.grant(a, "w1", "write", expires_at=far, granted_by="o")
        await store.grant(a, "r1", "read", expires_at=far, granted_by="o")
        assert await store.revoke_grants(a, write_only=True) == {"grants": 1, "pending_invites": 1}
        assert [g["permission"] for g in await store.pending_invite_grants(a)] == ["read"]      # read invite survives
        assert len(await store.pending_invite_grants(b)) == 2                                    # other artifact untouched
        joins = {f"iw-{a}": {"agent_id": "n1"}, f"ir-{a}": {"agent_id": "n2"}}
        assert await store.resolve_invite_grants(joins) == 1
        assert await store.grant_for(a, "n1") is None and (await store.grant_for(a, "n2"))["permission"] == "read"
        assert await store.revoke_all_grants(a) == 2                                             # n2 and r1
        assert await store.pending_invite_grants(a) == [] and len(await store.pending_invite_grants(b)) == 2
        assert await store.add_invite_grant(f"iw-{a}", a, "write", expires_at=far, granted_by="o")   # id stays reserved
        assert await store.pending_invite_grants(a) == []
    run(registry_pg, body)


def test_resolve_skips_invites_of_a_retracted_artifact(registry_pg):
    async def body(store, conn):
        aid = await artifact(conn)
        await store.add_invite_grant("inv-ret", aid, "write", expires_at=now() + timedelta(days=5), granted_by="o")
        await conn.execute("UPDATE artifacts SET lifecycle='retracted' WHERE artifact_id=$1", aid)
        assert await store.resolve_invite_grants({"inv-ret": {"agent_id": "ag"}}) == 0
        assert await store.grant_for(aid, "ag") is None
    run(registry_pg, body)


def test_bytes_last_24h_can_leave_queued_received_tickets_out(registry_pg):
    async def body(store, conn):
        ag = uid("ag")
        actor = Actor("agent", ag)
        t1, t2, t3 = (uid("t") for _ in range(3))
        await received(store, t1, actor, bytes=100)
        await store.record_outcome(t1, "created")
        await received(store, t2, actor, bytes=7)             # queued, nothing stored yet
        await received(store, t3, actor, bytes=9)
        await store.record_outcome(t3, "rejected", reason="no")
        assert await store.bytes_last_24h(ag) == 107
        assert await store.bytes_last_24h(ag, count_received=False) == 100
    run(registry_pg, body)


def test_expired_grants_and_lapsed_invites_are_not_listed(registry_pg):
    async def body(store, conn):
        aid = await artifact(conn)
        live_inv, old_inv = uid("inv"), uid("inv")
        await store.grant(aid, "live", "read", expires_at=now() + timedelta(days=1), granted_by="o")
        await store.grant(aid, "gone", "read", expires_at=now() + timedelta(days=1), granted_by="o")
        await conn.execute("UPDATE artifact_acl SET expires_at = now() - interval '1 minute' "
                           "WHERE artifact_id=$1 AND principal_id='collab:gone'", aid)
        await store.add_invite_grant(live_inv, aid, "read", expires_at=now() + timedelta(days=1), granted_by="o")
        await store.add_invite_grant(old_inv, aid, "read", expires_at=now() + timedelta(days=1), granted_by="o")
        await conn.execute("UPDATE artifact_collab_invite_grants SET expires_at = now() - interval '1 minute' "
                           "WHERE invite_id=$1", old_inv)
        assert [r["agent_id"] for r in await store.collaborators(aid)] == ["live"]
        assert [g["invite_id"] for g in await store.pending_invite_grants(aid)] == [live_inv]
    run(registry_pg, body)


def test_room_member_grants_are_not_agent_collaborators_and_survive_agent_revokes(registry_pg):
    async def body(store, conn):
        aid = await artifact(conn)
        await store.grant(aid, "ag1", "write", expires_at=now() + timedelta(days=1), granted_by="o")
        await conn.execute("INSERT INTO artifact_acl (artifact_id, principal_id, permissions, expires_at, granted_by) "
                           "VALUES ($1,'collab:user:bob','[\"read\"]'::jsonb, now() + interval '1 day', 'room:r1')", aid)
        assert [r["agent_id"] for r in await store.collaborators(aid)] == ["ag1"]
        assert all(g["agent_id"] != "user:bob" for g in await store.live_grants())
        await store.revoke_grants(aid, write_only=False)
        await store.revoke_grants(aid, write_only=True)
        assert await conn.fetchval("SELECT count(*) FROM artifact_acl WHERE principal_id='collab:user:bob'") == 1
        assert await store.grant_for(aid, "user:bob") is None
        with pytest.raises(ValueError):
            await store.revoke(aid, "user:bob")
        with pytest.raises(ValueError):
            await store.grant(aid, "user:bob", "read", expires_at=now() + timedelta(days=1), granted_by="o")
    run(registry_pg, body)


def test_a_room_revoke_leaves_agent_grants(registry_pg):
    from herdr_engineering.control_plane.artifact_room_access import revoke_room_grants_pg

    async def body(store, conn):
        aid = await artifact(conn)
        await store.grant(aid, "ag1", "read", expires_at=now() + timedelta(days=1), granted_by="o")
        await conn.execute("INSERT INTO artifact_acl (artifact_id, principal_id, permissions, expires_at, granted_by) "
                           "VALUES ($1,'collab:user:bob','[\"read\"]'::jsonb, now() + interval '1 day', 'room:r1')", aid)
        await revoke_room_grants_pg(conn, apply=True)
        assert [r["agent_id"] for r in await store.collaborators(aid)] == ["ag1"]
    run(registry_pg, body)

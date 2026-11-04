"""Member-created shares: share permission, audience rules, own caps and audit (plan 5, Task 2).

Real PostgreSQL for the store; the approve path runs the real ``approve_share`` against a real ``ShareStore`` behind
``SyncArtifactAdapter`` (the artifact registry / blob store are the in-memory fakes ``test_artifact_share_api`` uses).
"""
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import asyncpg
import pytest

from herdr_engineering.control_plane.artifact_adapter import SyncArtifactAdapter
from herdr_engineering.control_plane.artifact_share_api import ShareHTTPError, approve_share
from herdr_engineering.control_plane.artifact_shares import ShareError, ShareStore, new_token
from herdr_engineering.control_plane.share_bundle import BundleFile, BundleIndex
from tests.control_plane.test_artifact_share_api import AID, MIB, Bytes, Registry
from tests.control_plane.test_share_store import MIGRATIONS, run, share_postgres  # noqa: F401  (fixture)

OPERATOR = "api-key:0123456789abcdef:operator"


@pytest.fixture
def migrated(share_postgres):  # noqa: F811
    async def go():
        conn = await asyncpg.connect(share_postgres)
        try:
            await conn.execute((MIGRATIONS / "0047_member_artifacts.sql").read_text())
        finally:
            await conn.close()
    asyncio.run(go())
    return share_postgres


async def seed(conn, owner, *, artifact_id=None):
    aid, vid = artifact_id or "artifact_" + uuid.uuid4().hex, "version_" + uuid.uuid4().hex
    await conn.execute("INSERT INTO artifacts (artifact_id,created_by) VALUES ($1,$2)", aid, owner)
    await conn.execute("INSERT INTO artifact_versions (version_id,artifact_id,version_number,state) "
                       "VALUES ($1,$2,1,'ready')", vid, aid)
    return aid, vid


async def grant(conn, aid, principal, permissions, *, expires=None):
    await conn.execute(
        "INSERT INTO artifact_acl (artifact_id,principal_id,permissions,expires_at,granted_by) "
        "VALUES ($1,$2,$3::jsonb,$4,'member:alice') ON CONFLICT (artifact_id,principal_id) DO UPDATE SET "
        "permissions=EXCLUDED.permissions, expires_at=EXCLUDED.expires_at", aid, principal,
        json.dumps(permissions), expires)


def pub_kw(aid, vid, who, **over):
    base = dict(artifact_id=aid, version_id=vid, audience="public",
                expires_at=datetime.now(UTC) + timedelta(days=30), created_by=who)
    base.update(over)
    return base


def lab_kw(aid, vid, who, **over):
    base = dict(artifact_id=aid, version_id=vid, audience="lab", allow_emails=["a@x.org"], created_by=who,
                member_groups=frozenset({"lab"}), member_emails=frozenset({"a@x.org"}))
    base.update(over)
    return base


def index(size=10):
    return BundleIndex("index.html", [BundleFile("index.html", "a" * 64, size, "text/html")], size, [])


async def member_events(conn, principal):
    return await conn.fetch("SELECT * FROM artifact_member_events WHERE member_principal=$1 ORDER BY event_id",
                            principal)


async def make_live(store, row, who="member:alice", size=10):
    _, h = new_token()
    return await store.mark_live(row["share_id"], token_hash=h, index=index(size), approved_by=who)


def test_operator_share_on_operator_artifact_unchanged(migrated):
    async def body(store, conn):
        aid, vid = await seed(conn, OPERATOR)
        row = await store.create(**pub_kw(aid, vid, OPERATOR))
        assert row["state"] == "pending"
        row = await store.create(artifact_id=aid, version_id=vid, audience="lab", allow_emails=["z@x.org"],
                                 created_by=OPERATOR)   # operator needs no member_* args
        assert row["state"] == "pending"
        assert await conn.fetchval("SELECT count(*) FROM artifact_member_events WHERE share_id=$1",
                                   row["share_id"]) == 0
    run(migrated, body)


def test_member_public_on_own_artifact_one_event_per_step(migrated):
    async def body(store, conn):
        aid, vid = await seed(conn, "member:alice")
        row = await store.create(**pub_kw(aid, vid, "member:alice"))
        assert row["state"] == "pending" and row["created_by"] == "member:alice"
        live = await make_live(store, row)
        assert live["state"] == "live"
        assert (live["expires_at"] - datetime.now(UTC)) <= timedelta(days=90)
        await store.mark_revoked(row["share_id"], actor="member:alice")
        ev = [e for e in await member_events(conn, "member:alice") if e["share_id"] == row["share_id"]]
        assert [(e["action"], e["outcome"], e["audience"]) for e in ev] == [
            ("share_create", "ok", "public"), ("share_approve", "ok", "public"), ("share_revoke", "ok", "public")]
        assert all(e["artifact_id"] == aid for e in ev)
    run(migrated, body)


def test_member_public_default_rules_follow_operator_rules(migrated):
    async def body(store, conn):
        aid, vid = await seed(conn, "member:alice")
        for bad in (dict(expires_at=None), dict(expires_at=datetime.now(UTC) + timedelta(days=91))):
            with pytest.raises(ShareError):
                await store.create(**pub_kw(aid, vid, "member:alice", **bad))
        refused = [e for e in await member_events(conn, "member:alice") if e["artifact_id"] == aid]
        assert len(refused) == 2 and {e["outcome"] for e in refused} == {"refused"}
        assert {e["audience"] for e in refused} == {"public"}
    run(migrated, body)


@pytest.mark.parametrize("audience", ["lab", "public"])
@pytest.mark.parametrize("perms", [["read"], ["edit"], ["review"], ["read", "edit", "review"]])
def test_member_without_share_grant_is_not_found(migrated, audience, perms):
    async def body(store, conn):
        aid, vid = await seed(conn, "member:alice")
        await grant(conn, aid, "member:bob", perms)
        kw = lab_kw(aid, vid, "member:bob") if audience == "lab" else pub_kw(aid, vid, "member:bob")
        with pytest.raises(ShareError) as exc:
            await store.create(**kw)
        assert exc.value.code == "not_found" and str(exc.value) == "not found"
        assert await conn.fetchval("SELECT count(*) FROM artifact_shares WHERE artifact_id=$1", aid) == 0
        ev = await member_events(conn, "member:bob")
        assert ev[-1]["outcome"] == "refused" and ev[-1]["code"] == "not_found" and ev[-1]["audience"] == audience
    run(migrated, body)


def test_member_without_any_grant_or_unknown_artifact_is_not_found(migrated):
    async def body(store, conn):
        aid, vid = await seed(conn, "member:alice")
        with pytest.raises(ShareError) as exc:
            await store.create(**pub_kw(aid, vid, "member:nobody"))
        assert exc.value.code == "not_found"
        with pytest.raises(ShareError) as exc:
            await store.create(**pub_kw("artifact_missing", vid, "member:alice"))
        assert exc.value.code == "not_found"
    run(migrated, body)


@pytest.mark.parametrize("perms", [["share"], ["admin"]])
def test_member_with_share_or_admin_grant_may_share(migrated, perms):
    async def body(store, conn):
        aid, vid = await seed(conn, "member:alice")
        await grant(conn, aid, "member:bob", perms)
        for kw in (pub_kw(aid, vid, "member:bob"), lab_kw(aid, vid, "member:bob")):
            assert (await store.create(**kw))["state"] == "pending"
    run(migrated, body)


def test_expired_share_grant_is_refused(migrated):
    async def body(store, conn):
        aid, vid = await seed(conn, "member:alice")
        await grant(conn, aid, "member:bob", ["share"], expires=datetime.now(UTC) - timedelta(minutes=1))
        with pytest.raises(ShareError) as exc:
            await store.create(**pub_kw(aid, vid, "member:bob"))
        assert exc.value.code == "not_found"
    run(migrated, body)


def test_grant_revoked_between_create_and_approve_refuses_mark_live(migrated):
    async def body(store, conn):
        aid, vid = await seed(conn, "member:alice")
        await grant(conn, aid, "member:bob", ["share"])
        row = await store.create(**pub_kw(aid, vid, "member:bob"))
        await conn.execute("DELETE FROM artifact_acl WHERE artifact_id=$1 AND principal_id='member:bob'", aid)
        with pytest.raises(ShareError) as exc:
            await make_live(store, row, "member:bob")
        assert exc.value.code == "not_found"
        assert (await store.get(row["share_id"]))["state"] == "pending"
        ev = await member_events(conn, "member:bob")
        assert (ev[-1]["action"], ev[-1]["outcome"], ev[-1]["code"]) == ("share_approve", "refused", "not_found")
    run(migrated, body)


def test_operator_may_share_member_owned_artifact(migrated):
    async def body(store, conn):
        aid, vid = await seed(conn, "member:alice")
        for kw in (pub_kw(aid, vid, OPERATOR), dict(artifact_id=aid, version_id=vid, audience="lab",
                                                    allow_emails=["z@x.org"], created_by=OPERATOR)):
            row = await store.create(**kw)
            assert (await make_live(store, row, OPERATOR))["state"] == "live"
        assert await conn.fetchval("SELECT count(*) FROM artifact_member_events WHERE artifact_id=$1", aid) == 0
    run(migrated, body)


def test_member_lab_audience_must_sit_inside_member_scope(migrated):
    async def body(store, conn):
        aid, vid = await seed(conn, "member:alice")
        who = "member:alice"
        assert (await store.create(**lab_kw(aid, vid, who, allow_emails=[], allow_groups=[" lab "])))["allow_groups"] == ["lab"]
        assert (await store.create(**lab_kw(aid, vid, who, allow_emails=["A@X.org"])))["allow_emails"] == ["a@x.org"]
        for bad in (dict(allow_emails=[], allow_groups=["other"]),
                    dict(allow_emails=[], allow_groups=["lab", "other"]),
                    dict(allow_emails=[], allow_groups=["LAB"]),
                    dict(allow_emails=["b@x.org"]),
                    dict(allow_emails=["a@x.org", "b@x.org"]),
                    dict(member_groups=None, allow_emails=[], allow_groups=["lab"]),
                    dict(member_emails=None),
                    dict(member_groups=None, member_emails=None)):
            with pytest.raises(ShareError) as exc:
                await store.create(**lab_kw(aid, vid, who, **bad))
            assert exc.value.code == "member_audience_refused", bad
        ev = await member_events(conn, who)
        refused = [e for e in ev if e["artifact_id"] == aid and e["outcome"] == "refused"]
        assert len(refused) == 8 and {e["code"] for e in refused} == {"member_audience_refused"}
        assert all(e["audience"] == "lab" for e in refused)
    run(migrated, body)


def test_member_public_ignores_member_scope(migrated):
    async def body(store, conn):
        aid, vid = await seed(conn, "member:alice")
        assert (await store.create(**pub_kw(aid, vid, "member:alice")))["audience"] == "public"
    run(migrated, body)


def test_live_totals_split_by_creator_class(migrated):
    async def body(store, conn):
        await conn.execute("DELETE FROM artifact_share_events; DELETE FROM artifact_shares")
        aid, vid = await seed(conn, "member:alice")
        m = await store.create(**pub_kw(aid, vid, "member:alice"))
        o = await store.create(**pub_kw(aid, vid, OPERATOR))
        pending = await store.create(**pub_kw(aid, vid, "member:alice"))
        await make_live(store, m, size=100)
        await make_live(store, o, OPERATOR, size=7)
        assert await store.live_total_bytes(member=True) == 100
        assert await store.live_total_bytes(member=False) == 7
        assert pending["state"] == "pending"
    run(migrated, body)


def test_revoke_of_member_share_records_member_event_with_revoker(migrated):
    async def body(store, conn):
        aid, vid = await seed(conn, "member:alice")
        row = await store.create(**pub_kw(aid, vid, "member:alice"))
        await store.mark_revoked(row["share_id"], actor=OPERATOR)
        await store.mark_revoked(row["share_id"], actor=OPERATOR)   # idempotent: no second event
        ev = [e for e in await member_events(conn, "member:alice") if e["action"] == "share_revoke" and e["share_id"] == row["share_id"]]
        assert len(ev) == 1 and ev[0]["share_id"] == row["share_id"] and ev[0]["audience"] == "public"
        actor = ev[0]["actor"]
        actor = json.loads(actor) if isinstance(actor, str) else actor
        assert actor["id"] == OPERATOR and actor["kind"] == "operator"
        op = await store.create(**pub_kw(aid, vid, OPERATOR))
        await store.mark_revoked(op["share_id"], actor=OPERATOR)
        assert await conn.fetchval("SELECT count(*) FROM artifact_member_events WHERE share_id=$1",
                                   op["share_id"]) == 0
    run(migrated, body)


def test_spend_lock_is_exclusive_across_connections(migrated):
    async def go():
        a, b = await asyncpg.connect(migrated), await asyncpg.connect(migrated)
        try:
            sa, sb = ShareStore(a), ShareStore(b)
            order = []

            async def second():
                async with sb.spend_lock():
                    order.append("b")

            async with sa.spend_lock():
                task = asyncio.create_task(second())
                await asyncio.sleep(0.5)
                assert order == []          # b is blocked while a holds the lock
                order.append("a")
            await asyncio.wait_for(task, 5)
            assert order == ["a", "b"]
        finally:
            await a.close()
            await b.close()
    asyncio.run(go())


# ---------------------------------------------------------------------------------------- approve path


class Env:
    def __init__(self, dsn, tmp_path, **settings):
        self.dsn = dsn
        blobs = Bytes()
        self.registry = Registry(blobs)
        self.adapter = SyncArtifactAdapter(self._factory)
        config = {"public_url": "https://share.example/", "max_share_bytes": 512 * MIB, "max_files": 20000,
                  "max_total_bytes": 2048 * MIB, "default_public_days": 30, **settings}
        root = tmp_path / "shares"
        root.mkdir()
        self.root = root
        self.server = SimpleNamespace(share_settings=config, shares_root=root, share_store=self.adapter,
                                      artifact_store=self.registry, rustfs=blobs)

    async def _factory(self):
        return ShareStore(await asyncpg.connect(self.dsn))

    def create(self, who, **over):
        kw = dict(artifact_id=AID, version_id="v_html", audience="public",
                  expires_at=datetime.now(UTC) + timedelta(days=30), created_by=who)
        kw.update(over)
        return self.adapter("create", **kw)

    def approve(self, row, who):
        return approve_share(self.server, self.adapter("get", row["share_id"]), approved_by=who)


@pytest.fixture
def aenv(migrated, tmp_path):
    async def setup():
        conn = await asyncpg.connect(migrated)
        try:
            await conn.execute("DELETE FROM artifact_share_events; DELETE FROM artifact_shares")
            await conn.execute("DELETE FROM artifact_acl WHERE artifact_id=$1", AID)
            await conn.execute("DELETE FROM artifact_versions WHERE artifact_id=$1", AID)
            await conn.execute("DELETE FROM artifacts WHERE artifact_id=$1", AID)
            await conn.execute("INSERT INTO artifacts (artifact_id,created_by) VALUES ($1,'member:alice')", AID)
            for number, vid in enumerate(("v_html", "v_pdf"), 1):
                await conn.execute("INSERT INTO artifact_versions (version_id,artifact_id,version_number,state) "
                                   "VALUES ($1,$2,$3,'ready')", vid, AID, number)
        finally:
            await conn.close()
    asyncio.run(setup())
    made = []

    def factory(**settings):
        env = Env(migrated, tmp_path, **settings)
        made.append(env)
        return env
    yield factory
    for env in made:
        env.adapter.close()


def test_member_approve_goes_live_and_leaves_bundle(aenv):
    env = aenv()
    row = env.create("member:alice")
    url, summary = env.approve(row, "member:alice")
    assert url.startswith("https://share.example/s/") and summary["audience"] == "public"
    live = env.adapter("get", row["share_id"])
    assert live["state"] == "live" and (live["expires_at"] - datetime.now(UTC)) <= timedelta(days=90)
    assert (env.root / row["share_id"]).is_dir()


def test_grant_revoked_before_approve_leaves_no_bundle(aenv):
    env = aenv()
    async def setup():
        conn = await asyncpg.connect(env.dsn)
        try:
            await grant(conn, AID, "member:bob", ["share"])
        finally:
            await conn.close()
    asyncio.run(setup())
    row = env.create("member:bob")

    async def revoke():
        conn = await asyncpg.connect(env.dsn)
        try:
            await conn.execute("DELETE FROM artifact_acl WHERE artifact_id=$1", AID)
        finally:
            await conn.close()
    asyncio.run(revoke())
    with pytest.raises(ShareHTTPError) as exc:
        env.approve(row, "member:bob")
    assert exc.value.status == 404
    assert env.adapter("get", row["share_id"])["state"] == "pending"
    assert not (env.root / row["share_id"]).exists() and list(env.root.glob("by-token/*")) == []


def test_member_share_limits_cap_members_only(aenv):
    env = aenv(max_share_bytes=512 * MIB, member_share_limits={"max_share_bytes": 50, "max_total_bytes": None})
    big_member = env.create("member:alice")
    with pytest.raises(ShareHTTPError) as exc:
        env.approve(big_member, "member:alice")
    assert exc.value.status == 400 and "50" in exc.value.message
    assert env.adapter("get", big_member["share_id"])["state"] == "pending"
    assert not (env.root / big_member["share_id"]).exists()
    operator = env.create(OPERATOR)           # the same size passes the (much larger) global cap
    env.approve(operator, OPERATOR)
    assert env.adapter("get", operator["share_id"])["state"] == "live"


def test_null_share_limits_do_not_apply_global_caps_to_members(aenv):
    env = aenv(max_share_bytes=50, max_total_bytes=60, member_share_limits={"max_share_bytes": None,
                                                                            "max_total_bytes": None})
    member = env.create("member:alice")
    env.approve(member, "member:alice")       # bigger than the global 50-byte cap
    assert env.adapter("get", member["share_id"])["state"] == "live"
    operator = env.create(OPERATOR)
    with pytest.raises(ShareHTTPError) as exc:
        env.approve(operator, OPERATOR)       # the operator is still held to the global cap
    assert exc.value.status == 400


@pytest.mark.parametrize("operator_first", [True, False])
def test_member_and_operator_totals_are_capped_independently(aenv, operator_first):
    one = 13   # v_pdf is 13 bytes
    env = aenv(max_total_bytes=one, member_share_limits={"max_share_bytes": None, "max_total_bytes": one})
    m1, m2 = env.create("member:alice", version_id="v_pdf"), env.create("member:alice", version_id="v_pdf")
    o1, o2 = env.create(OPERATOR, version_id="v_pdf"), env.create(OPERATOR, version_id="v_pdf")
    first = [(o1, OPERATOR), (m1, "member:alice")]
    for row, who in (first if operator_first else first[::-1]):
        env.approve(row, who)                 # neither class eats the other's budget, in either order
    for second, who in ((m2, "member:alice"), (o2, OPERATOR)):
        with pytest.raises(ShareHTTPError) as exc:
            env.approve(second, who)
        assert exc.value.status == 400 and "total cap" in exc.value.message
        assert not (env.root / second["share_id"]).exists()


# ------------------------------------------------------------------------------------- review fixes


@pytest.mark.parametrize("who", ["collab:alice", "collab:", "member:bad user", "member:"])
def test_collab_and_malformed_member_principals_never_create_shares(migrated, who):
    async def body(store, conn):
        aid, vid = await seed(conn, who)          # even the "owner" of the artifact
        for kw in (pub_kw(aid, vid, who), lab_kw(aid, vid, who)):
            with pytest.raises(ShareError) as exc:
                await store.create(**kw)
            assert exc.value.code == "not_found" and str(exc.value) == "not found"
        assert await conn.fetchval("SELECT count(*) FROM artifact_shares WHERE artifact_id=$1", aid) == 0
    run(migrated, body)


def test_member_scope_is_case_insensitive_and_refuses_bare_strings(migrated):
    async def body(store, conn):
        aid, vid = await seed(conn, "member:alice")
        row = await store.create(**lab_kw(aid, vid, "member:alice", allow_emails=["A@X.org"],
                                          member_emails=frozenset({" A@x.ORG "})))
        assert row["allow_emails"] == ["a@x.org"]
        for bad in (dict(member_groups="lab"), dict(member_emails="a@x.org")):
            with pytest.raises(TypeError):
                await store.create(**lab_kw(aid, vid, "member:alice", **bad))
    run(migrated, body)


class Handler:
    """The four things ``_create`` uses from the HTTP handler; the store, planner and settings behind it are real."""

    def __init__(self, principal, body):
        self.headers = {"Content-Type": "application/json"}
        self._body, self._principal, self.sent = body, principal, None

    def _read_json(self):
        return self._body

    def _resource_principal(self):
        return self._principal

    def _json(self, value, status=200):
        self.sent = (status, value)


def test_create_route_member_lab_share_is_refused_without_scope_and_skips_owner_merge(aenv):
    from herdr_engineering.control_plane.artifact_share_api import _create
    env = aenv(owner_emails=["boss@x.org"])
    body = {"version_id": "v_html", "audience": "lab", "allow_emails": ["a@x.org"]}
    with pytest.raises(ShareError) as exc:       # the HTTP path passes no member scope: fail closed
        _create(Handler("member:alice", dict(body)), env.server, AID)
    assert exc.value.code == "member_audience_refused"
    assert env.adapter("list_for_artifact", AID) == []
    # the same request by an operator still gets the owner merge
    h = Handler(OPERATOR, dict(body))
    _create(h, env.server, AID)
    assert h.sent[0] == 201 and h.sent[1]["share"]["allow_emails"] == ["a@x.org", "boss@x.org"]


def test_create_route_member_public_share_works_and_maps_errors(aenv):
    from herdr_engineering.control_plane.artifact_share_api import _create
    env = aenv()
    h = Handler("member:alice", {"version_id": "v_html", "audience": "public"})
    _create(h, env.server, AID)
    assert h.sent[0] == 201 and h.sent[1]["share"]["created_by"] == "member:alice"
    with pytest.raises(ShareError) as exc:       # bob has no grant on alice's artifact
        _create(Handler("member:bob", {"version_id": "v_html", "audience": "public"}), env.server, AID)
    assert exc.value.code == "not_found"

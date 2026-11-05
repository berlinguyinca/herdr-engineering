# ruff: noqa: F811  (imported pytest fixtures are used as test arguments)
"""Plan 5, Task 15: the abuse and security sweep of member-owned artifacts.

One test per line of the brief, on the real stack: real Postgres (the isolated server of ``registry_pg``) behind the
real bridge worker for everything the database decides, a real ``CollabService`` + ``MemberService`` + ``MemberWeb`` on
real loopback sockets for the front doors, the real operator HTTP server for the operator's view.  No mocks.

Sections: A worker (Postgres), B front doors (MCP tools and the web API), C operator and public side,
D limits and switches, E parked risk items."""
from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from herdr_engineering.control_plane import collab_member_tools
from herdr_engineering.control_plane.artifact_member_service import MemberServiceError, member_actor
from herdr_engineering.control_plane.artifact_members import member_owner_key
from herdr_engineering.control_plane.artifact_shares import ShareError, ShareStore
from herdr_engineering.control_plane.artifacts import ArtifactStore  # noqa: F401
from herdr_engineering.control_plane.collab import CollabError
from herdr_engineering.control_plane.collab_bridge_files import upload_id_for
from herdr_engineering.control_plane.members_config import parse_members
from herdr_engineering.control_plane.share_session import mint_session as mint_share_session
from tests.control_plane import test_artifact_members_api as opapi
from tests.control_plane.registry_pg import registry_pg  # noqa: F401 - imported fixture
from tests.control_plane.test_artifact_member_intents import (
    ALICE,
    BOB,
    HTML,
    publish_payload,
    send,
    user,
    view_of,
    write_feeds,
)
from tests.control_plane.test_artifact_member_intents import agent as agent_actor
from tests.control_plane.test_artifact_member_intents import create as create_artifact
from tests.control_plane.test_artifact_member_service import ART, DATA, GHOST, OTHER, refuse
from tests.control_plane.test_artifact_members_api import BASE_BODY, env, retract  # noqa: F401 - env is a fixture
from tests.control_plane.test_collab_intents import Clock, settle, sql, stage
from tests.control_plane.test_member_share_flow import (
    DIRECTORY,
    approve,
    db_text,
    grant,
    published,
    secret_of,
    share,
    srun,
    token_of,
)
from tests.control_plane.test_member_web import (  # noqa: F401 - stack is a fixture
    HOST,
    ORIGIN,
    SECRET,
    exchange,
    req,
    session,
    stack,
)
from tests.control_plane.test_share_annotate import api, good_headers
from tests.control_plane.test_share_annotate import session as share_session
from tests.control_plane.test_share_lab import lab_config
from tests.control_plane.test_share_public import TOKEN
from tests.control_plane.test_share_public import Env as ShareEnv

GHOST_ART = "art_nothere1"
GHOST_SHARE = "share_" + "0" * 24


def verdict(out: dict) -> dict:
    """The part of a worker result a member can see, without ids that legitimately differ."""
    return {k: out.get(k) for k in ("result", "reason")}


# ============================================================================================ A. worker, Postgres
def test_a_member_cannot_touch_another_members_artifact_or_share_through_any_intent(registry_pg, tmp_path):
    """Publish, delete, share, approve, revoke and read the events of Bob's artifact (and of Bob's share, with the
    right ids and with crossed ids): every answer is the answer for something that does not exist, and nothing moves."""
    async def body(env, rt):
        aid, vid = await published(env, user("bob"), owner=BOB)
        sid = (await share(env, user("bob"), aid, vid))["share_id"]
        mine, mvid = await published(env, user("alice"))
        alice = user("alice")
        stage_for_bob = publish_payload(env, cbf_owner(ALICE), aid)
        stage_for_ghost = publish_payload(env, cbf_owner(ALICE), GHOST_ART)
        cases = [
            ("member.publish", stage_for_bob, stage_for_ghost, aid, GHOST_ART),
            ("member.artifact_delete", {"confirm": True}, {"confirm": True}, aid, GHOST_ART),
            ("member.share_create", {"version_id": vid, "audience": "public"},
             {"version_id": vid, "audience": "public"}, aid, GHOST_ART),
            ("member.share_approve", {"share_id": sid, "confirm": True}, {"share_id": GHOST_SHARE, "confirm": True},
             aid, aid),
            ("member.share_revoke", {"share_id": sid}, {"share_id": GHOST_SHARE}, aid, aid),
            ("member.share_events", {"share_id": sid}, {"share_id": GHOST_SHARE}, aid, aid),
            # Bob's real share id crossed with Alice's own artifact
            ("member.share_revoke", {"share_id": sid}, {"share_id": GHOST_SHARE}, mine, mine),
            ("member.share_events", {"share_id": sid}, {"share_id": GHOST_SHARE}, mine, mine),
            ("member.share_approve", {"share_id": sid, "confirm": True}, {"share_id": GHOST_SHARE, "confirm": True},
             mine, mine),
        ]
        for kind, real, ghost, real_aid, ghost_aid in cases:
            got = await send(env, alice, kind, real, aid=real_aid)
            want = await send(env, alice, kind, ghost, aid=ghost_aid)
            assert verdict(got) == verdict(want) == {"result": "rejected", "reason": "not_found"}, (kind, got, want)
            assert set(got) == set(want), (kind, got, want)            # not even an extra field to tell them apart
        (art,) = await sql(env, "SELECT lifecycle, current_version_id FROM artifacts WHERE artifact_id=$1", aid)
        assert art["lifecycle"] == "active" and art["current_version_id"] == vid
        (live,) = await sql(env, "SELECT state FROM artifact_shares WHERE share_id=$1", sid)
        assert live["state"] == "pending"
        assert (await sql(env, "SELECT count(*) AS n FROM artifact_shares"))[0]["n"] == 1
        assert (await sql(env, "SELECT count(*) AS n FROM artifact_versions WHERE artifact_id=$1", aid))[0]["n"] == 1
    srun(registry_pg, tmp_path, body)


def cbf_owner(principal: str) -> str:
    from herdr_engineering.control_plane.artifact_members import member_owner_key
    return member_owner_key(principal)


@pytest.mark.parametrize("granted", [["read"], ["read", "edit", "review"]])
def test_a_read_grant_cannot_be_turned_into_a_share_by_a_forged_intent_or_the_store(registry_pg, tmp_path, granted):
    """Forged ``member.share_create`` naming an artifact the member has no ``share`` on: not_found, no row; the same
    through ``ShareStore.create`` directly (public and lab)."""
    async def body(env, rt):
        aid, vid = await published(env, user("bob"), owner=BOB)
        await grant(env, aid, ALICE, granted)
        for extra in ({}, {"allow_emails": ["bob@lab.example.org"]}, {"audience": "lab"}):
            out = await share(env, user("alice"), aid, vid, **extra)
            ghost = await share(env, user("alice"), GHOST_ART, vid, **extra)
            assert verdict(out) == verdict(ghost) == {"result": "rejected", "reason": "not_found"}
        # the agent actor of the same member is no different
        agent_out = await send(env, agent_actor("ag1"), "member.share_create",
                               {"version_id": vid, "audience": "public"}, aid=aid)
        assert verdict(agent_out) == {"result": "rejected", "reason": "not_found"}
        assert not await sql(env, "SELECT 1 FROM artifact_shares")
        # the store itself (what every other door ends in)
        from datetime import UTC, datetime, timedelta

        async with env.pool.acquire() as c:
            store = ShareStore(c)
            for kw in ({"audience": "public", "expires_at": datetime.now(UTC) + timedelta(days=3)},
                       {"audience": "lab", "allow_emails": ["alice@lab.example.org"],
                        "member_groups": frozenset({"lab"}), "member_emails": frozenset({"alice@lab.example.org"})}):
                with pytest.raises(ShareError) as exc:
                    await store.create(artifact_id=aid, version_id=vid, created_by=ALICE, **kw)
                assert exc.value.code == "not_found"
        assert not await sql(env, "SELECT 1 FROM artifact_shares")
    srun(registry_pg, tmp_path, body)


def test_every_member_public_link_step_writes_a_member_event_without_url_or_token(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        sid = (await share(env, user("alice"), aid, vid, expires_in_days=7))["share_id"]
        ticket, done = await approve(env, user("alice"), aid, sid)
        url = secret_of(env, ticket)["url"]
        assert done["result"] == "live"
        revoked = await send(env, user("alice"), "member.share_revoke", {"share_id": sid}, aid=aid)
        assert revoked["result"] == "revoked"
        rows = await sql(env, "SELECT * FROM artifact_member_events WHERE action LIKE 'share_%' AND outcome='ok'"
                              " ORDER BY event_id")
        assert [r["action"] for r in rows] == ["share_create", "share_approve", "share_revoke"]
        for r in rows:
            assert r["member_principal"] == ALICE and r["audience"] == "public" and r["share_id"] == sid
            assert json.loads(r["actor"]) in ({"kind": "user", "id": "alice"}, ALICE) or ALICE in r["actor"]
            blob = json.dumps({k: str(v) for k, v in dict(r).items()})
            assert url not in blob and token_of(url) not in blob and "/s/" not in blob
        assert token_of(url) not in await db_text(env)
    srun(registry_pg, tmp_path, body)


def test_the_tombstoned_artifact_is_refused_by_every_member_writer_and_hidden_from_every_reader(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        other, ovid = await published(env)
        sid = (await share(env, user("alice"), aid, vid))["share_id"]
        await grant(env, aid, BOB, ["read"])
        await sql(env, "UPDATE artifacts SET lifecycle='tombstoned', tombstoned_at=now(), purge_after=now() "
                       "WHERE artifact_id=$1", aid)
        alice = user("alice")
        before = (await sql(env, "SELECT count(*) AS n FROM artifact_versions"))[0]["n"]
        outs = {
            "publish": await send(env, alice, "member.publish", publish_payload(env, cbf_owner(ALICE), aid,
                                                                                 base=vid), aid=aid),
            "share_create": await share(env, alice, aid, vid),
            "share_approve": (await approve(env, alice, aid, sid))[1],
        }
        for name, out in outs.items():
            assert out["result"] == "rejected", (name, out)
        # delete of a tombstone is not a second delete
        out = await send(env, alice, "member.artifact_delete", {"confirm": True}, aid=aid)
        assert out["result"] in ("rejected", "deleted") and (await sql(
            env, "SELECT lifecycle FROM artifacts WHERE artifact_id=$1", aid))[0]["lifecycle"] == "tombstoned"
        assert (await sql(env, "SELECT count(*) AS n FROM artifact_versions"))[0]["n"] == before
        (row,) = await sql(env, "SELECT state FROM artifact_shares WHERE share_id=$1", sid)
        assert row["state"] != "live"
        # an applied intent rewrites the view: the tombstone is in none of the member's readers
        await send(env, alice, "member.share_create", {"version_id": ovid, "audience": "public"}, aid=other)
        view = view_of(env, ALICE)
        assert aid not in [a["artifact_id"] for a in view["artifacts"]]
        assert other in [a["artifact_id"] for a in view["artifacts"]]
        assert sid not in [x["share_id"] for x in view["links"]] and aid not in json.dumps(view)
        await create_artifact(env, user("bob"))
        assert aid not in json.dumps(view_of(env, BOB))                    # bob held a read grant on it
    srun(registry_pg, tmp_path, body)


def test_a_disabled_member_feature_refuses_every_forged_member_intent(registry_pg, tmp_path):
    async def body(env, rt):
        for kind, payload in (
                ("member.artifact_create", {"title": "T", "filename": "a.html", "profile": "web"}),
                ("member.publish", {"base_version_id": None, "message": "m",
                                    "files": [{"name": "index.html", "upload_id": "upl_" + "1" * 24}]}),
                ("member.artifact_delete", {"confirm": True}),
                ("member.share_create", {"version_id": "v", "audience": "public"}),
                ("member.share_approve", {"share_id": GHOST_SHARE, "confirm": True}),
                ("member.share_revoke", {"share_id": GHOST_SHARE}),
                ("member.share_events", {"share_id": GHOST_SHARE})):
            for actor in (user("alice"), agent_actor("ag1")):
                out = await send(env, actor, kind, payload, aid=None if kind == "member.artifact_create" else "art_whatever1")
                assert out["result"] == "rejected" and out["reason"] == "members_disabled", (kind, out)
        assert not await sql(env, "SELECT 1 FROM artifacts")
        assert not await sql(env, "SELECT 1 FROM artifact_shares")
    _srun_disabled(registry_pg, tmp_path, body)


def _srun_disabled(pg, tmp_path, body):
    from herdr_engineering.control_plane.artifact_member_intents import (
        MemberRuntime,
        register_member_intents,
    )
    from tests.control_plane.test_collab_intents import run
    clock = Clock()
    runtime = MemberRuntime(outbox=tmp_path / "outbox", mirror_root=tmp_path / "mirror", group="lab", enabled=False,
                            defaults={}, shares_root=tmp_path / "shares", clock=clock)

    async def go(env):
        await sql(env, "TRUNCATE artifacts, artifact_acl, artifact_shares, artifact_member_usage, artifact_member_events,"
                       " artifact_blobs, artifact_collab_intents, artifact_collab_uploads CASCADE")
        register_member_intents(env.applier)
        write_feeds(env, users=DIRECTORY, agents={"ag1": ("alice", False)})
        return await body(env, runtime)
    return run(pg, tmp_path, go, clock=clock, context_extras={"members": runtime})


MEMBER_KINDS = {
    "member.artifact_create": {"title": "T", "filename": "a.html", "profile": "web"},
    "member.artifact_delete": {"confirm": True},
    "member.share_create": {"version_id": "version_x", "audience": "public"},
    "member.share_revoke": {"share_id": GHOST_SHARE},
}


@pytest.mark.parametrize("kind", sorted(MEMBER_KINDS))
def test_an_intent_stamped_sixty_one_seconds_ago_is_expired_and_changes_nothing(registry_pg, tmp_path, kind):
    async def body(env, rt):
        aid = await create_artifact(env, user("alice"))
        raw = env.intent(user("alice"), kind=kind, payload=MEMBER_KINDS[kind], age=61,
                         artifact_id=None if kind.endswith("artifact_create") else aid)
        await settle(env)
        assert env.result(raw["ticket"])["result"] == "expired"
        (art,) = await sql(env, "SELECT lifecycle FROM artifacts WHERE artifact_id=$1", aid)
        assert art["lifecycle"] == "active"
        assert (await sql(env, "SELECT count(*) AS n FROM artifacts"))[0]["n"] == 1
        assert not await sql(env, "SELECT 1 FROM artifact_shares")
        fresh = env.intent(user("alice"), kind="member.artifact_create", payload=MEMBER_KINDS["member.artifact_create"],
                           age=59)
        await settle(env)
        assert env.result(fresh["ticket"])["result"] == "created"            # the boundary is the age, not the kind
    srun(registry_pg, tmp_path, body)


@pytest.mark.parametrize("kind", sorted(MEMBER_KINDS))
def test_an_agent_revoked_between_append_and_apply_is_rejected(registry_pg, tmp_path, kind):
    async def body(env, rt):
        aid = await create_artifact(env, agent_actor("ag1"))
        raw = env.intent(agent_actor("ag1"), kind=kind, payload=MEMBER_KINDS[kind],
                         artifact_id=None if kind.endswith("artifact_create") else aid)
        doc = {"generated_at": env.clock(), "agents": {"ag1": {
            "owner": "alice", "status": "revoked", "expires_at": env.clock() + 3600, "external": False,
            "join_id": None, "hard_expiry": None}}}
        (env.outbox / "agents.json").write_text(json.dumps(doc))            # revoked after the append, before the apply
        await settle(env)
        out = env.result(raw["ticket"])
        assert out["result"] == "rejected", out
        (art,) = await sql(env, "SELECT lifecycle FROM artifacts WHERE artifact_id=$1", aid)
        assert art["lifecycle"] == "active"
        assert (await sql(env, "SELECT count(*) AS n FROM artifacts"))[0]["n"] == 1
    srun(registry_pg, tmp_path, body)


def test_a_disabled_members_staging_directories_are_swept(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create_artifact(env, user("alice"))
        _, mine = stage(env, cbf_owner(ALICE), aid, "index.html", HTML)
        _, bobs = stage(env, cbf_owner(BOB), aid, "index.html", HTML)
        write_feeds(env, users={**DIRECTORY, "alice": {"status": "disabled", "email": "alice@lab.example.org"}})
        async with env.pool.acquire() as conn:
            assert await env.applier.sweep_staging(conn, agents={}, grants=[]) == 1
        assert not mine.exists() and bobs.is_dir()                    # only the disabled member's staging goes
    srun(registry_pg, tmp_path, body)


# ============================================================================================ B. front doors
def failure(fn, *a, **kw) -> tuple:
    """(status, code, message) of the refusal ``fn`` raises."""
    with pytest.raises((MemberServiceError, CollabError)) as exc:
        fn(*a, **kw)
    e = exc.value
    return (e.status, getattr(e, "code", None), e.message)


def mcp(stack, tool, /, who="op", **args):
    pr, _ = stack.w.agent(who)
    return collab_member_tools.call(stack.s, pr, tool, args)


def chunk_args(stack, artifact, **kw):
    c = stack.w.chunk(DATA, 0, artifact=artifact, **kw)
    c["data_b64"] = c.pop("data")
    return c


def test_no_mcp_tool_or_web_route_tells_a_foreign_artifact_from_a_missing_one(stack):
    """``OTHER`` is lab's, ``GHOST`` does not exist; op is neither.  Every reader and writer answers alike, and
    the foreign artifact's link list is empty like the missing one's."""
    stack.w.view("lab", [OTHER])
    pub = {"files": [{"name": "a.html", "upload_id": "upl_" + "0" * 24}], "message": "m"}
    calls = {
        "head": lambda a: mcp(stack, "artifact_head", artifact_id=a),
        "history": lambda a: mcp(stack, "artifact_history", artifact_id=a),
        "upload": lambda a: mcp(stack, "artifact_upload_chunk", **chunk_args(stack, a)),
        "status": lambda a: mcp(stack, "artifact_upload_status", artifact_id=a, name="f.bin", size_bytes=len(DATA),
                                file_sha256="0" * 64),
        "publish": lambda a: mcp(stack, "artifact_publish", artifact_id=a, **pub),
        "delete": lambda a: mcp(stack, "artifact_delete", artifact_id=a, confirm=True),
        "share_create": lambda a: mcp(stack, "artifact_share_create", artifact_id=a, version_id="v_1",
                                      audience="public"),
        "share_approve": lambda a: mcp(stack, "artifact_share_approve", artifact_id=a,
                                       share_id="share_" + "1" * 24, confirm=True),
        "upload_abort": lambda a: mcp(stack, "artifact_upload_abort", artifact_id=a, upload_id="upl_" + "2" * 24),
    }
    queued = {   # no front-door precheck: the worker decides, and decides alike (section A); both are queued the same
        "share_revoke": lambda a: mcp(stack, "artifact_share_revoke", artifact_id=a, share_id="share_" + "1" * 24),
        "share_events": lambda a: mcp(stack, "artifact_share_events", artifact_id=a, share_id="share_" + "1" * 24),
    }
    for name, fn in queued.items():
        a, b = fn(OTHER), fn(GHOST)
        assert {k: v for k, v in a.items() if k != "ticket"}.keys() == {k: v for k, v in b.items() if k != "ticket"}.keys(), name
    for name, fn in calls.items():
        assert failure(fn, OTHER) == failure(fn, GHOST) == (404, "not_found", "not found"), name
    # the web, same artifacts, same answers
    web = [("GET", "/api/member/artifacts/{}", None), ("GET", "/api/member/artifacts/{}/history", None),
           ("POST", "/api/member/artifacts/{}/delete", {"confirm": True}),
           ("POST", "/api/member/artifacts/{}/publish", pub),
           ("POST", "/api/member/artifacts/{}/shares", {"version_id": "v_1", "audience": "public"}),
           ("POST", "/api/member/artifacts/{}/shares/share_" + "1" * 24 + "/approve", {"confirm": True}),
           ("POST", "/api/member/artifacts/{}/uploads/upl_" + "2" * 24 + "/abort", {}),
           ("POST", "/api/member/artifacts/{}/uploads/status", {"name": "f.bin", "size_bytes": 3, "file_sha256": "0" * 64})]
    for method, path, body in web:
        a, b = (req(stack, method, path.format(x), body=body) for x in (OTHER, GHOST))
        assert (a.status, a.raw) == (b.status, b.raw) and a.status == 404, path
    # revoke and events are queued for the worker on the web too; the run PUT is refused before any body byte is read
    for path in ("/api/member/artifacts/{}/shares/share_" + "1" * 24 + "/revoke",
                 "/api/member/artifacts/{}/shares/share_" + "1" * 24 + "/events"):
        a, b = (req(stack, "POST", path.format(x), body={}) for x in (OTHER, GHOST))
        assert a.status == b.status and a.json.keys() == b.json.keys(), path
    sha = hashlib.sha256(DATA).hexdigest()
    for x in (OTHER, GHOST):
        owner = member_owner_key(stack.s.member_check(stack.w.people["op"]).principal)
        up = upload_id_for(owner, x, "f.bin", sha, len(DATA))
        got = req(stack, "PUT", f"/api/member/artifacts/{x}/uploads/{up}/runs/0", raw=DATA,
                  extra=("X-Name: f.bin", f"X-Size-Bytes: {len(DATA)}", f"X-File-SHA256: {sha}", f"X-Run-SHA256: {sha}"))
        assert got.status == 404, (x, got.status)
    # links: a link of lab's on OTHER is not op's to list
    stack.w.view("lab", [OTHER])
    assert mcp(stack, "artifact_share_list", artifact_id=OTHER) == mcp(stack, "artifact_share_list", artifact_id=GHOST)
    assert req(stack, "GET", f"/api/member/artifacts/{OTHER}/shares").json == {"links": []}
    assert stack.w.staged("op") == [] and stack.w.staged("lab") == []


def intents_in(outbox) -> list[dict]:
    root = outbox / "intents"
    return [json.loads(p.read_text()) for p in sorted(root.glob("*.json"))] if root.exists() else []


def test_a_read_grant_reads_history_but_no_door_can_turn_it_into_a_share(registry_pg, tmp_path, stack):
    """Alice holds only ``read`` on Bob's artifact.  Through the real MCP tools and the real web API she may read
    its history; her share requests are queued as the doors do and the worker, which decides, refuses every one
    (public and lab, with no row), as it refuses the very same intent forged by hand."""
    stack.w.view("op", [ART], shared=["art_sh1"])
    hist = mcp(stack, "artifact_history", artifact_id="art_sh1")
    assert hist["versions"][0]["version_id"] == "v_9" and hist["truncated"] is True
    assert req(stack, "GET", "/api/member/artifacts/art_sh1/history").json == hist
    before = len(intents_in(stack.w.outbox))
    for audience in ("public", "lab"):
        mcp(stack, "artifact_share_create", artifact_id="art_sh1", version_id="v_9", audience=audience)
        web = req(stack, "POST", "/api/member/artifacts/art_sh1/shares",
                  body={"version_id": "v_9", "audience": audience})
        assert web.status == 200
    sent = intents_in(stack.w.outbox)[before:]
    assert len(sent) == 4 and {i["kind"] for i in sent} == {"member.share_create"}
    assert {i["actor"]["kind"] for i in sent} == {"agent", "user"}

    async def body(env, rt):
        aid, vid = await published(env, user("bob"), owner=BOB)
        await grant(env, aid, ALICE, ["read"])
        for intent in sent:
            actor = {"kind": intent["actor"]["kind"], "id": "ag1" if intent["actor"]["kind"] == "agent" else "alice"}
            payload = {**intent["payload"], "version_id": vid}
            out = await send(env, actor, "member.share_create", payload, aid=aid)
            assert verdict(out) == {"result": "rejected", "reason": "not_found"}, intent
        forged = await send(env, user("alice"), "member.share_create", {"version_id": vid, "audience": "public"},
                            aid=aid)
        assert verdict(forged) == {"result": "rejected", "reason": "not_found"}
        assert not await sql(env, "SELECT 1 FROM artifact_shares")
    srun(registry_pg, tmp_path, body)


def test_members_switched_off_means_no_tools_no_web_and_no_artifact_api(stack):
    pr, info = stack.w.agent("op")
    assert mcp(stack, "artifacts_mine")["available"] is True
    stack.s.member_service = None                      # what the cli does when members are not enabled
    from tests.control_plane.test_collab_mcp_artifacts import err, names
    assert not names(stack.w, info) & collab_member_tools.MEMBER_TOOL_NAMES
    assert err(stack.w, info, "artifacts_mine")["status"] == 403
    for method, path in (("GET", "/api/member/me"), ("POST", "/api/member/artifacts"),
                         ("GET", "/api/member/config")):
        got = req(stack, method, path, raw=b"" if method == "GET" else b"{}")
        assert got.status == 404 and got.json["error"] == "not_found", path
    assert exchange(stack).status == 404


def test_a_share_session_cannot_call_the_member_api(stack):
    shr = mint_share_session(SECRET, "shr-1", "op", 1_800_000_600.0, email="op@example.org", basis="e")
    for method, path, body in (("GET", "/api/member/me", None), ("GET", "/api/member/links", None),
                               ("POST", "/api/member/artifacts", {"title": "t", "filename": "a.html", "profile": "web"}),
                               ("POST", f"/api/member/artifacts/{ART}/delete", {"confirm": True}),
                               ("POST", f"/api/member/artifacts/{ART}/shares", {"version_id": "v_1", "audience": "lab"})):
        got = req(stack, method, path, body=body, member=None, auth=f"Member {shr}")
        assert got.status == 401, path
        got = req(stack, method, path, body=body, member=None, auth=f"Bearer {shr}")
        assert got.status == 401, path
    assert len(intents_in(stack.w.outbox)) == 0
    # the same token as a cookie is nothing either
    assert req(stack, "GET", "/api/member/me", member=None, extra=(f"Cookie: member={shr}",)).status == 401


@pytest.mark.parametrize("dup", [
    "Authorization: Member {s}", "Origin: {o}", "Content-Type: text/plain", "Content-Length: 2",
    "Host: {h}", "X-Forwarded-Proto: http", "Transfer-Encoding: chunked"])
def test_a_duplicated_header_is_never_resolved_in_the_attackers_favour(stack, dup):
    """Request smuggling-ish: the same header twice (or a framing header beside Content-Length) is refused on a
    write, and the artifact is untouched either way."""
    extra = (dup.format(s=session("lab"), o=ORIGIN, h=HOST),)
    before = len(intents_in(stack.w.outbox))
    got = req(stack, "POST", f"/api/member/artifacts/{ART}/delete", body={"confirm": True}, extra=extra)
    if dup.startswith(("Content-Type", "X-Forwarded-Proto")):
        # first value wins and the session + Origin checks, not these, carry the authority: never a 5xx
        assert got.status < 500, (dup, got.status)
        return
    assert got.status in (400, 401, 403, 404, 413), (dup, got.status)
    assert len(intents_in(stack.w.outbox)) == before
    # and on a read the second Authorization / Host is not a way to be somebody else
    if dup.startswith(("Authorization", "Host")):
        got = req(stack, "GET", "/api/member/me", extra=extra)
        assert got.status in (401, 404)
        assert got.raw.find(b"lab") == -1


def test_chunk_thirteen_hundred_in_a_minute_is_429_after_the_configured_rate(stack):
    ms = stack.s.member_service
    limit = ms.config.chunks_per_minute
    assert limit == 1200
    stack.w.write_grants(max_pending_intents_per_agent=50, chunks_per_minute=100_000)
    c = stack.w.chunk(DATA, 0)
    codes = []
    for _ in range(1300):
        try:
            ms.upload_chunk(member_actor(stack.w.people["op"]), c)
            codes.append(200)
        except MemberServiceError as exc:
            codes.append(exc.status)
    assert codes[:limit] == [200] * limit and set(codes[limit:]) == {429} and len(codes) == 1300
    # the window is per member: lab is not throttled by op's flood
    assert ms.upload_chunk(member_actor(stack.w.people["lab"]), stack.w.chunk(DATA, 0, artifact=OTHER))["replayed"] is False


def test_the_sixty_first_share_create_in_an_hour_is_429_and_the_window_slides(stack):
    ms = stack.s.member_service
    ms.config = dataclasses.replace(ms.config, share_creates_per_hour=60, writes_per_minute=10_000)
    stack.w.write_grants(max_pending_intents_per_agent=1000, commits_per_minute=100_000)
    actor = member_actor(stack.w.people["op"])
    for i in range(60):
        assert ms.share_create(actor, ART, "v_1", "public", idempotency_key=f"idem-share-{i:04d}")["status"] == "pending"
    assert refuse(ms.share_create, actor, ART, "v_1", "public").status == 429
    assert ms.share_create(member_actor(stack.w.people["lab"]), OTHER, "v_1", "public")["status"] == "pending"
    stack.s._now.t += 3601                     # an hour on, on the clock the service itself reads
    stack.w.write_grants(max_pending_intents_per_agent=1000)      # the worker's heartbeat moves with it
    assert ms.share_create(actor, ART, "v_1", "public")["status"] == "pending"


def test_a_limit_of_zero_is_refused_at_load_and_null_is_accepted():
    base = {"enabled": True, "group": "staff", "session_secret": "s" * 40}
    zero_ok = []
    for key in ("max_artifacts", "max_artifact_bytes", "max_member_bytes", "share_creates_per_hour", "groups_ttl"):
        try:
            parse_members({**base, key: 0}, public_host="artifacts.example.org")
            zero_ok.append(key)
        except ValueError:
            pass
        if key.startswith("max_"):          # the three size/count limits may be unlimited: null is accepted, asserted
            assert parse_members({**base, key: None}, public_host="artifacts.example.org") is not None, key
    assert zero_ok == []
    for section in ("rate_limits", "request_limits", "staging"):
        keys = {"rate_limits": ("chunks_per_minute", "writes_per_minute"),
                "request_limits": ("http_chunks_per_request", "max_publish_files"),
                "staging": ("staging_bytes_per_member",)}[section]
        for key in keys:
            with pytest.raises(ValueError):
                parse_members({**base, section: {key: 0}}, public_host="artifacts.example.org")
    got = parse_members({**base, "share_limits": {"max_share_bytes": None, "max_total_bytes": None}},
                        public_host="artifacts.example.org")
    assert got["share_limits"] == {"max_share_bytes": None, "max_total_bytes": None}
    with pytest.raises(ValueError):
        parse_members({**base, "share_limits": {"max_share_bytes": 0}}, public_host="artifacts.example.org")


# ============================================================================================ C. operator and public


def test_a_member_public_link_is_listed_by_the_operator_route_and_revocable_there(env):
    aid, vid = env.artifact(owner=opapi.ALICE)
    share = env.share(aid, vid, owner=opapi.ALICE)
    status, body = env.req(BASE_BODY + "/links?audience=public")
    assert status == 200 and [x["share_id"] for x in body["links"]] == [share["share_id"]]
    assert "token_hash" not in json.dumps(body)
    status, body = env.req(f"{BASE_BODY}/alice/shares/{share['share_id']}/revoke", body={})
    assert status == 200 and body["share"]["state"] == "revoked"
    assert env.req(BASE_BODY + "/links?state=live")[1]["links"] == []
    # bob cannot be used to reach alice's link
    assert env.req(f"{BASE_BODY}/bob/shares/{share['share_id']}/revoke", body={})[0] == 404


def test_visitors_to_a_member_public_link_get_no_annotation_route(tmp_path):
    env = ShareEnv(tmp_path, audience="public", files=opapi_files(),
                   config_extra=lab_config(tmp_path), state_extra={"shared_by": "Al"})
    try:
        ref = env.request("GET", "/nothing-here")[2]
        for method in ("GET", "POST", "PUT", "DELETE"):
            for path in (api(), f"/s/{TOKEN}/k/{share_session()}/v/index.html", f"/s/{TOKEN}/annotations"):
                body = b"{}"
                status, _, got = env.request(method, path, good_headers(body=body), body if method != "GET" else b"")
                assert status in (404, 405), (method, path, status)
                if status == 404:
                    assert got == ref
        assert not env.config.annotations_path.exists()
    finally:
        env.close()


def opapi_files():
    from tests.control_plane.test_share_annotate import FILES
    return FILES


# ============================================================================================ E. parked risks
def test_a_member_named_links_is_addressed_as_a_member_and_the_operator_list_is_untouched(env):
    LINKS = "member:links"
    aid, vid = env.artifact(owner=LINKS)
    share = env.share(aid, vid, owner=LINKS)
    got = env.req(BASE_BODY + "/links")
    assert got[0] == 200 and [x["share_id"] for x in got[1]["links"]] == [share["share_id"]]     # the list, not the member
    status, body = env.req(f"{BASE_BODY}/member:links")
    assert status == 200 and aid in json.dumps(body)
    assert env.req(f"{BASE_BODY}/member:links/limits", body={"max_artifacts": 7})[0] == 200
    assert env.sql("SELECT max_artifacts FROM artifact_member_usage WHERE member_principal=$1", LINKS)[0][0] == 7
    assert env.req(f"{BASE_BODY}/member:links/shares/{share['share_id']}/revoke", body={})[0] == 200


def test_purge_anonymises_the_members_events_and_acl_and_a_shared_blob_survives_for_the_other_member(env):
    same = opapi.entry(env.blobs, "index.html", opapi.HTML)             # one content-addressed object, two owners
    a_aid, a_vid = env.artifact(owner=opapi.ALICE, files=[same])
    b_aid, b_vid = env.artifact(owner=opapi.BOB, files=[opapi.entry(env.blobs, "index.html", opapi.HTML)])
    share = env.share(a_aid, a_vid, owner=opapi.ALICE)
    retract(env, a_aid)
    status, body = env.req(opapi.purge_path(a_aid), body={"confirm": a_aid})
    assert status == 200, body
    assert same["storage_key"] not in env.blobs.deleted, "bob's artifact still references the very same bytes"
    assert env.sql("SELECT count(*) FROM artifact_blobs WHERE scope=$1", b_aid)[0][0] >= 1
    assert env.sql("SELECT count(*) FROM artifact_versions WHERE artifact_id=$1 AND purged_at IS NULL", b_aid)[0][0] == 1
    events = env.sql("SELECT detail::text AS d FROM artifact_member_events WHERE artifact_id=$1", a_aid)
    assert all("a@lab.example.org" not in (r["d"] or "") for r in events)
    assert "a@lab.example.org" not in json.dumps([dict(r) for r in env.sql(
        "SELECT * FROM artifact_share_events WHERE share_id=$1", share["share_id"])], default=str)
    # and bob's purge, after his own delete, now removes the object
    retract(env, b_aid)
    assert env.req(opapi.purge_path(b_aid, "bob"), body={"confirm": b_aid})[0] == 200
    assert same["storage_key"] in env.blobs.deleted


def test_a_member_cannot_reach_the_others_share_by_guessing_or_replaying_its_id(env):
    a_aid, a_vid = env.artifact(owner=opapi.ALICE)
    b_aid, b_vid = env.artifact(owner=opapi.BOB)
    s = env.share(a_aid, a_vid, owner=opapi.ALICE)
    for member, aid in (("bob", b_aid), ("bob", a_aid)):
        status, body = env.req(f"{BASE_BODY}/{member}/shares/{s['share_id']}/revoke", body={})
        assert status == 404, (member, aid)
    ghost = env.req(f"{BASE_BODY}/bob/shares/share_{'0' * 24}/revoke", body={})
    cross = env.req(f"{BASE_BODY}/bob/shares/{s['share_id']}/revoke", body={})
    assert ghost == cross
    assert opapi._call(env.shares, "get", s["share_id"])["state"] == "live"


def test_a_member_session_is_nothing_on_the_share_routes(tmp_path):
    """The reverse direction: a signed-in member's token in the ``/s/<token>/k/<session>/`` slot opens nothing."""
    from tests.control_plane.test_share_annotate import make_env
    env = make_env(tmp_path)
    try:
        junk = env.request("GET", f"/s/{TOKEN}/k/{'x' * 40}/f/index.html", {"Sec-Fetch-Dest": "document"})
        for tok in (session("op"), session("lab")):
            for kind in ("f", "v"):
                got = env.request("GET", f"/s/{TOKEN}/k/{tok}/{kind}/index.html", {"Sec-Fetch-Dest": "document"})
                assert got[0] in (401, 403, 404) and got[0] == junk[0], (kind, got[0])
                assert b"hello" not in got[2]
            assert env.request("POST", api(tok), good_headers(body=b"{}"), b"{}")[0] in (401, 403, 404)
        assert not env.config.annotations_path.exists()
    finally:
        env.close()


def test_exactly_one_forwarded_proto_is_required_on_the_member_web(stack):
    """A second ``X-Forwarded-Proto`` is refused, whichever value comes first, and nothing is queued."""
    before = len(intents_in(stack.w.outbox))
    for helper_adds_https in (True, False):
        for extra in ("X-Forwarded-Proto: http", "X-Forwarded-Proto: https"):
            got = req(stack, "POST", f"/api/member/artifacts/{ART}/delete", body={"confirm": True}, extra=(extra,),
                      proto=helper_adds_https)
            single_https = not helper_adds_https and extra.endswith("https")      # the only valid combination
            assert got.status == (200 if single_https else 400), (helper_adds_https, extra, got.status)
    assert len(intents_in(stack.w.outbox)) == before + 1                          # exactly the one valid delete
    assert req(stack, "GET", "/api/member/me", proto=False).status == 400       # none at all


# ============================================================================ fix round 1: shares and ownership

from herdr_engineering.control_plane.artifact_member_intents import sweep_disabled_members  # noqa: E402


def test_a_member_share_create_honours_its_idempotency_key_per_member(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env, user("alice"))
        payload = {"version_id": vid, "audience": "public"}
        first = await send(env, user("alice"), "member.share_create", payload, aid=aid, key="share-key-0001")
        again = await send(env, agent_actor("ag1"), "member.share_create", payload, aid=aid, key="share-key-0001")
        assert first["result"] == again["result"] == "created" and first["share_id"] == again["share_id"]
        assert len(await sql(env, "SELECT 1 FROM artifact_shares")) == 1
        other = await send(env, user("alice"), "member.share_create", {**payload, "audience": "lab",
                           "allow_groups": ["lab"]}, aid=aid, key="share-key-0001")
        assert verdict(other) == {"result": "rejected", "reason": "idempotency_conflict"}
        assert len(await sql(env, "SELECT 1 FROM artifact_shares")) == 1
        # bob using alice's key on his own artifact gets his own share, and alice's id never reaches him
        baid, bvid = await published(env, user("bob"), owner=BOB)
        mine = await send(env, user("bob"), "member.share_create", {"version_id": bvid, "audience": "public"},
                          aid=baid, key="share-key-0001")
        assert mine["result"] == "created" and mine["share_id"] != first["share_id"]
        # and on alice's artifact he is refused exactly as for a missing one, key or no key
        steal = await send(env, user("bob"), "member.share_create", payload, aid=aid, key="share-key-0001")
        assert verdict(steal) == {"result": "rejected", "reason": "not_found"} and first["share_id"] not in str(steal)
        # no key: two creates are two shares
        await send(env, user("alice"), "member.share_create", payload, aid=aid)
        await send(env, user("alice"), "member.share_create", payload, aid=aid)
        assert len(await sql(env, "SELECT 1 FROM artifact_shares WHERE created_by=$1", ALICE)) == 3
    srun(registry_pg, tmp_path, body)


async def _grantee_link(env, rt):
    """Bob owns an artifact, alice holds ``share`` on it and has made a live public link."""
    aid, vid = await published(env, user("bob"), owner=BOB)
    await grant(env, aid, ALICE, ["read", "share"])
    out = await share(env, user("alice"), aid, vid)
    assert out["result"] == "created", out
    ticket, got = await approve(env, user("alice"), aid, out["share_id"])
    assert got["result"] == "live", got
    return aid, out["share_id"]


async def _state(env, sid):
    return (await sql(env, "SELECT state FROM artifact_shares WHERE share_id=$1", sid))[0]["state"]


def test_a_grantees_link_does_not_outlive_the_grant(registry_pg, tmp_path):
    async def body(env, rt):
        aid, sid = await _grantee_link(env, rt)
        write_feeds(env, users=DIRECTORY)
        async with env.pool.acquire() as conn:
            assert await sweep_disabled_members(conn, rt) == 0 and await _state(env, sid) == "live"
            await sql(env, "DELETE FROM artifact_acl WHERE artifact_id=$1 AND principal_id=$2", aid, ALICE)
            assert await sweep_disabled_members(conn, rt) == 1
        assert await _state(env, sid) == "revoked"
    srun(registry_pg, tmp_path, body)


def test_a_grants_expiry_and_a_downgrade_withdraw_the_link_too(registry_pg, tmp_path):
    async def body(env, rt):
        aid, sid = await _grantee_link(env, rt)
        write_feeds(env, users=DIRECTORY)
        await sql(env, "UPDATE artifact_acl SET permissions='[\"read\"]'::jsonb WHERE artifact_id=$1", aid)
        async with env.pool.acquire() as conn:
            assert await sweep_disabled_members(conn, rt) == 1
        assert await _state(env, sid) == "revoked"
    srun(registry_pg, tmp_path, body)


def test_a_grantees_link_does_not_outlive_a_disabled_owner(registry_pg, tmp_path):
    async def body(env, rt):
        aid, sid = await _grantee_link(env, rt)
        write_feeds(env, users={**DIRECTORY, "bob": {"status": "disabled", "email": "bob@lab.example.org"}})
        async with env.pool.acquire() as conn:
            assert await sweep_disabled_members(conn, rt) == 1
        assert await _state(env, sid) == "revoked"
        # an unknown owner (a restarted directory) proves nothing: a fresh link stays
        write_feeds(env, users=DIRECTORY)
        aid2, sid2 = await _grantee_link(env, rt)
        write_feeds(env, users={"alice": DIRECTORY["alice"], "bob": {"status": "unknown"}})
        async with env.pool.acquire() as conn:
            assert await sweep_disabled_members(conn, rt) == 0
        assert await _state(env, sid2) == "live"
    srun(registry_pg, tmp_path, body)


def test_the_worker_treats_a_purged_artifact_as_missing_for_revoke_events_and_delete(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env, user("alice"))
        out = await share(env, user("alice"), aid, vid)
        await sql(env, "UPDATE artifacts SET lifecycle='tombstoned' WHERE artifact_id=$1", aid)
        for kind, payload in (("member.share_events", {"share_id": out["share_id"]}),
                              ("member.share_revoke", {"share_id": out["share_id"]}),
                              ("member.artifact_delete", {"confirm": True})):
            got = await send(env, user("alice"), kind, payload, aid=aid)
            assert verdict(got) == {"result": "rejected", "reason": "not_found"}, kind
    srun(registry_pg, tmp_path, body)

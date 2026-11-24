# ruff: noqa: F811  (pytest fixtures are imported by name from registry_pg)
"""Member intents on the plan-3 applier (plan 5, Task 4): create, publish and delete as a signed-in member.

Real Postgres (the isolated server of ``registry_pg``), real temp dirs for the outbox and the mirror, and the same
``FakeBlobs`` object store the plan-3 applier tests use. No mocks.
"""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import asyncpg
import pytest

from herdr_engineering.control_plane import collab_bridge_files as cbf
from herdr_engineering.control_plane.artifact_member_intents import (
    MEMBER_DIRECTORY,
    MemberRuntime,
    publish_member_views,
    register_member_intents,
    sweep_disabled_members,
    write_member_view,
)
from herdr_engineering.control_plane.artifact_members import member_owner_key
from herdr_engineering.control_plane.collab_bridge_files import BridgeLimits
from tests.control_plane.registry_pg import ROOT, registry_pg, uid  # noqa: F401 - imported fixture
from tests.control_plane.test_collab_intents import (
    FAILED,
    NOT_FOUND,
    TTL,
    Clock,
    age_dir,
    run,
    settle,
    sql,
    stage,
)

ALICE, BOB = "member:alice", "member:bob"
AK, BK = member_owner_key(ALICE), member_owner_key(BOB)
KINDS = ("member.artifact_create", "member.publish", "member.artifact_delete")
HTML = b"<html><body>hello</body></html>"
BIG_HTML = b"<html>" + b"x" * (2 * cbf.CHUNK_BYTES + 1000) + b"</html>"      # 3 parts


def user(name):
    return {"kind": "user", "id": name}


def agent(name):
    return {"kind": "agent", "id": name}


AGENTS = {"ag1": ("alice", False), "ext1": ("alice", True), "bag": ("bob", False)}
USERS = {"alice": {"status": "approved"}, "bob": {"status": "approved"}, "dave": {"status": "disabled"},
         "carol": {"status": "approved", "groups": ["other-group"]}}


def write_feeds(env, *, directory_age=0.0, users=None, group="lab", agents=None):
    doc = {"generated_at": env.clock(), "agents": {
        a: {"owner": owner, "status": "active", "expires_at": env.clock() + 3600, "external": ext,
            "join_id": None, "hard_expiry": None} for a, (owner, ext) in (agents or AGENTS).items()}}
    (env.outbox / "agents.json").write_text(json.dumps(doc))
    users = USERS if users is None else users
    (env.outbox / MEMBER_DIRECTORY).write_text(json.dumps({"group": group, "updated": env.clock() - directory_age,
                                                           "users": users}))


def removed(env, ago, groups=()):
    """A directory record: removed from the group ``ago`` seconds before the worker's now."""
    return {"status": "removed", "groups": list(groups), "removed_since": env.clock() - ago}


GRACE = 3600.0


def mrun(pg, tmp_path, body, *, defaults=None, enabled=True, **kw):
    clock = kw.pop("clock", None) or Clock()
    runtime = MemberRuntime(outbox=tmp_path / "outbox", mirror_root=tmp_path / "mirror", group="lab", enabled=enabled,
                            defaults=defaults or {}, shares_root=tmp_path / "shares", clock=clock)

    async def go(env):
        await sql(env, "TRUNCATE artifacts, artifact_acl, artifact_shares, artifact_member_usage, artifact_member_events,"
                       " artifact_blobs, artifact_collab_intents, artifact_collab_uploads CASCADE")   # per-test state
        register_member_intents(env.applier)
        write_feeds(env)
        return await body(env, runtime)
    return run(pg, tmp_path, go, clock=clock, context_extras={"members": runtime}, **kw)


async def send(env, actor, kind, payload, *, aid=None, key=None):
    raw = env.intent(actor, kind=kind, payload=payload, artifact_id=aid, key=key)
    await settle(env)
    return env.result(raw["ticket"])


async def create(env, actor=None, title="Paper", filename="paper.html", profile="web", key=None):
    out = await send(env, actor or user("alice"), "member.artifact_create",
                     {"title": title, "filename": filename, "profile": profile}, key=key)
    assert out["result"] == "created", out
    return out["artifact_id"]


def publish_payload(env, owner, aid, files=None, *, base=None, message="v", entry=None):
    files = files or {"index.html": HTML}
    listed = [{"name": n, "upload_id": stage(env, owner, aid, n, d)[0]} for n, d in files.items()]
    payload = {"base_version_id": base, "files": listed, "message": message}
    if entry:
        payload["entry"] = entry
    return payload


async def publish(env, aid, files=None, *, actor=None, owner=ALICE, **kw):
    key = kw.pop("key", None)
    return await send(env, actor or user("alice"), "member.publish",
                      publish_payload(env, member_owner_key(owner), aid, files, **kw), aid=aid, key=key)


def view_of(env, principal):
    path = env.mirror_root / "member-views" / (member_owner_key(principal) + ".json")
    return json.loads(path.read_text())


# ----------------------------------------------------------------------------- the user-actor id charset

@pytest.mark.parametrize("actor_id", ["alice", "eu-west-1_Ab:cd+x@lab.example.org", "a" * 128, "_u", "+x"])
def test_a_user_actor_may_carry_any_cognito_username(actor_id):
    assert cbf._validate_actor({"kind": "user", "id": actor_id}) == cbf.Actor("user", actor_id)


@pytest.mark.parametrize("raw", [{"kind": "user", "id": "a" * 129}, {"kind": "user", "id": ""},
                                 {"kind": "user", "id": "a b"}, {"kind": "user", "id": "a/b"},
                                 {"kind": "agent", "id": "x:y"}, {"kind": "agent", "id": "_x"},
                                 {"kind": "agent", "id": "m_" + "a" * 24}])
def test_the_widened_charset_is_for_users_only_and_stays_bounded(raw):
    with pytest.raises(cbf.IntentError):
        cbf._validate_actor(raw)


def test_collab_public_alone_validates_the_member_kinds_without_asyncpg():
    code = ("import sys\n"
            "import herdr_engineering.control_plane.collab_artifacts\n"
            "from herdr_engineering.control_plane import collab_bridge_files as c\n"
            f"assert {KINDS!r}[0] in c._INTENT_KINDS and {KINDS!r}[2] in c._INTENT_KINDS\n"
            "assert 'asyncpg' not in sys.modules\n")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert done.returncode == 0, done.stderr


# ----------------------------------------------------------------------------- payload validators

def v(kind, payload, actor=None, aid="artifact_x"):
    raw = {"ticket": cbf.new_ticket(), "kind": kind, "actor": actor or user("alice"), "submitted_at": 1.0,
           "payload": payload}
    if kind != "member.artifact_create":
        raw["artifact_id"] = aid
    return cbf.validate_intent(raw, filename=raw["ticket"] + ".json", limits=BridgeLimits())


def test_validators_accept_the_documented_payloads_for_agents_and_users():
    for actor in (user("alice"), agent("ag1")):
        made = v("member.artifact_create", {"title": "T", "filename": "a.html", "profile": "document"}, actor)
        first = v("member.publish", {"base_version_id": None,
                                     "files": [{"name": "a.html", "upload_id": "upl_" + "0" * 24}],
                                     "message": "m"}, actor)
        second = v("member.publish", {"base_version_id": "version_1", "entry": "a.html", "message": "",
                                      "files": [{"name": "a.html", "upload_id": "upl_" + "0" * 24}]}, actor)
        gone = v("member.artifact_delete", {"confirm": True}, actor)
        assert all(r is not None for r in (made, first, second, gone))


@pytest.mark.parametrize("kind,payload", [
    ("member.artifact_create", {"title": "", "filename": "a.html", "profile": "web"}),
    ("member.artifact_create", {"title": "T", "filename": "a.html", "profile": "report"}),
    ("member.artifact_create", {"title": "T", "filename": "../a.html", "profile": "web"}),
    ("member.artifact_create", {"title": "T", "filename": "a.html", "profile": "web", "extra": 1}),
    ("member.publish", {"base_version_id": None, "files": [], "message": "m"}),
    ("member.publish", {"base_version_id": None, "message": "m",
                        "files": [{"name": "a.html", "upload_id": "../../etc"}]}),
    ("member.publish", {"base_version_id": None, "message": "m",
                        "files": [{"name": "../evil.css", "upload_id": "upl_" + "0" * 24}]}),
    ("member.publish", {"base_version_id": None, "message": "m",
                        "files": [{"name": "a.html", "upload_id": "upl_" + "0" * 24},
                                  {"name": "a.html", "upload_id": "upl_" + "1" * 24}]}),
    ("member.publish", {"base_version_id": None, "message": "x" * 1001,
                        "files": [{"name": "a.html", "upload_id": "upl_" + "0" * 24}]}),
    ("member.publish", {"base_version_id": None, "message": "m", "sha256": "0" * 64,
                        "files": [{"name": "a.html", "upload_id": "upl_" + "0" * 24}]}),
    ("member.artifact_delete", {"confirm": False}),
    ("member.artifact_delete", {}),
])
def test_validators_refuse_malformed_payloads(kind, payload):
    with pytest.raises(cbf.IntentError):
        v(kind, payload)


# ----------------------------------------------------------------------------- directory and member checks

@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("stale", ["old", "missing", "future", "wrong_group"])
def test_a_stale_missing_or_foreign_directory_rejects_every_member_kind(registry_pg, tmp_path, kind, stale):
    async def body(env, rt):
        aid = await create(env)
        (before,) = await sql(env, "SELECT count(*) AS n FROM artifacts")
        if stale == "missing":
            (env.outbox / MEMBER_DIRECTORY).unlink()
        elif stale == "old":
            write_feeds(env, directory_age=cbf.FEED_MAX_AGE + 5)
        elif stale == "future":
            write_feeds(env, directory_age=-3600)
        else:
            write_feeds(env, group="elsewhere")
        payload = {"member.artifact_create": {"title": "T", "filename": "a.html", "profile": "web"},
                   "member.publish": publish_payload(env, AK, aid),
                   "member.artifact_delete": {"confirm": True}}[kind]
        out = await send(env, user("alice"), kind, payload, aid=None if kind.endswith("create") else aid)
        assert out["result"] == "rejected"
        assert out["reason"] == "directory_stale"
        (n,) = await sql(env, "SELECT count(*) AS n FROM artifacts")
        assert n["n"] == before["n"]                         # nothing was created, published or deleted
        (art,) = await sql(env, "SELECT lifecycle, current_version_id FROM artifacts WHERE artifact_id=$1", aid)
        assert art["lifecycle"] == "active" and art["current_version_id"] is None
    mrun(registry_pg, tmp_path, body)


@pytest.mark.parametrize("actor,reason", [
    (agent("ext1"), "external_agent"), (user("carol"), "not_in_group"), (user("dave"), "user_disabled"),
    (user("zed"), "unknown_user"), (agent("bag"), None)])
def test_refused_members_are_rejected_with_the_reason_and_audited(registry_pg, tmp_path, actor, reason):
    async def body(env, rt):
        out = await send(env, actor, "member.artifact_create", {"title": "T", "filename": "a.html", "profile": "web"})
        if reason is None:
            assert out["result"] == "created"
            return
        assert out["result"] == "rejected" and out["reason"] == reason
        assert not await sql(env, "SELECT 1 FROM artifacts")
    mrun(registry_pg, tmp_path, body)


def test_a_disabled_member_feature_rejects_with_members_disabled(registry_pg, tmp_path):
    async def body(env, rt):
        out = await send(env, user("alice"), "member.artifact_create",
                         {"title": "T", "filename": "a.html", "profile": "web"})
        assert out["result"] == "rejected" and out["reason"] == "members_disabled"
    mrun(registry_pg, tmp_path, body, enabled=False)


# ----------------------------------------------------------------------------- create

def test_agent_and_user_actors_create_artifacts_owned_by_the_same_member(registry_pg, tmp_path):
    async def body(env, rt):
        a = await create(env, user("alice"), title="By user")
        b = await create(env, agent("ag1"), title="By agent")
        rows = await sql(env, "SELECT artifact_id, created_by, display_name, original_filename, payload "
                              "FROM artifacts ORDER BY created_at")
        assert [r["created_by"] for r in rows] == [ALICE, ALICE] and {r["artifact_id"] for r in rows} == {a, b}
        assert rows[0]["display_name"] == "By user" and rows[0]["original_filename"] == "paper.html"
        assert json.loads(rows[0]["payload"])["profile"] == "web"
        (use,) = await sql(env, "SELECT last_used_at FROM artifact_member_usage WHERE member_principal=$1", ALICE)
        assert use["last_used_at"] is not None
        ev = await sql(env, "SELECT action, outcome FROM artifact_member_events WHERE member_principal=$1", ALICE)
        assert {(e["action"], e["outcome"]) for e in ev} >= {("artifact_create", "ok")}
    mrun(registry_pg, tmp_path, body)


def test_a_replayed_create_returns_the_same_artifact(registry_pg, tmp_path):
    async def body(env, rt):
        a = await create(env, key="k-1")
        b = await create(env, key="k-1")
        assert a == b and len(await sql(env, "SELECT 1 FROM artifacts")) == 1
    mrun(registry_pg, tmp_path, body)


def test_without_limits_the_two_hundredth_artifact_is_created(registry_pg, tmp_path):
    async def body(env, rt):
        tickets = [env.intent(user("alice"), kind="member.artifact_create", key=f"bulk-{i}",
                              payload={"title": f"p{i}", "filename": "a.html", "profile": "web"})["ticket"]
                   for i in range(200)]                       # all through the handler, one settle
        for _ in range(20):                                   # the applier takes a bounded batch per tick
            await settle(env)
            if len(await sql(env, "SELECT 1 FROM artifacts")) == 200:
                break
        assert {env.result(t)["result"] for t in tickets} == {"created"}
        assert len(await sql(env, "SELECT 1 FROM artifacts WHERE created_by=$1", ALICE)) == 200
    mrun(registry_pg, tmp_path, body)


def test_create_idempotency_is_per_member_and_payload_across_actors(registry_pg, tmp_path):
    async def body(env, rt):
        pl = {"title": "T", "filename": "a.html", "profile": "web"}
        a = await send(env, user("alice"), "member.artifact_create", pl, key="same")
        b = await send(env, agent("ag1"), "member.artifact_create", pl, key="same")
        assert a["result"] == b["result"] == "created" and a["artifact_id"] == b["artifact_id"]
        c = await send(env, agent("ag1"), "member.artifact_create", {**pl, "title": "other"}, key="same")
        assert c == {**c, "result": "rejected", "reason": "idempotency_conflict"}
        d = await send(env, user("bob"), "member.artifact_create", pl, key="same")
        assert d["result"] == "created" and d["artifact_id"] != a["artifact_id"]      # keys are per member
        assert len(await sql(env, "SELECT 1 FROM artifacts")) == 2
    mrun(registry_pg, tmp_path, body)


def test_a_replayed_create_or_delete_is_refused_once_the_member_is_gone(registry_pg, tmp_path):
    async def body(env, rt):
        pl = {"title": "T", "filename": "a.html", "profile": "web"}
        a = await send(env, user("alice"), "member.artifact_create", pl, key="r-1")
        d = await send(env, user("alice"), "member.artifact_delete", {"confirm": True}, aid=a["artifact_id"], key="r-2")
        assert d["result"] == "deleted"
        write_feeds(env, users={"alice": {"status": "disabled"}, "bob": {"status": "approved"}})
        again = await send(env, user("alice"), "member.artifact_create", pl, key="r-1")
        assert again["result"] == "rejected" and again["reason"] == "user_disabled" and again["artifact_id"] is None
        gone = await send(env, user("alice"), "member.artifact_delete", {"confirm": True}, aid=a["artifact_id"],
                          key="r-2")
        assert gone["result"] == "rejected" and gone["reason"] == "user_disabled"
        write_feeds(env, directory_age=cbf.FEED_MAX_AGE + 5)
        stale = await send(env, user("alice"), "member.artifact_create", pl, key="r-1")
        assert stale["reason"] == "directory_stale"
    mrun(registry_pg, tmp_path, body)


def test_a_replayed_publish_is_refused_once_the_directory_is_stale(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        payload = publish_payload(env, AK, aid)
        a = await send(env, user("alice"), "member.publish", payload, aid=aid, key="p-1")
        assert a["result"] == "created"
        write_feeds(env, directory_age=cbf.FEED_MAX_AGE + 5)
        b = await send(env, user("alice"), "member.publish", payload, aid=aid, key="p-1")
        assert b["result"] == "rejected" and b["reason"] == "directory_stale" and "version_id" not in b
    mrun(registry_pg, tmp_path, body)


def test_conflicting_publishes_with_fresh_content_still_hit_the_member_byte_limit(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        first = await publish(env, aid, {"index.html": b"<html>0000</html>"})
        assert first["result"] == "created"
        reasons = []
        for i in range(1, 12):                                   # base None is stale: each is a conflict
            out = await publish(env, aid, {"index.html": b"<html>%04d</html>" % i})
            reasons.append(out.get("reason") or out["result"])
            (u,) = await sql(env, "SELECT bytes_used FROM artifact_member_usage WHERE member_principal=$1", ALICE)
            (blobs,) = await sql(env, "SELECT coalesce(sum(size_bytes),0)::bigint AS n FROM artifact_blobs "
                                      "WHERE scope=$1", aid)
            assert u["bytes_used"] == blobs["n"]                 # settled after every publish, whatever its outcome
        assert reasons[0] == "conflict" and "member_bytes" in reasons
        assert u["bytes_used"] <= 100
    mrun(registry_pg, tmp_path, body, defaults={"max_member_bytes": 100})


def test_a_max_artifacts_default_refuses_the_next_create(registry_pg, tmp_path):
    async def body(env, rt):
        for i in range(20):
            await create(env, title=f"p{i}")
        out = await send(env, user("alice"), "member.artifact_create",
                         {"title": "21", "filename": "a.html", "profile": "web"})
        assert out["result"] == "rejected" and out["reason"] == "member_artifacts"
        bob = await send(env, user("bob"), "member.artifact_create",
                         {"title": "b", "filename": "a.html", "profile": "web"})
        assert bob["result"] == "created"                       # the cap is per member
    mrun(registry_pg, tmp_path, body, defaults={"max_artifacts": 20})


# ----------------------------------------------------------------------------- publish

def test_a_three_chunk_staged_upload_publishes_and_the_staging_parts_go(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        payload = publish_payload(env, AK, aid, {"index.html": BIG_HTML, "style.css": b"p{}"}, message="first")
        parts_dir = next((env.outbox / "staging" / AK).iterdir())
        out = await send(env, agent("ag1"), "member.publish", payload, aid=aid)
        assert out["result"] == "created" and out["version_number"] == 1 and out["version_id"]
        (row,) = await sql(env, "SELECT * FROM artifact_versions WHERE artifact_id=$1", aid)
        assert row["created_by"] == ALICE and row["base_version_id"] is None
        prov = json.loads(row["provenance"])
        assert prov["via"] == "member_mcp" and prov["actor"] == {"kind": "agent", "id": "ag1"}
        assert prov["message"] == "first"
        names = {e["name"]: e for e in json.loads(row["output_manifest"])}
        assert set(names) == {"index.html", "style.css"}
        assert names["index.html"]["sha256"] == hashlib.sha256(BIG_HTML).hexdigest()
        assert names["index.html"]["media_type"] == "text/html" and names["style.css"]["media_type"] == "text/css"
        assert not (parts_dir / "0000000").exists()
        (use,) = await sql(env, "SELECT bytes_used FROM artifact_member_usage WHERE member_principal=$1", ALICE)
        assert use["bytes_used"] == len(BIG_HTML) + 3
        (upl,) = await sql(env, "SELECT charged FROM artifact_collab_uploads WHERE artifact_id=$1 AND name='index.html'",
                           aid)
        assert upl["charged"] is False                          # members are not on the per-agent daily quota
    mrun(registry_pg, tmp_path, body)


def test_the_user_actor_publishes_with_the_web_provenance(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        out = await publish(env, aid)
        assert out["result"] == "created"
        (row,) = await sql(env, "SELECT provenance FROM artifact_versions WHERE artifact_id=$1", aid)
        assert json.loads(row["provenance"])["via"] == "member_web"
    mrun(registry_pg, tmp_path, body)


def test_a_wrong_sha_is_rejected_and_the_staging_is_removed(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        payload = publish_payload(env, AK, aid, {"index.html": BIG_HTML})
        d = next((env.outbox / "staging" / AK).iterdir())
        (d / "0000001").write_bytes(b"y" * cbf.CHUNK_BYTES)     # same length, other bytes
        out = await send(env, user("alice"), "member.publish", payload, aid=aid)
        assert out["result"] == "rejected" and out["reason"] == FAILED
        assert not d.exists()
        assert not await sql(env, "SELECT 1 FROM artifact_versions WHERE artifact_id=$1", aid)
    mrun(registry_pg, tmp_path, body)


def test_another_members_upload_id_is_not_found(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        upl, _ = stage(env, BK, aid, "index.html", HTML)        # bob's staging, for alice's artifact
        out = await send(env, user("alice"), "member.publish", {
            "base_version_id": None, "message": "m", "files": [{"name": "index.html", "upload_id": upl}]}, aid=aid)
        assert out["result"] == "rejected" and out["reason"] == NOT_FOUND
    mrun(registry_pg, tmp_path, body)


def test_an_upload_staged_for_another_artifact_or_name_is_not_found(registry_pg, tmp_path):
    async def body(env, rt):
        aid, other = await create(env), await create(env, title="other")
        upl, _ = stage(env, AK, other, "index.html", HTML)
        out = await send(env, user("alice"), "member.publish", {
            "base_version_id": None, "message": "m", "files": [{"name": "index.html", "upload_id": upl}]}, aid=aid)
        assert out["reason"] == NOT_FOUND
        upl2, _ = stage(env, AK, aid, "other.html", HTML)
        out = await send(env, user("alice"), "member.publish", {
            "base_version_id": None, "message": "m", "files": [{"name": "index.html", "upload_id": upl2}]}, aid=aid)
        assert out["reason"] == NOT_FOUND
    mrun(registry_pg, tmp_path, body)


def test_another_members_artifact_is_not_found_even_with_a_share_grant(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, user("alice"))
        await sql(env, "INSERT INTO artifact_acl (artifact_id, principal_id, permissions, granted_by) "
                       "VALUES ($1,$2,'[\"read\",\"write\",\"admin\",\"share\"]'::jsonb,$3)", aid, BOB, ALICE)
        out = await publish(env, aid, actor=user("bob"), owner=BOB)
        assert out["result"] == "rejected" and out["reason"] == "not_found"
        out = await send(env, agent("bag"), "member.artifact_delete", {"confirm": True}, aid=aid)
        assert out["result"] == "rejected" and out["reason"] == "not_found"
        missing = await send(env, user("bob"), "member.artifact_delete", {"confirm": True}, aid="artifact_nope")
        assert missing["reason"] == "not_found"                  # no difference between absent and not yours
        (art,) = await sql(env, "SELECT lifecycle FROM artifacts WHERE artifact_id=$1", aid)
        assert art["lifecycle"] == "active"
    mrun(registry_pg, tmp_path, body)


@pytest.mark.parametrize("files", [{"style.css": b"p{}"}, {"app.js": b"1", "data.json": b"{}"},
                                   {"index.html": HTML, "run.exe": b"MZ"}, {"index.html": HTML, "x": b"1"}])
def test_publishes_without_an_html_or_pdf_or_with_other_types_are_refused(registry_pg, tmp_path, files):
    async def body(env, rt):
        aid = await create(env)
        out = await publish(env, aid, files)
        assert out["result"] == "rejected" and out["reason"], out
        assert not await sql(env, "SELECT 1 FROM artifact_versions WHERE artifact_id=$1", aid)
    mrun(registry_pg, tmp_path, body)


def test_a_pdf_alone_publishes_with_its_media_type(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, profile="document", filename="p.pdf")
        out = await publish(env, aid, {"paper.pdf": b"%PDF-1.4 x"})
        assert out["result"] == "created"
        (row,) = await sql(env, "SELECT output_manifest FROM artifact_versions WHERE artifact_id=$1", aid)
        assert json.loads(row["output_manifest"])[0]["media_type"] == "application/pdf"
    mrun(registry_pg, tmp_path, body)


def test_a_stale_base_is_a_conflict_with_the_head_and_the_right_base_publishes(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        first = await publish(env, aid, {"index.html": b"<html>1</html>"})
        stale = await publish(env, aid, {"index.html": b"<html>2</html>"}, base=None)
        assert stale["result"] == "conflict" and stale["head_version_id"] == first["version_id"]
        ok = await publish(env, aid, {"index.html": b"<html>3</html>"}, base=first["version_id"])
        assert ok["result"] == "created" and ok["version_number"] == 2
        (row,) = await sql(env, "SELECT base_version_id, parent_version_ids FROM artifact_versions "
                                "WHERE version_id=$1", ok["version_id"])
        assert row["base_version_id"] == first["version_id"]
    mrun(registry_pg, tmp_path, body)


def test_a_replayed_publish_returns_the_same_version(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        payload = publish_payload(env, AK, aid)
        a = await send(env, user("alice"), "member.publish", payload, aid=aid, key="pub-1")
        b = await send(env, user("alice"), "member.publish", payload, aid=aid, key="pub-1")
        assert a["version_id"] == b["version_id"]
        assert len(await sql(env, "SELECT 1 FROM artifact_versions WHERE artifact_id=$1", aid)) == 1
    mrun(registry_pg, tmp_path, body)


def test_member_bytes_limit_refuses_before_assembly_and_leaves_the_staging(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        payload = publish_payload(env, AK, aid, {"index.html": BIG_HTML})
        d = next((env.outbox / "staging" / AK).iterdir())
        out = await send(env, user("alice"), "member.publish", payload, aid=aid)
        assert out["result"] == "rejected" and out["reason"] == "member_bytes"
        assert d.is_dir() and not env.applier.rustfs.objects
    mrun(registry_pg, tmp_path, body, defaults={"max_member_bytes": 1000})


def test_a_retracted_artifact_refuses_publish_with_deleted(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        await sql(env, "UPDATE artifacts SET lifecycle='retracted' WHERE artifact_id=$1", aid)
        out = await publish(env, aid)
        assert out["result"] == "rejected" and out["reason"] == "deleted"
    mrun(registry_pg, tmp_path, body)


def test_an_entry_must_be_one_of_the_files(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        out = await publish(env, aid, {"index.html": HTML}, entry="missing.html")
        assert out["result"] == "rejected"
        ok = await publish(env, aid, {"a.html": HTML, "b.html": HTML + b" "}, entry="b.html")
        assert ok["result"] == "created"
    mrun(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- lanes

def test_one_member_is_one_lane_whatever_the_actor_and_members_differ(registry_pg, tmp_path):
    async def body(env, rt):
        spec = env.applier._kinds["member.publish"]
        mk = lambda actor: cbf.Intent("t", "member.publish", actor, "a", None, 1.0, None, "0" * 64)  # noqa: E731
        owned = {"owner": "alice"}
        assert spec.lane_key(mk(cbf.Actor("agent", "ag1")), owned) == spec.lane_key(mk(cbf.Actor("user", "alice")), None)
        assert spec.lane_key(mk(cbf.Actor("user", "bob")), None) != spec.lane_key(mk(cbf.Actor("user", "alice")), None)
        assert spec.lane_key(mk(cbf.Actor("user", "alice")), None).startswith("member:")
        for kind in KINDS:
            assert env.applier._kinds[kind].lane_key is not None
    mrun(registry_pg, tmp_path, body)


def test_two_publishes_from_a_user_and_her_agent_are_serialised_into_one_winner(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        one = env.intent(user("alice"), kind="member.publish", artifact_id=aid,
                         payload=publish_payload(env, AK, aid, {"index.html": b"<html>1</html>"}))
        two = env.intent(agent("ag1"), kind="member.publish", artifact_id=aid,
                         payload=publish_payload(env, AK, aid, {"index.html": b"<html>2</html>"}))
        await settle(env)
        results = sorted(env.result(t["ticket"])["result"] for t in (one, two))
        assert results == ["conflict", "created"]
        assert len(await sql(env, "SELECT 1 FROM artifact_versions WHERE artifact_id=$1", aid)) == 1
    mrun(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- delete

async def live_share(env, aid, vid, creator, state="live", sid=None, token="a"):
    sid = sid or uid("shr")
    await sql(env, "INSERT INTO artifact_shares (share_id, artifact_id, version_id, audience, state, created_by, "
                   "token_hash) VALUES ($1,$2,$3,'public',$4,$5,$6)", sid, aid, vid, state, creator, token * 64)
    return sid


def test_delete_retracts_revokes_every_live_share_and_clears_staging(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        pub = await publish(env, aid)
        shares = tmp_path / "shares"
        (shares / "by-token" / ("b" * 64)).mkdir(parents=True)
        (shares / "by-token" / ("b" * 64) / "state.json").write_text("{}")
        mine = await live_share(env, aid, pub["version_id"], ALICE, token="b")
        theirs = await live_share(env, aid, pub["version_id"], BOB, state="pending", token="c")
        agent_made = await live_share(env, aid, pub["version_id"], "agent:ag1", token="d")
        (shares / mine).mkdir()
        (shares / mine / "x").write_text("x")
        upl, d = stage(env, AK, aid, "later.html", HTML)         # an unpublished upload of this artifact
        _, keep = stage(env, AK, "artifact_other", "o.html", HTML)
        out = await send(env, user("alice"), "member.artifact_delete", {"confirm": True}, aid=aid)
        assert out["result"] == "deleted"
        (art,) = await sql(env, "SELECT lifecycle FROM artifacts WHERE artifact_id=$1", aid)
        assert art["lifecycle"] == "retracted"
        states = {r["share_id"]: r["state"] for r in await sql(env, "SELECT share_id, state FROM artifact_shares")}
        assert states == {mine: "revoked", theirs: "revoked", agent_made: "revoked"}
        assert not (shares / mine).exists() and not (shares / "by-token" / ("b" * 64)).exists()
        assert not d.exists() and keep.is_dir()
        again = await send(env, agent("ag1"), "member.artifact_delete", {"confirm": True}, aid=aid)
        assert again["result"] == "deleted"                      # idempotent, also from another actor of the member
        later = await publish(env, aid)
        assert later["result"] == "rejected" and later["reason"] == "deleted"
        view = view_of(env, ALICE)
        (item,) = view["artifacts"]
        assert item["lifecycle"] == "retracted" and view["usage"]["bytes_retracted"] == len(HTML)
        assert view["usage"]["bytes_used"] == len(HTML)
    mrun(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- staging sweep

def sweep(env):
    async def go():
        async with env.pool.acquire() as conn:
            return await env.applier.sweep_staging(conn, agents={}, grants=[])
    return go()


def test_a_members_staging_is_kept_while_valid_and_swept_after_the_ttl_or_when_disabled(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        _, fresh = stage(env, AK, aid, "a.html", HTML)
        _, old = stage(env, AK, aid, "b.html", HTML + b" ")
        age_dir(env, old, TTL + 5)
        assert await sweep(env) == 1 and fresh.is_dir() and not old.exists()
        write_feeds(env, users={"alice": {"status": "disabled"}})
        assert await sweep(env) == 1 and not fresh.exists()
        write_feeds(env, users={"alice": removed(env, GRACE + 1), "bob": removed(env, GRACE + 1)})
        _, bobs = stage(env, BK, aid, "c.html", HTML)            # bob is positively listed as removed
        assert await sweep(env) == 1 and not bobs.exists()
    mrun(registry_pg, tmp_path, body)


def test_unlisted_and_unknown_members_keep_their_staging_a_removed_one_loses_it(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        _, a = stage(env, AK, aid, "a.html", HTML)
        _, b = stage(env, BK, aid, "b.html", HTML)
        write_feeds(env, users={})                               # a restarted collab-public lists nobody
        assert await sweep(env) == 0 and a.is_dir() and b.is_dir()
        write_feeds(env, users={"alice": {"status": "unknown", "groups": ["lab"]}})
        assert await sweep(env) == 0 and a.is_dir() and b.is_dir()
        write_feeds(env, users={"alice": removed(env, GRACE + 1, ["other"])})
        assert await sweep(env) == 1 and not a.exists() and b.is_dir()
    mrun(registry_pg, tmp_path, body)


def test_a_restart_unknown_or_absent_directory_revokes_nothing_but_removed_and_disabled_do(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, user("alice"))
        pub = await publish(env, aid)
        sid = await live_share(env, aid, pub["version_id"], ALICE)
        pend = await live_share(env, aid, pub["version_id"], ALICE, state="pending", token="b")

        async def states():
            return {r["share_id"]: r["state"] for r in await sql(env, "SELECT share_id, state FROM artifact_shares")}
        for users in ({}, {"alice": {"status": "unknown", "groups": ["lab"]}}, {"bob": {"status": "approved"}}):
            write_feeds(env, users=users)
            async with env.pool.acquire() as conn:
                assert await sweep_disabled_members(conn, rt) == 0
            assert await states() == {sid: "live", pend: "pending"}
        write_feeds(env, users={"alice": removed(env, GRACE + 1)})
        async with env.pool.acquire() as conn:
            assert await sweep_disabled_members(conn, rt) == 2
        assert set((await states()).values()) == {"revoked"}
    mrun(registry_pg, tmp_path, body)


def test_removed_acts_only_after_the_grace_period_and_only_with_a_removed_since(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, user("alice"))
        pub = await publish(env, aid)
        sid = await live_share(env, aid, pub["version_id"], ALICE)
        _, staged = stage(env, AK, aid, "a.html", HTML)

        async def state():
            return (await sql(env, "SELECT state FROM artifact_shares WHERE share_id=$1", sid))[0]["state"]
        for entry in (removed(env, GRACE - 5), removed(env, 0), {"status": "removed", "groups": []},
                      {"status": "removed", "groups": [], "removed_since": None},
                      {"status": "removed", "groups": [], "removed_since": env.clock() + 99}):
            write_feeds(env, users={"alice": entry})
            async with env.pool.acquire() as conn:
                assert await sweep_disabled_members(conn, rt) == 0
            assert await state() == "live" and await sweep(env) == 0 and staged.is_dir()
            out = await send(env, user("alice"), "member.artifact_create",
                             {"title": "T", "filename": "a.html", "profile": "web"})
            assert out["result"] == "rejected" and out["reason"] == "not_in_group"      # writes are refused at once
        write_feeds(env, users={"alice": removed(env, GRACE + 1)})
        async with env.pool.acquire() as conn:
            assert await sweep_disabled_members(conn, rt) == 1
        assert await state() == "revoked" and await sweep(env) >= 1 and not staged.exists()
    mrun(registry_pg, tmp_path, body)


def test_a_member_re_added_before_the_grace_ends_is_never_revoked(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, user("alice"))
        pub = await publish(env, aid)
        sid = await live_share(env, aid, pub["version_id"], ALICE)
        write_feeds(env, users={"alice": removed(env, GRACE - 60)})
        env.clock.t += 120                                       # the grace would now be over ...
        write_feeds(env, users={"alice": {"status": "approved", "groups": ["lab"]}})      # ... but she was re-added
        async with env.pool.acquire() as conn:
            assert await sweep_disabled_members(conn, rt) == 0
        assert (await sql(env, "SELECT state FROM artifact_shares WHERE share_id=$1", sid))[0]["state"] == "live"
    mrun(registry_pg, tmp_path, body)


def test_a_disabled_member_is_revoked_even_when_the_directory_says_nothing_else(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, user("alice"))
        pub = await publish(env, aid)
        sid = await live_share(env, aid, pub["version_id"], ALICE)
        write_feeds(env, users={"alice": {"status": "disabled", "groups": ["lab"]}})
        async with env.pool.acquire() as conn:
            assert await sweep_disabled_members(conn, rt) == 1
        assert (await sql(env, "SELECT state FROM artifact_shares WHERE share_id=$1", sid))[0]["state"] == "revoked"
    mrun(registry_pg, tmp_path, body)


def test_an_unknown_member_is_refused_new_writes_with_directory_unknown_and_nothing_is_deleted(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, user("alice"))
        pub = await publish(env, aid)
        sid = await live_share(env, aid, pub["version_id"], ALICE)
        write_feeds(env, users={"alice": {"status": "unknown", "groups": ["lab"]}})
        out = await send(env, user("alice"), "member.artifact_create",
                         {"title": "T", "filename": "a.html", "profile": "web"})
        assert out["result"] == "rejected" and out["reason"] == "directory_unknown"
        assert (await sql(env, "SELECT state FROM artifact_shares WHERE share_id=$1", sid))[0]["state"] == "live"
        assert len(await sql(env, "SELECT 1 FROM artifacts")) == 1
    mrun(registry_pg, tmp_path, body)


def test_a_stale_directory_proves_nothing_so_only_the_ttl_applies(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        _, d = stage(env, AK, aid, "a.html", HTML)
        write_feeds(env, directory_age=cbf.FEED_MAX_AGE + 50, users={})
        assert await sweep(env) == 0 and d.is_dir()
    mrun(registry_pg, tmp_path, body)


def test_a_pending_member_publish_protects_its_uploads_from_the_sweep(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env)
        upl, d = stage(env, AK, aid, "a.html", HTML)
        age_dir(env, d, TTL + 5)
        env.intent(user("alice"), kind="member.publish", artifact_id=aid,
                   payload={"base_version_id": None, "message": "m", "files": [{"name": "a.html", "upload_id": upl}]})
        assert await sweep(env) == 0 and d.is_dir()
    mrun(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- the member view

def test_a_share_only_grant_appears_in_shared_with_me(registry_pg, tmp_path):
    async def body(env, rt):
        bob_aid = await create(env, user("bob"), title="Bobs")
        await publish(env, bob_aid, {"index.html": b"<html>b</html>"}, actor=user("bob"), owner=BOB)
        await sql(env, "INSERT INTO artifact_acl (artifact_id, principal_id, permissions, granted_by) "
                       "VALUES ($1,$2,'[\"share\"]'::jsonb,$3)", bob_aid, ALICE, BOB)
        async with env.pool.acquire() as conn:
            await write_member_view(conn, ALICE, env.mirror_root)
        (shared,) = view_of(env, ALICE)["shared_with_me"]
        assert shared["artifact_id"] == bob_aid and shared["permissions"] == ["share"]
    mrun(registry_pg, tmp_path, body)


def test_the_view_lists_artifacts_versions_links_usage_and_shared_with_me(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, title="Mine")
        v1 = await publish(env, aid, {"index.html": HTML}, message="one")
        v2 = await publish(env, aid, {"index.html": HTML + b" "}, base=v1["version_id"], message="two")
        bob_aid = await create(env, user("bob"), title="Bobs")
        bob_v = await publish(env, bob_aid, {"index.html": b"<html>b</html>"}, actor=user("bob"), owner=BOB)
        await sql(env, "INSERT INTO artifact_acl (artifact_id, principal_id, permissions, granted_by) "
                       "VALUES ($1,$2,'[\"read\"]'::jsonb,$3)", bob_aid, ALICE, BOB)
        sid = await live_share(env, aid, v2["version_id"], ALICE)
        async with env.pool.acquire() as conn:
            await write_member_view(conn, ALICE, env.mirror_root)
        view = view_of(env, ALICE)
        assert view["principal"] == ALICE and isinstance(view["updated"], float)
        (art,) = view["artifacts"]
        assert art["artifact_id"] == aid and art["title"] == "Mine" and art["lifecycle"] == "active"
        assert art["head_version_id"] == v2["version_id"] and art["head_version_number"] == 2
        assert [x["version_number"] for x in art["versions"]] == [2, 1]
        assert art["versions"][1]["message"] == "one" and art["versions"][0]["parent_version_ids"] == [
            v1["version_id"]]
        assert art["bytes"] == 2 * len(HTML) + 1
        usage = view["usage"]
        assert usage["artifact_count"] == 1 and usage["version_count"] == 2 and usage["bytes_used"] == art["bytes"]
        assert usage["bytes_retracted"] == 0 and usage["sharing_disabled"] is False
        assert set(usage["limits"]) == {"max_artifact_bytes", "max_member_bytes", "max_artifacts"}
        (shared,) = view["shared_with_me"]
        assert shared["artifact_id"] == bob_aid and shared["title"] == "Bobs" and shared["permissions"] == ["read"]
        assert shared["head_version_id"] == bob_v["version_id"]
        (link,) = view["links"]
        assert link["share_id"] == sid and link["audience"] == "public" and link["state"] == "live"
        assert set(link) == {"share_id", "artifact_id", "version_id", "audience", "state", "expires_at",
                             "recipients", "created_at", "mine"}
        assert link["mine"] is True
        mine = json.dumps(view_of(env, BOB))
        assert aid not in mine and "Mine" not in mine and sid not in mine      # bob's view never shows alice's data
    mrun(registry_pg, tmp_path, body)


def test_an_applied_intent_rewrites_the_view_of_the_acting_member_only(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, title="Fresh")
        assert [a["artifact_id"] for a in view_of(env, ALICE)["artifacts"]] == [aid]
        assert not (env.mirror_root / "member-views" / (BK + ".json")).exists()
        await publish(env, aid)
        assert view_of(env, ALICE)["artifacts"][0]["head_version_number"] == 1
    mrun(registry_pg, tmp_path, body)


def test_the_publisher_writes_every_known_members_view(registry_pg, tmp_path):
    async def body(env, rt):
        await create(env, user("alice"))
        await create(env, user("bob"))
        for key in (AK, BK):
            (env.mirror_root / "member-views" / (key + ".json")).unlink()
        async with env.pool.acquire() as conn:
            await publish_member_views(conn, rt)
        assert view_of(env, ALICE)["principal"] == ALICE and view_of(env, BOB)["principal"] == BOB
    mrun(registry_pg, tmp_path, body)


def test_the_publisher_revokes_the_shares_of_a_disabled_member_and_only_theirs(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, user("alice"))
        pub = await publish(env, aid)
        bob_aid = await create(env, user("bob"))
        bob_pub = await publish(env, bob_aid, {"index.html": HTML}, actor=user("bob"), owner=BOB)
        a = await live_share(env, aid, pub["version_id"], ALICE)
        b = await live_share(env, bob_aid, bob_pub["version_id"], BOB, token="c")
        write_feeds(env, users={"alice": {"status": "disabled"}, "bob": {"status": "approved"}})
        async with env.pool.acquire() as conn:
            await publish_member_views(conn, rt)
        states = {r["share_id"]: r["state"] for r in await sql(env, "SELECT share_id, state FROM artifact_shares")}
        assert states == {a: "revoked", b: "live"}
        write_feeds(env, users={}, directory_age=cbf.FEED_MAX_AGE + 5)      # a stale directory revokes nothing
        c = await live_share(env, bob_aid, bob_pub["version_id"], BOB, token="e")
        async with env.pool.acquire() as conn:
            await publish_member_views(conn, rt)
        assert (await sql(env, "SELECT state FROM artifact_shares WHERE share_id=$1", c))[0]["state"] == "live"
    mrun(registry_pg, tmp_path, body)


def test_the_results_never_echo_payload_text(registry_pg, tmp_path):
    async def body(env, rt):
        out = await send(env, user("alice"), "member.artifact_create",
                         {"title": "SECRET-TITLE", "filename": "a.html", "profile": "web"})
        assert "SECRET-TITLE" not in json.dumps(out)
        assert Path(env.mirror_root / "results").is_dir()
    mrun(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- lock order: usage row, then artifact row

_PROBE_SQL = (
    """CREATE OR REPLACE FUNCTION lock_order_probe_fn() RETURNS trigger AS $$
    BEGIN
      -- sleep only when this transaction already holds an artifact row lock (xmax = our xid): the publish's stage 3
      -- in the wrong order. Usage-first code reaches this trigger before it holds any artifact row.
      IF EXISTS (SELECT 1 FROM artifacts WHERE xmax::text = (txid_current() % 4294967296)::text) THEN
        PERFORM pg_sleep(0.6);
      END IF;
      RETURN NEW;
    END $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS lock_order_probe_trg ON artifact_member_usage",
    """CREATE TRIGGER lock_order_probe_trg BEFORE INSERT ON artifact_member_usage
      FOR EACH ROW EXECUTE FUNCTION lock_order_probe_fn()""",
)


def test_member_publish_and_purge_take_the_usage_row_before_the_artifact_row(registry_pg, tmp_path):
    """The trigger stretches the window between the publish's stage-3 locks (real Postgres sleep); the purge starts
    inside it. With the artifact row taken first a deadlock follows; usage -> row on both sides queues them."""
    from herdr_engineering.control_plane.artifacts import ArtifactStore
    from tests.control_plane.registry_pg import FakeBlobs

    async def body(env, rt):
        try:
            for round_no in range(3):
                aid = await create(env, title=f"T{round_no}")
                for statement in _PROBE_SQL:
                    await sql(env, statement)
                box = {}

                async def purge():
                    conn = await asyncpg.connect(registry_pg["dsn"])
                    try:
                        for _ in range(200):                  # wait until a publish backend sleeps in the trigger
                            if await conn.fetchval("SELECT count(*) FROM pg_stat_activity WHERE wait_event='PgSleep'"):
                                break
                            await asyncio.sleep(0.02)
                        return await ArtifactStore(conn, FakeBlobs()).purge_now(aid, actor="op", reason="x")
                    finally:
                        await conn.close()

                def target():
                    try:
                        box["purge"] = asyncio.run(purge())
                    except BaseException as exc:             # noqa: BLE001
                        box["purge"] = exc
                thread = threading.Thread(target=target, daemon=True)
                thread.start()
                try:
                    out = await publish(env, aid, {"index.html": HTML + str(round_no).encode()})
                except BaseException as exc:                  # noqa: BLE001
                    out = exc
                await asyncio.get_running_loop().run_in_executor(None, thread.join, 30)
                assert not isinstance(box["purge"], BaseException), box["purge"]     # no DeadlockDetectedError
                assert isinstance(out, dict) and out["result"] in ("created", "rejected", "failed"), out
                assert box["purge"]["result"] in ("deleted", "already_purged")
        finally:
            await sql(env, "DROP TRIGGER IF EXISTS lock_order_probe_trg ON artifact_member_usage")
            await sql(env, "DROP FUNCTION IF EXISTS lock_order_probe_fn()")
    mrun(registry_pg, tmp_path, body)

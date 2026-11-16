# ruff: noqa: F811  (pytest fixtures are imported by name)
"""``annotation_resolve``: the registry entry, the worker handler through the real applier, and the mirror publish."""
import json
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane import annotation_intents
from herdr_engineering.control_plane import collab_bridge_files as cbf
from herdr_engineering.control_plane.annotation_intents import (
    AnnotationResolvePayload,
    handle_annotation_resolve,
    register_annotation_resolve,
)
from herdr_engineering.control_plane.artifact_annotations import AnnotationStore
from herdr_engineering.control_plane.artifact_collab_store import CollabGrantStore
from herdr_engineering.control_plane.artifact_shares import ShareStore, new_token
from herdr_engineering.control_plane.artifacts import ArtifactStore
from herdr_engineering.control_plane.collab_bridge_files import (
    BridgeLimits,
    Intent,
    IntentError,
    new_ticket,
    validate_intent,
)
from herdr_engineering.control_plane.collab_intents import _check_outcome
from herdr_engineering.control_plane.share_bundle import BundleFile, BundleIndex
from tests.control_plane.registry_pg import ROOT, registry_pg, uid  # noqa: F401 - imported fixture
from tests.control_plane.test_annotation_worker import housekeeper, mirrored, real_mirror
from tests.control_plane.test_artifact_annotations_store import KW, create
from tests.control_plane.test_collab_intents import (
    BASE_DOC,
    RecMirror,
    agent,
    commit,
    edit,
    grant,
    owner_edit,
    put,
    run,
    seed,
    settle,
    sql,
)
from tests.control_plane.test_collab_tasks import W, svc  # noqa: F401 - imported fixture

LIMITS = BridgeLimits()
ANCHOR = {"kind": "text_quote", "file": "doc.md", "exact": "line"}
IDS = ["ann_" + "A" * 22, "ann_" + "B" * 22]


def raw_intent(payload=None, *, actor="user", artifact_id="art_1", key="k1", kind="annotation_resolve"):
    raw = {"ticket": new_ticket(), "kind": kind, "actor": {"kind": actor, "id": "a1"}, "submitted_at": time.time(),
           "payload": payload if payload is not None else {"version_id": "v1", "annotation_ids": IDS[:1]}}
    if actor == "agent":
        raw["actor"]["id"] = uid("agent")
    if artifact_id is not None:
        raw["artifact_id"] = artifact_id
    if key is not None:
        raw["idempotency_key"] = key
    return raw


def valid(raw):
    return validate_intent(raw, filename=raw["ticket"] + ".json", limits=LIMITS)


# ------------------------------------------------------------------------------------------------ registry

def test_a_well_formed_resolve_is_accepted_and_typed():
    intent = valid(raw_intent(actor="agent"))
    assert intent.payload == AnnotationResolvePayload("v1", (IDS[0],)) and intent.artifact_id == "art_1"


@pytest.mark.parametrize("make", [
    lambda: raw_intent({"version_id": "v1", "annotation_ids": IDS[:1], "changes": []}, actor="agent"),
    lambda: raw_intent({"version_id": "v1"}, actor="agent"),
    lambda: raw_intent(actor="user"),
    lambda: raw_intent({"version_id": "v1", "annotation_ids": [f"ann_{i:022d}" for i in range(51)]}, actor="agent"),
    lambda: raw_intent({"version_id": "v1", "annotation_ids": [IDS[0], IDS[0]]}, actor="agent"),
    lambda: raw_intent({"version_id": "v1", "annotation_ids": []}, actor="agent"),
    lambda: raw_intent({"version_id": "v1", "annotation_ids": ["nope"]}, actor="agent"),
    lambda: raw_intent({"version_id": 5, "annotation_ids": IDS[:1]}, actor="agent"),
    lambda: raw_intent(actor="agent", artifact_id=None),
    lambda: raw_intent(actor="agent", kind="share_create"),
])
def test_the_registry_refuses_malformed_resolves(make):
    with pytest.raises(IntentError):
        valid(make())


def test_fifty_ids_are_accepted():
    ids = [f"ann_{i:022d}" for i in range(50)]
    assert len(valid(raw_intent({"version_id": "v1", "annotation_ids": ids}, actor="agent")).payload.annotation_ids) == 50


def test_a_second_registration_raises():
    with pytest.raises(ValueError):
        cbf.register_intent_kind("annotation_resolve", lambda p, limits: p, actors=frozenset({"agent"}))


def test_plan_three_alone_does_not_know_the_kind():
    code = ("import json,sys,time\n"
            "from herdr_engineering.control_plane import collab_intents\n"
            "from herdr_engineering.control_plane import collab_bridge_files as c\n"
            "raw={'ticket':c.new_ticket(),'kind':'annotation_resolve','actor':{'kind':'agent','id':'a1'},"
            "'artifact_id':'art_1','idempotency_key':'k','submitted_at':time.time(),"
            "'payload':{'version_id':'v','annotation_ids':['ann_'+'A'*22]}}\n"
            "try:\n c.validate_intent(raw, filename=raw['ticket']+'.json', limits=c.BridgeLimits())\n print('accepted')\n"
            "except c.IntentError as e:\n print('refused:', e)\n")
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("refused:")


def test_the_module_is_light():
    code = ("import sys\nfrom herdr_engineering.control_plane import annotation_intents\n"
            "print(any(m in sys.modules for m in ('asyncpg','herdr_engineering.control_plane.collab_intents',"
            "'herdr_engineering.control_plane.artifact_annotations')))")
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"


# ------------------------------------------------------------------------------------------------ outcome

def test_check_outcome_accepts_50_and_refuses_51_and_bad_shapes():
    ok = {"result": "resolved", "resolved": IDS, "skipped": [{"annotation_id": IDS[0], "reason": "not_open"}]}
    _check_outcome(ok)
    _check_outcome({"result": "resolved", "resolved": [f"ann_{i:022d}" for i in range(50)], "skipped": []})
    for bad in ({"result": "resolved", "resolved": [f"ann_{i:022d}" for i in range(51)]},
                {"result": "resolved", "resolved": "x"}, {"result": "resolved", "resolved": [1]},
                {"result": "resolved", "skipped": [{"annotation_id": "a", "reason": "r"}] * 51},
                {"result": "resolved", "skipped": [{"annotation_id": "a", "reason": 3}]},
                {"result": "resolved", "skipped": ["x"]},
                {"result": "resolved", "skipped": [{"annotation_id": "a", "reason": "r", "extra": 1}]}):
        with pytest.raises(TypeError):
            _check_outcome(bad)


# ------------------------------------------------------------------------------------------------ handler

async def make_share(env, aid, vid, *, emails=("a@x.org",)):
    async with env.pool.acquire() as conn:
        shares = ShareStore(conn)
        share = await shares.create(artifact_id=aid, version_id=vid, audience="lab", created_by="owner",
                                    allow_emails=list(emails))
        await shares.mark_live(share["share_id"], token_hash=new_token()[1],
                               index=BundleIndex("doc.md", [BundleFile("doc.md", "a" * 64, 1, "text/markdown")], 1, []),
                               approved_by="op")
    return share["share_id"]


async def annotate(env, sid, n, *, anchor=None):
    ident = "ann_" + secrets.token_hex(11)                  # the database is shared by the module's tests
    async with env.pool.acquire() as conn:
        out = await AnnotationStore(conn).ingest(conn, create(sid, n, annotation_id=ident, anchor=anchor or dict(ANCHOR)),
                                                 "key" + ident, **KW)
    assert out == "inserted"
    return ident


def resolve_raw(env, a, aid, version_id, ids, *, key="rk1", ticket=None):
    return env.intent(agent(a), kind="annotation_resolve", artifact_id=aid, key=key, ticket=ticket,
                      payload={"version_id": version_id, "annotation_ids": ids})


def resolving(pg, tmp_path, body, **kw):
    kw.setdefault("mirror_cls", RecMirror)

    async def go(env):
        env.applier.register_commit()
        register_annotation_resolve(env.applier)
        a = uid("agent")
        env.feed({a: "active"})
        return await body(env, a)
    return run(pg, tmp_path, go, **kw)


async def agent_version(env, a, aid, base):
    """The agent's own merged version, made through the real commit path."""
    raw = commit(env, a, aid, base, [put("doc.md", edit(BASE_DOC, 2, "agent"))])
    await settle(env)
    return env.result(raw["ticket"])["version_id"]


async def status_of(env, ident):
    (row,) = await sql(env, "SELECT status, addressed_in_version_id, resolved_by FROM artifact_annotations "
                            "WHERE annotation_id=$1", ident)
    return dict(row)


def test_the_agents_own_merged_version_resolves_and_writes_events(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        sid = await make_share(env, aid, v1)
        ident = await annotate(env, sid, 1)
        mine = await agent_version(env, a, aid, v1)
        raw = resolve_raw(env, a, aid, mine, [ident, IDS[0]])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "resolved" and doc["resolved"] == [ident]
        assert doc["skipped"] == [{"annotation_id": IDS[0], "reason": "not_found"}]
        assert await status_of(env, ident) == {"status": "addressed", "addressed_in_version_id": mine,
                                               "resolved_by": f"collab:{a}"}
        events = await sql(env, "SELECT kind, actor FROM artifact_share_events WHERE kind='annotation_addressed'")
        assert [(e["kind"], e["actor"]) for e in events] == [("annotation_addressed", f"collab:{a}")]
        (row,) = await sql(env, "SELECT outcome FROM artifact_collab_intents WHERE ticket_id=$1", raw["ticket"])
        assert row["outcome"] == "resolved"
    resolving(registry_pg, tmp_path, body)


def test_the_owners_version_is_not_the_agents_own(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        sid = await make_share(env, aid, v1)
        ident = await annotate(env, sid, 1)
        v2 = await owner_edit(env, aid, v1, edit(BASE_DOC, 3, "owner"))
        raw = resolve_raw(env, a, aid, v2, [ident])
        await settle(env)
        assert env.result(raw["ticket"])["result"] == "rejected"
        assert env.result(raw["ticket"])["reason"] == "not_own_version"
        assert (await status_of(env, ident))["status"] == "open"
    resolving(registry_pg, tmp_path, body)


def test_an_older_version_is_skipped_and_a_withdrawn_id_is_not_open(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        mine = await agent_version(env, a, aid, v1)
        v3 = await owner_edit(env, aid, mine, edit(BASE_DOC, 5, "later"))
        sid = await make_share(env, aid, v3)
        newer, gone = await annotate(env, sid, 1), await annotate(env, sid, 2)
        await sql(env, "UPDATE artifact_annotations SET status='withdrawn' WHERE annotation_id=$1", gone)
        raw = resolve_raw(env, a, aid, mine, [newer, gone])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "resolved" and doc["resolved"] == []
        assert {(s["annotation_id"], s["reason"]) for s in doc["skipped"]} == {
            (newer, "version_not_newer"), (gone, "not_open")}
    resolving(registry_pg, tmp_path, body)


def test_a_version_of_another_artifact_is_bad_version(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        other, o1 = await seed(env)
        await grant(env, aid, a)
        raw = resolve_raw(env, a, aid, o1, IDS[:1])
        await settle(env)
        assert env.result(raw["ticket"])["reason"] == "bad_version"
    resolving(registry_pg, tmp_path, body)


def test_a_grant_revoked_after_the_intent_was_written_rejects(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        mine = await agent_version(env, a, aid, v1)
        sid = await make_share(env, aid, v1)
        ident = await annotate(env, sid, 1)
        raw = resolve_raw(env, a, aid, mine, [ident])
        async with env.pool.acquire() as conn:
            await CollabGrantStore(conn).revoke(aid, a)
        await settle(env)
        assert env.result(raw["ticket"])["result"] == "rejected"
        assert (await status_of(env, ident))["status"] == "open"
    resolving(registry_pg, tmp_path, body)


async def regrant(env, aid, a, permission, days):
    await grant(env, aid, a, permission, days=days)


def test_a_grant_downgraded_to_read_after_the_agent_committed_cannot_resolve(registry_pg, tmp_path):
    """The agent holds its OWN version, so only the permission check can refuse."""
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        sid = await make_share(env, aid, v1)
        ident = await annotate(env, sid, 1)
        mine = await agent_version(env, a, aid, v1)
        await regrant(env, aid, a, "read", 1.0)
        raw = resolve_raw(env, a, aid, mine, [ident])
        await settle(env)
        doc = env.result(raw["ticket"])
        assert doc["result"] == "rejected" and doc["reason"] == "no write grant"
        assert (await status_of(env, ident))["status"] == "open"
    resolving(registry_pg, tmp_path, body)


@pytest.mark.parametrize("days,allowed", [(-1.0, False), (0.0, False), (1 / 24, True)],
                         ids=["past", "boundary_now", "future_hour"])
def test_grant_expiry_is_enforced_by_the_handler(registry_pg, tmp_path, days, allowed):
    """past and ``expires == now`` (already behind the clock by the time the handler runs) refuse; a future one works."""
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        sid = await make_share(env, aid, v1)
        ident = await annotate(env, sid, 1)
        mine = await agent_version(env, a, aid, v1)
        await regrant(env, aid, a, "write", days)
        raw = resolve_raw(env, a, aid, mine, [ident])
        await settle(env)
        doc = env.result(raw["ticket"])
        if allowed:
            assert doc["result"] == "resolved" and doc["resolved"] == [ident]
        else:
            assert doc["result"] == "rejected" and doc["reason"] == "no write grant"
            assert (await status_of(env, ident))["status"] == "open"
    resolving(registry_pg, tmp_path, body)


def test_a_missing_idempotency_key_is_a_bad_envelope(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        raw = resolve_raw(env, a, aid, v1, IDS[:1], key=None)
        await settle(env)
        assert env.result(raw["ticket"]) | {} and env.result(raw["ticket"])["reason"] == "bad_envelope"
    resolving(registry_pg, tmp_path, body)


def test_replaying_a_key_returns_the_same_result_without_a_second_update(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        sid = await make_share(env, aid, v1)
        ident = await annotate(env, sid, 1)
        mine = await agent_version(env, a, aid, v1)
        first = resolve_raw(env, a, aid, mine, [ident], key="same")
        await settle(env)
        (before,) = await sql(env, "SELECT updated_at FROM artifact_annotations WHERE annotation_id=$1", ident)
        second = resolve_raw(env, a, aid, mine, [ident], key="same")
        await settle(env)
        one, two = env.result(first["ticket"]), env.result(second["ticket"])
        assert two["result"] == "resolved" and two["resolved"] == one["resolved"] == [ident]
        assert two["skipped"] == one["skipped"] == []
        (after,) = await sql(env, "SELECT updated_at FROM artifact_annotations WHERE annotation_id=$1", ident)
        assert before["updated_at"] == after["updated_at"]
        events = await sql(env, "SELECT 1 FROM artifact_share_events WHERE kind='annotation_addressed' "
                                "AND share_id=$1", sid)
        assert len(events) == 1
    resolving(registry_pg, tmp_path, body)


def test_crash_retry_reports_not_open_the_second_time(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        sid = await make_share(env, aid, v1)
        ident = await annotate(env, sid, 1)
        mine = await agent_version(env, a, aid, v1)
        from herdr_engineering.control_plane.collab_intents import IntentContext
        async with env.pool.acquire() as conn:
            ctx = IntentContext(actor=cbf.Actor("agent", a), agent=None, conn=conn, store=ArtifactStore(conn, env.applier.rustfs),
                                grants=CollabGrantStore(conn), limits=LIMITS, assemble=None, extras={})
            intent = Intent("ict_" + "0" * 24, "annotation_resolve", ctx.actor, aid, "k", time.time(),
                            AnnotationResolvePayload(mine, (ident,)), "0" * 64)
            first = await handle_annotation_resolve(ctx, intent)
            again = await handle_annotation_resolve(ctx, intent)
        assert first["resolved"] == [ident] and first["skipped"] == []
        assert again["resolved"] == [] and again["skipped"] == [{"annotation_id": ident, "reason": "not_open"}]
    resolving(registry_pg, tmp_path, body)


def test_the_handler_ignores_the_payloads_claims_about_other_artifacts(registry_pg, tmp_path):
    """An annotation of artifact B named in a resolve for artifact A is never touched."""
    async def body(env, a):
        aid, v1 = await seed(env)
        other, o1 = await seed(env)
        await grant(env, aid, a)
        sid = await make_share(env, other, o1)
        foreign = await annotate(env, sid, 1)
        mine = await agent_version(env, a, aid, v1)
        raw = resolve_raw(env, a, aid, mine, [foreign])
        await settle(env)
        assert env.result(raw["ticket"])["skipped"] == [{"annotation_id": foreign, "reason": "not_found"}]
        assert (await status_of(env, foreign))["status"] == "open"
    resolving(registry_pg, tmp_path, body)


# ------------------------------------------------------------------------------------------------ mirror

def test_the_mirror_drops_resolved_and_deleted_annotations_after_a_tick(registry_pg, tmp_path):
    async def body(env, a):
        aid, v1 = await seed(env)
        await grant(env, aid, a)
        sid = await make_share(env, aid, v1)
        keep, resolved, deleted = [await annotate(env, sid, n) for n in (1, 2, 3)]
        mine = await agent_version(env, a, aid, v1)
        mirror = real_mirror(tmp_path / "hk", aid)
        async with env.pool.acquire() as conn:
            hk = housekeeper(tmp_path / "hk", mirror=mirror)
            await hk.tick(conn)
            listed = [r["annotation_id"] for r in mirrored(mirror, aid)["annotations"]]
            assert sorted(listed) == sorted([keep, resolved, deleted])
            resolve_raw(env, a, aid, mine, [resolved])
            await settle(env)
            await AnnotationStore(conn).ingest(conn, {"op": "delete", "share_id": sid, "annotation_id": deleted,
                                                      "author_identity": "u1", "author_email": "A@x.org", "basis": "e", "at": datetime.now(UTC).isoformat()},
                                               "del" + deleted, **KW)
            await hk.tick(conn)
            assert [r["annotation_id"] for r in mirrored(mirror, aid)["annotations"]] == [keep], (
                await status_of(env, resolved), await status_of(env, deleted))
    resolving(registry_pg, tmp_path, body)


def test_publish_for_a_removed_artifact_writes_nothing_and_creates_no_directory(registry_pg, tmp_path):
    import asyncio
    mirror = real_mirror(tmp_path, "art_gone")
    root = mirror.root / "artifacts" / "art_gone"
    (root / "head.json").unlink()
    root.rmdir()
    assert asyncio.run(mirror.publish_annotations("art_gone", [{"annotation_id": IDS[0]}])) is False
    assert not (mirror.root / "artifacts" / "art_gone").exists()
    assert not (mirror.root / "artifacts").exists() or not list((mirror.root / "artifacts").iterdir())


def test_publish_caps_the_file_at_one_mib_newest_first_and_marks_truncation(tmp_path):
    import asyncio
    mirror = real_mirror(tmp_path, "art_big")
    rows = [{"annotation_id": f"ann_{i:022d}", "body": "x" * 4000} for i in range(400)]
    assert asyncio.run(mirror.publish_annotations("art_big", rows)) is True
    path = mirror.root / "artifacts" / "art_big" / "annotations.json"
    doc = json.loads(path.read_text())
    assert path.stat().st_size <= 1024 * 1024 and doc["truncated"] is True
    assert doc["annotations"][0]["annotation_id"] == rows[0]["annotation_id"] and 0 < len(doc["annotations"]) < 400
    assert set(doc) == {"artifact_id", "generated_at", "annotations", "truncated"}


def test_removing_the_artifact_removes_the_annotations_file(tmp_path):
    import asyncio
    mirror = real_mirror(tmp_path, "art_rm")
    asyncio.run(mirror.publish_annotations("art_rm", [{"annotation_id": IDS[0]}]))
    assert (mirror.root / "artifacts" / "art_rm" / "annotations.json").exists()
    asyncio.run(mirror.remove("art_rm"))
    assert not (mirror.root / "artifacts" / "art_rm").exists()
    assert asyncio.run(mirror.publish_annotations("art_rm", [{"annotation_id": IDS[0]}])) is False
    assert not (mirror.root / "artifacts" / "art_rm").exists()


def test_importing_collab_artifacts_registers_the_kind():
    code = ("from herdr_engineering.control_plane import collab_artifacts, collab_bridge_files as c; "
            "print(sorted(c._INTENT_KINDS))")
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ("['annotation_resolve', 'commit', 'library_accept', 'library_discard', "
                                   "'library_file', 'library_folder_create', 'library_folder_delete', "
                                   "'library_folder_rename', 'library_unfile', 'member.artifact_create', "
                                   "'member.artifact_delete', 'member.publish', 'member.share_approve', 'member.share_create', 'member.share_events', "
                                   "'member.share_revoke']")


def test_the_module_registers_its_kind_once():
    assert annotation_intents.KIND in cbf._INTENT_KINDS

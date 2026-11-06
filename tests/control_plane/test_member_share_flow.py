# ruff: noqa: F811  (pytest fixtures are imported by name from registry_pg)
"""Plan 5, Task 7: member lab and public share links on the real bridge loop (worker side).

Real Postgres, real temp directories, the plan-3 applier with the member handlers; the approvals run the real
``approve_share`` (bundle on disk, state.json, mark_live).  The secret URL travels only through the secret result file.
"""
import asyncio
import json
import logging
import os
import stat
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from herdr_engineering.control_plane import artifact_adapter
from herdr_engineering.control_plane import collab_bridge_files as cbf
from herdr_engineering.control_plane.artifact_member_intents import (
    SECRETS_DIR,
    MemberRuntime,
    register_member_intents,
    sweep_secrets,
    take_secret,
)
from herdr_engineering.control_plane.artifact_share_api import approve_share, share_settings_config
from herdr_engineering.control_plane.artifact_shares import ShareStore
from herdr_engineering.control_plane.artifacts import ArtifactStore
from tests.control_plane.registry_pg import registry_pg  # noqa: F401 - imported fixture
from tests.control_plane.test_artifact_member_intents import (
    ALICE,
    BOB,
    HTML,
    agent,
    publish,
    send,
    user,
    view_of,
    write_feeds,
)
from tests.control_plane.test_artifact_member_intents import create as create_artifact
from tests.control_plane.test_collab_intents import Clock, run, settle, sql

PUBLIC_URL = "https://share.example.org"
DIRECTORY = {
    "alice": {"status": "approved", "email": "Alice@Lab.example.org", "display_name": "Alice <i>A</i>"},
    "bob": {"status": "approved", "email": "bob@lab.example.org"},
    "carol": {"status": "approved", "email": "carol@lab.example.org", "groups": ["other-group"]},
    "dave": {"status": "disabled", "email": "dave@lab.example.org"},
    "erin": {"status": "approved", "email": "erin@lab.example.org"},
    "frank": {"status": "approved", "email": "frank@lab.example.org",
              "groups": ["lab", "us-east-1_POOL_Google", "admins"]},      # a federated sign-in adds automatic groups
}


def share_settings(**limits):
    return {**share_settings_config(None, public_url=PUBLIC_URL), "member_share_limits": limits}


def srun(pg, tmp_path, body, *, settings=None, defaults=None, shares=True, clock=None, **runtime_kw):
    clock = clock or Clock()
    runtime = MemberRuntime(
        outbox=tmp_path / "outbox", mirror_root=tmp_path / "mirror", group="lab", enabled=True,
        defaults=defaults or {}, shares_root=tmp_path / "shares" if shares else None, clock=clock,
        share_settings=(settings or share_settings()) if shares else None, **runtime_kw)

    async def go(env):
        await sql(env, "TRUNCATE artifacts, artifact_acl, artifact_shares, artifact_member_usage, artifact_member_events,"
                       " artifact_blobs, artifact_collab_intents, artifact_collab_uploads CASCADE")
        register_member_intents(env.applier)
        write_feeds(env, users=DIRECTORY)
        return await body(env, runtime)
    return run(pg, tmp_path, go, clock=clock, context_extras={"members": runtime})


async def published(env, who=None, **kw):
    """(artifact_id, version_id) of a fresh artifact with one published HTML version."""
    actor = who or user("alice")
    aid = await create_artifact(env, actor)
    out = await publish(env, aid, actor=actor, owner=kw.pop("owner", ALICE), **kw)
    assert out["result"] == "created", out
    return aid, out["version_id"]


async def grant(env, aid, principal, permissions):
    await sql(env, "INSERT INTO artifact_acl (artifact_id,principal_id,permissions,granted_by) "
                   "VALUES ($1,$2,$3::jsonb,'member:bob')", aid, principal, json.dumps(permissions))


async def share(env, who, aid, vid, audience="public", **extra):
    payload = {"version_id": vid, "audience": audience, **extra}
    return await send(env, who, "member.share_create", payload, aid=aid)


async def approve(env, who, aid, sid):
    raw = env.intent(who, kind="member.share_approve", payload={"share_id": sid, "confirm": True}, artifact_id=aid)
    await settle(env)
    return raw["ticket"], env.result(raw["ticket"])


def secret_of(env, ticket):
    path = env.outbox / SECRETS_DIR / f"{ticket}.json"
    return json.loads(path.read_text()) if path.exists() else None


def everything_stored(env, rt):
    """Every byte the worker left behind apart from the bundle and the secret file, as text."""
    chunks = []
    for root in (env.mirror_root, env.outbox):
        for p in root.rglob("*"):
            if p.is_file() and SECRETS_DIR not in p.parts:
                chunks.append(p.read_text(errors="replace"))
    return "\n".join(chunks)


async def db_text(env):
    rows = []
    for table in ("artifact_member_events", "artifact_share_events", "artifact_collab_intents", "artifact_shares"):
        rows += [json.dumps(dict(r), default=str) for r in await sql(env, f"SELECT * FROM {table}")]
    return "\n".join(rows)


def token_of(url):
    return url.split("/s/")[1].strip("/")


# ----------------------------------------------------------------------------- public create and approve

def test_a_public_share_is_created_approved_and_the_url_is_read_once(registry_pg, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    async def body(env, rt):
        aid, vid = await published(env)
        out = await share(env, user("alice"), aid, vid, expires_in_days=7)
        assert out["result"] == "created" and out["audience"] == "public" and out["share_id"].startswith("share_")
        assert out["summary"]["audience"] == "public" and out["summary"]["expires_at"]
        assert out["summary"]["file_count"] == 1
        sid = out["share_id"]
        (row,) = await sql(env, "SELECT state, created_by, token_hash FROM artifact_shares")
        assert (row["state"], row["created_by"], row["token_hash"]) == ("pending", ALICE, None)

        ticket, done = await approve(env, user("alice"), aid, sid)
        assert done["result"] == "live" and done["secret_result"] is True and done["share_id"] == sid
        assert "url" not in json.dumps(done)
        secret = secret_of(env, ticket)
        assert secret["url"].startswith(PUBLIC_URL + "/s/") and secret["principal"] == ALICE
        path = env.outbox / SECRETS_DIR / f"{ticket}.json"
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        (live,) = await sql(env, "SELECT state, token_hash, approved_by FROM artifact_shares")
        assert live["state"] == "live" and live["approved_by"] == ALICE
        assert (tmp_path / "shares" / sid / "files" / "index.html").read_bytes() == HTML

        first = take_secret(env.outbox, ticket, ALICE)
        assert first == secret["url"]
        assert take_secret(env.outbox, ticket, ALICE) is None            # read once
        assert not path.exists()

        token = token_of(first)
        stored = everything_stored(env, rt) + await db_text(env) + "\n".join(r.getMessage() for r in caplog.records)
        assert token not in stored and first not in stored
        assert token not in json.dumps(view_of(env, ALICE))
        assert (await sql(env, "SELECT count(*) AS n FROM artifact_member_events WHERE action='share_approve'"
                                " AND outcome='ok'"))[0]["n"] == 1
        links = view_of(env, ALICE)["links"]
        assert [(x["share_id"], x["state"], x["audience"]) for x in links] == [(sid, "live", "public")]
    srun(registry_pg, tmp_path, body)


def test_only_the_creator_reads_the_secret(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        sid = (await share(env, user("alice"), aid, vid))["share_id"]
        ticket, done = await approve(env, user("alice"), aid, sid)
        assert done["result"] == "live"
        assert take_secret(env.outbox, ticket, BOB) is None
        assert (env.outbox / SECRETS_DIR / f"{ticket}.json").exists()      # a wrong reader does not consume it
        assert take_secret(env.outbox, ticket, ALICE) is not None
    srun(registry_pg, tmp_path, body)


def test_a_second_approve_of_a_live_share_gives_no_second_url(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        sid = (await share(env, user("alice"), aid, vid))["share_id"]
        await approve(env, user("alice"), aid, sid)
        ticket, again = await approve(env, user("alice"), aid, sid)
        assert again["result"] == "already_approved" and secret_of(env, ticket) is None
    srun(registry_pg, tmp_path, body)


def test_an_unread_secret_is_swept_after_sixty_seconds(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        sid = (await share(env, user("alice"), aid, vid))["share_id"]
        ticket, _ = await approve(env, user("alice"), aid, sid)
        assert await asyncio.to_thread(sweep_secrets, rt) == 0
        env.clock.t += 59
        assert await asyncio.to_thread(sweep_secrets, rt) == 0 and secret_of(env, ticket) is not None
        env.clock.t += 2
        assert await asyncio.to_thread(sweep_secrets, rt) == 1 and secret_of(env, ticket) is None
        assert take_secret(env.outbox, ticket, ALICE) is None
    srun(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- lab

def test_a_lab_share_to_the_group_and_a_pool_email_is_created_and_approved(registry_pg, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    async def body(env, rt):
        aid, vid = await published(env)
        out = await share(env, user("alice"), aid, vid, "lab", allow_groups=["lab"],
                          allow_emails=["Bob@lab.example.org"])
        assert out["result"] == "created" and out["audience"] == "lab"
        (row,) = await sql(env, "SELECT allow_groups, allow_emails FROM artifact_shares")
        assert list(row["allow_groups"]) == ["lab"] and list(row["allow_emails"]) == ["bob@lab.example.org"]
        ticket, done = await approve(env, user("alice"), aid, out["share_id"])
        assert done["result"] == "live" and done["audience"] == "lab"
        url = take_secret(env.outbox, ticket, ALICE)
        assert url.startswith(PUBLIC_URL + "/s/")
        assert token_of(url) not in await db_text(env) + "\n".join(r.getMessage() for r in caplog.records)
    srun(registry_pg, tmp_path, body)


@pytest.mark.parametrize("extra", [
    {"allow_groups": ["Local.Dev "]}, {"allow_groups": ["lab "]}, {"allow_groups": [" lab"]}, {"allow_groups": ["LAB"]},
    {"allow_groups": ["other-group"]}, {"allow_emails": ["stranger@elsewhere.org"]},
    {"allow_emails": ["dave@lab.example.org"]}, {"allow_groups": []}, {}])
def test_a_lab_audience_outside_the_members_pool_is_refused(registry_pg, tmp_path, extra):
    async def body(env, rt):
        aid, vid = await published(env)
        out = await share(env, user("alice"), aid, vid, "lab", **extra)
        assert out["result"] == "rejected" and out["reason"], out
        assert not await sql(env, "SELECT 1 FROM artifact_shares")
    srun(registry_pg, tmp_path, body)


def test_the_operator_owner_emails_are_not_merged_into_a_members_lab_share(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        out = await share(env, user("alice"), aid, vid, "lab", allow_groups=["lab"])
        assert out["result"] == "created"
        (row,) = await sql(env, "SELECT allow_emails FROM artifact_shares")
        assert list(row["allow_emails"]) == []
    srun(registry_pg, tmp_path, body,
         settings={**share_settings(), "owner_emails": ["operator@lab.example.org"]})


# ----------------------------------------------------------------------------- grants and ownership

def test_a_share_grant_holder_may_share_bobs_artifact_and_sees_the_link(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env, user("bob"), owner=BOB)
        await grant(env, aid, ALICE, ["read", "share"])
        out = await share(env, user("alice"), aid, vid)
        assert out["result"] == "created", out
        ticket, done = await approve(env, user("alice"), aid, out["share_id"])
        assert done["result"] == "live"
        assert take_secret(env.outbox, ticket, ALICE)
        assert [x["share_id"] for x in view_of(env, ALICE)["links"]] == [out["share_id"]]
    srun(registry_pg, tmp_path, body)


@pytest.mark.parametrize("permissions", [["read"], ["read", "edit"], None])
def test_without_the_share_grant_a_member_gets_not_found_and_no_row(registry_pg, tmp_path, permissions):
    async def body(env, rt):
        aid, vid = await published(env, user("bob"), owner=BOB)
        if permissions is not None:
            await grant(env, aid, ALICE, permissions)
        out = await share(env, user("alice"), aid, vid)
        ghost = await share(env, user("alice"), "art_nothere1", vid)
        assert out["result"] == "rejected" and out["reason"] == "not_found"
        assert ghost["result"] == out["result"] and ghost["reason"] == out["reason"]       # no existence oracle
        assert not await sql(env, "SELECT 1 FROM artifact_shares")
    srun(registry_pg, tmp_path, body)


def test_a_share_grant_that_is_revoked_before_approve_leaves_the_share_pending(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env, user("bob"), owner=BOB)
        await grant(env, aid, ALICE, ["read", "share"])
        sid = (await share(env, user("alice"), aid, vid))["share_id"]
        await sql(env, "DELETE FROM artifact_acl WHERE artifact_id=$1", aid)
        ticket, done = await approve(env, user("alice"), aid, sid)
        assert done["result"] == "rejected" and done["reason"] == "not_found" and secret_of(env, ticket) is None
        (row,) = await sql(env, "SELECT state FROM artifact_shares")
        assert row["state"] == "pending" and not (tmp_path / "shares" / sid).exists()
    srun(registry_pg, tmp_path, body)


def test_only_the_creator_may_approve_a_share(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)                      # alice's artifact
        await grant(env, aid, BOB, ["read", "share"])
        sid = (await share(env, user("bob"), aid, vid))["share_id"]       # created by bob
        ticket, by_owner = await approve(env, user("alice"), aid, sid)
        assert by_owner["result"] == "rejected" and by_owner["reason"] == "not_found"
        assert secret_of(env, ticket) is None
        (row,) = await sql(env, "SELECT state FROM artifact_shares")
        assert row["state"] == "pending"
    srun(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- expiry and sharing_disabled

def test_a_public_share_beyond_ninety_days_is_refused(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        out = await share(env, user("alice"), aid, vid, expires_in_days=91)
        assert out["result"] in {"rejected", "failed"}, out
        assert not await sql(env, "SELECT 1 FROM artifact_shares")
        ok = await share(env, user("alice"), aid, vid, expires_in_days=90)
        assert ok["result"] == "created"
    srun(registry_pg, tmp_path, body)


def test_a_sharing_disabled_member_cannot_create_or_approve(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        pending = (await share(env, user("alice"), aid, vid))["share_id"]
        await sql(env, "UPDATE artifact_member_usage SET sharing_disabled=true WHERE member_principal=$1", ALICE)
        for audience, extra in (("public", {}), ("lab", {"allow_groups": ["lab"]})):
            out = await share(env, user("alice"), aid, vid, audience, **extra)
            assert out["result"] == "rejected" and out["reason"] == "sharing_disabled"
        ticket, done = await approve(env, user("alice"), aid, pending)
        assert done["reason"] == "sharing_disabled" and secret_of(env, ticket) is None
        assert len(await sql(env, "SELECT 1 FROM artifact_shares")) == 1
    srun(registry_pg, tmp_path, body)


def test_sharing_that_is_not_wired_answers_sharing_unavailable(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        out = await share(env, user("alice"), aid, vid)
        assert out["result"] == "rejected" and out["reason"] == "sharing_unavailable"
    srun(registry_pg, tmp_path, body, shares=False)


def test_a_user_outside_the_lab_group_cannot_create_shares(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        out = await share(env, user("carol"), aid, vid)
        assert out["result"] == "rejected"
        by_agent = await share(env, agent("ag1"), aid, vid)           # alice's own agent: her share
        assert by_agent["result"] == "created"
        (row,) = await sql(env, "SELECT created_by FROM artifact_shares")
        assert row["created_by"] == ALICE
    srun(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- revoke

def test_revoke_authorisation(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)                                     # alice owns it
        await grant(env, aid, BOB, ["read", "share"])
        bobs = (await share(env, user("bob"), aid, vid))["share_id"]
        # carol is in the directory but holds no grant: not_found, identical to a share that does not exist
        no_grant = await send(env, user("erin"), "member.share_revoke", {"share_id": bobs}, aid=aid)
        ghost = await send(env, user("erin"), "member.share_revoke", {"share_id": "share_" + "0" * 24}, aid=aid)
        assert no_grant["result"] == ghost["result"] == "rejected" and no_grant["reason"] == ghost["reason"] == "not_found"
        # bob's link on a third party's artifact: alice is the owner, so she may revoke it
        other_aid, other_vid = await published(env, user("bob"), owner=BOB)
        await grant(env, other_aid, ALICE, ["read", "share"])
        alices = (await share(env, user("alice"), other_aid, other_vid))["share_id"]
        # a member with no relation to the artifact cannot revoke someone else's link
        stranger = await send(env, user("bob"), "member.share_revoke", {"share_id": bobs}, aid=other_aid)
        assert stranger["reason"] == "not_found"                       # share belongs to another artifact
        third = await send(env, user("erin"), "member.share_revoke", {"share_id": alices}, aid=other_aid)
        assert third["reason"] == "not_found"
        states = {r["share_id"]: r["state"] for r in await sql(env, "SELECT share_id, state FROM artifact_shares")}
        assert states == {bobs: "pending", alices: "pending"}
        owner_revokes = await send(env, user("alice"), "member.share_revoke", {"share_id": bobs}, aid=aid)
        assert owner_revokes["result"] == "revoked"
        assert (await sql(env, "SELECT state FROM artifact_shares WHERE share_id=$1", bobs))[0]["state"] == "revoked"
        mine = await send(env, user("alice"), "member.share_revoke", {"share_id": alices}, aid=other_aid)
        assert mine["result"] == "revoked"                              # the creator revokes her own link
    srun(registry_pg, tmp_path, body)


def test_revoking_a_live_share_removes_its_bundle_and_state(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        sid = (await share(env, user("alice"), aid, vid))["share_id"]
        await approve(env, user("alice"), aid, sid)
        assert (tmp_path / "shares" / sid).exists()
        out = await send(env, user("alice"), "member.share_revoke", {"share_id": sid}, aid=aid)
        assert out["result"] == "revoked" and not (tmp_path / "shares" / sid).exists()
        assert not list((tmp_path / "shares" / "by-token").glob("*")) if (tmp_path / "shares" / "by-token").exists() \
            else True
    srun(registry_pg, tmp_path, body)


def test_share_events_are_for_the_creator_and_the_owner_only(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        await grant(env, aid, BOB, ["read", "share"])
        sid = (await share(env, user("bob"), aid, vid))["share_id"]
        mine = await send(env, user("bob"), "member.share_events", {"share_id": sid}, aid=aid)
        owner = await send(env, user("alice"), "member.share_events", {"share_id": sid}, aid=aid)
        other = await send(env, user("erin"), "member.share_events", {"share_id": sid}, aid=aid)
        assert [e["kind"] for e in mine["events"]] == [e["kind"] for e in owner["events"]] == ["created"]
        assert other["result"] == "rejected" and other["reason"] == "not_found"
    srun(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- caps and the spend lock

def test_member_and_operator_totals_are_capped_independently(registry_pg, tmp_path):
    async def body(env, rt):
        size = len(HTML)
        a1, v1 = await published(env)
        a2, v2 = await published(env)
        s1 = (await share(env, user("alice"), a1, v1))["share_id"]
        s2 = (await share(env, user("alice"), a2, v2))["share_id"]
        # an operator share on a third artifact, approved through the real approve_share on its own connections
        a3, v3 = await published(env)
        conn = await asyncpg.connect(registry_pg["dsn"])
        try:
            op = await ShareStore(conn).create(artifact_id=a3, version_id=v3, audience="public",
                                               expires_at=datetime.now(UTC) + timedelta(days=5),
                                               created_by="operator")
        finally:
            await conn.close()

        class Server:
            pass

        async def make_store():
            return ShareStore(await asyncpg.connect(registry_pg["dsn"]))

        async def make_artifacts():
            return ArtifactStore(await asyncpg.connect(registry_pg["dsn"]), env.applier.rustfs)
        server = Server()
        server.share_store = artifact_adapter.SyncArtifactAdapter(make_store)
        server.artifact_store = artifact_adapter.SyncArtifactAdapter(make_artifacts)
        server.rustfs, server.shares_root = env.applier.rustfs, tmp_path / "shares"
        server.share_settings = share_settings(max_total_bytes=size + 5)
        server.share_settings["max_total_bytes"] = size + 5
        try:
            _, summary = await asyncio.to_thread(approve_share, server, op, approved_by="operator")
            assert summary["total_bytes"] == size
            _, done1 = await approve(env, user("alice"), a1, s1)
            assert done1["result"] == "live"                       # 31 member + 31 operator live: each under its own cap
            ticket2, done2 = await approve(env, user("alice"), a2, s2)
            assert done2["result"] == "rejected" and "total cap" in done2["reason"]
            assert not (tmp_path / "shares" / s2).exists()
            assert (await sql(env, "SELECT state FROM artifact_shares WHERE share_id=$1", s2))[0]["state"] == "pending"
            assert secret_of(env, ticket2) is None
        finally:
            server.share_store.close() if hasattr(server.share_store, "close") else None
            server.artifact_store.close() if hasattr(server.artifact_store, "close") else None
    srun(registry_pg, tmp_path, body, settings=share_settings(max_total_bytes=len(HTML) + 5))


def test_approve_waits_for_the_cross_process_spend_lock(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        sid = (await share(env, user("alice"), aid, vid))["share_id"]
        holder = await asyncpg.connect(registry_pg["dsn"])
        try:
            async with ShareStore(holder).spend_lock():
                raw = env.intent(user("alice"), kind="member.share_approve",
                                 payload={"share_id": sid, "confirm": True}, artifact_id=aid)
                task = asyncio.create_task(settle(env))
                await asyncio.sleep(1.5)
                assert env.result(raw["ticket"]) is None and not task.done()
            await task
        finally:
            await holder.close()
        assert env.result(raw["ticket"])["result"] == "live"
    srun(registry_pg, tmp_path, body)


def test_a_failed_secret_write_revokes_the_link_it_could_not_hand_over(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        sid = (await share(env, user("alice"), aid, vid))["share_id"]
        (env.outbox / SECRETS_DIR).write_text("a file where the directory should be")
        ticket, done = await approve(env, user("alice"), aid, sid)
        assert done["result"] == "rejected" and done["reason"] == "secret_unavailable"
        assert (await sql(env, "SELECT state FROM artifact_shares"))[0]["state"] == "revoked"
        assert not (tmp_path / "shares" / sid).exists()
    srun(registry_pg, tmp_path, body)


def test_the_validators_are_registered_by_the_light_module():
    for kind in ("member.share_create", "member.share_approve", "member.share_revoke", "member.share_events"):
        assert kind in cbf._INTENT_KINDS


# ----------------------------------------------------------------------------- review round 1
FRANK = "member:frank"


def test_a_lab_share_may_name_only_the_lab_group_never_the_members_other_groups(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env, user("frank"), owner=FRANK)
        for groups in (["us-east-1_POOL_Google"], ["admins"], ["lab", "admins"]):
            out = await share(env, user("frank"), aid, vid, "lab", allow_groups=groups)
            assert out["result"] == "rejected", (groups, out)
        assert not await sql(env, "SELECT 1 FROM artifact_shares")
        ok = await share(env, user("frank"), aid, vid, "lab", allow_groups=["lab"])
        assert ok["result"] == "created"
        (row,) = await sql(env, "SELECT allow_groups FROM artifact_shares")
        assert list(row["allow_groups"]) == ["lab"]
    srun(registry_pg, tmp_path, body)


def test_lab_shares_always_expire_and_never_beyond_ninety_days(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        default = await share(env, user("alice"), aid, vid, "lab", allow_groups=["lab"])
        assert default["result"] == "created"
        (row,) = await sql(env, "SELECT expires_at FROM artifact_shares WHERE share_id=$1", default["share_id"])
        left = row["expires_at"] - datetime.now(UTC)
        assert timedelta(days=29) < left <= timedelta(days=30, minutes=1)
        too_long = await share(env, user("alice"), aid, vid, "lab", allow_groups=["lab"], expires_in_days=91)
        assert too_long["result"] == "rejected"
        assert (await share(env, user("alice"), aid, vid, "lab", allow_groups=["lab"],
                            expires_in_days=90))["result"] == "created"
        assert all(r["expires_at"] is not None for r in await sql(env, "SELECT expires_at FROM artifact_shares"))
    srun(registry_pg, tmp_path, body)


def test_a_failed_commit_of_the_approval_issues_no_link_and_leaves_nothing_behind(registry_pg, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    async def body(env, rt):
        aid, vid = await published(env)
        sid = (await share(env, user("alice"), aid, vid))["share_id"]
        async with env.pool.acquire() as conn:     # a real deferred constraint: the UPDATE succeeds, the COMMIT fails
            await conn.execute("""
                CREATE OR REPLACE FUNCTION induced_commit_failure() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF NEW.state = 'live' THEN RAISE EXCEPTION 'induced commit failure'; END IF; RETURN NULL; END $$;
                CREATE CONSTRAINT TRIGGER induced_commit_failure AFTER UPDATE ON artifact_shares
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION induced_commit_failure();""")
        try:
            ticket, done = await approve(env, user("alice"), aid, sid)
        finally:
            async with env.pool.acquire() as conn:
                await conn.execute("DROP TRIGGER induced_commit_failure ON artifact_shares;"
                                   "DROP FUNCTION induced_commit_failure()")
        assert done["result"] == "rejected" and done["reason"] == "approval_failed", done
        assert secret_of(env, ticket) is None and "secret_result" not in done
        (row,) = await sql(env, "SELECT state, token_hash FROM artifact_shares")
        assert (row["state"], row["token_hash"]) == ("pending", None)
        left = [p for p in (tmp_path / "shares").rglob("*") if not p.is_dir()] if (tmp_path / "shares").exists() else []
        assert not (tmp_path / "shares" / sid).exists()
        assert left == [], left                      # no bundle, no state.json for a link that does not exist
        assert PUBLIC_URL + "/s/" not in caplog.text
        # the share is still approvable afterwards (nothing was spent)
        t2, again = await approve(env, user("alice"), aid, sid)
        assert again["result"] == "live" and take_secret(env.outbox, t2, ALICE)
    srun(registry_pg, tmp_path, body)


def test_a_corrupt_blob_at_approval_is_a_refusal_with_an_event_not_internal(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        sid = (await share(env, user("alice"), aid, vid))["share_id"]
        for key in list(env.applier.rustfs.objects):
            env.applier.rustfs.objects[key] = b"tampered" + env.applier.rustfs.objects[key]
        ticket, done = await approve(env, user("alice"), aid, sid)
        assert done["result"] == "rejected" and done["reason"] != "internal", done
        events = await sql(env, "SELECT outcome, code FROM artifact_member_events WHERE action='share_approve'")
        assert [(e["outcome"], e["code"]) for e in events] == [("refused", done["reason"])]
        assert (await sql(env, "SELECT state FROM artifact_shares"))[0]["state"] == "pending"
        assert not (tmp_path / "shares" / sid).exists()
    srun(registry_pg, tmp_path, body)


def test_a_repeated_approve_with_the_same_idempotency_key_is_already_approved(registry_pg, tmp_path):
    async def body(env, rt):
        aid, vid = await published(env)
        sid = (await share(env, user("alice"), aid, vid))["share_id"]
        payload = {"share_id": sid, "confirm": True}
        r1 = await send(env, user("alice"), "member.share_approve", payload, aid=aid, key="approve-key-0001")
        r2 = await send(env, user("alice"), "member.share_approve", payload, aid=aid, key="approve-key-0001")
        assert r1["result"] == "live" and r2["result"] == "already_approved" and "secret_result" not in r2
        events = await sql(env, "SELECT kind FROM artifact_share_events WHERE share_id=$1 AND kind='approved'", sid)
        assert len(events) == 1                                       # approve_share ran once
        assert sorted(p.name for p in (env.outbox / SECRETS_DIR).iterdir()) == [r1_ticket_name(env)]
    srun(registry_pg, tmp_path, body)


def r1_ticket_name(env):
    (only,) = [p.name for p in (env.outbox / SECRETS_DIR).iterdir()]
    return only


def _states(tmp_path):
    return [json.loads(p.read_text()) for p in (tmp_path / "shares").glob("by-token/*/state.json")]


def test_approved_member_link_records_the_directory_display_name_or_the_user_id(registry_pg, tmp_path):
    async def body(env, rt):
        for who, owner, expected in ((user("alice"), ALICE, "Alice <i>A</i>"), (user("bob"), BOB, "bob")):
            aid, vid = await published(env, who, owner=owner)
            out = await share(env, who, aid, vid, expires_in_days=7)
            _, done = await approve(env, who, aid, out["share_id"])
            assert done["result"] == "live", done
            (state,) = [s for s in _states(tmp_path) if s["share_id"] == out["share_id"]]
            assert state["shared_by"] == expected
    srun(registry_pg, tmp_path, body)


# ----------------------------------------------------------------------------- security: a pending push is not shareable

async def _pending_push(env, who):
    aid = await create_artifact(env, who)
    out = await publish(env, aid, actor=who, owner=ALICE)
    assert out["result"] == "created", out
    (row,) = await sql(env, "SELECT intake FROM artifacts WHERE artifact_id=$1", aid)
    return aid, out["version_id"], row["intake"]


@pytest.mark.parametrize("audience,extra", [("public", {"expires_in_days": 7}),
                                            ("lab", {"allow_groups": ["lab"], "expires_in_days": 7})])
def test_a_pending_agent_push_cannot_be_shared_until_accepted(registry_pg, tmp_path, audience, extra):
    async def body(env, rt):
        ag = agent("ag1")
        aid, vid, intake = await _pending_push(env, ag)
        assert intake == "pending"
        out = await share(env, ag, aid, vid, audience=audience, **extra)
        assert out["result"] == "rejected" and out["reason"] == "pending_not_accepted", out
        assert await sql(env, "SELECT 1 FROM artifact_shares") == []
        out = await share(env, user("alice"), aid, vid, audience=audience, **extra)      # the owner too: accept first
        assert out["result"] == "rejected" and out["reason"] == "pending_not_accepted", out
        accepted = await send(env, user("alice"), "library_accept", {}, aid=aid)
        assert accepted["result"] == "accepted"
        out = await share(env, ag, aid, vid, audience=audience, **extra)
        assert out["result"] == "created", out
    srun(registry_pg, tmp_path, body)


def test_an_approval_of_a_share_made_before_the_artifact_went_pending_is_refused(registry_pg, tmp_path):
    async def body(env, rt):
        ag = agent("ag1")
        aid, vid, _ = await _pending_push(env, ag)
        await sql(env, "UPDATE artifacts SET intake='accepted' WHERE artifact_id=$1", aid)
        s = await share(env, ag, aid, vid, expires_in_days=7)
        assert s["result"] == "created", s
        await sql(env, "UPDATE artifacts SET intake='pending' WHERE artifact_id=$1", aid)
        ticket, done = await approve(env, ag, aid, s["share_id"])
        assert done["result"] == "rejected" and done["reason"] == "pending_not_accepted", done
        (st,) = await sql(env, "SELECT state FROM artifact_shares WHERE share_id=$1", s["share_id"])
        assert st["state"] == "pending"
    srun(registry_pg, tmp_path, body)


def test_the_share_store_itself_refuses_a_pending_artifact_for_any_creator(registry_pg, tmp_path):
    from herdr_engineering.control_plane.artifact_shares import ShareError

    async def body(env, rt):
        aid, vid, _ = await _pending_push(env, agent("ag1"))
        for creator in (None, "operator", ALICE):
            with pytest.raises(ShareError) as exc:
                await ShareStore(await asyncpg.connect(registry_pg["dsn"])).create(
                    artifact_id=aid, version_id=vid, audience="public", created_by=creator,
                    expires_at=datetime.now(UTC) + timedelta(days=7))
            assert exc.value.code == "pending_not_accepted"
    srun(registry_pg, tmp_path, body)

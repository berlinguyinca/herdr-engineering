# ruff: noqa: F811  (pytest fixtures are imported by name)
"""Pending intake for agent pushes (artifact library, Task 5), on real Postgres and the real intent applier."""

from herdr_engineering.control_plane.artifact_library import (
    PENDING_CAP,
    LibraryError,
    LibraryStore,
    PendingCapExceeded,
)
from tests.control_plane.registry_pg import registry_pg  # noqa: F401 - imported fixture
from tests.control_plane.test_artifact_member_intents import (
    AK,
    ALICE,
    BIG_HTML,
    agent,
    create,
    mrun,
    publish_payload,
    send,
    settle,
    sql,
    user,
)
from tests.control_plane.test_collab_approval import s as approval_svc  # noqa: F401 - imported fixture
from tests.control_plane.test_collab_member_tools import names, world  # noqa: F401 - imported fixture

ACCEPT, DISCARD = "library_accept", "library_discard"


async def intake(env, aid):
    (row,) = await sql(env, "SELECT intake FROM artifacts WHERE artifact_id=$1", aid)
    return row["intake"]


async def push(env, i=0, key=None):
    return await send(
        env,
        agent("ag1"),
        "member.artifact_create",
        {"title": f"p{i}", "filename": "a.html", "profile": "web"},
        key=key,
    )


def test_agent_create_is_pending_human_create_is_accepted(registry_pg, tmp_path):
    async def body(env, rt):
        pushed = await push(env)
        assert pushed["result"] == "created"
        assert await intake(env, pushed["artifact_id"]) == "pending"
        assert await intake(env, await create(env)) == "accepted"

    mrun(registry_pg, tmp_path, body)


def test_26th_pending_refused_with_clear_message(registry_pg, tmp_path):
    async def body(env, rt):
        for i in range(PENDING_CAP):
            assert (await push(env, i))["result"] == "created"
        out = await push(env, 99)
        assert out["result"] == "rejected" and out["reason"] == "pending_cap"
        assert str(PENDING_CAP) in out["message"]  # the clear text reaches the agent-facing result
        exc = PendingCapExceeded()  # the result carries the stable code; the error carries the text
        assert (exc.status, exc.code) == (429, "pending_cap") and str(PENDING_CAP) in exc.message
        assert (await create(env)) and await LibraryStore(env.pool).count_pending(
            ALICE
        ) == PENDING_CAP

    mrun(registry_pg, tmp_path, body)


def test_same_idempotency_key_does_not_double_count(registry_pg, tmp_path):
    async def body(env, rt):
        first = await push(env, key="same-key-1")
        again = await push(env, key="same-key-1")
        assert again["artifact_id"] == first["artifact_id"]
        assert await LibraryStore(env.pool).count_pending(ALICE) == 1
        for i in range(PENDING_CAP - 1):
            await push(env, i)
        replay = await push(
            env, key="same-key-1"
        )  # at the cap, a retry is still a replay, not a refusal
        assert replay["result"] == "created" and replay["artifact_id"] == first["artifact_id"]
        assert await LibraryStore(env.pool).count_pending(ALICE) == PENDING_CAP

    mrun(registry_pg, tmp_path, body)


def test_concurrent_creates_at_24_pending_admit_exactly_one(registry_pg, tmp_path):
    async def body(env, rt):
        for i in range(PENDING_CAP - 1):
            assert (await push(env, i))["result"] == "created"
        raws = [
            env.intent(
                agent("ag1"),
                kind="member.artifact_create",
                payload={"title": f"race{i}", "filename": "a.html", "profile": "web"},
                artifact_id=None,
                key=f"race-key-{i}",
            )
            for i in range(2)
        ]
        await settle(env)
        outs = [env.result(r["ticket"]) for r in raws]
        assert sorted(o["result"] for o in outs) == ["created", "rejected"]
        assert [o["reason"] for o in outs if o["result"] == "rejected"] == ["pending_cap"]
        assert await LibraryStore(env.pool).count_pending(ALICE) == PENDING_CAP

    mrun(registry_pg, tmp_path, body)


def test_accept_moves_to_mine(registry_pg, tmp_path):
    async def body(env, rt):
        aid = (await push(env))["artifact_id"]
        out = await send(env, user("alice"), ACCEPT, {}, aid=aid)
        assert out["result"] == "accepted"
        assert await intake(env, aid) == "accepted"
        assert await LibraryStore(env.pool).count_pending(ALICE) == 0
        again = await send(env, user("alice"), ACCEPT, {}, aid=aid)  # no longer pending
        assert again["result"] == "rejected" and again["reason"] == "not_found"

    mrun(registry_pg, tmp_path, body)


def test_discard_deletes_and_frees_a_slot(registry_pg, tmp_path):
    async def body(env, rt):
        ids = [(await push(env, i))["artifact_id"] for i in range(PENDING_CAP)]
        assert (await push(env, 99))["reason"] == "pending_cap"
        out = await send(env, user("alice"), DISCARD, {}, aid=ids[0])
        assert out["result"] == "deleted"
        (row,) = await sql(env, "SELECT lifecycle FROM artifacts WHERE artifact_id=$1", ids[0])
        assert row["lifecycle"] == "retracted"
        assert (await push(env, 100))["result"] == "created"

    mrun(registry_pg, tmp_path, body)


def test_other_member_cannot_accept_or_discard_404(registry_pg, tmp_path):
    async def body(env, rt):
        aid = (await push(env))["artifact_id"]
        for kind in (ACCEPT, DISCARD):
            out = await send(env, user("bob"), kind, {}, aid=aid)
            assert out["result"] == "rejected" and out["reason"] == "not_found"
        missing = await send(env, user("alice"), ACCEPT, {}, aid="artifact_" + "0" * 32)
        assert missing["reason"] == "not_found"
        accepted = await create(env)
        for kind in (ACCEPT, DISCARD):  # an accepted artifact is not pending
            assert (await send(env, user("alice"), kind, {}, aid=accepted))["reason"] == "not_found"
        assert await intake(env, aid) == "pending"
        store = LibraryStore(env.pool)
        try:
            await store.accept_pending("member:bob", aid)
        except LibraryError as exc:
            assert (exc.status, exc.code) == (404, "not_found")
        else:
            raise AssertionError("expected not_found")

    mrun(registry_pg, tmp_path, body)


def test_pending_counts_toward_member_byte_limit(registry_pg, tmp_path):
    async def body(env, rt):
        aid = (await push(env))["artifact_id"]
        assert await intake(env, aid) == "pending"
        payload = publish_payload(env, AK, aid, {"index.html": BIG_HTML})
        out = await send(env, agent("ag1"), "member.publish", payload, aid=aid)
        assert out["result"] == "rejected" and out["reason"] == "member_bytes"

    mrun(registry_pg, tmp_path, body, defaults={"max_member_bytes": 1000})


def test_agent_tool_menu_has_no_accept_or_discard(world):
    listed = names(world, world.info)
    assert "artifact_create" in listed
    assert not {n for n in listed if "accept" in n or "discard" in n or "library" in n}

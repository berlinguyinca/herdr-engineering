# ruff: noqa: F811  (pytest fixtures are imported by name)
"""Library worker intents and the ``library`` key of the member view (Task 6), on real Postgres and the real worker."""

import dataclasses
import hashlib
import os

from herdr_engineering.control_plane.artifact_library import LibraryStore
from herdr_engineering.control_plane.artifact_member_intents import publish_member_views
from tests.control_plane.registry_pg import registry_pg  # noqa: F401 - imported fixture
from tests.control_plane.test_artifact_member_intents import (
    ALICE,
    BOB,
    HTML,
    USERS,
    agent,
    create,
    mrun,
    publish,
    send,
    sql,
    user,
    view_of,
    write_feeds,
)
from tests.control_plane.test_collab_approval import s as approval_svc  # noqa: F401 - imported fixture

FOLDER_CREATE, FOLDER_RENAME, FOLDER_DELETE = (
    "library_folder_create",
    "library_folder_rename",
    "library_folder_delete",
)
FILE, UNFILE, CLAIM, ACCEPT, DISCARD = (
    "library_file",
    "library_unfile",
    "library_claim",
    "library_accept",
    "library_discard",
)


def lrun(pg, tmp_path, body):
    async def clean(env, rt):
        await sql(env, "TRUNCATE artifact_library_folders, artifact_share_claims CASCADE")
        return await body(env, rt)

    return mrun(pg, tmp_path, clean)


async def new_folder(env, who="alice", name="Papers"):
    out = await send(env, user(who), FOLDER_CREATE, {"name": name})
    assert out["result"] == "created", out
    return (
        await sql(
            env,
            "SELECT folder_id FROM artifact_library_folders WHERE owner=$1 AND name=$2",
            f"member:{who}",
            name,
        )
    )[0][0]


async def live_share(env, aid, *, groups=("lab",)):
    vid = (await sql(env, "SELECT current_version_id FROM artifacts WHERE artifact_id=$1", aid))[0][
        0
    ]
    if vid is None:
        vid = "version_" + "f" * 32
        await sql(
            env,
            "INSERT INTO artifact_versions (version_id,artifact_id,version_number,state) VALUES ($1,$2,1,'ready')",
            vid,
            aid,
        )
    sid = "share_" + "ab" * 12
    await sql(
        env,
        "INSERT INTO artifact_shares (share_id,artifact_id,version_id,audience,allow_groups,state,title,created_by) "
        "VALUES ($1,$2,$3,'lab',$4,'live','Shared',$5)",
        sid,
        aid,
        vid,
        list(groups),
        ALICE,
    )
    return sid


def test_each_intent_updates_db_and_view_file(registry_pg, tmp_path):
    async def body(env, rt):
        mine = await create(env, title="Mine")
        pushed = (
            await send(
                env,
                agent("ag1"),
                "member.artifact_create",
                {"title": "Pushed", "filename": "a.html", "profile": "web"},
            )
        )["artifact_id"]
        fid = await new_folder(env)
        lib = view_of(env, ALICE)["library"]
        assert [(f["folder_id"], f["name"], f["item_count"]) for f in lib["folders"]] == [
            (fid, "Papers", 0)
        ]
        assert {i["artifact_id"] for i in lib["items"]} == {mine, pushed}

        out = await send(env, user("alice"), FOLDER_RENAME, {"folder_id": fid, "name": "Reads"})
        assert out["result"] == "renamed"
        assert view_of(env, ALICE)["library"]["folders"][0]["name"] == "Reads"

        assert (await send(env, user("alice"), FILE, {"folder_id": fid}, aid=mine))[
            "result"
        ] == "filed"
        lib = view_of(env, ALICE)["library"]
        assert lib["folders"][0]["item_count"] == 1
        (item,) = [i for i in lib["items"] if i["artifact_id"] == mine]
        assert item["folder_ids"] == [fid]
        assert (await sql(env, "SELECT count(*) FROM artifact_library_items"))[0][0] == 1

        assert (await send(env, user("alice"), UNFILE, {"folder_id": fid}, aid=mine))[
            "result"
        ] == "unfiled"
        assert (await sql(env, "SELECT count(*) FROM artifact_library_items"))[0][0] == 0
        assert view_of(env, ALICE)["library"]["folders"][0]["item_count"] == 0

        sid = await live_share(env, mine, groups=("nobody",))
        assert (await send(env, user("bob"), CLAIM, {"share_id": sid}))["result"] == "claimed"
        assert (await sql(env, "SELECT member_id FROM artifact_share_claims"))[0][0] == "bob"
        (opened,) = [i for i in view_of(env, BOB)["library"]["items"] if i["artifact_id"] == mine]
        assert opened["source"] == "opened" and opened["share_id"] == sid

        assert (await send(env, user("alice"), ACCEPT, {}, aid=pushed))["result"] == "accepted"
        (acc,) = [i for i in view_of(env, ALICE)["library"]["items"] if i["artifact_id"] == pushed]
        assert acc["source"] == "mine"

        pushed2 = (
            await send(
                env,
                agent("ag1"),
                "member.artifact_create",
                {"title": "P2", "filename": "a.html", "profile": "web"},
            )
        )["artifact_id"]
        assert (await send(env, user("alice"), DISCARD, {}, aid=pushed2))["result"] == "deleted"
        assert pushed2 not in {i["artifact_id"] for i in view_of(env, ALICE)["library"]["items"]}

        assert (await send(env, user("alice"), FOLDER_DELETE, {"folder_id": fid}))[
            "result"
        ] == "deleted"
        assert view_of(env, ALICE)["library"]["folders"] == []

    lrun(registry_pg, tmp_path, body)


def test_view_file_contains_pending_only_for_owner(registry_pg, tmp_path):
    async def body(env, rt):
        pushed = (
            await send(
                env,
                agent("ag1"),
                "member.artifact_create",
                {"title": "Pushed", "filename": "a.html", "profile": "web"},
            )
        )["artifact_id"]
        await sql(
            env,
            "INSERT INTO artifact_acl (artifact_id,principal_id,permissions,granted_by) "
            "VALUES ($1,$2,'[\"read\"]'::jsonb,$3)",
            pushed,
            BOB,
            ALICE,
        )
        await new_folder(env, "alice")
        await new_folder(env, "bob")
        (mine,) = [i for i in view_of(env, ALICE)["library"]["items"] if i["artifact_id"] == pushed]
        assert mine["status"] == "pending" and mine["source"] == "pending"
        assert pushed not in {i["artifact_id"] for i in view_of(env, BOB)["library"]["items"]}

    lrun(registry_pg, tmp_path, body)


def test_view_capped_at_2000_newest_first(registry_pg, tmp_path):
    async def body(env, rt):
        await sql(
            env,
            "INSERT INTO artifacts (artifact_id,created_by,display_name,intake,created_at) "
            "SELECT 'artifact_bulk' || lpad(g::text, 6, '0'), $1, 'bulk ' || g, 'accepted', "
            "now() - make_interval(secs => g) FROM generate_series(1, 2100) g",
            ALICE,
        )
        await new_folder(env)
        items = view_of(env, ALICE)["library"]["items"]
        assert len(items) == 2000
        stamps = [i["shared_at"] for i in items]
        assert stamps == sorted(stamps, reverse=True)
        assert items[0]["artifact_id"] == "artifact_bulk000001"  # newest
        assert "artifact_bulk002000" == items[-1]["artifact_id"]
        assert isinstance(stamps[0], str)

    lrun(registry_pg, tmp_path, body)


def test_replayed_intent_is_idempotent(registry_pg, tmp_path):
    async def body(env, rt):
        mine = await create(env)
        first = await send(env, user("alice"), FOLDER_CREATE, {"name": "A"}, key="same-key-1")
        again = await send(env, user("alice"), FOLDER_CREATE, {"name": "A"}, key="same-key-1")
        assert first["result"] == again["result"] == "created"
        assert (await sql(env, "SELECT count(*) FROM artifact_library_folders"))[0][0] == 1
        fid = (await sql(env, "SELECT folder_id FROM artifact_library_folders"))[0][0]
        for _ in range(2):
            assert (await send(env, user("alice"), FILE, {"folder_id": fid}, aid=mine))[
                "result"
            ] == "filed"
        assert (await sql(env, "SELECT count(*) FROM artifact_library_items"))[0][0] == 1
        sid = await live_share(env, mine)
        for _ in range(2):
            assert (await send(env, user("bob"), CLAIM, {"share_id": sid}))["result"] == "claimed"
        assert (await sql(env, "SELECT count(*) FROM artifact_share_claims"))[0][0] == 1
        # create is create-or-get by name: a second, differently keyed request yields the same single folder
        dup = await send(env, user("alice"), FOLDER_CREATE, {"name": "A"})
        assert dup["result"] == "created"
        assert (await sql(env, "SELECT count(*) FROM artifact_library_folders"))[0][0] == 1
        assert len(await LibraryStore(env.pool).list_folders(ALICE)) == 1

    lrun(registry_pg, tmp_path, body)


def test_intent_for_other_members_folder_fails_404(registry_pg, tmp_path):
    async def body(env, rt):
        mine = await create(env)
        fid = await new_folder(env, "alice")
        ghost = "fld_" + "0" * 24
        for other in (fid, ghost):
            for kind, payload, aid in (
                (FOLDER_RENAME, {"folder_id": other, "name": "x"}, None),
                (FOLDER_DELETE, {"folder_id": other}, None),
                (FILE, {"folder_id": other}, mine),
                (UNFILE, {"folder_id": other}, mine),
            ):
                out = await send(env, user("bob"), kind, payload, aid=aid)
                assert out["result"] == "rejected" and out["reason"] == "not_found", (kind, out)
        assert [f.name for f in await LibraryStore(env.pool).list_folders(ALICE)] == ["Papers"]

    lrun(registry_pg, tmp_path, body)


def test_view_uses_directory_email_and_groups(registry_pg, tmp_path):
    async def body(env, rt):
        mine = await create(env)
        sid = await live_share(env, mine, groups=("lab",))
        write_feeds(env, users={**USERS, "bob": {"status": "approved", "email": "bob@x.org"}})
        await new_folder(env, "bob")
        ids = {i["artifact_id"]: i for i in view_of(env, BOB)["library"]["items"]}
        assert ids[mine]["share_id"] == sid and ids[mine]["source"] == "assigned"

    lrun(registry_pg, tmp_path, body)


POOL = "library-content"


def pool_files(env):
    root = env.mirror_root / POOL
    return {p.name: p.read_bytes() for p in root.iterdir()} if root.exists() else {}


async def tick(env, rt):
    async with env.pool.acquire() as conn:
        await publish_member_views(conn, dataclasses.replace(rt, rustfs=env.applier.rustfs))


def files_of(env, who, aid):
    items = [i for i in view_of(env, who)["library"]["items"] if i["artifact_id"] == aid]
    return items[0]["files"] if items else None


def test_view_files_pooled_for_owner_assigned_and_claimed(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, title="Doc")
        await publish(env, aid, {"index.html": HTML, "style.css": b"a{}"})
        await sql(
            env,
            "INSERT INTO artifact_acl (artifact_id,principal_id,permissions,granted_by) "
            "VALUES ($1,$2,'[\"read\"]'::jsonb,$3)",
            aid,
            BOB,
            ALICE,
        )
        await tick(env, rt)
        want = {"index.html": HTML, "style.css": b"a{}"}
        for who in (ALICE, BOB):
            files = files_of(env, who, aid)
            assert {f["path"] for f in files} == set(want)
            for f in files:
                assert f["size"] == len(want[f["path"]]) and f["content_type"]
                assert pool_files(env)[f["sha256"]] == want[f["path"]]
                assert hashlib.sha256(want[f["path"]]).hexdigest() == f["sha256"]
        mode = (
            env.mirror_root / POOL / files_of(env, ALICE, aid)[0]["sha256"]
        ).stat().st_mode & 0o777
        assert mode in (0o644, 0o640, 0o600)

    lrun(registry_pg, tmp_path, body)


def test_view_files_end_when_access_ends(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, title="Doc")
        await publish(env, aid)
        sid = await live_share(env, aid, groups=("nobody",))
        assert (await send(env, user("bob"), CLAIM, {"share_id": sid}))["result"] == "claimed"
        await tick(env, rt)
        assert files_of(env, BOB, aid)
        await sql(env, "UPDATE artifact_shares SET state='revoked' WHERE share_id=$1", sid)
        await tick(env, rt)
        assert not files_of(env, BOB, aid)  # revoked: no files (or no item at all)
        await sql(
            env,
            "INSERT INTO artifact_acl (artifact_id,principal_id,permissions,granted_by,expires_at) "
            "VALUES ($1,$2,'[\"read\"]'::jsonb,$3, now() - interval '1 hour')",
            aid,
            BOB,
            ALICE,
        )
        await tick(env, rt)
        assert not files_of(env, BOB, aid)  # expired grant

    lrun(registry_pg, tmp_path, body)


def test_oversized_item_has_no_files_and_is_marked_too_large(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, title="Big")
        await publish(env, aid)
        await sql(
            env, "ALTER TABLE artifact_versions DISABLE TRIGGER USER"
        )  # immutable by trigger; fake a big file
        await sql(
            env,
            "UPDATE artifact_versions SET output_manifest = (SELECT jsonb_agg(jsonb_set(e, '{size_bytes}', "
            "to_jsonb(30000000))) FROM jsonb_array_elements(output_manifest) e) WHERE artifact_id=$1",
            aid,
        )
        await sql(env, "ALTER TABLE artifact_versions ENABLE TRIGGER USER")
        await tick(env, rt)
        (item,) = [i for i in view_of(env, ALICE)["library"]["items"] if i["artifact_id"] == aid]
        assert item["files"] == [] and item["too_large"] is True

    lrun(registry_pg, tmp_path, body)


def test_pool_gc_removes_unreferenced_old_blobs_only(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, title="Doc")
        await publish(env, aid)
        await tick(env, rt)
        kept = {f["sha256"] for f in files_of(env, ALICE, aid)}
        pool = env.mirror_root / POOL
        old, young = "a" * 64, "b" * 64
        for name in (old, young):
            (pool / name).write_bytes(b"orphan")
        os.utime(pool / old, (1, 1))
        await tick(env, rt)
        names = set(pool_files(env))
        assert old not in names and young in names and kept <= names

    lrun(registry_pg, tmp_path, body)


def test_claimed_item_lists_the_share_pinned_version_not_the_head(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, title="Doc")
        await publish(env, aid, {"index.html": HTML})
        v1 = (await sql(env, "SELECT current_version_id FROM artifacts WHERE artifact_id=$1", aid))[
            0
        ][0]
        sid = await live_share(env, aid, groups=("nobody",))
        assert (await send(env, user("bob"), CLAIM, {"share_id": sid}))["result"] == "claimed"
        await publish(env, aid, {"index.html": HTML, "v2-only.css": b"b{}"}, base=v1)
        await tick(env, rt)
        assert {f["path"] for f in files_of(env, BOB, aid)} == {"index.html"}  # v1 only
        assert {f["path"] for f in files_of(env, ALICE, aid)} == {
            "index.html",
            "v2-only.css",
        }  # owner: head
        # assigned beats opened: with a grant the head decides
        await sql(
            env,
            "INSERT INTO artifact_acl (artifact_id,principal_id,permissions,granted_by) "
            "VALUES ($1,$2,'[\"read\"]'::jsonb,$3)",
            aid,
            BOB,
            ALICE,
        )
        await tick(env, rt)
        assert {f["path"] for f in files_of(env, BOB, aid)} == {"index.html", "v2-only.css"}

    lrun(registry_pg, tmp_path, body)


def test_pool_files_use_the_collab_group_modes_and_a_hit_is_touched(registry_pg, tmp_path):
    async def body(env, rt):
        aid = await create(env, title="Doc")
        await publish(env, aid)
        rt = dataclasses.replace(rt, secret_gid=os.getgid())
        await tick(env, rt)
        pool = env.mirror_root / POOL
        sha = files_of(env, ALICE, aid)[0]["sha256"]
        assert (pool.stat().st_mode & 0o777, (pool / sha).stat().st_mode & 0o777) == (0o750, 0o640)
        assert (pool / sha).stat().st_gid == os.getgid()
        os.utime(pool / sha, (1, 1))
        await tick(env, rt)  # a hit refreshes the mtime, so the GC age guard protects it
        assert (pool / sha).stat().st_mtime > 1000

    lrun(registry_pg, tmp_path, body)


def test_pool_without_a_group_falls_back_to_0644_with_a_warning(registry_pg, tmp_path, caplog):
    async def body(env, rt):
        aid = await create(env, title="Doc")
        await publish(env, aid)
        await tick(env, dataclasses.replace(rt, secret_gid=None))
        sha = files_of(env, ALICE, aid)[0]["sha256"]
        assert (env.mirror_root / POOL / sha).stat().st_mode & 0o777 == 0o644
        assert "world-readable" in caplog.text

    lrun(registry_pg, tmp_path, body)


def test_pool_fill_is_capped_per_tick_and_finished_next_tick(registry_pg, tmp_path, monkeypatch):
    from herdr_engineering.control_plane import artifact_member_intents as ami

    async def body(env, rt):
        aid = await create(env, title="Doc")
        await publish(env, aid, {"index.html": HTML, "style.css": b"a{}"})
        for blob in (
            env.mirror_root / POOL
        ).iterdir():  # the publish's own view write pooled them: start empty
            blob.unlink()
        monkeypatch.setattr(ami, "POOL_TICK_FILL_BYTES", 1)
        await tick(env, rt)
        assert files_of(env, ALICE, aid) == [] and not pool_files(
            env
        )  # over the tick budget: nothing yet
        monkeypatch.setattr(ami, "POOL_TICK_FILL_BYTES", 10**9)
        await tick(env, rt)
        assert len(files_of(env, ALICE, aid)) == 2

    lrun(registry_pg, tmp_path, body)


def test_gc_skip_after_a_failed_view_write_is_logged(registry_pg, tmp_path, caplog):
    async def body(env, rt):
        aid = await create(env, title="Doc")
        await publish(env, aid)
        await tick(env, rt)
        blocker = env.mirror_root / "member-views"
        saved = blocker.rename(env.mirror_root / "mv-saved")
        blocker.write_text("not a directory")  # every view write now fails
        try:
            await tick(env, rt)
        finally:
            blocker.unlink()
            saved.rename(blocker)
        assert "garbage collection skipped" in caplog.text

    lrun(registry_pg, tmp_path, body)
